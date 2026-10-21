"""Cable transport fault injection; no framework, pairing, or device writes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.pocket_device import (APPROVED_BUNDLE_IDS, CHUNK_BYTES, SPACE_RESERVE_BYTES,
                               PocketDeviceTransport, PocketDeviceError, PocketTransferUncertain,
                               _WorkerProcess, _WorkerSession)

DEVICE = "a" * 32
BUNDLE = "com.nategrenade.stillroom.pocket.dev"
IDENTIFIER = "b" * 32
STAGE = f".stillroom-{IDENTIFIER}.partial"
PAYLOAD = STAGE + "/library.partial"
FINAL = f"Stillroom-{IDENTIFIER}.stillroomlibrary"


class Session:
    def __init__(self):
        self.files = {"existing.stillroomlibrary": b"never touch"}
        self.dirs = set()
        self.handles = {}
        self.operations = []
        self.free = 1024 ** 4
        self.fail_write = False
        self.corrupt = False
        self.disconnect_rename = None
        self.closed = False
        self.after_rename = lambda: None

    def __enter__(self): self.closed = False; return self
    def __exit__(self, *_): self.closed = True
    def free_bytes(self): return self.free
    def listdir(self, path=""): return sorted(self.files)
    def mkdir(self, path):
        self.operations.append(("mkdir", path))
        if path in self.dirs or path in self.files: raise PocketDeviceError("exists", "Already exists")
        self.dirs.add(path)
    def stat(self, path):
        self.operations.append(("stat", path))
        if path in self.files: return {"size": len(self.files[path]), "type": "file"}
        if path in self.dirs: return {"size": 0, "type": "directory"}
        return None
    def open_write(self, path):
        self.operations.append(("open_write", path))
        if path.rsplit("/", 1)[0] not in self.dirs: raise AssertionError("No owned directory")
        self.files[path] = b""
        handle = len(self.handles) + 1
        self.handles[handle] = path
        return handle
    def write(self, handle, data):
        self.operations.append(("write", len(data)))
        if self.fail_write: raise PocketDeviceError("disconnected", "Disconnected")
        self.files[self.handles[handle]] += data
    def close_file(self, handle): self.operations.append(("close_file", handle)); del self.handles[handle]
    def read(self, path, offset, size):
        self.operations.append(("read", path, offset, size))
        if path in self.handles.values(): raise AssertionError("Read before closing write handle")
        data = self.files[path][offset:offset + size]
        return bytes(value ^ 1 for value in data) if self.corrupt else data
    def rename(self, old, new):
        self.operations.append(("rename", old, new))
        if self.disconnect_rename == "before": raise PocketDeviceError("disconnected", "Disconnected")
        if new in self.files: raise AssertionError("Never overwrite a destination")
        self.files[new] = self.files.pop(old)
        self.after_rename()
        if self.disconnect_rename == "after": raise PocketDeviceError("disconnected", "Disconnected")
    def remove(self, path):
        self.operations.append(("remove", path))
        if path in self.files: del self.files[path]
        elif path in self.dirs:
            if any(name.startswith(path + "/") for name in self.files): raise PocketDeviceError("not_empty", "Directory not empty")
            self.dirs.remove(path)
        else: raise PocketDeviceError("not_found", "Missing")


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "selection.stillroomlibrary"
        self.data = bytes(range(256)) * (CHUNK_BYTES // 256 * 2 + 7)
        self.path.write_bytes(self.data)
        self.session = Session()
        self.opens = []
        def factory(device, bundle): self.opens.append((device, bundle)); return self.session
        self.transport = PocketDeviceTransport(session_factory=factory)
        self.uuid_patch = patch("app.pocket_device.uuid.uuid4", return_value=uuid.UUID(IDENTIFIER))
        self.uuid_patch.start()

    def tearDown(self): self.uuid_patch.stop(); self.temporary.cleanup()
    def send(self, **kwargs): return self.transport.send(self.path, DEVICE, BUNDLE, **kwargs)
    def assert_owned_cleanup(self):
        self.assertEqual(self.session.files, {"existing.stillroomlibrary": b"never touch"})
        self.assertEqual(self.session.dirs, set())
        self.assertEqual(self.session.handles, {})

    def test_success_streams_then_closes_reads_every_byte_and_publishes_without_touching_existing(self):
        events = []
        result = self.send(progress=events.append)
        self.assertEqual(result["status"], "copied")
        self.assertEqual(result["bytes"], len(self.data))
        self.assertEqual(result["sha256"], hashlib.sha256(self.data).hexdigest())
        self.assertEqual(self.session.files[FINAL], self.data)
        self.assertEqual(self.session.files["existing.stillroomlibrary"], b"never touch")
        self.assertEqual(self.session.dirs, set())
        writes = [op[1] for op in self.session.operations if op[0] == "write"]
        self.assertGreater(len(writes), 1); self.assertLessEqual(max(writes), CHUNK_BYTES)
        reads = [op for op in self.session.operations if op[0] == "read"]
        self.assertEqual(sum(op[3] for op in reads[:-1]), len(self.data))
        self.assertEqual(reads[-1][2:], (len(self.data), 1))
        self.assertEqual({event["phase"] for event in events}, {"staging", "transferring", "verifying", "finalizing"})
        receipts = [event for event in events if "receipt" in event]
        self.assertEqual([event["phase"] for event in receipts], ["staging", "finalizing"])
        self.assertTrue(receipts[0]["receipt"]["cleanupPending"]); self.assertNotIn("sha256", receipts[0]["receipt"])
        self.assertEqual(receipts[1]["receipt"]["sha256"], result["sha256"])
        self.assertTrue(self.session.closed)
        self.assertEqual(self.path.read_bytes(), self.data)

    def test_invalid_target_never_opens_any_device_or_arbitrary_bundle(self):
        for device, bundle in [("../bad", BUNDLE), (DEVICE, "com.example.other"), (DEVICE, "com.nategrenade.stillroom.pocket")]:
            with self.subTest(device=device, bundle=bundle), self.assertRaises(PocketDeviceError):
                self.transport.send(self.path, device, bundle)
        self.assertEqual(APPROVED_BUNDLE_IDS, {BUNDLE}); self.assertEqual(self.opens, [])

    def test_missing_empty_or_symlink_export_never_touches_phone(self):
        self.path.unlink()
        with self.assertRaises(PocketDeviceError): self.send()
        self.path.touch()
        with self.assertRaises(PocketDeviceError): self.send()
        self.path.unlink(); self.path.symlink_to(Path(self.temporary.name))
        with self.assertRaises(PocketDeviceError): self.send()
        self.assertEqual(self.opens, [])

    def test_free_space_or_unknown_space_fails_before_creating_anything(self):
        for free in [None, True, -1, len(self.data) + SPACE_RESERVE_BYTES - 1]:
            self.session.free = free
            with self.subTest(free=free), self.assertRaises(PocketDeviceError): self.send()
            self.assertEqual(self.session.operations, [])

    def test_existing_staging_directory_is_never_adopted_or_cleaned(self):
        self.session.dirs.add(STAGE); self.session.files[PAYLOAD] = b"someone else's partial"
        with self.assertRaises(PocketDeviceError) as error: self.send()
        self.assertEqual(error.exception.code, "exists")
        self.assertEqual(self.session.files[PAYLOAD], b"someone else's partial")
        self.assertFalse(any(op[0] in {"open_write", "remove"} for op in self.session.operations))

    def test_lost_stage_creation_acknowledgement_never_assumes_deletion_ownership(self):
        def mkdir(path):
            self.session.dirs.add(path)
            raise PocketDeviceError("disconnected", "Disconnected")
        self.session.mkdir = mkdir
        with self.assertRaises(PocketDeviceError) as error: self.send()
        self.assertEqual(error.exception.code, "cleanup_pending")
        self.assertTrue(error.exception.details["stageCreationUncertain"])
        self.assertIn(STAGE, self.session.dirs)
        self.assertFalse(any(op[0] in {"remove", "open_write"} for op in self.session.operations))

    def test_existing_final_filename_is_not_overwritten_and_only_owned_partial_is_removed(self):
        self.session.files[FINAL] = b"existing final"
        with self.assertRaises(PocketDeviceError) as error: self.send()
        self.assertEqual(error.exception.code, "exists")
        self.assertEqual(self.session.files[FINAL], b"existing final")
        self.assertNotIn(PAYLOAD, self.session.files)
        self.assertFalse(any(op[0] == "rename" for op in self.session.operations))

    def test_write_disconnect_closes_handle_and_cleans_only_current_partial(self):
        self.session.fail_write = True
        with self.assertRaises(PocketDeviceError) as error: self.send()
        self.assertEqual(error.exception.code, "disconnected"); self.assert_owned_cleanup()

    def test_corrupt_full_readback_is_not_published(self):
        self.session.corrupt = True
        with self.assertRaises(PocketDeviceError) as error: self.send()
        self.assertEqual(error.exception.code, "verification_failed"); self.assert_owned_cleanup()
        self.assertFalse(any(op[0] == "rename" for op in self.session.operations))

    def test_cancel_before_copy_opens_nothing(self):
        with self.assertRaises(PocketDeviceError) as error: self.send(cancelled=lambda: True)
        self.assertEqual(error.exception.code, "cancelled"); self.assertEqual(self.opens, [])

    def test_cancel_during_write_or_readback_cleans_owned_partial(self):
        for phase in ["transferring", "verifying", "finalizing"]:
            self.session = Session(); cancelled = [False]
            def progress(event):
                if event["phase"] == phase: cancelled[0] = True
            with self.subTest(phase=phase), self.assertRaises(PocketDeviceError) as error:
                self.send(cancelled=lambda: cancelled[0], progress=progress)
            self.assertEqual(error.exception.code, "cancelled"); self.assert_owned_cleanup()

    def test_receipt_persistence_must_succeed_before_any_phone_mutation_or_publication(self):
        for phase in ["staging", "finalizing"]:
            self.session = Session()
            def progress(event):
                if event["phase"] == phase: raise RuntimeError("Receipt persistence failed")
            with self.subTest(phase=phase), self.assertRaises(RuntimeError): self.send(progress=progress)
            self.assert_owned_cleanup()
            self.assertFalse(any(op[0] == "rename" for op in self.session.operations))
            if phase == "staging": self.assertFalse(any(op[0] == "mkdir" for op in self.session.operations))

    def test_cancellation_with_unconfirmed_cleanup_reports_pending_receipt_instead_of_cancelled(self):
        cancelled = [False]
        def progress(event):
            if event["phase"] == "transferring": cancelled[0] = True
        self.session.remove = lambda path: (_ for _ in ()).throw(PocketDeviceError("disconnected", "Disconnected"))
        with self.assertRaises(PocketDeviceError) as error: self.send(cancelled=lambda: cancelled[0], progress=progress)
        self.assertEqual(error.exception.code, "cleanup_pending")
        self.assertTrue(error.exception.details["cleanupPending"])
        self.assertEqual(error.exception.details["partialPath"], STAGE)
        self.assertIn("hidden incomplete", str(error.exception))
        self.assertIn(PAYLOAD, self.session.files)

    def test_local_change_during_write_fails_and_cleans_partial(self):
        changed = [False]
        def progress(event):
            if event["phase"] == "transferring" and not changed[0]:
                changed[0] = True
                with self.path.open("ab") as stream: stream.write(b"change")
        with self.assertRaises(PocketDeviceError) as error: self.send(progress=progress)
        self.assertEqual(error.exception.code, "export_changed"); self.assert_owned_cleanup()

    def test_late_cancel_after_publication_is_a_completed_verified_copy(self):
        cancelled = [False]; self.session.after_rename = lambda: cancelled.__setitem__(0, True)
        result = self.send(cancelled=lambda: cancelled[0])
        self.assertEqual(result["status"], "copied"); self.assertEqual(self.session.files[FINAL], self.data)

    def test_session_teardown_error_cannot_change_acknowledged_publication_into_failure(self):
        class RaisingClose(Session):
            def __exit__(self, *_): self.closed = True; raise PocketDeviceError("disconnected", "Disconnected during teardown")
        self.session = RaisingClose()
        result = self.send()
        self.assertEqual(result["status"], "copied"); self.assertEqual(self.session.files[FINAL], self.data)

    def test_unknown_rename_outcome_is_never_cleaned_or_retried_and_inspect_reconciles_readonly(self):
        for when, expected in [("before", "still_partial"), ("after", "copied")]:
            self.session = Session(); self.session.disconnect_rename = when
            with self.subTest(when=when), self.assertRaises(PocketTransferUncertain) as error: self.send()
            receipt = error.exception.details
            rename_count = sum(op[0] == "rename" for op in self.session.operations)
            self.assertEqual(rename_count, 1)
            self.assertFalse(any(op[0] == "remove" for op in self.session.operations))
            self.session.operations.clear()
            result = self.transport.inspect(receipt)
            self.assertEqual(result["status"], expected)
            self.assertTrue(all(op[0] in {"stat", "read"} for op in self.session.operations))
            self.assertEqual(self.session.files["existing.stillroomlibrary"], b"never touch")

    def test_inspect_conflict_or_missing_never_mutates_and_rejects_path_receipts(self):
        result = self.send(); self.session.files[FINAL] = b"other data"; self.session.operations.clear()
        self.assertEqual(self.transport.inspect(result)["status"], "conflict")
        self.assertTrue(all(op[0] in {"stat", "read"} for op in self.session.operations))
        del self.session.files[FINAL]
        self.assertEqual(self.transport.inspect(result)["status"], "not_found")
        for key, value in [("partialPath", "../existing"), ("filename", "/Documents/existing"), ("bytes", True), ("sha256", "bad"), ("bundleId", "other")]:
            with self.subTest(key=key), self.assertRaises(PocketDeviceError): self.transport.inspect({**result, key: value})

    def test_cleanup_receipts_without_hash_can_be_checked_readonly_but_never_report_an_unverified_final_copy(self):
        receipt = {"deviceId": DEVICE, "bundleId": BUNDLE, "transferId": IDENTIFIER, "partialPath": STAGE,
                   "filename": FINAL, "bytes": len(self.data), "cleanupPending": True, "stageCreationUncertain": True}
        self.assertEqual(self.transport.inspect(receipt)["status"], "not_found")
        self.session.dirs.add(STAGE)
        self.assertEqual(self.transport.inspect(receipt)["status"], "still_partial")
        self.session.files[PAYLOAD] = b"partial"
        self.assertEqual(self.transport.inspect(receipt)["status"], "still_partial")
        self.session.files[FINAL] = self.data
        self.assertEqual(self.transport.inspect(receipt)["status"], "conflict")
        self.assertTrue(all(op[0] == "stat" for op in self.session.operations))
        without_flag = {key: value for key, value in receipt.items() if key not in {"cleanupPending", "stageCreationUncertain"}}
        with self.assertRaises(PocketDeviceError): self.transport.inspect(without_flag)


class WorkerTests(unittest.TestCase):
    def worker(self, script, timeout=1):
        return _WorkerProcess(command=[sys.executable, "-u", "-c", script], timeout=timeout)

    def test_worker_deadline_terminates_and_reaps_only_its_owned_child(self):
        worker = self.worker("import sys,time; sys.stdin.readline(); time.sleep(5)", timeout=.05)
        try:
            with self.assertRaises(PocketDeviceError) as error: worker.rpc("devices")
            self.assertEqual(error.exception.code, "timeout"); self.assertIsNotNone(worker.process.poll()); self.assertTrue(worker.closed)
        finally: worker.close()

    def test_total_cleanup_deadline_overrides_the_longer_per_command_timeout(self):
        worker = self.worker("import sys,time; sys.stdin.readline(); time.sleep(5)", timeout=3)
        worker.deadline = time.monotonic() + .05
        try:
            with self.assertRaises(PocketDeviceError) as error: worker.rpc("close_file", handle=1)
            self.assertEqual(error.exception.code, "timeout"); self.assertTrue(worker.closed)
            self.assertIsNotNone(worker.process.poll())
        finally: worker.close()

    def test_large_blocked_pipe_write_is_inside_the_same_request_deadline(self):
        script = 'import sys,json,time; r=json.loads(sys.stdin.readline()); print(json.dumps({"id":r["id"],"ok":True,"result":True}),flush=True); time.sleep(5)'
        worker = self.worker(script, timeout=1)
        try:
            self.assertTrue(worker.rpc("open"))
            worker.timeout = .05
            with self.assertRaises(PocketDeviceError) as error: worker.rpc("write", handle=1, data="a" * (CHUNK_BYTES * 2))
            self.assertEqual(error.exception.code, "timeout"); self.assertIsNotNone(worker.process.poll())
            self.assertFalse(worker.writer.is_alive())
        finally: worker.close()

    def test_crashed_or_wrong_serial_worker_is_reaped_and_never_treated_as_success(self):
        for script in ["import os; os._exit(7)", 'import sys,json; sys.stdin.readline(); print(json.dumps({"id":99,"ok":True,"result":[]}))']:
            worker = self.worker(script)
            try:
                with self.subTest(script=script), self.assertRaises(PocketDeviceError) as error: worker.rpc("devices")
                self.assertEqual(error.exception.code, "disconnected"); self.assertIsNotNone(worker.process.poll())
            finally: worker.close()

    def test_worker_safe_error_roundtrip_and_session_bounded_read_validation(self):
        worker = self.worker('import sys,json; r=json.loads(sys.stdin.readline()); print(json.dumps({"id":r["id"],"ok":False,"error":{"code":"trust_required","message":"Unlock and approve Trust yourself."}}))')
        try:
            with self.assertRaises(PocketDeviceError) as error: worker.rpc("open")
            self.assertEqual(error.exception.code, "trust_required"); self.assertNotIn("native", str(error.exception))
        finally: worker.close()


if __name__ == "__main__": unittest.main()
