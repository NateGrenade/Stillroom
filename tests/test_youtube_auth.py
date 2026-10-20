"""No real browser/account access: all tool calls and credentials are fixtures."""
import json
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from app import import_sources
from app.import_jobs import ImportJobs
from app.library import Library, LibraryError
from app.playlist_imports import PlaylistImports
from app.youtube_auth import YouTubeAuth, MAX_COOKIE_BYTES
from app.youtube_playlists import read_youtube_playlist

VIDEO_ID = "abcdefghijk"
VIDEO = "https://www.youtube.com/watch?v=" + VIDEO_ID
PLAYLIST_ID = "PL" + "a" * 32
PLAYLIST = "https://www.youtube.com/playlist?list=" + PLAYLIST_ID
COOKIES = ("# Netscape HTTP Cookie File\n"
           "# private export comment should be dropped\n"
           ".youtube.com\tTRUE\t/\tTRUE\t1999999999\tSID\tfictional-session-value\n"
           "#HttpOnly_.google.com\tTRUE\t/\tTRUE\t1999999999\tSID\tfictional-google-value\n"
           ".example.com\tTRUE\t/\tTRUE\t1999999999\tSID\tunrelated-site-secret\n").encode()


class YouTubeAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = YouTubeAuth(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_default_off_reads_no_browser_and_creates_no_storage(self):
        with patch("subprocess.Popen") as run:
            state = self.manager.status()
            self.assertEqual(state["mode"], "off")
            self.assertFalse(state["hasCookies"])
            self.assertIsNone(self.manager.snapshot())
            self.assertFalse(self.manager.directory.exists())
            run.assert_not_called()

    def test_method_configuration_alone_does_not_enable_import_or_extract_cookies(self):
        with patch("subprocess.Popen") as run:
            state = self.manager.configure({"mode": "browser", "browser": "firefox"})
            self.assertEqual(state["mode"], "browser")
            self.assertIsNone(self.manager.snapshot(False))
            session = self.manager.snapshot(True)
            with session.command_options() as flags:
                self.assertEqual(flags, ["--cookies-from-browser", "firefox"])
            self.assertEqual(list(self.manager.directory.iterdir()), [self.manager.directory / "settings.json"])
            self.assertEqual(YouTubeAuth(self.root).status()["mode"], "browser")
            run.assert_not_called()

    def test_sessions_freeze_browser_choice_and_do_not_expose_path_or_values(self):
        self.manager.configure({"mode": "browser", "browser": "firefox"})
        session = self.manager.snapshot(True)
        self.manager.configure({"mode": "browser", "browser": "chrome"})
        with session.command_options() as flags:
            self.assertEqual(flags[-1], "firefox")
        self.assertNotIn(str(self.root), json.dumps(self.manager.status()))

    def test_invalid_settings_cannot_inject_profiles_or_arguments(self):
        for settings in ({"mode": "browser", "browser": "firefox:/etc"},
                         {"mode": "browser", "browser": "chrome --exec=touch"},
                         {"mode": "browser", "browser": []}, {"mode": "password", "password": "secret"},
                         {"mode": "cookies", "path": "/secret"}, {"mode": []}):
            with self.subTest(settings=settings), self.assertRaises(LibraryError):
                self.manager.configure(settings)
        for enabled in ("true", 1, None, [], {}):
            with self.subTest(enabled=enabled), self.assertRaises(LibraryError):
                self.manager.snapshot(enabled)
        with self.assertRaises(LibraryError):
            self.manager.snapshot(True)

    def test_upload_filters_other_sites_and_has_private_permissions(self):
        state = self.manager.upload_cookies(COOKIES)
        self.assertEqual(state["mode"], "cookies")
        self.assertTrue(state["hasCookies"])
        stored = next(self.manager.directory.glob("cookies-*.txt"))
        content = stored.read_bytes()
        self.assertIn(b"fictional-session-value", content)
        self.assertIn(b"#HttpOnly_.google.com", content)
        self.assertNotIn(b"unrelated-site-secret", content)
        self.assertNotIn(b"private export comment", content)
        self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.manager.directory.stat().st_mode), 0o700)
        self.assertNotIn("fictional", json.dumps(state))
        self.assertNotIn(str(self.root), json.dumps(state))

    def test_cookie_working_copy_is_private_isolated_and_cleaned_even_on_failure(self):
        self.manager.upload_cookies(COOKIES)
        original = next(self.manager.directory.glob("cookies-*.txt"))
        session = self.manager.snapshot(True)
        with self.assertRaisesRegex(RuntimeError, "simulated failure"):
            with session.command_options() as flags:
                path = Path(flags[1])
                self.assertNotEqual(path, original)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(path.read_bytes(), original.read_bytes())
                path.write_text("pretend yt-dlp refreshed a cookie")
                raise RuntimeError("simulated failure")
        self.assertFalse(path.exists())
        self.assertIn(b"fictional-session-value", original.read_bytes())

    def test_replacing_or_removing_upload_invalidates_old_session(self):
        self.manager.upload_cookies(COOKIES)
        old = self.manager.snapshot(True)
        self.manager.upload_cookies(COOKIES.replace(b"fictional-session-value", b"new-fixture"))
        with self.assertRaisesRegex(LibraryError, "removed or replaced"):
            with old.command_options():
                self.fail("The old session must never silently switch to a new upload")
        current = self.manager.snapshot(True)
        state = self.manager.clear_cookies()
        self.assertEqual(state["mode"], "off")
        self.assertFalse(state["hasCookies"])
        self.assertEqual(list(self.manager.directory.glob("cookies-*.txt")), [])
        with self.assertRaises(LibraryError):
            with current.command_options():
                self.fail("Deleted cookies must be unavailable")

    def test_malformed_uploads_never_store_or_echo_secrets(self):
        for content in (b"", b"x" * (MAX_COOKIE_BYTES + 1), b"{\"password\":\"secret\"}",
                        b"# Netscape HTTP Cookie File\nmalformed-secret", COOKIES.replace(b"1999999999", b"-42"),
                        b"# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tSID\tunrelated-secret\n"):
            with self.subTest(size=len(content)), self.assertRaises(LibraryError) as caught:
                self.manager.upload_cookies(content)
            self.assertNotIn("malformed-secret", str(caught.exception))
            self.assertNotIn("unrelated-secret", str(caught.exception))
        self.assertFalse(self.manager.directory.exists())

    def test_cookie_and_settings_symlinks_cannot_read_unrelated_files(self):
        self.manager.upload_cookies(COOKIES)
        external = self.root / "unrelated"
        external.write_bytes(COOKIES)
        stored = next(self.manager.directory.glob("cookies-*.txt"))
        stored.unlink()
        stored.symlink_to(external)
        with self.assertRaises(LibraryError):
            self.manager.snapshot(True)
        self.assertEqual(external.read_bytes(), COOKIES)
        settings = self.manager.directory / "settings.json"
        settings.unlink()
        settings.symlink_to(external)
        with self.assertRaises(LibraryError):
            self.manager.status()

    def test_authenticated_error_details_never_expose_tool_output(self):
        for error in ("ERROR: cookie SID=fictional-session-value from /Users/private/profile", "ERROR: private video SID=fictional-session-value", "ERROR: unknown fictional-session-value"):
            self.assertNotIn("fictional", str(import_sources._failure(error, authenticated=True)))
            self.assertNotIn("/Users", str(import_sources._failure(error, authenticated=True)))

    def test_authenticated_playlist_allows_accessible_private_video_not_placeholders(self):
        self.manager.configure({"mode": "browser", "browser": "firefox"})
        data = {"id": PLAYLIST_ID, "_type": "playlist", "title": "Fixture private playlist", "playlist_count": 5,
                "entries": [{"id": VIDEO_ID, "title": "Accessible private recording", "duration": 20, "availability": "private"},
                            {"id": VIDEO_ID, "title": "[Private video]", "duration": 20, "availability": "private"},
                            {"id": VIDEO_ID, "title": "No access", "duration": 20, "availability": "needs_auth"},
                            {"id": VIDEO_ID, "title": "Insufficient details", "availability": "private"},
                            {"id": VIDEO_ID, "title": "Unavailable", "duration": 20, "availability": "unavailable"}]}
        with patch.object(import_sources, "_downloader", return_value="/tools/yt-dlp"), \
                patch.object(import_sources, "executable", side_effect=lambda name: "/tools/" + name), \
                patch.object(import_sources, "_run_tool", return_value=subprocess.CompletedProcess([], 0, json.dumps(data), "")) as run:
            result = read_youtube_playlist(PLAYLIST, youtube_session=self.manager.snapshot(True))
        self.assertEqual([t["available"] for t in result["tracks"]], [True, False, False, False, False])
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--cookies-from-browser") + 1], "firefox")
        self.assertNotIn("--no-cookies-from-browser", command)
        self.assertIn("--no-cookies", command)
        self.assertIn("--ignore-config", command)

    def test_single_video_read_and_download_use_one_temporary_cookie_copy(self):
        self.manager.upload_cookies(COOKIES)
        staging = self.root / "job"
        paths = []
        def tool(command, **kwargs):
            paths.append(Path(command[command.index("--cookies") + 1]))
            self.assertIn(b"fictional-session-value", paths[-1].read_bytes())
            if "--dump-single-json" in command:
                return subprocess.CompletedProcess([], 0, json.dumps({"id": VIDEO_ID, "title": "Fixture", "duration": 20}), "")
            (staging / "source.webm").write_bytes(b"fake audio")
            return subprocess.CompletedProcess([], 0, "", "")
        with patch.object(import_sources, "_downloader", return_value="/tools/yt-dlp"), \
                patch.object(import_sources, "executable", side_effect=lambda name: "/tools/" + name), \
                patch.object(import_sources, "_run_tool", side_effect=tool):
            import_sources.acquire_youtube(VIDEO, staging, youtube_session=self.manager.snapshot(True))
        self.assertEqual(len(paths), 2)
        self.assertEqual(paths[0], paths[1])
        self.assertFalse(paths[0].exists())

    def test_single_job_snapshots_only_explicit_opt_in_and_keeps_it_out_of_public_state(self):
        library = Library(self.root)
        self.manager.configure({"mode": "browser", "browser": "firefox"})
        jobs = ImportJobs(library, youtube_auth=self.manager)
        try:
            with patch.object(jobs, "_run"):
                public = jobs.create({"source": "youtube", "url": VIDEO})
                opted = jobs.create({"source": "youtube", "url": VIDEO, "useYouTubeAccount": True})
                for value in (public, opted):
                    jobs.jobs[value["id"]]["_worker"].join(1)
            self.assertIsNone(jobs.jobs[public["id"]]["_youtube_session"])
            self.assertEqual(jobs.jobs[opted["id"]]["_youtube_session"].browser, "firefox")
            self.assertNotIn("_youtube_session", opted)
        finally:
            jobs.close()

    def test_playlist_session_selection_is_reused_for_download_even_after_settings_change(self):
        library = Library(self.root)
        self.manager.configure({"mode": "browser", "browser": "firefox"})
        class Jobs:
            def __init__(self): self.calls = []
            def create(self, options, **kwargs):
                self.calls.append((options, kwargs))
                return {"id": "fixture", "status": "error", "error": "Fixture stops before audio processing"}
            def discard(self, identifier): pass
        jobs = Jobs()
        imports = PlaylistImports(library, jobs, youtube_auth=self.manager)
        data = {"title": "Private playlist", "url": PLAYLIST, "notes": [], "complete": True, "total": 1,
                "tracks": [{"id": VIDEO_ID, "title": "Fixture", "artist": "Artist", "duration": 20, "available": True, "youtubeUrl": VIDEO}]}
        try:
            with patch("app.playlist_imports.read_youtube_playlist", return_value=data) as reader:
                result = imports.create({"source": "youtube", "url": PLAYLIST, "useYouTubeAccount": True})
                imports.sessions[result["id"]]["_worker"].join(2)
            read_session = reader.call_args.kwargs["youtube_session"]
            self.assertTrue(imports.get(result["id"])["useYouTubeAccount"])
            self.manager.configure({"mode": "browser", "browser": "chrome"})
            imports.start_import(result["id"], {"title": "Private playlist", "selections": [{"rowId": "0", "url": VIDEO}]})
            imports.sessions[result["id"]]["_worker"].join(2)
            self.assertEqual(len(jobs.calls), 1)
            self.assertIs(jobs.calls[0][1]["youtube_session"], read_session)
            self.assertEqual(read_session.browser, "firefox")
            self.assertTrue(jobs.calls[0][0]["useYouTubeAccount"])
            receipt = (library.data / "playlist-imports" / (result["id"] + ".json")).read_text()
            self.assertNotIn("_youtube_session", receipt)
            self.assertNotIn(str(self.root), receipt)
        finally:
            imports.close()


if __name__ == "__main__":
    unittest.main()
