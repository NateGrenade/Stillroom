"""Acquire a single YouTube recording and read best-effort audio metadata.

This module only stages files; importing and audio processing belong to the caller.
Account sessions require an explicit caller-provided opt-in. User yt-dlp
configuration and plugins are always disabled.
"""

from __future__ import annotations

import contextlib
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

from .library import LibraryError, MAX_AUDIO_BYTES, executable, local_audio_input_options
from .runtime import find_executable, stop_process_tree, subprocess_options


APP_ROOT = Path(__file__).resolve().parents[1]
MAX_VIDEO_SECONDS = 6 * 60 * 60
METADATA_TIMEOUT = 120
DOWNLOAD_TIMEOUT = 20 * 60
MAX_TOOL_OUTPUT = 8 * 1024 * 1024
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}


def normalize_youtube_url(url: str) -> str:
    """Return one canonical watch URL, removing playlist and timestamp parameters."""
    error = "Paste a YouTube video link (youtube.com/watch?v=… or youtu.be/…)."
    if not isinstance(url, str) or not url.strip() or len(url) > 2048:
        raise LibraryError(error)
    url = url.strip()
    if any(ord(char) < 33 or ord(char) == 127 for char in url) or "\\" in url:
        raise LibraryError(error)
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("https", "http") or parsed.username or parsed.password:
            raise ValueError()
        if parsed.port not in (None, 443 if parsed.scheme == "https" else 80):
            raise ValueError()
        host = (parsed.hostname or "").lower()
        if host in ("youtu.be", "www.youtu.be"):
            match = re.fullmatch(r"/([A-Za-z0-9_-]{11})/?", parsed.path)
            video_id = match[1] if match else ""
        elif host in YOUTUBE_HOSTS:
            if parsed.path in ("/watch", "/watch/"):
                values = parse_qs(parsed.query, keep_blank_values=True).get("v", [])
                video_id = values[0] if len(values) == 1 else ""
            else:
                match = re.fullmatch(r"/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})/?", parsed.path)
                video_id = match[1] if match else ""
        else:
            raise ValueError()
        if not VIDEO_ID.fullmatch(video_id):
            raise ValueError()
    except (ValueError, TypeError):
        raise LibraryError(error) from None
    return "https://www.youtube.com/watch?v=" + video_id


def _text(value, limit=300):
    if not isinstance(value, str):
        return ""
    return " ".join("".join(c if ord(c) >= 32 and ord(c) != 127 else " " for c in value).split())[:limit].strip()


def _first_text(*values):
    return next((cleaned for value in values if (cleaned := _text(value))), "")


def _metadata(info, source_url):
    artist = _first_text(info.get("artist"), info.get("creator"))
    notes = []
    if not artist:
        artist = _first_text(info.get("channel"), info.get("uploader"))
        if artist:
            notes.append("YouTube did not identify a music artist; the channel is used as the creator. You can edit it.")
    return {
        "title": _first_text(info.get("title"), info.get("track")) or "YouTube recording",
        "artist": artist,
        "album": _text(info.get("album")),
        "sourceUrl": source_url,
    }, notes


def _downloader():
    found = find_executable("yt-dlp", root=APP_ROOT)
    if found:
        return found
    raise LibraryError("YouTube imports need yt-dlp. Install or restore the audio tools, then try again.", 503)


def _base_command(auth_options=None):
    command = [
        _downloader(), "--ignore-config", "--no-config-locations", "--no-plugin-dirs",
        "--no-cookies", "--no-cookies-from-browser", "--no-cache-dir",
        "--no-remote-components", "--no-js-runtimes", "--no-playlist",
        "--use-extractors", "youtube", "--no-progress", "--no-colors",
        "--no-update", "--socket-timeout", "20", "--retries", "2",
        "--fragment-retries", "2", "--extractor-retries", "2",
        "--file-access-retries", "1", "--abort-on-unavailable-fragments",
    ]
    if auth_options:
        command.remove("--no-cookies-from-browser" if auth_options[0] == "--cookies-from-browser" else "--no-cookies")
        command.extend(auth_options)
    try:
        node = executable("node")
    except LibraryError:
        raise LibraryError("YouTube imports need Node.js. Install or restore the audio tools, then try again.", 503) from None
    command.extend(["--js-runtimes", "node:" + node])
    command.extend(["--ffmpeg-location", str(Path(executable("ffmpeg")).parent)])
    return command


def _stop(process):
    # yt-dlp can spawn its JavaScript runtime. Stop the entire isolated process group.
    stop_process_tree(process)


