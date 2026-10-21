"""Job/API failure checks; synthetic bundles and fake devices only."""
import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app.library import LibraryError
from app.library_bundle import ExportError
from app.pocket_exports import PocketExports
from app.pocket_device import PocketDeviceError, PocketTransferUncertain


class FakeExporter:
    def __init__(self):
        self.calls = []
        self.failure = None

    def preflight(self, output, **kwargs):
        self.calls.append(("review", output, kwargs))
        return {"reviewToken": "a" * 64, "trackCount": 1, "playlistCount": 0,
                "audioBytes": 12, "bundleBytes": 24, "requiredBytes": 24, "availableBytes": 1000}

    def export(self, output, **kwargs):
        self.calls.append(("export", output, kwargs))
        if self.failure:
            raise self.failure
        output.write_bytes(b"STLRMB01" + b"synthetic-bundle")
        return {}


class FakeTransport:
    def __init__(self):
        self.sent = []
        self.failure = None
        self.entered = threading.Event()
        self.wait_for_cancel = False

    def devices(self):
        return [{"id": "device-12345678", "name": "Test phone", "apps": []}]

    def send(self, path, device_id, bundle_id, **kwargs):
        self.sent.append((path.read_bytes(), device_id, bundle_id))
        self.entered.set()
        if self.wait_for_cancel:
            while not kwargs["cancelled"]():
                time.sleep(.005)
            raise PocketDeviceError("cancelled", "Cancelled; incomplete file removed.")
        if self.failure:
            raise self.failure
        kwargs["progress"]({"phase": "verifying", "completedBytes": path.stat().st_size,
                            "totalBytes": path.stat().st_size})
        return {"filename": "Stillroom-test.stillroomlibrary", "bytes": path.stat().st_size, "sha256": "b" * 64}


class PocketExportJobsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "source"
        self.root.mkdir()
        self.marker = self.root / "unmodified"
        self.marker.write_bytes(b"source")
        library = type("Library", (), {"root": self.root})()
        self.exporter = FakeExporter()
        self.transport = FakeTransport()
        self.jobs = PocketExports(library, transport=self.transport, exporter=self.exporter,
                                  ledger_path=Path(self.tmp.name) / "export-runtime.json")
        self.selection = {"mode": "tracks", "trackIds": ["c" * 32]}

    def tearDown(self):
        self.jobs.close()
        self.assertEqual(self.marker.read_bytes(), b"source")
        self.assertEqual([p.name for p in self.root.iterdir()], ["unmodified"])
        self.tmp.cleanup()

    def request(self):
        review = self.jobs.review({"selection": self.selection})
        return {"selection": copy.deepcopy(self.selection), "reviewToken": review["reviewToken"],
                "deviceId": "device-12345678", "bundleId": "com.nategrenade.stillroom.pocket.dev", "requestId": "d" * 32}

    def finish(self, job):
        self.jobs._jobs[job["id"]]["_thread"].join(2)
        final = self.jobs.get(job["id"])
        self.assertIn(final["state"], {"complete", "failed", "cancelled", "uncertain"})
        return final

    def test_success_is_readonly_and_temporary_bundle_removed(self):
        body = self.request()
        final = self.finish(self.jobs.start(body))
        self.assertEqual(final["state"], "complete")
        self.assertEqual(len(self.transport.sent), 1)
        self.assertNotIn("_thread", final)
        self.assertFalse(self.exporter.calls[-1][1].exists())
        self.assertEqual(self.exporter.calls[-1][2]["expected_review_token"], body["reviewToken"])
        self.assertFalse(self.exporter.calls[-1][1].is_relative_to(self.root))

    def test_response_retry_returns_same_job_without_second_transfer(self):
        body = self.request()
        first = self.jobs.start(body)
        self.finish(first)
        second = self.jobs.start(body)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.transport.sent), 1)

    def test_reused_request_id_different_target_refused(self):
        body = self.request()
        self.finish(self.jobs.start(body))
        body["deviceId"] = "different-device-1234"
        with self.assertRaises(LibraryError):
            self.jobs.start(body)

    def test_unreviewed_selection_or_root_override_refused(self):
        body = self.request()
        body["selection"] = {"mode": "all"}
        with self.assertRaises(LibraryError):
            self.jobs.start(body)
        with self.assertRaises(LibraryError):
            self.jobs.review({"selection": self.selection, "root": "/somewhere"})

    def test_changed_source_review_fails_before_phone_write(self):
        body = self.request()
        self.exporter.failure = ExportError("Source changed. Review again.")
        final = self.finish(self.jobs.start(body))
        self.assertEqual(final["state"], "failed")
        self.assertEqual(self.transport.sent, [])

    def test_cancel_waits_for_confirmed_cleanup(self):
        body = self.request()
        self.transport.wait_for_cancel = True
        job = self.jobs.start(body)
        self.assertTrue(self.transport.entered.wait(2))
        self.assertTrue(self.jobs.busy)
        self.jobs.cancel(job["id"])
        self.assertEqual(self.finish(job)["state"], "cancelled")
        self.assertFalse(self.jobs.busy)

    def test_uncertain_publication_stays_uncertain_and_never_resends(self):
        body = self.request()
        self.transport.failure = PocketTransferUncertain({"filename": "Stillroom-test.stillroomlibrary"})
        first = self.jobs.start(body)
        final = self.finish(first)
        self.assertEqual(final["state"], "uncertain")
        self.assertEqual(final["transfer"]["filename"], "Stillroom-test.stillroomlibrary")
        self.jobs.start(body)
        self.assertEqual(len(self.transport.sent), 1)

    def test_unexpected_exception_does_not_leak_paths_or_secrets(self):
        body = self.request()
        self.transport.failure = RuntimeError("secret-session-private-path")
        final = self.finish(self.jobs.start(body))
        self.assertEqual(final["state"], "failed")
        self.assertNotIn("secret", str(final))

    def test_windows_reports_unvalidated_adapter_without_install(self):
        with patch("app.pocket_exports.sys.platform", "win32"):
            status = self.jobs.devices()
            with self.assertRaises(LibraryError):
                self.jobs.review({"selection": self.selection})
            self.assertEqual(self.exporter.calls, [])
        self.assertFalse(status["supported"])
        self.assertEqual(status["devices"], [])
        self.assertIn("Windows", status["message"])

    def test_exact_verified_app_identity_required(self):
        body = self.request()
        body["bundleId"] = "com.unrelated.app"
        with self.assertRaises(LibraryError):
            self.jobs.start(body)
        self.assertEqual(self.transport.sent, [])

    def test_closed_jobs_refuse_new_work(self):
        self.jobs.close()
        with self.assertRaises(LibraryError):
            self.jobs.review({"selection": self.selection})

    def test_expired_review_requires_explicit_rereview(self):
        body = self.request()
        self.jobs._reviews[body["reviewToken"]] = (0, self.selection)
        with self.assertRaises(LibraryError):
            self.jobs.start(body)

    def test_cleanup_uncertainty_survives_restart_and_blocks_new_request(self):
        body = self.request()
        receipt = {"deviceId": body["deviceId"], "bundleId": body["bundleId"],
                   "transferId": "e" * 32, "partialPath": ".stillroom-" + "e" * 32 + ".partial",
                   "filename": "Stillroom-" + "e" * 32 + ".stillroomlibrary", "bytes": 100, "cleanupPending": True}
        self.transport.failure = PocketDeviceError("cleanup_pending", "Reconnect to check incomplete transfer.", details=receipt)
        job = self.finish(self.jobs.start(body))
        self.assertEqual(job["state"], "uncertain")
        self.assertTrue(self.jobs.busy)
        body["requestId"] = "f" * 32
        with self.assertRaises(LibraryError):
            self.jobs.start(body)
        library = type("Library", (), {"root": self.root})()
        restored = PocketExports(library, transport=self.transport, exporter=self.exporter,
                                 ledger_path=self.jobs._ledger_path)
        try:
            self.assertEqual(restored.get(job["id"])["transfer"], receipt)
            self.assertTrue(restored.busy)
            self.transport.inspect = lambda record: {"status": "not_found", **record}
            checked = restored.check_transfer(job["id"])
            self.assertEqual(checked["state"], "failed")
            self.assertFalse(restored.busy)
        finally:
            restored.close()

    def test_ledger_save_failure_stops_before_thread_or_phone_copy(self):
        body = self.request()
        with patch("app.pocket_exports.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(LibraryError):
                self.jobs.start(body)
        self.assertEqual(self.transport.sent, [])
        self.assertFalse(self.jobs.busy)
        self.assertEqual(self.jobs.jobs(), [])
