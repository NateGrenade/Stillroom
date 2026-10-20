"""Integration coverage for preview-before-save, media serving and safe retries."""
import base64
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app.server import Server
from app.library import LibraryError
from test_backend import make_wave


class ImportJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "Melody.wav"
        make_wave(self.source, duration=5)
        self.server = Server(self.root / "library", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=15)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        status, data = response.status, response.read()
        connection.close()
        return status, data

    def json(self, method, path, body):
        status, raw = self.request(method, path, json.dumps(body), {"Content-Type": "application/json"})
        return status, json.loads(raw)

    def upload(self, options=None):
        status, raw = self.request("POST", "/api/import-jobs/file", self.source.read_bytes(), {
            "Content-Type": "audio/wav", "X-Filename": self.source.name,
            "X-Track-Metadata": base64.b64encode(json.dumps(options or {}).encode()).decode(),
        })
        self.assertEqual(status, 202, raw)
        return json.loads(raw)["job"]["id"]

    def ready(self, identifier):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            job = self.server.import_jobs.get(identifier)
            if job["status"] in ("ready", "error", "cancelled"):
                self.assertEqual(job["status"], "ready", job)
                return job
            time.sleep(0.02)
        self.fail("Preparation did not finish")

    def test_upload_preview_edit_commit_and_retry(self):
        identifier = self.upload({"start": "0:01.125", "end": "0:03.625", "exact": True})
        job = self.ready(identifier)
        self.assertEqual(self.server.library.library()["tracks"], [])
        self.assertEqual(job["metadata"]["title"], "Melody (0:01.125–0:03.625)")
        self.assertAlmostEqual(job["duration"], 2.5, places=3)
        self.assertAlmostEqual(job["analysis"]["appliedStart"], 1.125, delta=1/44100)
        status, raw = self.request("GET", job["audioUrl"], headers={"Range": "bytes=0-63"})
        self.assertEqual(status, 206)
        self.assertEqual(len(raw), 64)
        status, result = self.json("POST", f"/api/import-jobs/{identifier}/commit", {
            "metadata": {"title": "Reviewed melody", "artist": "Composer", "album": "Sketches"},
        })
        self.assertEqual(status, 201, result)
        self.assertEqual(result["track"]["title"], "Reviewed melody")
        self.assertEqual(result["track"]["artist"], "Composer")
        status, retry = self.json("POST", f"/api/import-jobs/{identifier}/commit", {"metadata": {"title": "Ignored retry"}})
        self.assertEqual(status, 201)
        self.assertEqual(retry["track"]["id"], result["track"]["id"])
        self.assertEqual(len(self.server.library.library()["tracks"]), 1)
        self.assertFalse(any((self.server.import_jobs.session / identifier).glob("*")))

    def test_invalid_commit_keeps_preview_for_correction(self):
        identifier = self.upload()
        job = self.ready(identifier)
        status, result = self.json("POST", f"/api/import-jobs/{identifier}/commit", {"metadata": {"title": ""}})
        self.assertEqual(status, 400, result)
        self.assertEqual(self.server.import_jobs.get(identifier)["status"], "ready")
        self.assertEqual(self.request("GET", job["audioUrl"])[0], 200)

    def test_discard_never_adds_track_and_removes_preview(self):
        identifier = self.upload()
        job = self.ready(identifier)
        status, _ = self.request("DELETE", f"/api/import-jobs/{identifier}")
        self.assertEqual(status, 200)
        self.assertEqual(self.request("GET", job["audioUrl"])[0], 409)
        self.assertEqual(self.server.library.library()["tracks"], [])
        self.assertFalse((self.server.import_jobs.session / identifier).exists())

    def test_bad_urls_ranges_and_cross_origin_fail_before_acquisition(self):
        with patch("app.import_jobs.acquire_youtube") as acquire:
            for options in ({"url": "http://127.0.0.1/audio"},
                            {"url": "https://youtu.be/BaW_jenozKc", "start": "3:99"},
                            {"url": "https://youtu.be/BaW_jenozKc", "start": 3, "end": 2},
                            {"url": "https://youtu.be/BaW_jenozKc", "exact": "false"}):
                status, _ = self.json("POST", "/api/import-jobs", options)
                self.assertEqual(status, 400, options)
            status, _ = self.request("POST", "/api/import-jobs", b"{}", {
                "Content-Type": "application/json", "Origin": "https://example.com",
            })
            self.assertEqual(status, 403)
            acquire.assert_not_called()

    def test_failed_download_has_visible_error_and_no_saved_audio(self):
        with patch("app.import_jobs.acquire_youtube", side_effect=LibraryError("This video is unavailable.")):
            status, result = self.json("POST", "/api/import-jobs", {"url": "https://youtu.be/BaW_jenozKc"})
            self.assertEqual(status, 202)
            identifier = result["job"]["id"]
            self.server.import_jobs.jobs[identifier]["_worker"].join(5)
        job = self.server.import_jobs.get(identifier)
        self.assertEqual(job["status"], "error")
        self.assertIn("unavailable", job["error"])
        self.assertFalse((self.server.import_jobs.session / identifier).exists())
        self.assertEqual(self.server.library.library()["tracks"], [])

    def test_auto_batch_metadata_preserves_embedded_artist(self):
        with patch("app.server.local_metadata", return_value={"title": "Embedded", "artist": "Tagged creator", "album": "Album"}):
            options = {"metadataMode": "auto", "artist": "", "kind": "original"}
            status, raw = self.request("POST", "/api/import", self.source.read_bytes(), {
                "Content-Type": "audio/wav", "X-Filename": self.source.name,
                "X-Track-Metadata": base64.b64encode(json.dumps(options).encode()).decode(),
            })
        self.assertEqual(status, 201, raw)
        self.assertEqual(json.loads(raw)["track"]["artist"], "Tagged creator")

    def test_commit_cannot_override_prepared_loop_bounds(self):
        identifier = self.upload({"start": 1, "end": 4, "exact": True, "loop": True})
        job = self.ready(identifier)
        result = self.server.import_jobs.commit(identifier, {"metadata": {"loopStart": -100, "sourceUrl": "https://example.com"}})
        self.assertEqual(result["track"]["loopStart"], job["loopStart"])
        self.assertEqual(result["track"]["sourceUrl"], job["metadata"].get("sourceUrl", ""))

    def test_cancel_signals_worker_and_releases_preparation_slot(self):
        started = threading.Event()
        def wait_for_cancel(url, directory, progress=None, cancel_event=None):
            started.set()
            if cancel_event.wait(5):
                raise LibraryError("Preparation cancelled.")
            self.fail("Worker was not cancelled")
        with patch("app.import_jobs.acquire_youtube", side_effect=wait_for_cancel):
            job = self.server.import_jobs.create({"url": "https://youtu.be/BaW_jenozKc"})
            self.assertTrue(started.wait(2))
            self.server.import_jobs.discard(job["id"])
            worker = self.server.import_jobs.jobs[job["id"]]["_worker"]
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(self.server.import_jobs.get(job["id"])["status"], "cancelled")
        self.assertFalse((self.server.import_jobs.session / job["id"]).exists())
        self.assertEqual(self.server.library.library()["tracks"], [])


if __name__ == "__main__":
    unittest.main()
