"""Bounded, temporary preparation jobs; only an explicit commit saves a track."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import uuid

from .library import LibraryError, MAX_AUDIO_BYTES, clean_text, metadata_values, probe
from .import_sources import acquire_youtube, local_metadata, normalize_youtube_url
from .audio_prepare import prepare_audio, parse_timestamp
from .youtube_auth import YouTubeAuth, YouTubeSession


def process_exists(pid):
    """Conservatively check an owner without signalling it on Windows."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # Invalid parameter means the PID no longer exists. Access denial
            # and other errors leave the session intact.
            return ctypes.get_last_error() != 87
        try:
            code = wintypes.DWORD()
            return not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def metadata_options(options):
    mode = options.get("metadataMode", "auto")
    if mode not in ("auto", "manual"):
        raise LibraryError("Choose Auto or Manual song details.")
    metadata = options.get("metadata", {})
    if not isinstance(metadata, dict):
        raise LibraryError("Song details must be an object.")
    result = {}
    for key in ("title", "artist", "album"):
        if key in metadata:
            result[key] = clean_text(metadata[key], key)
    if "tags" in metadata:
        tags = metadata["tags"]
        if not isinstance(tags, list) or len(tags) > 32:
            raise LibraryError("Use at most 32 tags.")
        result["tags"] = [clean_text(tag, "tag", 100, required=True) for tag in tags]
    return mode, result


def merge_metadata(detected, options):
    mode, provided = metadata_options(options)
    result = dict(detected) if mode == "auto" else {
        "title": detected.get("title", "Untitled"), "artist": "", "album": "",
        "sourceUrl": detected.get("sourceUrl", ""),
    }
    for key, value in provided.items():
        if value or mode == "manual":
            result[key] = value
    if not result.get("title"):
        result["title"] = detected.get("title") or "Untitled"
    return result


def range_time(seconds):
    seconds = round(seconds, 3)
    minutes, seconds = divmod(seconds, 60)
    return f"{int(minutes)}:{seconds:06.3f}".rstrip("0").rstrip(".")


