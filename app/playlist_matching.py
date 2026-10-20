"""Find reviewable YouTube recordings for playlist metadata without downloading.

Scores are transparent metadata heuristics, not audio fingerprints or promises
that the YouTube upload is the same recording. Every returned URL is rebuilt
from a validated YouTube video ID; extractor-provided URLs are never executed.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata

from . import import_sources
from .library import LibraryError


MAX_RESULTS = 5
SEARCH_TIMEOUT = 75
_PROMO_WORDS = frozenset({
    "official", "audio", "video", "music", "lyric", "lyrics", "visualizer",
    "visualiser", "hd", "hq", "4k", "1080p", "720p", "provided", "youtube",
    "topic", "vevo",
})
_VERSION_PATTERNS = {
    "remix": r"\bremix(?:ed)?\b",
    "cover": r"\bcover\b",
    "sped up": r"\bsped\s*up\b|\bnightcore\b",
    "slowed": r"\bslowed\b|\bslow(?:ed)?\s*(?:and\s*)?reverb\b",
    "instrumental": r"\binstrumental\b|\bkaraoke\b|\bpiano\s+(?:version|cover)\b",
    "remaster": r"\bremaster(?:ed)?\b",
    "acoustic": r"\bacoustic\b|\bunplugged\b",
    "extended": r"\bextended\b",
    "radio edit": r"\bradio\s+edit\b",
    "demo": r"\bdemo\b",
}


def _text(value, limit=300):
    return import_sources._text(value, limit)


def _normal(value):
    decomposed = unicodedata.normalize("NFKD", _text(value)).casefold()
    decomposed = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.findall(r"[^\W_]+", decomposed, flags=re.UNICODE))


def _duration(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or not 0 < value <= import_sources.MAX_VIDEO_SECONDS:
        return None
    return float(value)


def _artists(track):
    artists = track.get("artists")
    if isinstance(artists, list):
        names = [_text(value) for value in artists[:10] if _text(value)]
        if names:
            return list(dict.fromkeys(names))
    artist = _text(track.get("artist"))
    return [artist] if artist else []


def _contains(haystack, needle):
    return bool(needle) and f" {needle} " in f" {haystack} "


def _versions(value):
    text = _normal(value)
    found = {name for name, pattern in _VERSION_PATTERNS.items() if re.search(pattern, text)}
    # "Live Forever" and other song names containing live are not automatically
    # live recordings. A trailing/parenthesized live or venue wording is evidence.
    raw = _text(value).casefold()
    if (re.search(r"(?:[([]\s*|\s[-–—]\s*)live(?=\s*[()\[\]]|\s*$)|\blive\s+(?:at|from|in|on|\d{4})\b", raw)
            or re.search(r"\blive\s*[)\]]?\s*$", raw)):
        found.add("live")
    return found


def _candidate(track, entry):
    if not isinstance(entry, dict):
        return None
    video_id = entry.get("id")
    if not isinstance(video_id, str) or not import_sources.VIDEO_ID.fullmatch(video_id):
        return None
    if entry.get("_type", "url") not in ("url", "video"):
        return None
    if entry.get("ie_key") not in (None, "Youtube"):
        return None
    if entry.get("is_live") or entry.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        return None
    if entry.get("availability") in ("private", "premium_only", "subscriber_only", "needs_auth"):
        return None
    url = "https://www.youtube.com/watch?v=" + video_id
    for key in ("url", "webpage_url"):
        supplied = entry.get(key)
        if supplied is not None:
            try:
                if import_sources.normalize_youtube_url(supplied) != url:
                    return None
            except LibraryError:
                return None
    title = _text(entry.get("title"))
    if not title or title.casefold() in ("[private video]", "[deleted video]"):
        return None
    channel = _text(entry.get("channel")) or _text(entry.get("uploader"))
    duration = _duration(entry.get("duration"))
    expected_duration = _duration(track.get("duration"))
    names = _artists(track)
    wanted_title = _normal(track.get("title"))
    actual_title = _normal(title)
    actual_channel = _normal(re.sub(r"vevo$", "", channel, flags=re.I))
    channel_identity = re.sub(r"\s+(?:topic|official|official music)$", "", actual_channel)
    artist_channel = bool(names) and channel_identity == _normal(names[0])
    wanted_tokens = set(wanted_title.split())
    title_coverage = len(wanted_tokens.intersection(actual_title.split())) / max(1, len(wanted_tokens))
    title_phrase = _contains(actual_title, wanted_title)
    artists_matched = sum(
        _contains(actual_title, _normal(name)) or _contains(actual_channel, _normal(name))
        for name in names
    )
    artist_coverage = artists_matched / len(names) if names else 0
    score = 45 * title_coverage + 30 * artist_coverage
    reasons = []
    if title_phrase:
        reasons.append("Song title appears in the video title.")
    elif title_coverage == 1:
        reasons.append("Title words match, but their order differs.")
    else:
        reasons.append("The song title does not fully match.")
    if names and artists_matched == len(names):
        reasons.append("Artist names appear in the title or channel.")
    elif artists_matched:
        reasons.append("Only some of the artist names match.")
    else:
        reasons.append("The artist could not be confirmed from the title or channel.")

    duration_close = False
    if duration is not None and expected_duration is not None:
        difference = abs(duration - expected_duration)
        duration_close = difference <= max(3, expected_duration * 0.015)
        if duration_close:
            score += 25
            reasons.append(f"Duration is within {difference:.1f} seconds of the playlist track.")
        elif difference <= max(10, expected_duration * 0.05):
            score += 12
            reasons.append(f"Duration differs by {difference:.1f} seconds; check the beginning and ending.")
        else:
            score -= 20
            reasons.append(f"Duration differs by {difference:.1f} seconds.")
    else:
        reasons.append("Duration is unavailable for comparison.")

    wanted_versions = _versions(track.get("title")) | _versions(track.get("album"))
    actual_versions = _versions(title)
    mismatched_versions = wanted_versions.symmetric_difference(actual_versions)
    if mismatched_versions:
        score -= 30
        reasons.append("Recording version needs review: " + ", ".join(sorted(mismatched_versions)) + ".")

    # Extra meaningful title words often distinguish songs with shared names.
    # A matching duration alone must never turn those into automatic selections.
    expected_words = set(wanted_tokens)
    for name in names:
        expected_words.update(_normal(name).split())
    extras = set(actual_title.split()) - expected_words - _PROMO_WORDS
    extras -= {"feat", "ft", "featuring"}
    if extras:
        score -= 8
        reasons.append("Additional wording in the video title needs review.")
    if not artist_channel:
        score -= 5
        reasons.append("The channel does not clearly identify the primary artist; verify this upload.")
    strong = (title_phrase and names and artists_matched == len(names)
              and artist_channel and duration_close and not mismatched_versions and not extras and score >= 95)
    return {
        "url": url, "title": title, "channel": channel, "duration": duration,
        "score": max(0, min(100, round(score))),
        "confidence": "strong" if strong else "review", "reasons": reasons,
    }


def search_candidates(track, cancel_event=None):
    """Return up to five candidates, ranked with a stable URL tie-break.

    ``track`` uses title, artist or artists (list of names), album, and duration
    in seconds. Missing duration/artist always requires manual review. Searches
    use no credentials or cookies and never fetch media or remote components.
    """
    if not isinstance(track, dict) or not _text(track.get("title")):
        raise LibraryError("A song title is needed to search YouTube.")
    command = list(import_sources._base_command())
    command[command.index("--use-extractors") + 1] = "youtube:search"
    query = " ".join([_text(track.get("title")), *_artists(track)[:5], "audio"])[:1000]
    result = import_sources._run_tool(command + [
        "--flat-playlist", "--skip-download", "--dump-single-json",
        "--playlist-end", str(MAX_RESULTS), "--", f"ytsearch{MAX_RESULTS}:{query}",
    ], timeout=SEARCH_TIMEOUT, cancel_event=cancel_event)
    if result.returncode:
        raise LibraryError("YouTube search is unavailable. Try again or paste a YouTube link for this song.", 502)
    try:
        info = json.loads(result.stdout)
    except (ValueError, TypeError, RecursionError):
        raise LibraryError("YouTube returned unreadable search results. Try again.", 502) from None
    if not isinstance(info, dict) or not isinstance(info.get("entries"), list) or len(info["entries"]) > 100:
        raise LibraryError("YouTube returned unexpected search results. Try again.", 502)
    candidates = {}
    for entry in info["entries"][:MAX_RESULTS]:
        candidate = _candidate(track, entry)
        if candidate:
            candidates.setdefault(candidate["url"], candidate)
    return sorted(candidates.values(), key=lambda value: (-value["score"], value["url"]))