def _directory_bytes(path):
    size = 0
    for entry in path.iterdir():
        with contextlib.suppress(FileNotFoundError):
            if entry.is_file() and not entry.is_symlink():
                size += entry.stat().st_size
    return size


def _run_tool(command, *, timeout, cwd=None, watch_directory=None, cancel_event=None):
    """Bound process time, captured output and staged bytes, including unknown sizes."""
    if cancel_event is not None and cancel_event.is_set():
        raise LibraryError("Preparation cancelled.")
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                cwd=cwd, **subprocess_options(new_process_group=True),
            )
        except OSError:
            raise LibraryError("The audio tools could not start. Install or restore them, then try again.", 503) from None
        deadline = time.monotonic() + timeout
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise LibraryError("Preparation cancelled.")
                code = process.poll()
                if stdout.tell() + stderr.tell() > MAX_TOOL_OUTPUT:
                    raise LibraryError("YouTube returned too much information for this video. Try a different video.", 502)
                if watch_directory is not None and _directory_bytes(watch_directory) > MAX_AUDIO_BYTES:
                    raise LibraryError("This recording exceeds the 512 MB import limit. Try a shorter video.", 413)
                if code is not None:
                    break
                if time.monotonic() >= deadline:
                    raise LibraryError("YouTube took too long to respond. Check your connection and try again.", 504)
                time.sleep(0.1)
        finally:
            _stop(process)
        stdout.seek(0)
        stderr.seek(0)
        return subprocess.CompletedProcess(command, code, stdout.read().decode("utf-8", "replace"), stderr.read().decode("utf-8", "replace"))


def _failure(stderr, authenticated=False):
    # Do not expose signed CDN URLs or terminal control sequences from third-party errors.
    detail = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", stderr or "")
    lines = [line.strip() for line in detail.splitlines() if line.strip()]
    detail = next((line for line in reversed(lines) if "ERROR:" in line), lines[-1] if lines else "")
    detail = re.sub(r"https?://\S+", "[source]", detail)
    detail = _text(detail, 500)
    lower = detail.lower()
    if "not a bot" in lower:
        message = ("YouTube still asked the selected account session to confirm it is not a bot. Check the session in your browser or retry later; account access may not resolve every challenge." if authenticated else "YouTube asked the downloader to confirm it is not a bot. Try an account session for this import or retry later; account access may not resolve every challenge.")
    elif any(term in lower for term in ("sign in", "sign-in", "login", "private video", "age-restricted", "members-only", "premium")):
        message = ("YouTube did not grant this account access to the video. Check access in your selected browser or refresh the uploaded cookies." if authenticated else "YouTube requires sign-in or access for this video. Enable Use YouTube account for this import, use a public video or upload an audio file.")
    elif any(term in lower for term in ("cookie", "keychain", "decrypt", "browser database")) and authenticated:
        message = "The selected YouTube session could not be read or has expired. Sign in to YouTube in that browser, allow its access prompt if shown, then retry. You can also upload a fresh cookies.txt export."
    elif any(term in lower for term in ("not available", "unavailable", "has been removed", "copyright", "video has been deleted")):
        message = "This YouTube video is unavailable. Try another link or upload an audio file."
    elif any(term in lower for term in ("requested format", "signature", "challenge", "http error 403")):
        message = "YouTube could not provide the audio. The downloader may need an update; try again or upload an audio file."
    else:
        message = "The YouTube audio could not be downloaded. Check the link and your connection, then try again."
    # Authenticated tools may mention cookie values or local account paths.
    # Never forward any raw tool details for an authenticated request.
    return LibraryError(message + (" Details: " + detail if detail and not authenticated else ""), 502)


def acquire_youtube(url: str, staging: Path, progress=None, cancel_event=None, youtube_session=None) -> dict:
    """Acquire with the explicitly selected session, if any, for both tool calls."""
    context = youtube_session.command_options() if youtube_session is not None else contextlib.nullcontext(None)
    with context as auth_options:
        return _acquire_youtube(url, staging, progress, cancel_event, auth_options)