class ImportJobs:
    """Two active preparations, eight previews, with one-hour transient storage."""

    def __init__(self, library, youtube_auth=None):
        self.library = library
        self.youtube_auth = youtube_auth or YouTubeAuth(library.root)
        staging = library.root / "imports"
        staging.mkdir(exist_ok=True)
        # Reclaim only directories positively identified as our stopped sessions.
        for previous in staging.glob(".preview-*"):
            if previous.is_symlink() or not previous.is_dir():
                continue
            try:
                owner = json.loads((previous / "owner.json").read_text(encoding="utf-8"))
                if not isinstance(owner, dict) or owner.get("app") != "stillroom-import-preview" or not isinstance(owner.get("pid"), int) or owner["pid"] <= 1:
                    continue
                if not process_exists(owner["pid"]):
                    shutil.rmtree(previous)
            except (OSError, ValueError, TypeError):
                continue
        self.session = Path(tempfile.mkdtemp(prefix=".preview-", dir=staging))
        (self.session / "owner.json").write_text(json.dumps({"app": "stillroom-import-preview", "pid": os.getpid()}), encoding="utf-8")
        self.jobs = {}
        self.lock = threading.RLock()
        self.closed = False

    def _cleanup_expired(self):
        now = time.monotonic()
        for identifier, job in list(self.jobs.items()):
            if job["status"] not in ("queued", "working", "saving") and now - job["_touched"] > 3600:
                shutil.rmtree(job["_dir"], ignore_errors=True)
                del self.jobs[identifier]

    def _get(self, identifier):
        job = self.jobs.get(identifier)
        if job is None:
            raise LibraryError("This preview has expired. Prepare the track again.", 404)
        return job

    @staticmethod
    def _public(job):
        return copy.deepcopy({key: value for key, value in job.items() if not key.startswith("_")})

    def get(self, identifier):
        with self.lock:
            self._cleanup_expired()
            job = self._get(identifier)
            job["_touched"] = time.monotonic()
            return self._public(job)

    def create(self, options, upload=None, filename=None, *, youtube_session=None):
        if not isinstance(options, dict):
            raise LibraryError("Import options must be an object.")
        options = copy.deepcopy(options)
        metadata_options(options)
        for key in ("exact", "loop"):
            if key in options and not isinstance(options[key], bool):
                raise LibraryError(f"{key} must be true or false.")
        # Reject malformed intervals before spending time downloading a video.
        start = parse_timestamp(options.get("start"), "Start")
        end = parse_timestamp(options.get("end"), "End")
        if start is not None and end is not None and end <= start:
            raise LibraryError("End must be after Start.")
        if upload is None:
            if options.get("source", "youtube") != "youtube":
                raise LibraryError("Choose a YouTube link or an audio file.")
            options["url"] = normalize_youtube_url(options.get("url", ""))
        if youtube_session is not None and not isinstance(youtube_session, YouTubeSession):
            raise LibraryError("Choose a valid YouTube account session.")
        if not isinstance(options.get("useYouTubeAccount", False), bool):
            raise LibraryError("Use YouTube account must be true or false.")
        if upload is None and youtube_session is None:
            youtube_session = self.youtube_auth.snapshot(options.get("useYouTubeAccount", False))
        with self.lock:
            if self.closed:
                raise LibraryError("The app is shutting down. Please reopen it.", 503)
            self._cleanup_expired()
            if sum(job.get("_worker") is not None and job["_worker"].is_alive() for job in self.jobs.values()) >= 2:
                raise LibraryError("Two tracks are already being prepared. Wait for one to finish.", 429)
            if sum(job["status"] == "ready" for job in self.jobs.values()) >= 8:
                raise LibraryError("Save or close an existing preview before preparing another.", 429)
            identifier = uuid.uuid4().hex
            directory = self.session / identifier
            directory.mkdir()
            job = {"id": identifier, "status": "queued", "stage": "queued", "message": "Preparing your track…",
                   "_dir": directory, "_touched": time.monotonic(), "_options": options, "_cancel": threading.Event(),
                   "_youtube_session": youtube_session}
            source = None
            if upload is not None:
                source = directory / "uploaded.audio"
                try:
                    shutil.copyfile(upload, source)
                except Exception:
                    shutil.rmtree(directory, ignore_errors=True)
                    raise
            self.jobs[identifier] = job
            worker = threading.Thread(target=self._run, args=(identifier, source, filename), daemon=True)
            job["_worker"] = worker
            worker.start()
            return self._public(job)

    def _progress(self, identifier, *values):
        with self.lock:
            job = self._get(identifier)
            if self.closed or job["status"] == "cancelled":
                raise LibraryError("Preparation cancelled.")
            job.update(status="working", stage=values[0] if len(values) > 1 else "preparing", message=values[-1])

    def _run(self, identifier, source, filename):
        job = self.jobs[identifier]
        directory, options = job["_dir"], job["_options"]
        try:
            progress = lambda *values: self._progress(identifier, *values)
            progress("reading", "Reading song details…")
            notes = []
            if source is None:
                auth = {"youtube_session": job["_youtube_session"]} if job.get("_youtube_session") is not None else {}
                acquired = acquire_youtube(options["url"], directory, progress=progress, cancel_event=job["_cancel"], **auth)
                source = Path(acquired["file"])
                detected = acquired["metadata"]
                if options.get("metadataMode", "auto") == "auto" and not options.get("metadata", {}).get("artist"):
                    notes.extend(acquired.get("notes", []))
            else:
                detected = local_metadata(source, filename=filename, cancel_event=job["_cancel"])
            metadata = merge_metadata(detected, options)
            prepared = prepare_audio(source, directory, options, progress=progress, cancel_event=job["_cancel"])
            progress("checking", "Checking the prepared audio…")
            output = Path(prepared["file"]).resolve()
            if not output.is_relative_to(directory.resolve()) or not output.is_file():
                raise LibraryError("The prepared audio could not be located.", 500)
            if output.stat().st_size > MAX_AUDIO_BYTES:
                raise LibraryError("The prepared audio exceeds the 512 MB import limit.", 413)
            duration, _ = probe(output)
            metadata.update(prepared["metadata"])
            analysis = prepared["analysis"]
            if (options.get("metadataMode", "auto") == "auto" and not options.get("metadata", {}).get("title")
                    and (analysis["requestedStart"] is not None or analysis["requestedEnd"] is not None)):
                suffix = f" ({range_time(analysis['appliedStart'])}–{range_time(analysis['appliedEnd'])})"
                metadata["title"] = metadata["title"][:300-len(suffix)] + suffix
            # Check all bounds and editable fields before offering a preview.
            metadata_values(metadata, filename or "YouTube audio", duration)
            analysis["notes"] = notes + analysis.get("notes", [])
            manifest = {"metadata": metadata, "options": options, "analysis": analysis}
            (directory / "preparation.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            with self.lock:
                if self.closed or job["status"] == "cancelled":
                    return
                job.update(status="ready", stage="ready", message="Ready to preview and save.", metadata=metadata,
                           analysis=analysis, duration=prepared["duration"], fileDuration=duration,
                           loopStart=metadata.get("loopStart", 0), loopEnd=metadata.get("loopEnd"),
                           audioUrl=f"/api/import-jobs/{identifier}/audio", _file=output, _touched=time.monotonic())
        except Exception as exc:
            with self.lock:
                if job["status"] != "cancelled":
                    message = str(exc) if isinstance(exc, LibraryError) else "This track could not be prepared. Try another file or link."
                    job.update(status="error", stage="error", message=message, error=message)
                    # Unexpected authenticated tool failures may carry account
                    # output; do not write those exception details to logs.
                    if not isinstance(exc, LibraryError) and job.get("_youtube_session") is None:
                        import traceback
                        traceback.print_exc()
        finally:
            if self.closed or job["status"] in ("cancelled", "error"):
                shutil.rmtree(directory, ignore_errors=True)

    def audio(self, identifier):
        with self.lock:
            job = self._get(identifier)
            if job["status"] != "ready":
                raise LibraryError("Prepare this track before playing its preview.", 409)
            job["_touched"] = time.monotonic()
            return job["_file"]

    def commit(self, identifier, changes):
        if not isinstance(changes, dict):
            raise LibraryError("Song details must be an object.")
        with self.lock:
            job = self._get(identifier)
            if job["status"] == "complete":
                return {"track": copy.deepcopy(job["track"]), "job": self._public(job)}
            if job["status"] != "ready":
                raise LibraryError("Wait for the preview to finish before saving.", 409)
            _, provided = metadata_options({"metadataMode": "manual", "metadata": changes.get("metadata", {})})
            metadata = {**job["metadata"], **provided}
            # Validate before changing state so corrections can be made in-place.
            metadata_values(metadata, "Untitled", job["fileDuration"])
            job["status"] = "saving"
        try:
            track = self.library.import_file(job["_file"], metadata)
        except Exception:
            with self.lock:
                job["status"] = "ready"
            raise
        with self.lock:
            job.update(status="complete", stage="complete", message="Added to your library.", track=track, metadata=metadata)
            # Keep the idempotency result, discard all transient audio.
            shutil.rmtree(job["_dir"], ignore_errors=True)
            return {"track": track, "job": self._public(job)}

    def discard(self, identifier):
        with self.lock:
            job = self._get(identifier)
            if job["status"] == "saving":
                raise LibraryError("This track is being saved. Please wait.", 409)
            active = job["status"] in ("queued", "working")
            if job["status"] != "complete":
                job.update(status="cancelled", stage="cancelled", message="Preview closed.")
                job["_cancel"].set()
            if not active:
                shutil.rmtree(job["_dir"], ignore_errors=True)
            return {"success": True}

    def close(self):
        with self.lock:
            self.closed = True
            workers = []
            for job in self.jobs.values():
                job["_cancel"].set()
                if job.get("_worker"):
                    workers.append(job["_worker"])
                if job["status"] not in ("working", "queued", "saving"):
                    shutil.rmtree(job["_dir"], ignore_errors=True)
        deadline = time.monotonic() + 3
        for worker in workers:
            worker.join(max(0, deadline - time.monotonic()))
        if not any(worker.is_alive() for worker in workers):
            shutil.rmtree(self.session, ignore_errors=True)
