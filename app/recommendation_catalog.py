"""Bounded, metadata-only discovery and explainable deterministic recommendations.

The best library match supplies up to 40 artist + 25 tag + 10 album + 10
title-word + 15 duration points. Unique explicit likes/dislikes add/subtract
artist (12/20), tag (8/15), and recording-version (3/8) points; total feedback
is capped at +30/-60 and the final score at 0..100. Unrated listens and skips
are deliberately absent from this model. Missing metadata earns no points.

Discovery uses the existing credential-free YouTube *search* integration, not
its acquisition API. At most three stable library seeds and five results per
seed are requested. Artist catalogs are a conservative starting pool, not an
audio-similarity model. The same metadata snapshot always has the same ranking;
remote YouTube search results can, of course, change between refreshes.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import re
import unicodedata

from . import import_sources, playlist_matching
from .library import LibraryError


MAX_SEEDS = 3
MAX_RESULTS_PER_SEED = 5
MAX_SONG_SECONDS = 15 * 60
_PROMO = re.compile(r"\b(?:official\s+)?(?:audio|music\s+video|video|lyric\s+video|lyrics?|visuali[sz]er)\b|\b(?:hd|hq|4k|1080p|720p)\b", re.I)
_TITLE_STOPWORDS = frozenset({"the", "and", "with", "feat", "featuring", "from", "for", "this", "that", "you", "your"})


def _text(value, limit=300):
    return import_sources._text(value, limit)


def _normal(value):
    return playlist_matching._normal(value)


def _artist(value):
    value = _text(value)
    value = re.sub(r"(?:\s*[-–—]\s*|\s+)(?:topic|official(?:\s+music)?)$", "", value, flags=re.I)
    value = re.sub(r"vevo$", "", value, flags=re.I)
    return value.strip()


def _title(value, artist=""):
    """Remove presentation labels, retaining every recording/version qualifier."""
    value = _text(value)
    # Remove a redundant artist prefix only when it is explicitly separated.
    parts = re.split(r"\s+[-–—|]\s+", value, maxsplit=1)
    if len(parts) == 2 and _normal(parts[0]) == _normal(_artist(artist)):
        value = parts[1]
    def group(match):
        content = match.group(1)
        remaining = _PROMO.sub("", content)
        return "" if not _normal(remaining) else match.group(0)
    value = re.sub(r"[\[(]([^\])]+)[\])]", group, value)
    # A bare trailing label is presentation too, but a song actually named
    # "Music", "Audio", or "Video" must retain its name.
    value = re.sub(r"\s+[-–—|]\s*(?:official\s+)?(?:audio|music\s+video|video|lyric\s+video|lyrics?|visuali[sz]er)(?:\s+hd)?$", "", value, flags=re.I)
    value = re.sub(r"\s+official\s+(?:audio|music\s+video|video|lyric\s+video|visuali[sz]er)$", "", value, flags=re.I)
    return _text(value).strip(" -–—|")


def _source(track):
    try:
        return import_sources.normalize_youtube_url(track.get("sourceUrl", track.get("url", "")))
    except (LibraryError, AttributeError):
        return ""


def canonical_key(track):
    """Recording identity by title + artist + version, independent of upload.

    Exact normalized source URLs are checked separately. Unknown artists use
    the source as identity rather than conflating unrelated songs by title.
    """
    if not isinstance(track, dict):
        return ""
    artist = _artist(track.get("artist"))
    title = _title(track.get("title"), artist)
    if not artist or not title:
        return "source:" + _source(track) if _source(track) else ""
    versions = playlist_matching._versions(title) | playlist_matching._versions(track.get("album"))
    return "recording:" + _normal(artist) + "|" + _normal(title) + "|" + ",".join(sorted(versions))


def _tags(track):
    values = track.get("tags", [])
    return {_normal(value) for value in values[:32] if _normal(value)} if isinstance(values, list) else set()


def _versions(track):
    return playlist_matching._versions(track.get("title")) | playlist_matching._versions(track.get("album"))


def _words(track):
    return {word for word in _normal(_title(track.get("title"), track.get("artist"))).split()
            if len(word) > 2 and word not in _TITLE_STOPWORDS and word not in playlist_matching._PROMO_WORDS}


def _overlap(left, right):
    return len(left & right) / len(left | right) if left and right else 0


def _duration(value):
    return playlist_matching._duration(value)


def _candidate(value):
    if not isinstance(value, dict) or not (source := _source(value)):
        return None
    title = _title(value.get("title"), value.get("artist"))
    if not title:
        return None
    seeds = value.get("seedIds", [])
    # Preserve persisted lifecycle fields supplied by the owning service.
    return dict(value, id=hashlib.sha256(source.encode()).hexdigest()[:32],
                title=title, artist=_artist(value.get("artist")), album=_text(value.get("album")),
                tags=sorted(_tags(value)), duration=_duration(value.get("duration")), sourceUrl=source,
                seedIds=sorted({_text(seed, 100) for seed in seeds if _text(seed, 100)}) if isinstance(seeds, list) else [])


def _identity_sets(tracks):
    tracks = [track for track in tracks if isinstance(track, dict)]
    return ({key for track in tracks if (key := canonical_key(track))},
            {url for track in tracks if (url := _source(track))})


def _unique(candidates):
    """Merge source/recording aliases transitively with stable representatives."""
    groups = []
    def order(candidate):
        return (candidate["sourceUrl"], canonical_key(candidate), candidate["title"], candidate["artist"],
                candidate["album"], candidate["duration"] or 0, tuple(candidate["tags"]))
    for candidate in sorted(candidates, key=order):
        keys, sources = _identity_sets([candidate])
        members = [candidate]
        rest = []
        for group_keys, group_sources, group_members in groups:
            if keys & group_keys or sources & group_sources:
                keys |= group_keys
                sources |= group_sources
                members.extend(group_members)
            else:
                rest.append((group_keys, group_sources, group_members))
        # Each prior group is already disjoint. The candidate can bridge groups
        # with distinct metadata for one source, so combine every match.
        rest.append((keys, sources, members))
        groups = rest
    result = []
    for keys, sources, members in groups:
        members.sort(key=order)
        candidate = dict(members[0])
        candidate["seedIds"] = sorted({seed for member in members for seed in member["seedIds"]})
        candidate["tags"] = sorted({tag for member in members for tag in member["tags"]})
        result.append((candidate, keys, sources))
    return result


def _feedback_rows(feedback):
    rows = []
    for row in feedback:
        if not isinstance(row, dict):
            continue
        decision = row.get("verdict", row.get("decision", row.get("rating")))
        if decision not in ("up", "down"):
            continue
        candidate = _candidate(row)
        if candidate:
            candidate["_decision"] = decision
            rows.append(candidate)
    result = []
    for candidate, keys, sources in _unique(rows):
        # A stale positive alias must never override an explicit rejection.
        candidate["_decision"] = "down" if any(row["_decision"] == "down" and
            (canonical_key(row) in keys or row["sourceUrl"] in sources) for row in rows) else "up"
        result.append((candidate, keys, sources))
    return result


def _similarity(candidate, seed):
    reasons = []
    artist_match = bool(_normal(candidate.get("artist"))) and _normal(candidate.get("artist")) == _normal(_artist(seed.get("artist")))
    points = [
        (40 if artist_match else 0, "Same artist"),
        (round(25 * _overlap(_tags(candidate), _tags(seed)), 2), "Shared tags"),
        (10 if artist_match and _normal(candidate.get("album")) and _normal(candidate.get("album")) == _normal(seed.get("album")) else 0, "Same album"),
        (round(10 * _overlap(_words(candidate), _words(seed)), 2), "Shared title words"),
    ]
    duration, seed_duration = _duration(candidate.get("duration")), _duration(seed.get("duration"))
    if duration and seed_duration:
        points.append((round(15 * max(0, 1 - abs(duration - seed_duration) / max(30, seed_duration * .5)), 2), "Similar duration"))
    for points_value, label in points:
        if points_value:
            reasons.append(f"{label}: +{points_value:g}.")
    return round(sum(value for value, _ in points), 2), reasons


def rank_candidates(candidates, tracks, feedback):
    """Rank metadata without I/O, input mutation, randomness or wall-clock use.

    Owned recordings and exact rejected aliases are excluded. Score ties use
    canonical recording identity, then source URL. Feedback may use ``verdict``,
    ``decision``, or ``rating`` with an explicit ``up``/``down`` value.
    """
    tracks = [dict(track) for track in tracks if isinstance(track, dict)]
    owned_keys, owned_sources = _identity_sets(tracks)
    votes = _feedback_rows(feedback)
    rejected_keys = set().union(*(keys for row, keys, _ in votes if row["_decision"] == "down"))
    rejected_sources = set().union(*(sources for row, _, sources in votes if row["_decision"] == "down"))
    seeds = sorted(tracks, key=lambda track: (canonical_key(track), _text(track.get("id"))))
    cleaned = [candidate for value in candidates if (candidate := _candidate(value))]
    result = []
    for candidate, keys, sources in _unique(cleaned):
        if keys & (owned_keys | rejected_keys) or sources & (owned_sources | rejected_sources):
            continue
        best_score, reasons, best_seed = 0, [], None
        for seed in seeds:
            score, match_reasons = _similarity(candidate, seed)
            if score > best_score:
                best_score, reasons, best_seed = score, match_reasons, seed
        if best_seed:
            reasons.insert(0, f"Closest library song: {_text(best_seed.get('artist'))} — {_text(best_seed.get('title'))}.")
        else:
            reasons = ["No matching library metadata: +0."]
        adjustment = 0
        for vote, _, _ in sorted(votes, key=lambda value: (canonical_key(value[0]), value[0]["sourceUrl"])):
            up = vote["_decision"] == "up"
            artist_match = bool(candidate["artist"]) and _normal(candidate["artist"]) == _normal(vote["artist"])
            artist = (12 if up else 20) if artist_match else 0
            tags = round((8 if up else 15) * _overlap(_tags(candidate), _tags(vote)), 2)
            version = round((3 if up else 8) * _overlap(_versions(candidate), _versions(vote)), 2)
            change = round(artist + tags + version, 2)
            if change:
                adjustment += change if up else -change
                features = ", ".join(label for points, label in ((artist, "artist"), (tags, "tags"), (version, "recording version")) if points)
                reasons.append(f"{'Liked' if up else 'Disliked'} “{vote['title']}” shares {features}: {'+' if up else '-'}{change:g}.")
        bounded = max(-60, min(30, round(adjustment, 2)))
        if bounded != round(adjustment, 2):
            reasons.append(f"Feedback adjustment capped at {bounded:+g}.")
        total = round(best_score + bounded, 2)
        score = max(0, min(100, total))
        if total != score:
            reasons.append(f"Final score limited to {score:g} (0–100 scale).")
        result.append(dict(candidate, score=score, reasons=reasons))
    return sorted(result, key=lambda candidate: (-candidate["score"], canonical_key(candidate), candidate["sourceUrl"]))


def _seeds(tracks):
    unique = {}
    for track in sorted((t for t in tracks if isinstance(t, dict)), key=lambda t: (canonical_key(t), _text(t.get("id")))):
        if _text(track.get("title")):
            unique.setdefault(canonical_key(track) or _normal(track["title"]), track)
    artists = Counter(_normal(_artist(track.get("artist"))) for track in unique.values() if _artist(track.get("artist")))
    seeds = []
    for artist in sorted(artists, key=lambda value: (-artists[value], value))[:MAX_SEEDS]:
        seeds.append(next(track for track in unique.values() if _normal(_artist(track.get("artist"))) == artist))
    if not seeds:
        seeds = list(unique.values())[:MAX_SEEDS]
    return seeds


def _discovered(entry, seed):
    if not isinstance(entry, dict):
        return None
    title = _text(entry.get("title"))
    if re.search(r"\b(?:full\s+album|playlist|greatest\s+hits|best\s+of|compilation)\b", title, re.I):
        return None
    duration = _duration(entry.get("duration"))
    if duration and duration > MAX_SONG_SECONDS:
        return None
    artist = _artist(entry.get("channel"))
    seed_artist = _artist(seed.get("artist"))
    # Compatibility normalization makes stylized mathematical Unicode letters
    # comparable without guessing artist identity from the search query.
    comparable = unicodedata.normalize("NFKC", title)
    parts = re.split(r"\s+(?:[-|]|//)\s+|\s*[–—]\s*", comparable)
    explicit_artist = False
    if len(parts) >= 2 and seed_artist and _normal(parts[0]) == _normal(seed_artist):
        artist, title = seed_artist, " — ".join(parts[1:])
        explicit_artist = True
    elif len(parts) >= 2 and seed_artist:
        # "Song // Artist (Slowed + Reverb)" states both creator and version.
        tail = parts[-1]
        version_groups = re.findall(r"[\[(][^\])]+[\])]", tail)
        qualifying = [value for value in version_groups if playlist_matching._versions(value)]
        bare_artist = tail
        for qualifier in qualifying:
            bare_artist = bare_artist.replace(qualifier, "")
        if _normal(bare_artist) == _normal(seed_artist):
            artist = seed_artist
            title = " — ".join(parts[:-1]) + (" " + " ".join(qualifying) if qualifying else "")
            explicit_artist = True
    if seed_artist:
        if _normal(artist) != _normal(seed_artist) and not explicit_artist:
            return None
        artist = seed_artist
    elif not (_words({"title": title}) & _words(seed)):
        # Duration coincidence by itself is insufficient discovery evidence.
        return None
    # Search metadata has no reliable album or genre. Never copy a seed's tags
    # onto another recording and then claim that as evidence for similarity.
    return _candidate({"title": title, "artist": artist, "album": "", "tags": [],
                       "duration": duration, "sourceUrl": entry.get("url"),
                       "seedIds": [_text(seed.get("id"), 100)]})


def discover_candidates(tracks, cancel=None, progress=None):
    """Fetch <=15 metadata rows, checking cancellation between bounded searches.

    ``progress`` receives a display message before each search. Failures are
    surfaced rather than silently presenting an incomplete refresh as success.
    """
    seeds = _seeds(tracks)
    if not seeds:
        raise LibraryError("Add a song to your library before finding recommendations.")
    candidates = []
    for index, seed in enumerate(seeds):
        if cancel is not None and cancel.is_set():
            raise LibraryError("Recommendation discovery cancelled.", 409)
        artist = _artist(seed.get("artist"))
        query = artist or " ".join(sorted(_tags(seed))) or _text(seed.get("title"))
        if progress:
            progress(f"Finding songs for {query} ({index + 1}/{len(seeds)})…")
        entries = playlist_matching.search_candidates({"title": query}, cancel_event=cancel)
        for entry in entries[:MAX_RESULTS_PER_SEED]:
            candidate = _discovered(entry, seed)
            if candidate:
                candidates.append(candidate)
    if cancel is not None and cancel.is_set():
        raise LibraryError("Recommendation discovery cancelled.", 409)
    return [candidate for candidate in rank_candidates(candidates, tracks, []) if candidate["score"] > 0]