def _acquire_youtube(url: str, staging: Path, progress=None, cancel_event=None, auth_options=None) -> dict:
    """Stage one video, returning ``file``, ``metadata``, and best-effort ``notes``.

    The optional callback receives ``(stage, message)``. URL time offsets are
    intentionally discarded: the caller's timestamp fields control trimming.
    ``staging`` must be a fresh, private job directory managed by the caller.
    Setting the optional threading Event cancels active tools and their children.
    """
    source_url = normalize_youtube_url(url)
    staging = Path(staging).resolve()
    staging.mkdir(parents=True, exist_ok=True)
    if any(staging.iterdir()):
        raise LibraryError("The YouTube import needs an empty staging folder.", 500)
    command = _base_command(auth_options) if auth_options else _base_command()
    if progress:
        progress("reading", "Reading YouTube video information…")
    result = _run_tool(command + ["--skip-download", "--dump-single-json", "--", source_url], timeout=METADATA_TIMEOUT, cwd=staging, cancel_event=cancel_event)
    if result.returncode:
        raise _failure(result.stderr, authenticated=bool(auth_options))
    try:
        info = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise LibraryError("YouTube returned unreadable video information. Try again or update the downloader.", 502) from None
    expected_id = source_url.rsplit("=", 1)[-1]
    if not isinstance(info, dict) or info.get("id") != expected_id or info.get("_type", "video") != "video" or "entries" in info:
        raise LibraryError("YouTube did not return the requested single video.", 502)
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        raise LibraryError("Live, upcoming, and still-processing streams cannot be imported. Try a finished video.")
    duration = info.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise LibraryError("YouTube did not provide a finished recording with a known duration. Try another video.")
    if duration > MAX_VIDEO_SECONDS:
        raise LibraryError("YouTube imports support videos up to 6 hours. Try a shorter video.")
    metadata, notes = _metadata(info, source_url)
    if progress:
        progress("downloading", "Downloading the recording…")
    download = _run_tool(command + [
        "--no-simulate", "--format", "bestaudio/best", "--downloader", "native",
        "--max-filesize", str(MAX_AUDIO_BYTES),
        "--match-filters", "!is_live & duration <= " + str(MAX_VIDEO_SECONDS),
        "--restrict-filenames", "--output", str(staging / "source.%(ext)s"),
        "--print", "after_move:filepath", "--", source_url,
    ], timeout=DOWNLOAD_TIMEOUT, cwd=staging, watch_directory=staging, cancel_event=cancel_event)
    if download.returncode:
        raise _failure(download.stderr, authenticated=bool(auth_options))
    candidates = [p for p in staging.glob("source.*") if p.is_file() and not p.is_symlink() and p.suffix not in (".part", ".ytdl", ".json", ".tmp")]
    if len(candidates) != 1:
        download_message = download.stderr + "\n" + download.stdout
        if "max-filesize" in download_message or "larger than" in download_message:
            raise LibraryError("This recording exceeds the 512 MB import limit. Try a shorter video.", 413)
        raise LibraryError("YouTube did not produce a complete audio file. Try again or upload an audio file.", 502)
    path = candidates[0]
    if path.stat().st_size <= 0:
        raise LibraryError("YouTube returned an empty audio file. Try another video.", 502)
    if path.stat().st_size > MAX_AUDIO_BYTES:
        raise LibraryError("This recording exceeds the 512 MB import limit. Try a shorter video.", 413)
    return {"file": str(path), "metadata": metadata, "notes": notes}


def local_metadata(path, filename=None, cancel_event=None) -> dict:
    """Use embedded tags and then the supplied display filename; never guess artists."""
    path = Path(path)
    fallback = _text(Path(str(filename or path.name)).stem) or "Untitled recording"
    metadata = {"title": fallback, "artist": "", "album": "", "sourceUrl": ""}
    try:
        result = _run_tool([
            executable("ffprobe"), "-v", "error", *local_audio_input_options(for_probe=True), "-show_entries",
            "format_tags=title,artist,album_artist,composer,album:stream_tags=title,artist,album_artist,composer,album",
            "-of", "json", str(path.resolve()),
        ], timeout=30, cancel_event=cancel_event)
        info = json.loads(result.stdout) if result.returncode == 0 else {}
        if not isinstance(info, dict):
            return metadata
        tags = {}
        for stream in info.get("streams", []):
            if isinstance(stream, dict) and isinstance(stream.get("tags"), dict):
                tags.update({key.lower(): value for key, value in stream["tags"].items()})
        file_format = info.get("format", {})
        if isinstance(file_format, dict) and isinstance(file_format.get("tags"), dict):
            tags.update({key.lower(): value for key, value in file_format["tags"].items()})
        metadata.update({
            "title": _text(tags.get("title")) or fallback,
            "artist": _first_text(tags.get("artist"), tags.get("album_artist"), tags.get("composer")),
            "album": _text(tags.get("album")),
        })
    except (LibraryError, ValueError, TypeError):
        if cancel_event is not None and cancel_event.is_set():
            raise LibraryError("Preparation cancelled.") from None
        pass  # Metadata is optional; the actual import independently verifies audio.
    return metadata
