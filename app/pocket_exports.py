"""User-selected, read-only library exports to an explicitly chosen USB iPhone.

Only the server's own library root is used. Jobs own temporary output outside
the library; neither media nor the source database is opened for writing.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import uuid

from .library import LibraryError
from .library_bundle import LibraryExporter, ExportError
from .pocket_device import PocketDeviceTransport, PocketDeviceError, PocketTransferUncertain, APPROVED_BUNDLE_IDS

TERMINAL = {"complete", "cancelled", "failed", "uncertain"}
REVIEW_LIFETIME = 15 * 60
MAX_HISTORY = 16


class PocketExports:
    def __init__(self, library, *, transport=None, exporter=None, ledger_path=None):
        self.root = library.root
        self.transport = transport or PocketDeviceTransport()
        self.exporter = exporter or LibraryExporter(self.root)
        self._lock = threading.RLock()
        self._closed = False
        self._jobs = {}
        self._reviews = {}
        self._requests = {}
        self._ledger_path = Path(ledger_path) if ledger_path else self.root / "data" / ".pocket-exports.json"
        self._ledger_error = False
        self._load_ledger()

    def _load_ledger(self):
        try:
            if not os.path.lexists(self._ledger_path):
                return
            descriptor = os.open(self._ledger_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024 * 1024:
                    raise ValueError()
                encoded = source.read(1024 * 1024 + 1)
                if len(encoded) > 1024 * 1024:
                    raise ValueError()
                value = json.loads(encoded)
            if value.get("format") != "stillroom-pocket-exports-v1" or not isinstance(value.get("jobs"), list) or not isinstance(value.get("requests"), dict):
                raise ValueError()
            if len(value["jobs"]) > MAX_HISTORY or len(value["requests"]) > 1024:
                raise ValueError()
            for request_id, request in value["requests"].items():
                if (not re.fullmatch(r"[0-9a-f]{32}", request_id) or not isinstance(request, dict)
                        or not re.fullmatch(r"[0-9a-f]{32}", request.get("id", ""))
                        or not re.fullmatch(r"[0-9a-f]{64}", request.get("fingerprint", ""))):
                    raise ValueError()
            for saved in value["jobs"]:
                if (not isinstance(saved, dict) or not re.fullmatch(r"[0-9a-f]{32}", saved.get("id", ""))
                        or saved.get("requestId") not in value["requests"]):
                    raise ValueError()
                job = {key: saved[key] for key in ("id", "requestId", "state", "phase", "completedBytes", "totalBytes", "message", "code", "transfer", "result") if key in saved}
                if job.get("state") not in TERMINAL:
                    job.update(state="uncertain" if job.get("transfer") else "failed",
                               message="Stillroom restarted during this export. Check the transfer before trying again." if job.get("transfer") else "Stillroom stopped before a cable copy began. Review the selection to try again.")
                job["_cancel"] = threading.Event()
                job["_thread"] = None
                self._jobs[job["id"]] = job
            self._requests = value["requests"]
        except (OSError, ValueError, TypeError, AttributeError):
            self._jobs.clear(); self._requests.clear()
            self._ledger_error = True

    def _save_ledger(self, *, required=False):
        temporary = None
        try:
            parent = self._ledger_path.parent
            if parent.is_symlink() or not parent.is_dir():
                raise OSError()
            if os.path.lexists(self._ledger_path):
                info = self._ledger_path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise OSError()
            value = {"format": "stillroom-pocket-exports-v1", "jobs": self.jobs(), "requests": self._requests}
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded) > 1024 * 1024:
                raise ValueError()
            descriptor, temporary = tempfile.mkstemp(prefix=".pocket-export-record-", dir=parent)
            with os.fdopen(descriptor, "wb") as target:
                target.write(encoded); target.flush(); os.fsync(target.fileno())
            os.replace(temporary, self._ledger_path)
            temporary = None
        except (OSError, ValueError):
            if required:
                raise LibraryError("The local export recovery record could not be saved. No new cable operation was started. Check free space and folder access.", 503) from None
        finally:
            if temporary:
                try: os.unlink(temporary)
                except OSError: pass

    @property
    def busy(self):
        with self._lock:
            return any(job["state"] not in {"complete", "cancelled", "failed"} for job in self._jobs.values())

    def _ready(self):
        if self._closed:
            raise LibraryError("iPhone export is closing. Restart Stillroom to try again.", 503)
        if self._ledger_error:
            raise LibraryError("The previous export recovery record could not be read safely. Keep it intact and resolve it before starting another cable export.", 409)

    def devices(self):
        self._ready()
        # This initial adapter supports macOS's installed Apple device service.
        # No development tools or automatic pairing/install are invoked.
        if sys.platform != "darwin":
            return {"supported": False, "platform": sys.platform, "devices": [],
                    "message": "Direct cable export currently supports macOS. Windows needs an Apple Devices-compatible transfer adapter, which has not been validated. No software was installed."}
        try:
            return {"supported": True, "platform": "macOS", "devices": self.transport.devices(),
                    "message": "Connect by USB, unlock your iPhone and approve Trust yourself if requested. Only already-trusted cable connections are used. Select the intended Pocket Stillroom app."}
        except PocketDeviceError as exc:
            return {"supported": True, "platform": "macOS", "devices": [], "message": str(exc), "code": exc.code}

    def review(self, body):
        self._ready()
        if sys.platform != "darwin":
            raise LibraryError("Direct cable export currently supports macOS. No export or software installation was started.", 503)
        if set(body) != {"selection"}:
            raise LibraryError("Provide only the song or playlist selection.")
        selection = copy.deepcopy(body["selection"])
        try:
            with tempfile.TemporaryDirectory(prefix="stillroom-pocket-review-") as directory:
                review = self.exporter.preflight(Path(directory).resolve() / "review.stillroomlibrary", selection=selection)
        except ExportError as exc:
            raise LibraryError(str(exc), 409) from None
        token = review["reviewToken"]
        with self._lock:
            self._reviews = {key: value for key, value in self._reviews.items() if time.monotonic() - value[0] < REVIEW_LIFETIME}
            if len(self._reviews) >= MAX_HISTORY:
                self._reviews.pop(next(iter(self._reviews)))
            self._reviews[token] = (time.monotonic(), selection)
        public = {key: review[key] for key in ("reviewToken", "audioBytes", "bundleBytes", "requiredBytes", "availableBytes")}
        public["trackCount"] = review.get("tracks", review.get("trackCount", 0))
        public["playlistCount"] = review.get("playlists", review.get("playlistCount", 0))
        return public

    def start(self, body):
        self._ready()
        if sys.platform != "darwin":
            raise LibraryError("Direct cable export currently supports macOS. No export or software installation was started.", 503)
        if set(body) != {"selection", "reviewToken", "deviceId", "bundleId", "requestId"}:
            raise LibraryError("Review a selection and choose a connected iPhone and Pocket Stillroom app.")
        token, device_id, bundle_id = (body[key] for key in ("reviewToken", "deviceId", "bundleId"))
        if (not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token)
                or not isinstance(device_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,100}", device_id)
                or not isinstance(body["requestId"], str) or not re.fullmatch(r"[0-9a-f]{32}", body["requestId"])
                or bundle_id not in APPROVED_BUNDLE_IDS):
            raise LibraryError("Choose a valid connected iPhone and the intended Pocket Stillroom app.")
        with self._lock:
            self._ready()
            fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, allow_nan=False).encode()).hexdigest()
            existing = self._requests.get(body["requestId"])
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise LibraryError("This export request was already used for another selection.", 409)
                if existing["id"] in self._jobs:
                    return self.get(existing["id"])
                raise LibraryError("This request already finished in this app session. Check the received files on iPhone before starting a new export.", 409)
            if len(self._requests) >= 1024:
                raise LibraryError("This app session has reached its export request limit. Finish current work and restart Stillroom.", 409)
            if self.busy:
                raise LibraryError("Finish, cancel or check the unresolved iPhone export first.", 409)
            review = self._reviews.get(token)
            if not review or time.monotonic() - review[0] >= REVIEW_LIFETIME or review[1] != body["selection"]:
                raise LibraryError("This selection review expired or changed. Review it again before exporting.", 409)
            identifier = uuid.uuid4().hex
            cancel = threading.Event()
            job = {"id": identifier, "requestId": body["requestId"], "_fingerprint": fingerprint,
                   "state": "preparing", "phase": "preparing", "completedBytes": 0,
                   "totalBytes": 0, "message": "Checking the reviewed selection…", "_cancel": cancel}
            self._jobs[identifier] = job
            self._requests[body["requestId"]] = {"id": identifier, "fingerprint": fingerprint}
            for key in list(self._jobs):
                if len(self._jobs) <= MAX_HISTORY:
                    break
                if self._jobs[key]["state"] in TERMINAL:
                    del self._jobs[key]
            thread = threading.Thread(target=self._run, args=(identifier, copy.deepcopy(body)), daemon=True,
                                      name="stillroom-pocket-export")
            job["_thread"] = thread
            try:
                self._save_ledger(required=True)
            except LibraryError:
                del self._jobs[identifier]; del self._requests[body["requestId"]]
                raise
            thread.start()
            return self.get(identifier)

    def _update(self, identifier, *, persist=False, required=False, **values):
        with self._lock:
            if values.get("state") in TERMINAL:
                values.setdefault("phase", values["state"])
            self._jobs[identifier].update(values)
            if persist:
                self._save_ledger(required=required)

    def _run(self, identifier, request):
        cancel = self._jobs[identifier]["_cancel"]
        def progress(event):
            phase = event.get("phase", "preparing")
            state = "verifying" if phase == "verifying" else "transferring" if phase in {"transferring", "writing", "finalizing"} else "preparing"
            values = {key: event[key] for key in ("completedBytes", "totalBytes") if key in event}
            if event.get("receipt"):
                values["transfer"] = copy.deepcopy(event["receipt"])
            self._update(identifier, state=state, phase=phase, persist=bool(event.get("receipt")),
                         required=bool(event.get("receipt")), **values)
        try:
            with tempfile.TemporaryDirectory(prefix="stillroom-pocket-export-") as directory:
                output = Path(directory).resolve() / "library.stillroomlibrary"
                self.exporter.export(output, selection=request["selection"], expected_review_token=request["reviewToken"],
                                     cancelled=cancel.is_set, progress=progress)
                if cancel.is_set():
                    self._update(identifier, state="cancelled", message="Export cancelled before transfer.", persist=True)
                    return
                bundle_size = output.stat().st_size
                self._update(identifier, state="transferring", phase="transferring", completedBytes=0,
                             totalBytes=bundle_size, message="Copying to the chosen iPhone…")
                result = self.transport.send(output, request["deviceId"], request["bundleId"], cancelled=cancel.is_set,
                                             progress=progress)
                # Transfer publication is the commit boundary: a late Cancel
                # must not falsely report that an already verified file vanished.
                self._update(identifier, state="complete", phase="complete", result=result,
                             completedBytes=bundle_size, totalBytes=bundle_size,
                             message="Verified on iPhone. Open Pocket Stillroom, tap +, then choose the received library.", persist=True)
        except PocketTransferUncertain as exc:
            self._update(identifier, state="uncertain", message=str(exc), code=exc.code,
                         transfer=copy.deepcopy(exc.details), persist=True)
        except (ExportError, PocketDeviceError) as exc:
            if getattr(exc, "code", "") == "cleanup_pending":
                self._update(identifier, state="uncertain", message=str(exc), code=exc.code,
                             transfer=copy.deepcopy(exc.details), persist=True)
                return
            is_cancel = (isinstance(exc, ExportError) and cancel.is_set()) or getattr(exc, "code", "") == "cancelled"
            self._update(identifier, state="cancelled" if is_cancel else "failed", message=str(exc),
                         code=getattr(exc, "code", "export_failed"), persist=True)
        except Exception:
            # Do not leak library paths, device credentials or native exceptions
            # into browser responses or diagnostic logs.
            current = self.get(identifier)
            if current["state"] == "complete":
                # An acknowledged, published phone copy stays successful even
                # if local temporary-directory teardown reports an error.
                return
            receipt = current.get("transfer")
            self._update(identifier, state="uncertain" if receipt else "failed",
                         message="The export ended unexpectedly. Reconnect and unlock the iPhone, then check the transfer before trying again." if receipt else "The export could not finish safely. Review the selection again.",
                         code="export_uncertain" if receipt else "export_failed", persist=True)

    def get(self, identifier):
        with self._lock:
            if identifier not in self._jobs:
                raise LibraryError("iPhone export not found. It may belong to an earlier app session.", 404)
            return copy.deepcopy({key: value for key, value in self._jobs[identifier].items() if not key.startswith("_")})

    def jobs(self):
        with self._lock:
            return [self.get(identifier) for identifier in self._jobs]

    def cancel(self, identifier):
        with self._lock:
            job = self.get(identifier)
            if job["state"] not in TERMINAL:
                self._jobs[identifier]["_cancel"].set()
                self._jobs[identifier]["message"] = "Cancelling and removing this transfer’s incomplete file…"
            return self.get(identifier)

    def check_transfer(self, identifier):
        with self._lock:
            job = self.get(identifier)
            if job["state"] != "uncertain" or not job.get("transfer"):
                raise LibraryError("Only an uncertain transfer needs a connection check.", 409)
            receipt = job["transfer"]
        try:
            result = self.transport.inspect(receipt)
        except PocketDeviceError as exc:
            self._update(identifier, message=str(exc))
            return self.get(identifier)
        status = result.get("status")
        if status == "copied":
            self._update(identifier, state="complete", phase="complete", result=result,
                         message="The received file is complete and verified. Open Pocket Stillroom and tap + to import it.", persist=True)
        elif status in {"still_partial", "not_found"}:
            self._update(identifier, state="failed", phase="failed", result=result,
                         message="No complete file from this transfer was found. Review the selection before starting a new export. Any incomplete hidden transfer is not offered for import.", persist=True)
        else:
            self._update(identifier, message="The previous transfer could not be verified. Its files were left untouched. Check the iPhone before retrying.")
        return self.get(identifier)

    def close(self):
        with self._lock:
            self._closed = True
            threads = []
            for job in self._jobs.values():
                job["_cancel"].set()
                if job.get("_thread"):
                    threads.append(job["_thread"])
        deadline = time.monotonic() + 35
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
