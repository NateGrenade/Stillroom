"""Read a bounded YouTube playlist without fetching any media.

The downloader only expands the playlist, never its videos. Returned video
links are rebuilt from validated IDs; supplied extractor links are not trusted.
Account sessions require an explicit caller-provided opt-in; personal
configuration and plugins are always disabled.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
from urllib.parse import parse_qs, urlsplit

from . import import_sources
from .library import LibraryError


MAX_TRACKS = 100
READ_TIMEOUT = 120
PLAYLIST_ID = re.compile(r"[A-Za-z0-9_-]{10,150}\Z")
URL_ERROR = "Paste a YouTube playlist link with a list=… parameter."


def normalize_youtube_playlist_url(url):
    """Accept playlist, watch-with-list, and short video-with-list links."""
    if not isinstance(url, str) or not url.strip() or len(url) > 2048:
        raise LibraryError(URL_ERROR)
    url = url.strip()
    if any(ord(char) < 33 or ord(char) == 127 for char in url) or "\\" in url:
        raise LibraryError(URL_ERROR)
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("https", "http") or parsed.username or parsed.password:
            raise ValueError()
        if parsed.port not in (None, 443 if parsed.scheme == "https" else 80):
            raise ValueError()
        host = (parsed.hostname or "").lower()
        if host in import_sources.YOUTUBE_HOSTS:
            if parsed.path not in ("/playlist", "/playlist/", "/watch", "/watch/"):
                raise ValueError()
            if parsed.path.startswith("/watch"):
                import_sources.normalize_youtube_url(url)
        elif host in ("youtu.be", "www.youtu.be"):
            import_sources.normalize_youtube_url(url)
        else:
            raise ValueError()
        values = parse_qs(parsed.query, keep_blank_values=True).get("list", [])
        if len(values) != 1 or not PLAYLIST_ID.fullmatch(values[0]):
            raise ValueError()
    except (ValueError, TypeError, LibraryError):
        raise LibraryError(URL_ERROR) from None
    if values[0].startswith('RD'):
        raise LibraryError('YouTube Mixes and radio queues are not supported. Save the videos as a regular playlist, then import its link.')
    return "https://www.youtube.com/playlist?list=" + values[0]


def _track(entry, index, authenticated=False):
    """Keep unavailable positions in the review while never inventing a link."""
    entry = entry if isinstance(entry, dict) else {}
    video_id = entry.get("id")
    valid_id = isinstance(video_id, str) and import_sources.VIDEO_ID.fullmatch(video_id)
    title = import_sources._text(entry.get("title"))
    metadata, _ = import_sources._metadata(entry, "")
    track = {
        "id": video_id if valid_id else "",
        "title": title or f"Unavailable video {index + 1}",
        "artist": metadata["artist"], "album": metadata["album"],
        "duration": None, "youtubeUrl": "", "available": False,
    }
    unavailable = "This video is unavailable or private; it will be skipped."
    if (not valid_id or not title or title.casefold() in ("[private video]", "[deleted video]", "private video", "deleted video")
            or entry.get("availability") in ("needs_auth", "unavailable", "deleted")
            or entry.get("availability") in ("private", "premium_only", "subscriber_only") and not authenticated):
        track["message"] = unavailable
        return track
    if entry.get("_type", "url") not in ("url", "video") or entry.get("ie_key") not in (None, "Youtube"):
        track["message"] = "This entry is not a supported YouTube video; it will be skipped."
        return track
    url = "https://www.youtube.com/watch?v=" + video_id
    for key in ("url", "webpage_url"):
        supplied = entry.get(key)
        if supplied is None or supplied == video_id:
            continue
        try:
            if import_sources.normalize_youtube_url(supplied) != url:
                raise LibraryError("Mismatched video")
        except LibraryError:
            track["message"] = "This entry has an invalid video link; it will be skipped."
            return track
    if entry.get("is_live") or entry.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        track["message"] = "Live, upcoming, and still-processing streams will be skipped."
        return track
    duration = entry.get("duration")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration) and duration > 0:
        track["duration"] = float(duration)
        if duration > import_sources.MAX_VIDEO_SECONDS:
            track["message"] = "Videos longer than 6 hours will be skipped."
            return track
    if entry.get("availability") in ("private", "premium_only", "subscriber_only") and track["duration"] is None:
        track["message"] = "YouTube did not expose playable details for this account; this video will be skipped."
        return track
    track.update(youtubeUrl=url, available=True)
    return track


def _failure(stderr, authenticated=False):
    # Reuse single-video error sanitization so signed URLs and terminal escapes
    # are never forwarded to the interface.
    safe = str(import_sources._failure(stderr))
    _, separator, detail = safe.partition(" Details: ")
    lower = detail.lower()
    if "not a bot" in lower:
        message = ("YouTube still asked the selected account session to confirm it is not a bot. Check the session in your browser or retry later; account access may not resolve every challenge." if authenticated else "YouTube asked the downloader to confirm it is not a bot. Try an account session for this import or retry later; account access may not resolve every challenge.")
    elif any(word in lower for word in ("private", "sign in", "sign-in", "login", "does not exist")):
        message = ("The selected YouTube session could not access this playlist. Check that the selected browser account can open it, or upload fresh cookies." if authenticated else "YouTube did not expose this playlist publicly. Enable Use YouTube account for this import, or use a public or unlisted playlist.")
    elif authenticated and any(word in lower for word in ("cookie", "keychain", "decrypt", "browser database")):
        return import_sources._failure(stderr, authenticated=True)
    else:
        message = "The YouTube playlist could not be read. Check the link and your connection, then try again."
    return LibraryError(message + (separator + detail if separator and not authenticated else ""), 502)


def read_youtube_playlist(url, cancel_event=None, youtube_session=None):
    """Expand the playlist using only the session selected for this import."""
    context = youtube_session.command_options() if youtube_session is not None else contextlib.nullcontext(None)
    with context as auth_options:
        return _read_youtube_playlist(url, cancel_event, auth_options)


def _read_youtube_playlist(url, cancel_event=None, auth_options=None):
    """Return up to 100 ordered entries, with placeholders for unavailable ones.

    The 101st entry is read only to detect truncation. The existing downloader
    runner bounds output, time, and cancellation, including child processes.
    """
    source_url = normalize_youtube_playlist_url(url)
    command = list(import_sources._base_command(auth_options) if auth_options else import_sources._base_command())
    command.remove("--no-playlist")
    command[command.index("--use-extractors") + 1] = "youtube:tab,youtube:playlist"
    result = import_sources._run_tool(command + [
        "--yes-playlist", "--flat-playlist", "--skip-download", "--simulate",
        "--dump-single-json", "--ignore-errors", "--playlist-end", str(MAX_TRACKS + 1),
        "--", source_url,
    ], timeout=READ_TIMEOUT, cancel_event=cancel_event)
    if result.returncode:
        raise _failure(result.stderr, authenticated=bool(auth_options))
    if cancel_event is not None and cancel_event.is_set():
        raise LibraryError("Playlist reading cancelled.")
    try:
        info = json.loads(result.stdout)
    except (ValueError, TypeError, RecursionError):
        raise LibraryError("YouTube returned unreadable playlist information. Try again or update the downloader.", 502) from None
    expected_id = source_url.rsplit("=", 1)[-1]
    if (not isinstance(info, dict) or info.get("id") != expected_id or info.get("_type") != "playlist"
            or not isinstance(info.get("entries"), list)):
        raise LibraryError("YouTube did not return the requested playlist. Check the link and try again.", 502)
    entries = info["entries"]
    total = info.get("playlist_count")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        total = None
    truncated = len(entries) > MAX_TRACKS or total is not None and total > MAX_TRACKS
    extraction_warning = bool(re.search(r"\b(?:WARNING|ERROR):", result.stderr or '', re.IGNORECASE))
    tracks = [_track(entry, i, authenticated=bool(auth_options)) for i, entry in enumerate(entries[:MAX_TRACKS])]
    notes = []
    if truncated:
        notes.append("Only the first 100 playlist entries can be reviewed at once.")
    if total is not None and total > len(entries) and not truncated:
        notes.append("YouTube did not expose every playlist entry. Hidden videos are not included.")
    if total is None:
        notes.append("YouTube did not report a total. This review may be incomplete.")
    if extraction_warning:
        notes.append("YouTube reported a problem while reading the playlist. This review may be incomplete; only the returned entries are shown.")
    unavailable = sum(not track["available"] for track in tracks)
    if unavailable:
        notes.append(f"{unavailable} unavailable or unsupported videos will be skipped.")
    notes.append("Titles and creators come from YouTube; a channel name may be used when the music artist is unavailable.")
    return {
        "title": import_sources._text(info.get("title"), 120) or "YouTube playlist",
        "url": source_url, "notes": notes,
        "complete": not truncated and not extraction_warning and total is not None and total <= len(entries),
        "total": total, "tracks": tracks,
    }
