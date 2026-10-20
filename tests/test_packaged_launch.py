"""Portable launcher and authenticated shutdown behavior without user data."""

import ctypes
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from app.import_jobs import ImportJobs, process_exists
from app.library import Library
from app.launch import detached_process_options, health, stop_server
from app.server import DEFAULT_ROOT, SERVER_CONTROL_FILE, Server
from test_backend import make_wave


class ShutdownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="Stillroom empty package ")
        self.root = Path(self.temp.name).resolve()
        self.server = Server(self.root, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.control_path = self.root / "data" / SERVER_CONTROL_FILE
        self.control = json.loads(self.control_path.read_text())

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def request_shutdown(self, token=None, extra_headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Content-Type": "application/json", **(extra_headers or {})}
        connection.request("POST", "/api/shutdown", json.dumps({"token": token}), headers)
        response = connection.getresponse()
        status, data = response.status, json.loads(response.read())
        connection.close()
        return status, data

    def test_bad_or_missing_token_cannot_stop_server(self):
        for token in (None, "", "wrong-token", "\N{SNOWMAN}", {"token": "wrong"}):
            status, _ = self.request_shutdown(token)
            self.assertEqual(status, 403)
            self.assertTrue(self.thread.is_alive())
        current = health(self.server.server_port)
        self.assertNotIn("token", current)
        self.assertNotIn(self.control["token"], json.dumps(current))

    def test_cross_origin_cannot_stop_with_valid_token(self):
        for headers in ({"Origin": "https://example.com"}, {"Sec-Fetch-Site": "cross-site"}):
            status, _ = self.request_shutdown(self.control["token"], headers)
            self.assertEqual(status, 403)
        self.assertTrue(self.thread.is_alive())

    def test_valid_shutdown_and_close_clean_control_and_previews(self):
        session = self.server.import_jobs.session
        status, result = self.request_shutdown(self.control["token"])
        self.assertEqual(status, 200)
        self.assertTrue(result["success"])
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())
        self.server.server_close()
        self.assertFalse(self.control_path.exists())
        self.assertFalse(session.exists())

    def test_losing_launch_keeps_original_control_record(self):
        with self.assertRaises(OSError):
            Server(self.root, self.server.server_port)
        self.assertEqual(json.loads(self.control_path.read_text()), self.control)
        self.assertTrue(self.server.import_jobs.session.exists())

    def test_close_does_not_delete_another_servers_record(self):
        replacement = {**self.control, "token": "replacement-server"}
        self.control_path.write_text(json.dumps(replacement))
        self.server.shutdown()
        self.server.server_close()
        self.assertEqual(json.loads(self.control_path.read_text()), replacement)

    @unittest.skipIf(os.name == "nt", "POSIX permissions only")
    def test_control_file_is_private(self):
        self.assertEqual(self.control_path.stat().st_mode & 0o777, 0o600)


class PackagedLauncherTests(unittest.TestCase):
    def test_windows_server_uses_exclusive_port_ownership(self):
        server = Server.__new__(Server)
        server.socket = Mock()
        with patch("app.server.sys.platform", "win32"), patch.object(socket, "SO_EXCLUSIVEADDRUSE", -5, create=True), patch.object(ThreadingHTTPServer, "server_bind") as bind:
            server.server_bind()
        server.socket.setsockopt.assert_called_once_with(socket.SOL_SOCKET, -5, 1)
        self.assertFalse(server.allow_reuse_address)
        self.assertFalse(server.allow_reuse_port)
        bind.assert_called_once_with()

    def test_empty_library_start_reopen_and_graceful_stop(self):
        with tempfile.TemporaryDirectory(prefix="Stillroom package with spaces ") as directory:
            root = Path(directory).resolve()
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            command = [sys.executable, "-m", "app.launch", "--root", str(root), "--port", str(port), "--no-open"]
            try:
                first = subprocess.run(command, cwd=DEFAULT_ROOT, capture_output=True, text=True, timeout=20)
                self.assertEqual(first.returncode, 0, first.stderr)
                running = health(port)
                self.assertEqual(running["root"], str(root))
                self.assertTrue((root / "data" / SERVER_CONTROL_FILE).is_file())
                self.assertEqual(list((root / "audio").iterdir()), [])
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request("GET", "/api/library")
                library = json.loads(connection.getresponse().read())
                connection.close()
                self.assertEqual(library["tracks"], [])
                self.assertEqual(library["playlists"], [])
                second = subprocess.run(command, cwd=DEFAULT_ROOT, capture_output=True, text=True, timeout=20)
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual(health(port)["pid"], running["pid"])
                with tempfile.TemporaryDirectory() as other_root:
                    collision = subprocess.run([sys.executable, "-m", "app.launch", "--root", other_root,
                                                "--port", str(port), "--no-open"], cwd=DEFAULT_ROOT,
                                               capture_output=True, text=True, timeout=20)
                    self.assertNotEqual(collision.returncode, 0)
                    self.assertIn("Another Stillroom library", collision.stderr)
                    self.assertEqual(health(port)["pid"], running["pid"])
            finally:
                stopped = subprocess.run(command + ["--stop"], cwd=DEFAULT_ROOT, capture_output=True, text=True, timeout=20)
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
            self.assertIsNone(health(port))
            self.assertFalse((root / "data" / SERVER_CONTROL_FILE).exists())
            self.assertEqual(list((root / "imports").iterdir()), [])

    def test_windows_detaches_without_posix_session_option(self):
        with patch("app.launch.sys.platform", "win32"):
            options = detached_process_options()
        self.assertNotIn("start_new_session", options)
        self.assertTrue(options["creationflags"] & 0x00000008)
        self.assertTrue(options["creationflags"] & 0x00000200)

    def test_unauthenticated_stop_never_terminates_or_contacts_a_process(self):
        with tempfile.TemporaryDirectory() as directory:
            for platform in ("darwin", "linux", "win32"):
                with self.subTest(platform=platform), patch("app.launch.sys.platform", platform), patch("os.kill") as kill, patch("app.launch.build_opener") as opener:
                    with self.assertRaisesRegex(RuntimeError, "could not verify"):
                        stop_server(Path(directory), 8768, {"pid": 424242})
                    kill.assert_not_called()
                    opener.assert_not_called()

    def test_mismatched_control_record_cannot_authorize_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control_path = root / "data" / SERVER_CONTROL_FILE
            control_path.parent.mkdir()
            record = {"app": "stillroom", "root": str(root), "port": 8768,
                      "pid": 12345, "token": "synthetic-control-token"}
            for changed in ({"pid": 424242}, {"port": 8769}, {"root": str(root / "other")},
                            {"token": ""}, {"app": "other"}):
                control_path.write_text(json.dumps({**record, **changed}))
                with self.subTest(changed=changed), patch("os.kill") as kill, patch("app.launch.build_opener") as opener:
                    with self.assertRaisesRegex(RuntimeError, "could not verify"):
                        stop_server(root, 8768, {"pid": 12345})
                    kill.assert_not_called()
                    opener.assert_not_called()


class PreviewOwnerTests(unittest.TestCase):
    def test_windows_process_check_queries_handle_and_does_not_signal(self):
        kernel = Mock()
        kernel.OpenProcess.return_value = 17
        def exit_code(handle, code):
            code._obj.value = 259
            return True
        kernel.GetExitCodeProcess.side_effect = exit_code
        with patch("app.import_jobs.sys.platform", "win32"), patch("ctypes.WinDLL", create=True, return_value=kernel), patch("app.import_jobs.os.kill") as kill:
            self.assertTrue(process_exists(12345))
            kill.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(17)

    def test_windows_missing_or_inaccessible_owner_is_handled_conservatively(self):
        kernel = Mock()
        kernel.OpenProcess.return_value = 0
        with patch("app.import_jobs.sys.platform", "win32"), patch("ctypes.WinDLL", create=True, return_value=kernel), patch("ctypes.get_last_error", create=True) as error, patch("app.import_jobs.os.kill") as kill:
            error.return_value = 87
            self.assertFalse(process_exists(12345))
            error.return_value = 5
            self.assertTrue(process_exists(12345))
            kill.assert_not_called()


class UnicodePreviewTests(unittest.TestCase):
    def test_non_ascii_song_details_prepare_on_legacy_windows_locale(self):
        original_open = Path.open
        def legacy_open(path, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
            # Model a Windows machine whose default text encoding is cp1252.
            if "b" not in mode and encoding is None:
                encoding = "cp1252"
            return original_open(path, mode, buffering, encoding, errors, newline)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            make_wave(source, duration=.5)
            jobs = ImportJobs(Library(root / "library"))
            try:
                details = {"title": "夜の音楽 🎵", "artist": "音楽家", "album": "静かな部屋"}
                with patch.object(Path, "open", legacy_open):
                    job = jobs.create({"metadataMode": "manual", "metadata": details}, upload=source, filename="source.wav")
                    jobs.jobs[job["id"]]["_worker"].join(timeout=15)
                    prepared = jobs.get(job["id"])
                    self.assertEqual(prepared["status"], "ready", prepared)
                    manifest = json.loads((jobs.jobs[job["id"]]["_dir"] / "preparation.json").read_text(encoding="utf-8"))
                    self.assertEqual(manifest["metadata"]["title"], details["title"])
                    saved = jobs.commit(job["id"], {})
                    self.assertEqual(saved["track"]["artist"], details["artist"])
            finally:
                jobs.close()


if __name__ == "__main__":
    unittest.main()
