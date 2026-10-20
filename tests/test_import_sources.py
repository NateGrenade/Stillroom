"""Acquisition checks isolate network, subprocess options, and metadata fallback."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import import_sources as sources
from app.library import LibraryError


VIDEO = "BaW_jenozKc"
URL = "https://www.youtube.com/watch?v=" + VIDEO


class YoutubeURLTests(unittest.TestCase):
    def test_video_forms_have_one_canonical_url_and_ignore_time_and_playlist(self):
        for url in (
            URL, " https://youtu.be/" + VIDEO + "?t=90 ",
            "https://music.youtube.com/watch?v=" + VIDEO + "&list=PLabc&start=90",
            "http://m.youtube.com/watch?v=" + VIDEO + "#t=90",
            "https://www.youtube.com/shorts/" + VIDEO,
            "https://youtube.com/live/" + VIDEO,
            "https://youtube.com/embed/" + VIDEO + "/",
            "https://youtube.com:443/watch?v=" + VIDEO,
        ):
            with self.subTest(url=url):
                self.assertEqual(sources.normalize_youtube_url(url), URL)

    def test_rejects_non_video_and_host_confusion(self):
        for url in (
            "", None, "ytsearch:music", "file:///etc/passwd", "https://127.0.0.1/watch?v=" + VIDEO,
            "https://youtube.com.evil.example/watch?v=" + VIDEO,
            "https://youtube.com@localhost/watch?v=" + VIDEO,
            "https://user:password@youtube.com/watch?v=" + VIDEO,
            "https://youtube.com:444/watch?v=" + VIDEO,
            "https://youtube.com./watch?v=" + VIDEO,
            "https://youtu.be/" + VIDEO + "/other", "https://youtube.com/playlist?list=PLtest",
            "https://youtube.com/watch?v=" + VIDEO + "&v=abcdefghijk",
            "https://youtube.com/watch?v=short", "https://youtu.be/../../etc",
            "https://youtube.com/watch?v=" + VIDEO + "\n&bad=1",
            "https://youtube.com\\@evil.example/watch?v=" + VIDEO,
            "https://youtube.com/%2e%2e/watch?v=" + VIDEO,
        ):
            with self.subTest(url=url), self.assertRaises(LibraryError):
                sources.normalize_youtube_url(url)


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.staging = Path(self.temp.name) / "job"
        self.info = {"id": VIDEO, "title": " Song\nTitle ", "duration": 30, "channel": "Channel", "album": "Release"}

    def tearDown(self):
        self.temp.cleanup()

    def fake_command(self):
        return patch.object(sources, "_base_command", return_value=["/tools/yt-dlp"])

    def fake_run(self, command, **kwargs):
        if "--dump-single-json" in command:
            return subprocess.CompletedProcess(command, 0, json.dumps(self.info), "")
        (self.staging / "source.webm").write_bytes(b"pretend audio for isolated acquisition test")
        return subprocess.CompletedProcess(command, 0, str(self.staging / "source.webm"), "")

    def test_success_retains_source_and_reports_channel_fallback(self):
        progress = []
        with self.fake_command(), patch.object(sources, "_run_tool", side_effect=self.fake_run) as run:
            result = sources.acquire_youtube("https://youtu.be/" + VIDEO + "?t=12", self.staging, lambda *item: progress.append(item))
        self.assertEqual(result["metadata"], {"title": "Song Title", "artist": "Channel", "album": "Release", "sourceUrl": URL})
        self.assertTrue(result["notes"])
        self.assertTrue(Path(result["file"]).is_file())
        self.assertEqual([item[0] for item in progress], ["reading", "downloading"])
        self.assertEqual(run.call_args.args[0][-2:], ["--", URL])
        self.assertEqual(run.call_args.kwargs["watch_directory"], self.staging.resolve())

    def test_embedded_artist_wins_over_creator_and_channel(self):
        self.info.update(artist=" Actual Artist ", creator="Other", title="x" * 500)
        with self.fake_command(), patch.object(sources, "_run_tool", side_effect=self.fake_run):
            result = sources.acquire_youtube(URL, self.staging)
        self.assertEqual(result["metadata"]["artist"], "Actual Artist")
        self.assertEqual(len(result["metadata"]["title"]), 300)
        self.assertEqual(result["notes"], [])

    def test_rejects_streams_unknown_duration_and_wrong_video_before_download(self):
        for fields in (
            {"is_live": True}, {"live_status": "is_upcoming"}, {"live_status": "post_live"},
            {"duration": None}, {"duration": True}, {"duration": -1},
            {"duration": 21601}, {"id": "abcdefghijk"}, {"entries": []}, {"_type": "playlist"},
        ):
            with self.subTest(fields=fields):
                info = dict(self.info, **fields)
                with self.fake_command(), patch.object(sources, "_run_tool", return_value=subprocess.CompletedProcess([], 0, json.dumps(info), "")) as run:
                    with self.assertRaises(LibraryError):
                        sources.acquire_youtube(URL, self.staging)
                self.assertEqual(run.call_count, 1)

    def test_failure_explains_access_without_exposing_signed_url(self):
        failure = subprocess.CompletedProcess([], 1, "", "WARNING: noise\nERROR: Sign in to confirm age https://youtube.example/?token=secret")
        with self.fake_command(), patch.object(sources, "_run_tool", return_value=failure):
            with self.assertRaises(LibraryError) as caught:
                sources.acquire_youtube(URL, self.staging)
        self.assertIn("public video or upload", str(caught.exception))
        self.assertNotIn("token=", str(caught.exception))

    def test_success_exit_without_download_is_not_success(self):
        results = [subprocess.CompletedProcess([], 0, json.dumps(self.info), ""), subprocess.CompletedProcess([], 0, "", "")]
        with self.fake_command(), patch.object(sources, "_run_tool", side_effect=results):
            with self.assertRaisesRegex(LibraryError, "complete audio file"):
                sources.acquire_youtube(URL, self.staging)

    def test_maximum_file_skip_reports_size_limit_even_with_success_exit(self):
        results = [subprocess.CompletedProcess([], 0, json.dumps(self.info), ""), subprocess.CompletedProcess([], 0, "File is larger than max-filesize", "")]
        with self.fake_command(), patch.object(sources, "_run_tool", side_effect=results):
            with self.assertRaises(LibraryError) as caught:
                sources.acquire_youtube(URL, self.staging)
        self.assertEqual(caught.exception.status, 413)

    def test_nonempty_staging_is_never_overwritten(self):
        self.staging.mkdir()
        keep = self.staging / "source.webm"
        keep.write_bytes(b"keep")
        with self.assertRaisesRegex(LibraryError, "empty staging"):
            sources.acquire_youtube(URL, self.staging)
        self.assertEqual(keep.read_bytes(), b"keep")

    def test_downloader_has_no_user_configuration_cookies_or_plugins(self):
        with patch.object(sources, "_downloader", return_value="/tools/yt-dlp"), patch.object(sources, "executable", side_effect=lambda name: "/tools/"+name):
            command = sources._base_command()
        for option in ("--ignore-config", "--no-config-locations", "--no-plugin-dirs", "--no-cookies", "--no-cookies-from-browser", "--no-playlist", "--no-remote-components"):
            self.assertIn(option, command)
        self.assertEqual(command[command.index("--js-runtimes") + 1], "node:/tools/node")
        self.assertEqual(command[command.index("--ffmpeg-location") + 1], "/tools")
        self.assertNotIn("--cookies-from-browser", command)


class ToolBoundsTests(unittest.TestCase):
    def test_cancellation_reaps_running_process_promptly(self):
        cancel = threading.Event()
        failures = []
        processes = []
        real_popen = subprocess.Popen

        def start_process(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        with tempfile.TemporaryDirectory() as folder:
            pid_file = Path(folder) / "process.pid"
            code = "import os, time; from pathlib import Path; Path('process.pid').write_text(str(os.getpid())); time.sleep(20)"
            def run():
                try:
                    sources._run_tool([sys.executable, "-c", code], timeout=30, cwd=folder, cancel_event=cancel)
                except LibraryError as error:
                    failures.append(str(error))
            with patch.object(sources.subprocess, "Popen", side_effect=start_process):
                worker = threading.Thread(target=run)
                worker.start()
                deadline = time.monotonic() + 5
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                try:
                    self.assertTrue(pid_file.exists(), "Subprocess did not start")
                    cancel.set()
                    worker.join(2)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(failures, ["Preparation cancelled."])
                    # Inspect the owned Popen handle: os.kill(pid, 0) is unsafe
                    # on Windows and cannot be used as a portable liveness test.
                    self.assertIsNotNone(processes[0].poll())
                finally:
                    cancel.set()
                    worker.join(5)

    def test_cancelled_job_does_not_start_another_process(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(sources.subprocess, "Popen") as start:
            with self.assertRaisesRegex(LibraryError, "cancelled"):
                sources._run_tool(["anything"], timeout=5, cancel_event=cancel)
        start.assert_not_called()

    def test_timeout_stops_child(self):
        with self.assertRaises(LibraryError) as caught:
            sources._run_tool([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.05)
        self.assertEqual(caught.exception.status, 504)

    def test_file_size_guard_also_catches_unknown_download_sizes(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            code = "from pathlib import Path; import time; Path('source.part').write_bytes(b'x'*1000); time.sleep(5)"
            with patch.object(sources, "MAX_AUDIO_BYTES", 500):
                with self.assertRaises(LibraryError) as caught:
                    sources._run_tool([sys.executable, "-c", code], timeout=5, cwd=directory, watch_directory=directory)
            self.assertEqual(caught.exception.status, 413)

    def test_output_limit_is_enforced(self):
        with patch.object(sources, "MAX_TOOL_OUTPUT", 500):
            with self.assertRaises(LibraryError):
                sources._run_tool([sys.executable, "-c", "print('x'*1000)"], timeout=5)


class LocalMetadataTests(unittest.TestCase):
    def test_file_tags_override_stream_tags_and_keep_real_artist(self):
        info = {"streams": [{"tags": {"title": "Stream title", "composer": "Composer"}}], "format": {"tags": {"TITLE": " Real title ", "ARTIST": "Artist", "ALBUM": "Album"}}}
        with patch.object(sources, "_run_tool", return_value=subprocess.CompletedProcess([], 0, json.dumps(info), "")):
            result = sources.local_metadata("staged.wav", "Original filename.wav")
        self.assertEqual(result, {"title": "Real title", "artist": "Artist", "album": "Album", "sourceUrl": ""})

    def test_unreadable_tags_fall_back_without_guessing_artist(self):
        with patch.object(sources, "_run_tool", side_effect=LibraryError("bad tags")):
            result = sources.local_metadata("staged.wav", "Artist - Song.wav")
        self.assertEqual(result["title"], "Artist - Song")
        self.assertEqual(result["artist"], "")

    def test_cancelled_metadata_does_not_get_swallowed_as_missing_tags(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(LibraryError, "cancelled"):
            sources.local_metadata("staged.wav", cancel_event=cancel)


if __name__ == "__main__":
    unittest.main()
