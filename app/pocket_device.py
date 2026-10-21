"""Bounded, paired-only USB transfers into Pocket Stillroom's Documents.

Apple's private MobileDevice ABI runs only in an owned worker process. The
parent never imports a native framework and every command has a deadline.
Files are staged in a newly created UUID directory, closed and read back in
full before publication. A lost rename acknowledgement is never retried.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import stat
import subprocess
import sys
import threading
import time
import uuid

from .library import LibraryError
from .runtime import subprocess_options

APPROVED_BUNDLE_IDS = frozenset({"com.nategrenade.stillroom.pocket.dev"})
CHUNK_BYTES = 128 * 1024
MAX_TRANSFER_BYTES = 32 * 1024 * 1024 * 1024 + 16 * 1024 * 1024
SPACE_RESERVE_BYTES = 32 * 1024 * 1024
COMMAND_TIMEOUT = 30
_DEVICE_ID = re.compile(r"[a-f0-9]{32}\Z")
_TRANSFER_ID = re.compile(r"[a-f0-9]{32}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")


class PocketDeviceError(LibraryError):
    def __init__(self, code, message, *, details=None, status=409):
        super().__init__(message, status)
        self.code = code
        self.details = details or {}


class PocketTransferUncertain(PocketDeviceError):
    def __init__(self, details):
        super().__init__("transfer_uncertain", "The iPhone disconnected while finishing the copy. Reconnect it and check this transfer before exporting again.", details=details)


def _selection(device_id, bundle_id):
    if not isinstance(device_id, str) or not _DEVICE_ID.fullmatch(device_id):
        raise PocketDeviceError("invalid_device", "Choose a currently connected USB iPhone.")
    if bundle_id not in APPROVED_BUNDLE_IDS:
        raise PocketDeviceError("invalid_app", "Choose the verified Pocket Stillroom Dev app.")


def _cancel(cancelled):
    if cancelled():
        raise PocketDeviceError("cancelled", "Export cancelled.")


@contextlib.contextmanager
def _transfer_session(manager, publication):
    session = manager.__enter__()
    try:
        yield session
    except BaseException:
        # Teardown must preserve the authoritative operation/cleanup result.
        with contextlib.suppress(Exception):
            manager.__exit__(*sys.exc_info())
        raise
    else:
        try:
            manager.__exit__(None, None, None)
        except Exception:
            if not publication["committed"]:
                raise


class _WorkerProcess:
    """One serialized request stream and a bounded wait for native responses."""
    def __init__(self, *, timeout=COMMAND_TIMEOUT, command=None):
        self.timeout = timeout
        self.process = subprocess.Popen(command or [sys.executable, "-m", "app.pocket_device", "--worker"],
                                        cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        **subprocess_options())
        self.responses = queue.Queue(maxsize=2)
        self.lock = threading.Lock()
        self.serial = 0
        self.closed = False
        self.deadline = None
        self.writer = None
        self.reader = threading.Thread(target=self._read, daemon=True, name="stillroom-pocket-native-response")
        self.reader.start()

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(CHUNK_BYTES * 3 + 4096)
                if not line:
                    break
                if not line.endswith(b"\n"):
                    break
                self.responses.put_nowait(json.loads(line))
        except (OSError, ValueError, queue.Full):
            pass
        finally:
            with contextlib.suppress(queue.Full):
                self.responses.put_nowait(None)

    def rpc(self, op, **args):
        with self.lock:
            if self.closed:
                raise PocketDeviceError("disconnected", "The iPhone connection ended. Reconnect and unlock it, then try again.")
            self.serial += 1
            try:
                deadline = time.monotonic() + self.timeout
                if self.deadline is not None:
                    deadline = min(deadline, self.deadline)
                if deadline <= time.monotonic():
                    raise queue.Empty()
                line = json.dumps({"id": self.serial, "op": op, "args": args}, separators=(",", ":")).encode() + b"\n"
                written = queue.Queue(maxsize=1)
                def write():
                    try:
                        self.process.stdin.write(line)
                        self.process.stdin.flush()
                        written.put_nowait(True)
                    except (OSError, ValueError):
                        written.put_nowait(False)
                    finally:
                        if self.closed:
                            with contextlib.suppress(OSError, ValueError):
                                self.process.stdin.close()
                # A stopped native worker may not read the next pipe-sized
                # frame. The same deadline must cover writing and its reply.
                self.writer = threading.Thread(target=write, daemon=True, name="stillroom-pocket-native-request")
                self.writer.start()
                if not written.get(timeout=max(0, deadline - time.monotonic())):
                    raise OSError()
                if time.monotonic() >= deadline:
                    raise queue.Empty()
                response = self.responses.get(timeout=max(0, deadline - time.monotonic()))
                if time.monotonic() >= deadline:
                    raise queue.Empty()
            except queue.Empty:
                self.close()
                raise PocketDeviceError("timeout", "The iPhone stopped responding. Reconnect and unlock it, then check the transfer.") from None
            except (OSError, ValueError):
                self.close()
                raise PocketDeviceError("disconnected", "The iPhone connection ended. Reconnect and unlock it, then check the transfer.") from None
            if not isinstance(response, dict) or response.get("id") != self.serial:
                self.close()
                raise PocketDeviceError("disconnected", "The iPhone helper stopped unexpectedly. Reconnect and check the transfer.")
            if not response.get("ok"):
                error = response.get("error", {})
                code = error.get("code") if isinstance(error.get("code"), str) else "io_error"
                message = error.get("message") if isinstance(error.get("message"), str) else "The iPhone operation could not finish."
                raise PocketDeviceError(code[:80], message[:500])
            return response.get("result")

    def close(self):
        if self.closed:
            return
        self.closed = True
        # Kill before closing stdin: its Python buffered lock may currently be
        # held by a blocked writer, which must first see the pipe reader exit.
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        if self.writer is not None:
            self.writer.join(timeout=.2)
        if self.writer is None or not self.writer.is_alive():
            with contextlib.suppress(OSError, ValueError):
                self.process.stdin.close()
        with contextlib.suppress(OSError):
            self.process.stdout.close()


class _WorkerSession:
    def __init__(self, device_id, bundle_id, worker_factory=_WorkerProcess):
        self.worker = worker_factory()
        try:
            self.worker.rpc("open", deviceId=device_id, bundleId=bundle_id)
        except Exception:
            self.worker.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if not self.worker.closed:
            with contextlib.suppress(Exception):
                self.worker.rpc("close")
        self.worker.close()

    def begin_cleanup(self):
        # One deadline covers all cleanup commands and session shutdown, rather
        # than allowing each close/remove call another full native timeout.
        self.worker.deadline = time.monotonic() + 2

    def free_bytes(self): return self.worker.rpc("free_bytes")
    def listdir(self, path=""): return self.worker.rpc("listdir", path=path)
    def stat(self, path): return self.worker.rpc("stat", path=path)
    def mkdir(self, path): return self.worker.rpc("mkdir", path=path)
    def open_write(self, path): return self.worker.rpc("open_write", path=path)
    def write(self, handle, data): return self.worker.rpc("write", handle=handle, data=base64.b64encode(data).decode("ascii"))
    def close_file(self, handle): return self.worker.rpc("close_file", handle=handle)
    def read(self, path, offset, size):
        encoded = self.worker.rpc("read", path=path, offset=offset, size=size)
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raise PocketDeviceError("io_error", "The iPhone returned an invalid read response.") from None
        if len(data) > size:
            raise PocketDeviceError("io_error", "The iPhone returned an invalid read response.")
        return data
    def rename(self, old, new): return self.worker.rpc("rename", old=old, new=new)
    def remove(self, path): return self.worker.rpc("remove", path=path)


class PocketDeviceTransport:
    def __init__(self, *, session_factory=None, device_provider=None, worker_factory=_WorkerProcess):
        self.worker_factory = worker_factory
        self.session_factory = session_factory
        self.device_provider = device_provider

    def devices(self):
        if self.device_provider:
            return self.device_provider()
        if sys.platform != "darwin":
            raise PocketDeviceError("unsupported", "Direct cable export currently supports macOS. No software was installed.", status=503)
        worker = self.worker_factory()
        try:
            return worker.rpc("devices")
        finally:
            worker.close()

    def open(self, device_id, bundle_id):
        _selection(device_id, bundle_id)
        if self.session_factory:
            return self.session_factory(device_id, bundle_id)
        if sys.platform != "darwin":
            raise PocketDeviceError("unsupported", "Direct cable export currently supports macOS. No software was installed.", status=503)
        return _WorkerSession(device_id, bundle_id, self.worker_factory)

    @staticmethod
    def _verify(session, path, size, expected, cancelled, progress):
        info = session.stat(path)
        if not info or info.get("type") != "file" or info.get("size") != size:
            raise PocketDeviceError("verification_failed", "The iPhone copy has an unexpected size. It was not published.")
        digest = hashlib.sha256()
        offset = 0
        while offset < size:
            _cancel(cancelled)
            data = session.read(path, offset, min(CHUNK_BYTES, size - offset))
            if not data:
                raise PocketDeviceError("verification_failed", "The iPhone copy ended early. It was not published.")
            digest.update(data)
            offset += len(data)
            progress({"phase": "verifying", "completedBytes": offset, "totalBytes": size})
        if session.read(path, size, 1) or digest.hexdigest() != expected:
            raise PocketDeviceError("verification_failed", "The iPhone copy did not match the export. It was not published.")

    @staticmethod
    def _cleanup(session, stage, payload):
        cleaned = True
        for path in (payload, stage):
            try:
                session.remove(path)
            except PocketDeviceError as exc:
                if exc.code != "not_found":
                    cleaned = False
            except Exception:
                cleaned = False
        return cleaned

    def send(self, local_bundle, device_id, bundle_id, *, cancelled=lambda: False, progress=lambda event: None):
        _selection(device_id, bundle_id)
        _cancel(cancelled)
        transfer_id = uuid.uuid4().hex
        stage = f".stillroom-{transfer_id}.partial"
        payload = stage + "/library.partial"
        filename = f"Stillroom-{transfer_id}.stillroomlibrary"
        receipt = {"deviceId": device_id, "bundleId": bundle_id, "transferId": transfer_id,
                   "partialPath": stage, "filename": filename}
        try:
            descriptor = os.open(os.fspath(local_bundle), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            raise PocketDeviceError("invalid_export", "The prepared export is unavailable. Review the selection again.") from None
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_TRANSFER_BYTES:
                raise PocketDeviceError("invalid_export", "The prepared export is empty or exceeds the transfer limit.")
            size = before.st_size
            receipt["bytes"] = size
            publication = {"committed": False}
            with _transfer_session(self.open(device_id, bundle_id), publication) as session:
                _cancel(cancelled)
                free = session.free_bytes()
                if not isinstance(free, int) or isinstance(free, bool) or free < 0:
                    raise PocketDeviceError("space_unknown", "Available iPhone space could not be checked. Unlock and reconnect it, then try again.")
                if free < size + SPACE_RESERVE_BYTES:
                    raise PocketDeviceError("no_space", "The iPhone needs more free space for this export.")
                owned = False
                creating_stage = False
                handle = None
                committing = False
                try:
                    # mkdir is the ownership boundary. Existing names are never
                    # opened, truncated, adopted, or removed by this operation.
                    progress({"phase": "staging", "receipt": {**receipt, "cleanupPending": True}, "cleanupPending": True,
                              "completedBytes": 0, "totalBytes": size})
                    _cancel(cancelled)
                    creating_stage = True
                    session.mkdir(stage)
                    creating_stage = False
                    owned = True
                    _cancel(cancelled)
                    handle = session.open_write(payload)
                    digest = hashlib.sha256()
                    completed = 0
                    while data := source.read(CHUNK_BYTES):
                        _cancel(cancelled)
                        if completed + len(data) > size:
                            raise PocketDeviceError("export_changed", "The prepared export changed during the copy. Review it again.")
                        session.write(handle, data)
                        digest.update(data)
                        completed += len(data)
                        progress({"phase": "transferring", "completedBytes": completed, "totalBytes": size})
                    session.close_file(handle)
                    handle = None
                    after = os.fstat(source.fileno())
                    if completed != size or (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                        raise PocketDeviceError("export_changed", "The prepared export changed during the copy. Review it again.")
                    receipt["sha256"] = digest.hexdigest()
                    self._verify(session, payload, size, receipt["sha256"], cancelled, progress)
                    _cancel(cancelled)
                    if session.stat(filename) is not None:
                        raise PocketDeviceError("exists", "A transfer with this generated filename already exists. Nothing was replaced.")
                    progress({"phase": "finalizing", "receipt": dict(receipt), "completedBytes": size, "totalBytes": size})
                    _cancel(cancelled)
                    # AFC has no verified no-replace rename primitive. A random
                    # 128-bit name plus this absent check avoids ordinary name
                    # collisions; another actor racing that exact name cannot
                    # be excluded by this private protocol.
                    committing = True
                    session.rename(payload, filename)
                    publication["committed"] = True
                    committing = False
                    # Publication is the commit boundary. A late Cancel cannot
                    # turn a verified, published file into a cancelled result.
                    with contextlib.suppress(Exception):
                        session.remove(stage)
                    return {"status": "copied", **receipt}
                except Exception as exc:
                    if hasattr(session, "begin_cleanup"):
                        session.begin_cleanup()
                    if creating_stage and isinstance(exc, PocketDeviceError) and exc.code in {"disconnected", "timeout", "io_error", "unavailable"}:
                        # A lost mkdir acknowledgement establishes no deletion
                        # ownership. Keep the path for inspection, not adoption.
                        raise PocketDeviceError("cleanup_pending", "The connection ended while creating this transfer's hidden staging folder. Its creation is uncertain; no existing files were changed or removed.", details={**receipt, "cleanupPending": True, "stageCreationUncertain": True}) from None
                    if handle is not None:
                        with contextlib.suppress(Exception):
                            session.close_file(handle)
                    if committing:
                        raise PocketTransferUncertain(receipt) from None
                    if owned:
                        cleaned = self._cleanup(session, stage, payload)
                        if not cleaned:
                            raise PocketDeviceError("cleanup_pending", "The copy stopped, but removal of its hidden incomplete file could not be confirmed. Reconnect the iPhone before checking or exporting again.", details={**receipt, "cleanupPending": True}) from None
                    raise

    def inspect(self, receipt, *, cancelled=lambda: False):
        """Read-only reconciliation; never resend, rename or delete anything."""
        if not isinstance(receipt, dict):
            raise PocketDeviceError("invalid_receipt", "This transfer receipt is invalid.")
        device_id, bundle_id, transfer_id = (receipt.get(key) for key in ("deviceId", "bundleId", "transferId"))
        _selection(device_id, bundle_id)
        size, expected = receipt.get("bytes"), receipt.get("sha256")
        cleanup_receipt = receipt.get("cleanupPending") is True or receipt.get("stageCreationUncertain") is True
        if (not isinstance(transfer_id, str) or not _TRANSFER_ID.fullmatch(transfer_id)
                or type(size) is not int or not 0 < size <= MAX_TRANSFER_BYTES
                or (expected is None and not cleanup_receipt)
                or (expected is not None and (not isinstance(expected, str) or not _HASH.fullmatch(expected)))
                or receipt.get("filename") != f"Stillroom-{transfer_id}.stillroomlibrary"
                or receipt.get("partialPath") != f".stillroom-{transfer_id}.partial"):
            raise PocketDeviceError("invalid_receipt", "This transfer receipt is invalid.")
        receipt = {key: receipt[key] for key in ("deviceId", "bundleId", "transferId", "partialPath", "filename", "bytes", "sha256", "cleanupPending", "stageCreationUncertain") if key in receipt}
        _cancel(cancelled)
        with self.open(device_id, bundle_id) as session:
            final = session.stat(receipt["filename"])
            if final is not None:
                if expected is None:
                    return {**receipt, "status": "conflict", "message": "A final file exists, but this interrupted transfer has no verification hash. Nothing was changed."}
                try:
                    self._verify(session, receipt["filename"], size, expected, cancelled, lambda event: None)
                except PocketDeviceError as exc:
                    if exc.code != "verification_failed":
                        raise
                    return {"status": "conflict", **receipt, "message": "The destination exists but does not match this transfer. Nothing was changed."}
                return {"status": "copied", **receipt}
            stage = session.stat(receipt["partialPath"])
            return {"status": "still_partial" if stage is not None else "not_found", **receipt,
                    "message": "No verified final file was found. No files were changed."}


def _worker_main():
    # This import, framework loading, and all native references remain confined
    # to the worker. stdout is exclusively the bounded JSON protocol.
    from .pocket_device_native import NativeBackend
    backend = None
    session = None
    try:
        while line := sys.stdin.buffer.readline(CHUNK_BYTES * 3 + 4096):
            identifier = None
            try:
                if len(line) > CHUNK_BYTES * 3 + 4096:
                    raise ValueError()
                request = json.loads(line)
                identifier, op, args = request["id"], request["op"], request.get("args", {})
                if type(identifier) is not int or not isinstance(args, dict):
                    raise ValueError()
                if backend is None:
                    backend = NativeBackend()
                if op == "devices": result = backend.devices()
                elif op == "open":
                    if session is not None: raise ValueError()
                    _selection(args["deviceId"], args["bundleId"])
                    session = backend.open(args["deviceId"], args["bundleId"])
                    result = True
                elif op == "close":
                    if session: session.close(); session = None
                    result = True
                elif session is None: raise ValueError()
                elif op == "free_bytes": result = session.free_bytes()
                elif op == "listdir": result = session.listdir(args.get("path", ""))
                elif op == "stat": result = session.stat(args["path"])
                elif op == "mkdir": result = session.mkdir(args["path"])
                elif op == "open_write": result = session.open_write(args["path"])
                elif op == "write":
                    data = base64.b64decode(args["data"], validate=True)
                    if len(data) > CHUNK_BYTES: raise ValueError()
                    result = session.write(args["handle"], data)
                elif op == "close_file": result = session.close_file(args["handle"])
                elif op == "read":
                    if type(args["offset"]) is not int or args["offset"] < 0 or type(args["size"]) is not int or not 0 < args["size"] <= CHUNK_BYTES: raise ValueError()
                    result = base64.b64encode(session.read(args["path"], args["offset"], args["size"])).decode("ascii")
                elif op == "rename": result = session.rename(args["old"], args["new"])
                elif op == "remove": result = session.remove(args["path"])
                else: raise ValueError()
                response = {"id": identifier, "ok": True, "result": result}
            except Exception as exc:
                # Only backend-owned safe messages cross the process boundary.
                code = getattr(exc, "code", "io_error")
                message = str(exc) if getattr(exc, "code", None) else "The iPhone operation could not finish safely."
                response = {"id": identifier, "ok": False, "error": {"code": code, "message": message}}
            print(json.dumps(response, separators=(",", ":")), flush=True)
    finally:
        if session:
            with contextlib.suppress(Exception): session.close()
        if backend:
            with contextlib.suppress(Exception): backend.close()


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    _worker_main()
