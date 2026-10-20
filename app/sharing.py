"""Private, bounded exports made from newly encoded audio only.

No original file is served here. A stable, private snapshot lets deletion and
editing proceed while ffmpeg works, and each response owns its temporary files.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata

from .import_sources import normalize_youtube_url
from .library import LibraryError, executable
from .runtime import stop_process_tree, subprocess_options

MAX_SHARE_BYTES = 512 * 1024 * 1024
MAX_EXPORT_SECONDS = 6 * 60 * 60
PROBE_TIMEOUT = 30
ENCODE_TIMEOUT = 15 * 60
SNAPSHOT_TIMEOUT = 30
INPUT_FORMATS = {".mp3": "mp3", ".m4a": "mov", ".aac": "aac", ".wav": "wav", ".flac": "flac",
                 ".opus": "ogg", ".ogg": "ogg", ".webm": "matroska"}
GENERIC_FILENAME = "stillroom-audio.m4a"
_TRACK_ID = re.compile(r"[0-9a-f]{32}\Z")
_REVISION = re.compile(r"[0-9a-f]{64}\Z")
_DEVICE_NAME = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


def _display(value):
    if not isinstance(value, str):
        return ""
    return " ".join("".join(char if ord(char) >= 32 and ord(char) != 127 else " "
                            for char in value).split())[:300]


def download_filename(title, artist="", kind="original", *, include_info=True):
    """Return a short ASCII attachment name, also safe on Windows."""
    if not include_info:
        return GENERIC_FILENAME
    label = " - ".join(part for part in (_display(artist), _display(title)) if part)
    label = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode("ascii")
    label = re.sub(r"[^A-Za-z0-9 ._-]+", "-", label)
    label = re.sub(r"\s+", " ", label).strip(" .-_")[:100].rstrip(" .")
    if not label:
        label = "Stillroom audio"
    if _DEVICE_NAME.match(label):
        label = "Stillroom - " + label
    if kind == "loop":
        label += " - loop"
    return label + ".m4a"


def _selection(row, duration=None):
    """Use loop bounds only for loops; original songs always export in full."""
    try:
        length = row["file_duration"] if duration is None else duration
        if isinstance(length, bool) or not isinstance(length, (int, float)):
            raise ValueError()
        length = float(length)
        if not math.isfinite(length) or not 0 < length <= MAX_EXPORT_SECONDS:
            raise ValueError()
        if row["kind"] == "original":
            return 0.0, length
        if row["kind"] != "loop":
            raise ValueError()
        start = row["loop_start"]
        end = row["loop_end"]
        if (isinstance(start, bool) or not isinstance(start, (int, float)) or
                (end is not None and (isinstance(end, bool) or not isinstance(end, (int, float))))):
            raise ValueError()
        start = float(start)
        end = length if end is None else float(end)
        if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end <= length:
            raise ValueError()
        return start, end
    except (KeyError, TypeError, ValueError, OverflowError):
        raise LibraryError("This song has invalid export bounds. Choose another song.", 409) from None


@dataclass(frozen=True)
class Export:
    path: Path
    filename: str
    duration: float


class Sharing:
    def __init__(self, library, mutation_lock):
        self.library = library
        self.mutation_lock = mutation_lock
        self._slots = threading.BoundedSemaphore(1)
        self._closed = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._state_lock = threading.Lock()

    @property
    def busy(self):
        return not self._idle.is_set()

    @property
    def closed(self):
        return self._closed.is_set()

    def close(self):
        with self._state_lock:
            self._closed.set()
        # Encoders observe cancellation every 100 ms. HTTP writes already have
        # a 30-second socket timeout; leave time for their context to clean up.
        self._idle.wait(35)

    def _check_open(self):
        if self.closed:
            raise LibraryError("Song sharing is closing. Restart Stillroom and try again.", 503)

    @staticmethod
    def _revision(row, source_stat):
        signature = {"song": row, "audio": [source_stat.st_dev, source_stat.st_ino,
                     source_stat.st_size, source_stat.st_mtime_ns]}
        return hashlib.sha256(json.dumps(signature, sort_keys=True, ensure_ascii=False,
                                         allow_nan=False).encode("utf-8")).hexdigest()

    def _row(self, track_id):
        if not isinstance(track_id, str) or not _TRACK_ID.fullmatch(track_id):
            raise LibraryError("Song not found.", 404)
        with self.library.connect() as conn:
            row = conn.execute("""SELECT id, title, artist, album, kind, file_duration,
                loop_start, loop_end, source_url, audio_filename FROM tracks WHERE id=?""", (track_id,)).fetchone()
        if row is None:
            raise LibraryError("Song not found. It may already have been deleted.", 404)
        filename = row["audio_filename"]
        if not isinstance(filename, str) or not re.fullmatch(re.escape(track_id) + r"\.(?:mp3|m4a|aac|wav|flac|opus|ogg|webm)", filename):
            raise LibraryError("This song's library audio is unavailable.", 409)
        _selection(row)
        return dict(row)

    @contextlib.contextmanager
    def _source(self, row):
        """Reject symlinks and copy only from a checked, regular opened handle."""
        audio = self.library.audio
        source = audio / row["audio_filename"]
        try:
            if audio.is_symlink() or audio.resolve() != self.library.root / "audio":
                raise LibraryError("This song's library audio is unavailable.", 409)
            before = source.lstat()
            if not stat.S_ISREG(before.st_mode):
                raise LibraryError("This song's library audio is unavailable.", 409)
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(source, flags)
            with os.fdopen(descriptor, "rb") as handle:
                opened = os.fstat(handle.fileno())
                current = source.lstat()
                if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode) or
                        (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or
                        (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino) or audio.is_symlink()):
                    raise LibraryError("This song's library audio is unavailable.", 409)
                if not opened.st_size:
                    raise LibraryError("This song's library audio is unavailable.", 409)
                if opened.st_size > MAX_SHARE_BYTES:
                    raise LibraryError("Audio sharing is limited to 512 MB.", 413)
                yield handle
        except OSError:
            raise LibraryError("This song's library audio is unavailable.", 409) from None

    def preview(self, track_id):
        self._check_open()
        with self.mutation_lock:
            row = self._row(track_id)
            with self._source(row) as source:
                revision = self._revision(row, os.fstat(source.fileno()))
            start, end = _selection(row)
        try:
            source_url = normalize_youtube_url(row["source_url"])
        except LibraryError:
            source_url = ""
        title, artist, album = (_display(row[field]) for field in ("title", "artist", "album"))
        return {"title": title, "artist": artist, "album": album, "sourceUrl": source_url,
                "kind": row["kind"], "exportDuration": end - start,
                "filename": download_filename(title, artist, row["kind"]), "revision": revision}

    def _snapshot(self, row, destination, revision):
        deadline = time.monotonic() + SNAPSHOT_TIMEOUT
        with self._source(row) as source, destination.open("xb") as output:
            before = os.fstat(source.fileno())
            if not hmac.compare_digest(revision, self._revision(row, before)):
                raise LibraryError("This song changed. Reopen sharing to review its current details.", 409)
            os.chmod(destination, 0o600)
            size = 0
            while True:
                self._check_open()
                if time.monotonic() > deadline:
                    raise LibraryError("Copying this song took too long. Try again.", 503)
                chunk = source.read(256 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_SHARE_BYTES:
                    raise LibraryError("Audio sharing is limited to 512 MB.", 413)
                output.write(chunk)
            after = os.fstat(source.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise LibraryError("This song changed. Reopen sharing to review its current details.", 409)

    def _run(self, arguments, *, timeout, output=None, capture=False):
        try:
            process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL,
                                       **subprocess_options(new_process_group=True))
        except OSError:
            raise LibraryError("The audio tools could not be started. Try again.", 503) from None
        deadline = time.monotonic() + timeout
        try:
            while True:
                self._check_open()
                if output is not None and output.exists() and output.stat().st_size > MAX_SHARE_BYTES:
                    raise LibraryError("The shared audio exceeds the 512 MB limit. Choose a shorter selection.", 413)
                if time.monotonic() > deadline:
                    raise LibraryError("Preparing this song took too long. Try a shorter selection.", 503)
                try:
                    result, _ = process.communicate(timeout=.1)
                    break
                except subprocess.TimeoutExpired:
                    pass
            if process.returncode:
                raise LibraryError("This song could not be prepared for sharing. Try another song.")
            if capture and len(result) > 64 * 1024:
                raise LibraryError("This song could not be inspected for sharing.")
            if output is not None and (not output.is_file() or not output.stat().st_size or output.stat().st_size > MAX_SHARE_BYTES):
                raise LibraryError("The shared audio exceeds its file limit or is empty.", 413)
            return result
        finally:
            if process.poll() is None:
                stop_process_tree(process)
                process.communicate()

    @staticmethod
    def _input_arguments(source):
        demuxer = INPUT_FORMATS[source.suffix]
        options = ["-protocol_whitelist", "file", "-format_whitelist", demuxer,
                   "-f", demuxer, "-probesize", "5000000", "-analyzeduration", "10000000"]
        if demuxer == "mov":
            options += ["-enable_drefs", "0", "-use_absolute_path", "0"]
        return options

    def _duration(self, source):
        raw = self._run([executable("ffprobe"), "-v", "error", *self._input_arguments(source),
                         "-select_streams", "a:0", "-show_entries", "stream=codec_type,duration:format=duration",
                         "-of", "json", str(source)], timeout=PROBE_TIMEOUT, capture=True)
        try:
            details = json.loads(raw)
            streams = details.get("streams", [])
            if len(streams) != 1 or streams[0].get("codec_type") != "audio":
                raise ValueError()
            duration = float(streams[0].get("duration") or details.get("format", {}).get("duration", 0))
            if not math.isfinite(duration) or not 0 < duration <= MAX_EXPORT_SECONDS:
                raise ValueError()
            return duration
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise LibraryError("This song has no valid audio duration for sharing.") from None

    @contextlib.contextmanager
    def export(self, track_id, include_info, revision, *, check_ready=None):
        if type(include_info) is not bool:
            raise LibraryError("includeInfo must be true or false.")
        if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
            raise LibraryError("Reopen sharing to review this song before exporting it.")
        with self._state_lock:
            self._check_open()
            if not self._slots.acquire(blocking=False):
                raise LibraryError("Another song is being shared. Wait for its download to finish and try again.", 409)
            self._idle.clear()
        prepared = False
        try:
            with tempfile.TemporaryDirectory(prefix="stillroom-share-") as directory:
                work = Path(directory)
                os.chmod(work, 0o700)
                with self.mutation_lock:
                    if check_ready is not None:
                        check_ready()
                    row = self._row(track_id)
                    source = work / ("source" + Path(row["audio_filename"]).suffix)
                    self._snapshot(row, source, revision)
                duration = self._duration(source)
                start, end = _selection(row, duration)
                # Compressed-file duration estimates can undershoot EOF. A
                # whole-file selection must decode to EOF, with a hard safety
                # ceiling, rather than trimming to that estimate.
                encode_duration = (MAX_EXPORT_SECONDS - start if row["kind"] == "original" or row["loop_end"] is None
                                   else end - start)
                output = work / "audio.m4a"
                command = [executable("ffmpeg"), "-v", "error", "-nostdin", "-y",
                           *self._input_arguments(source), "-i", str(source),
                           "-ss", f"{start:.9f}", "-t", f"{encode_duration:.9f}",
                           "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1",
                           "-map_metadata:s:a:0", "-1", "-map_chapters", "-1",
                           "-c:a", "aac", "-b:a", "256k", "-threads", "1",
                           "-fflags", "+bitexact", "-flags:a", "+bitexact",
                           "-metadata", "encoder=", "-metadata:s:a:0", "encoder=",
                           "-metadata:s:a:0", "language=", "-metadata:s:a:0", "handler_name=",
                           "-disposition:a:0", "0"]
                if include_info:
                    for field in ("title", "artist", "album"):
                        value = _display(row[field])
                        if value:
                            command += ["-metadata", field + "=" + value]
                command += ["-movflags", "+faststart+disable_chpl", "-fs", str(MAX_SHARE_BYTES + 1), "-f", "ipod", str(output)]
                self._run(command, timeout=ENCODE_TIMEOUT, output=output)
                os.chmod(output, 0o600)
                prepared = True
                yield Export(output, download_filename(row["title"], row["artist"], row["kind"], include_info=include_info), end - start)
        except OSError:
            if prepared:
                # Response disconnects must reach Handler's connection-error
                # branch after this context has cleaned its private files.
                raise
            raise LibraryError("This song could not be prepared for sharing. Try again.", 503) from None
        finally:
            with self._state_lock:
                self._slots.release()
                self._idle.set()
