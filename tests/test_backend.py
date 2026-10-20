"""Backend regression checks. All media and databases live in temporary folders."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
import math
from pathlib import Path
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.launch import health
from app.library import Library, LibraryError, executable
from app.server import Server


def make_wave(path, frequency=220, duration=1.25):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"".join(struct.pack("<h", int(4000 * math.sin(2 * math.pi * frequency * n / 8000))) for n in range(round(duration * 8000))))


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.wav"
        make_wave(self.source)
        self.library = Library(self.root / "library")

    def tearDown(self):
        self.temp.cleanup()

    def test_import_is_copied_and_persists_with_metadata(self):
        track = self.library.import_file(self.source, {"title": "First", "artist": "Composer", "tags": ["calm", "calm"], "sourceUrl": "https://example.com/song"})
        stored = self.library.root / track["audioUrl"].lstrip("/")
        self.assertEqual(stored.read_bytes(), self.source.read_bytes())
        self.assertEqual(track["tags"], ["calm"])
        self.assertAlmostEqual(track["duration"], 1.25)
        self.source.unlink()
        self.assertEqual(Library(self.library.root).library()["tracks"], [track])

    def test_mp3_original_is_preserved_exactly(self):
        mp3 = self.root / "original.mp3"
        subprocess.run([executable("ffmpeg"), "-v", "error", "-i", str(self.source), str(mp3)], check=True)
        track = self.library.import_file(mp3)
        self.assertTrue(track["audioUrl"].endswith(".mp3"))
        self.assertEqual((self.library.root / track["audioUrl"].lstrip("/")).read_bytes(), mp3.read_bytes())

    def test_unsupported_container_is_converted(self):
        aiff = self.root / "original.aiff"
        subprocess.run([executable("ffmpeg"), "-v", "error", "-i", str(self.source), str(aiff)], check=True)
        track = self.library.import_file(aiff)
        self.assertTrue(track["audioUrl"].endswith(".m4a"))
        self.assertGreater(track["duration"], 1)

    def test_loop_duration_and_bounds(self):
        track = self.library.import_file(self.source, {"kind": "loop", "loopStart": 0.1, "loopEnd": 1.1})
        self.assertAlmostEqual(track["duration"], 1)
        self.assertAlmostEqual(track["fileDuration"], 1.25)
        self.assertEqual((track["loopStart"], track["loopEnd"]), (0.1, 1.1))
        for metadata in ({"loopStart": -1}, {"loopStart": 1.25}, {"loopEnd": 1.3}, {"loopEnd": 0}, {"loopStart": float("nan")}, {"loopEnd": True}):
            with self.subTest(metadata=metadata), self.assertRaises(LibraryError):
                self.library.import_file(self.source, metadata, allow_duplicate=True)
        self.assertEqual(len(self.library.library()["tracks"]), 1)
        self.assertEqual(len(list(self.library.audio.iterdir())), 1)

    def test_invalid_import_leaves_no_media_or_rows(self):
        bad = self.root / "not-audio.mp3"
        bad.write_text("not playable")
        for source, metadata in ((bad, {}), (self.source, {"tags": "calm"}), (self.source, {"title": ""}), (self.source, {"sourceUrl": "file:///private"})):
            with self.subTest(source=source, metadata=metadata), self.assertRaises(LibraryError):
                self.library.import_file(source, metadata)
        self.assertEqual(self.library.library()["tracks"], [])
        self.assertEqual(list(self.library.audio.iterdir()), [])

    def test_duplicates_and_explicit_override(self):
        self.library.import_file(self.source, {"title": "Song"})
        with self.assertRaises(LibraryError) as caught:
            self.library.import_file(self.source, {"title": "Different"})
        self.assertEqual(caught.exception.status, 409)
        different = self.root / "different.wav"
        make_wave(different, frequency=330)
        with self.assertRaises(LibraryError):
            self.library.import_file(different, {"title": " song "})
        self.library.import_file(different, {"title": "Song", "kind": "loop"})
        self.library.import_file(self.source, {"title": "Song"}, allow_duplicate=True)
        self.assertEqual(len(self.library.library()["tracks"]), 3)

    def test_playlist_order_rename_validation_delete_and_durability(self):
        first = self.library.import_file(self.source)
        second = self.library.import_file(self.source, {"title": "Second"}, allow_duplicate=True)
        playlist = self.library.create_playlist("Quiet")
        changed = self.library.update_playlist(playlist["id"], {"name": "Focus", "trackIds": [second["id"], first["id"]]})
        self.assertEqual(changed["trackIds"], [second["id"], first["id"]])
        self.assertEqual(Library(self.library.root).library()["playlists"], [changed])
        for ids in (["missing"], [first["id"], first["id"]]):
            with self.assertRaises(LibraryError):
                self.library.update_playlist(playlist["id"], {"name": "Should roll back", "trackIds": ids})
            self.assertEqual(self.library.library()["playlists"], [changed])
        self.library.delete_playlist(playlist["id"])
        self.assertEqual(self.library.library()["playlists"], [])
        self.assertEqual(len(list(self.library.audio.iterdir())), 2)

    def test_concurrent_duplicate_imports_have_one_winner(self):
        def attempt(_):
            try:
                self.library.import_file(self.source)
                return "added"
            except LibraryError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(attempt, range(4)))
        self.assertEqual(results.count("added"), 1)
        self.assertEqual(results.count(409), 3)
        self.assertEqual(len(list(self.library.audio.iterdir())), 1)

    def test_delete_track_removes_all_playlist_memberships_and_only_its_copy(self):
        first = self.library.import_file(self.source)
        second = self.library.import_file(self.source, {"title": "Second"}, allow_duplicate=True)
        third = self.library.import_file(self.source, {"title": "Third"}, allow_duplicate=True)
        for name, ids in (("One", [first["id"], second["id"], third["id"]]), ("Two", [second["id"], first["id"]])):
            playlist = self.library.create_playlist(name)
            self.library.update_playlist(playlist["id"], {"trackIds": ids})
        original_bytes = self.source.read_bytes()
        result = self.library.delete_track(second["id"])
        self.assertTrue(result["success"])
        self.assertFalse(result["cleanupPending"])
        current = Library(self.library.root).library()
        self.assertEqual(current["tracks"], [first, third])
        self.assertEqual([p["trackIds"] for p in current["playlists"]], [[first["id"], third["id"]], [first["id"]]])
        self.assertEqual(self.source.read_bytes(), original_bytes)
        self.assertEqual({p.name for p in self.library.audio.iterdir()}, {Path(t["audioUrl"]).name for t in (first, third)})
        with self.assertRaises(LibraryError) as caught:
            self.library.delete_track(second["id"])
        self.assertEqual(caught.exception.status, 404)

    def test_delete_track_handles_an_already_missing_audio_copy(self):
        track = self.library.import_file(self.source)
        (self.library.audio / Path(track["audioUrl"]).name).unlink()
        self.assertTrue(self.library.delete_track(track["id"])["success"])
        self.assertEqual(self.library.library()["tracks"], [])
        self.assertTrue(self.source.is_file())

    def test_delete_track_refuses_a_symlink_or_external_audio_path(self):
        track = self.library.import_file(self.source)
        stored = self.library.audio / Path(track["audioUrl"]).name
        stored.unlink()
        stored.symlink_to(self.source)
        with self.assertRaises(LibraryError) as caught:
            self.library.delete_track(track["id"])
        self.assertEqual(caught.exception.status, 409)
        self.assertTrue(self.source.is_file())
        stored.unlink()
        with self.library.connect() as conn:
            conn.execute("UPDATE tracks SET audio_filename=? WHERE id=?", ("../../source.wav", track["id"]))
            conn.commit()
        with self.assertRaises(LibraryError):
            self.library.delete_track(track["id"])
        self.assertTrue(self.source.is_file())
        self.assertEqual(len(self.library.library()["tracks"]), 1)

    def test_delete_track_restores_audio_and_playlist_on_database_failure(self):
        track = self.library.import_file(self.source)
        playlist = self.library.add_to_playlist("Saved", track["id"])
        def failed_save(conn, deleted):
            raise sqlite3.IntegrityError("simulated database failure")
        with self.assertRaises(sqlite3.IntegrityError):
            self.library.delete_track(track["id"], _on_delete=failed_save)
        self.assertEqual(self.library.library(), {"tracks": [track], "playlists": [playlist]})
        self.assertEqual((self.library.audio / Path(track["audioUrl"]).name).read_bytes(), self.source.read_bytes())
        self.assertEqual(list(self.library.deletions.iterdir()), [])

    def test_delete_track_file_permission_failure_preserves_library(self):
        track = self.library.import_file(self.source)
        with patch("app.library.os.replace", side_effect=PermissionError("locked")):
            with self.assertRaises(LibraryError) as caught:
                self.library.delete_track(track["id"])
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.library.library()["tracks"], [track])
        self.assertTrue((self.library.audio / Path(track["audioUrl"]).name).is_file())
        self.assertEqual(list(self.library.deletions.iterdir()), [])

    def test_delete_track_pending_file_cleanup_is_retried_on_launch(self):
        track = self.library.import_file(self.source)
        original_unlink = Path.unlink
        def locked_audio(path, *args, **kwargs):
            if path.suffix == ".audio":
                raise PermissionError("temporarily locked")
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", locked_audio):
            result = self.library.delete_track(track["id"])
        self.assertTrue(result["cleanupPending"])
        self.assertEqual(self.library.library()["tracks"], [])
        self.assertEqual(list(self.library.audio.iterdir()), [])
        self.assertEqual(len(list(self.library.deletions.iterdir())), 2)
        Library(self.library.root)
        self.assertEqual(list(self.library.deletions.iterdir()), [])
        self.assertTrue(self.source.is_file())

    def test_interrupted_delete_recovers_audio_and_retains_playlist(self):
        track = self.library.import_file(self.source)
        playlist = self.library.add_to_playlist("Saved", track["id"])
        script = "from app.library import Library; import os,sys; Library(sys.argv[1]).delete_track(sys.argv[2], _on_delete=lambda *_: os._exit(23))"
        result = subprocess.run([sys.executable, "-c", script, str(self.library.root), track["id"]], cwd=ROOT, timeout=10)
        self.assertEqual(result.returncode, 23)
        restored = Library(self.library.root)
        self.assertEqual(restored.library(), {"tracks": [track], "playlists": [playlist]})
        self.assertEqual((restored.audio / Path(track["audioUrl"]).name).read_bytes(), self.source.read_bytes())
        self.assertEqual(list(restored.deletions.iterdir()), [])

    def test_concurrent_deletes_have_one_success_and_no_remaining_audio(self):
        track = self.library.import_file(self.source)
        def attempt(_):
            try:
                return self.library.delete_track(track["id"])["success"]
            except LibraryError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(attempt, range(2)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(404), 1)
        self.assertEqual(self.library.library()["tracks"], [])
        self.assertEqual(list(self.library.audio.iterdir()), [])

    def test_reimport_during_delete_keeps_the_new_copy_and_playlist_valid(self):
        track = self.library.import_file(self.source)
        self.library.add_to_playlist("Saved", track["id"])
        deleting, release = threading.Event(), threading.Event()
        def hold_transaction(conn, deleted):
            deleting.set()
            self.assertTrue(release.wait(5))
        with ThreadPoolExecutor(max_workers=2) as workers:
            removal = workers.submit(self.library.delete_track, track["id"], _on_delete=hold_transaction)
            self.assertTrue(deleting.wait(5))
            replacement = workers.submit(self.library.import_file, self.source)
            release.set()
            self.assertTrue(removal.result(timeout=5)["success"])
            added = replacement.result(timeout=5)
        self.assertNotEqual(added["id"], track["id"])
        self.assertEqual(self.library.library()["tracks"], [added])
        self.assertEqual(self.library.library()["playlists"][0]["trackIds"], [])
        self.assertEqual([p.name for p in self.library.audio.iterdir()], [Path(added["audioUrl"]).name])


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.wav"
        make_wave(self.source)
        (self.root / "public").mkdir()
        (self.root / "public" / "index.html").write_text("<html>Stillroom</html>")
        self.server = Server(self.root, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def json_request(self, method, path, body):
        status, headers, raw = self.request(method, path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        return status, json.loads(raw)

    def test_health_static_and_library(self):
        status, headers, raw = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["app"], "stillroom")
        self.assertEqual(self.request("GET", "/")[2], b"<html>Stillroom</html>")
        self.assertEqual(json.loads(self.request("GET", "/api/library")[2]), {"tracks": [], "playlists": []})

    def test_raw_import_metadata_and_api_playlists(self):
        metadata = {"title": "Caf\u00e9", "artist": "Composer", "kind": "loop", "loopStart": 0.1, "loopEnd": 1.1, "tags": ["focus"]}
        status, headers, raw = self.request("POST", "/api/import", self.source.read_bytes(), {"Content-Type": "audio/wav", "X-Filename": quote("Caf\u00e9.wav"), "X-Track-Metadata": base64.b64encode(json.dumps(metadata).encode()).decode()})
        self.assertEqual(status, 201, raw)
        track = json.loads(raw)["track"]
        self.assertEqual(track["title"], "Caf\u00e9")
        status, playlist = self.json_request("POST", "/api/playlists", {"name": "Focus"})
        self.assertEqual(status, 201)
        status, changed = self.json_request("PATCH", "/api/playlists/" + playlist["id"], {"trackIds": [track["id"]], "name": "Work"})
        self.assertEqual(status, 200)
        self.assertEqual(changed["trackIds"], [track["id"]])
        self.assertEqual(self.request("DELETE", "/api/playlists/" + playlist["id"])[0], 200)
        self.assertEqual(len(json.loads(self.request("GET", "/api/library")[2])["tracks"]), 1)

    def test_delete_track_api_removes_saved_audio_and_all_playlist_references(self):
        track = self.server.library.import_file(self.source)
        self.server.library.add_to_playlist("Saved", track["id"])
        path = "/api/tracks/" + track["id"]
        self.assertEqual(self.request("DELETE", path, headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(len(self.server.library.library()["tracks"]), 1)
        self.assertEqual(self.request("DELETE", path, b"unexpected body")[0], 400)
        self.assertEqual(len(self.server.library.library()["tracks"]), 1)
        status, _, raw = self.request("DELETE", path)
        self.assertEqual(status, 200, raw)
        self.assertEqual(json.loads(raw)["trackId"], track["id"])
        self.assertEqual(self.request("GET", track["audioUrl"])[0], 404)
        current = json.loads(self.request("GET", "/api/library")[2])
        self.assertEqual(current["tracks"], [])
        self.assertEqual(current["playlists"][0]["trackIds"], [])
        self.assertTrue(self.source.is_file())
        self.assertEqual(self.request("DELETE", path)[0], 404)

    def test_range_and_head(self):
        track = self.server.library.import_file(self.source)
        body = self.source.read_bytes()
        for range_value, expected in (("bytes=4-12", body[4:13]), ("bytes=4-", body[4:]), ("bytes=-12", body[-12:]), (f"bytes=0-{len(body)+10}", body)):
            with self.subTest(range_value=range_value):
                status, headers, raw = self.request("GET", track["audioUrl"], headers={"Range": range_value})
                self.assertEqual(status, 206)
                self.assertEqual(raw, expected)
                self.assertEqual(int(headers["Content-Length"]), len(expected))
        for invalid in ("bytes=999999-", "bytes=5-2", "bytes=-0", "bytes=1-2,4-5", "bytes=", "bad"):
            self.assertEqual(self.request("GET", track["audioUrl"], headers={"Range": invalid})[0], 416)
        status, headers, raw = self.request("HEAD", track["audioUrl"], headers={"Range": "bytes=4-12"})
        self.assertEqual(status, 206)
        self.assertEqual(raw, b"")
        self.assertEqual(headers["Content-Length"], "9")

    def test_host_origin_and_content_type_restrictions(self):
        for headers in ({"Host": "evil.example"}, {"Host": "127.0.0.1:1"}, {"Origin": "https://evil.example", "Content-Type": "application/json"}, {"Sec-Fetch-Site": "cross-site", "Content-Type": "application/json"}):
            self.assertEqual(self.request("POST", "/api/playlists", b'{"name":"Forbidden"}', headers)[0], 403)
        self.assertEqual(self.request("POST", "/api/playlists", b'{"name":"Forbidden"}', {"Content-Type": "text/plain"})[0], 415)
        allowed = {"Origin": f"http://127.0.0.1:{self.port}", "Content-Type": "application/json"}
        self.assertEqual(self.request("POST", "/api/playlists", b'{"name":"Allowed"}', allowed)[0], 201)
        self.assertEqual(self.server.library.library()["playlists"][0]["name"], "Allowed")

    def test_traversal_and_symlink_escape_are_denied(self):
        secret = self.root / "private.txt"
        secret.write_text("secret")
        (self.root / "public" / "leak.txt").symlink_to(secret)
        for path in ("/../private.txt", "/%2e%2e/private.txt", "/audio/../private.txt", "/audio/%2e%2e/private.txt", "/leak.txt", "/%5cprivate.txt"):
            with self.subTest(path=path):
                status, _, raw = self.request("GET", path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"secret", raw)

    def test_bad_uploads_do_not_leave_files(self):
        for headers, body, expected in (({"Content-Type": "text/plain", "X-Filename": "bad.mp3"}, b"bad", 415), ({"Content-Type": "audio/mpeg", "X-Filename": "../bad.mp3"}, b"bad", 400), ({"Content-Type": "audio/mpeg", "X-Filename": "bad.mp3"}, b"bad", 400), ({"Content-Type": "audio/mpeg", "X-Filename": "bad.mp3", "X-Track-Metadata": "broken!"}, b"bad", 400), ({"Content-Type": "audio/mpeg", "X-Filename": "bad.mp3", "Content-Length": str(512 * 1024 * 1024 + 1)}, b"", 413)):
            self.assertEqual(self.request("POST", "/api/import", body, headers)[0], expected)
        self.assertEqual(self.server.library.library()["tracks"], [])
        self.assertEqual(list((self.root / "audio").iterdir()), [])
        self.assertEqual(list((self.root / "data").glob("upload-*")), [])

    def test_cli_import_visible_to_running_server(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "import_track.py"), "--root", str(self.root), "--file", str(self.source), "--title", "CLI song", "--playlist", "New songs"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        imported = json.loads(result.stdout)["track"]
        current = json.loads(self.request("GET", "/api/library")[2])
        self.assertEqual(current["tracks"], [imported])
        self.assertEqual(current["playlists"][0]["trackIds"], [imported["id"]])

    def test_cli_rejects_invalid_playlist_before_importing(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "import_track.py"), "--root", str(self.root), "--file", str(self.source), "--playlist", "x" * 121], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.server.library.library()["tracks"], [])


class LauncherTests(unittest.TestCase):
    def test_detached_idempotent_start_and_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            command = [sys.executable, "-m", "app.launch", "--root", directory, "--port", str(port), "--no-open"]
            try:
                first = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=15)
                self.assertEqual(first.returncode, 0, first.stderr)
                initial = health(port)
                self.assertIsNotNone(initial)
                second = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=15)
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual(health(port)["pid"], initial["pid"])
            finally:
                stopped = subprocess.run(command + ["--stop"], cwd=ROOT, capture_output=True, text=True, timeout=15)
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
            self.assertIsNone(health(port))


if __name__ == "__main__":
    unittest.main()
