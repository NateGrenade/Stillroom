"""Read song identities from Spotify's ordinary public playlist embed.

No Spotify audio, preview URLs, tokens, cookies, or account information leave
this module. The embed is not a paginated catalog API: its visible track list
may be incomplete even when it has fewer than 100 entries.
"""

from __future__ import annotations

from html.parser import HTMLParser
from http.client import HTTPException
import json
import math
import re
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .library import LibraryError


MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_TRACKS = 100
REQUEST_TIMEOUT = 15
READ_TIMEOUT = 35
SPOTIFY_ID = r"[A-Za-z0-9]{22}"
PLAYLIST_PATH = re.compile(r"/(?:intl-[a-zA-Z-]+/|embed/)?playlist/(" + SPOTIFY_ID + r")/?\Z")
TRACK_URI = re.compile(r"spotify:track:(" + SPOTIFY_ID + r")\Z")
PUBLIC_ERROR = "Spotify did not expose this playlist publicly. Check the link, make the playlist public, or try again later."


def normalize_spotify_playlist_url(url):
    """Accept a playlist share/embed link and remove all tracking parameters."""
    error = "Paste a public Spotify playlist link (open.spotify.com/playlist/…)."
    if not isinstance(url, str) or not url.strip() or len(url) > 2048:
        raise LibraryError(error)
    url = url.strip()
    if any(ord(c) < 33 or ord(c) == 127 for c in url) or "\\" in url:
        raise LibraryError(error)
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
            raise ValueError()
        if (parsed.hostname or "").lower() != "open.spotify.com":
            raise ValueError()
        if parsed.port not in (None, 443 if parsed.scheme == "https" else 80):
            raise ValueError()
        match = PLAYLIST_PATH.fullmatch(parsed.path)
        if not match:
            raise ValueError()
    except (ValueError, TypeError):
        raise LibraryError(error) from None
    return "https://open.spotify.com/playlist/" + match[1]


class _PlaylistRedirects(HTTPRedirectHandler):
    """Allow only HTTPS redirects to the same public Spotify playlist."""

    max_redirections = 3
    max_repeats = 1

    def __init__(self, playlist_url):
        self.playlist_url = playlist_url

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            same = normalize_spotify_playlist_url(newurl) == self.playlist_url
            secure = urlsplit(newurl).scheme == "https"
        except LibraryError:
            same = secure = False
        if not same or not secure:
            raise LibraryError(PUBLIC_ERROR, 502)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise LibraryError("Playlist reading cancelled.")


def _fetch_embed(playlist_url, cancel_event=None):
    _cancelled(cancel_event)
    embed = playlist_url.replace("/playlist/", "/embed/playlist/", 1)
    opener = build_opener(_PlaylistRedirects(playlist_url))
    request = Request(embed, headers={
        "User-Agent": "Mozilla/5.0 (compatible; Stillroom; public playlist metadata)",
        "Accept": "text/html,application/xhtml+xml", "Accept-Encoding": "identity",
    })
    deadline = time.monotonic() + READ_TIMEOUT
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
            try:
                declared = int(response.headers.get("Content-Length", "0"))
            except (ValueError, TypeError):
                declared = 0
            if declared > MAX_PAGE_BYTES:
                raise LibraryError("Spotify returned too much playlist information. Try a smaller playlist.", 413)
            data = bytearray()
            while True:
                _cancelled(cancel_event)
                if time.monotonic() > deadline:
                    raise LibraryError("Spotify took too long to respond. Try again later.", 504)
                chunk = response.read(min(64 * 1024, MAX_PAGE_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_PAGE_BYTES:
                    raise LibraryError("Spotify returned too much playlist information. Try a smaller playlist.", 413)
            _cancelled(cancel_event)
            return data.decode("utf-8", errors="replace")
    except HTTPError as error:
        if error.code == 429:
            raise LibraryError("Spotify is limiting requests. Wait a little, then try again.", 429) from None
        raise LibraryError(PUBLIC_ERROR, 502) from None
    except (TimeoutError, socket.timeout):
        raise LibraryError("Spotify took too long to respond. Try again later.", 504) from None
    except (URLError, OSError, HTTPException):
        raise LibraryError("Could not reach Spotify. Check your connection and try again.", 502) from None


class _NextData(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.inside = False
        self.fragments = []
        self.found = False

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "__NEXT_DATA__" and not self.found:
            self.inside = self.found = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.fragments.append(data)


def _text(value, limit=300):
    if not isinstance(value, str):
        return ""
    return " ".join("".join(c if ord(c) >= 32 and ord(c) != 127 else " " for c in value).split())[:limit].strip()


def _track_metadata(item):
    if not isinstance(item, dict) or item.get("entityType", "track") != "track":
        return None
    uri = item.get("uri")
    match = TRACK_URI.fullmatch(uri) if isinstance(uri, str) else None
    title = _text(item.get("title") or item.get("name"))
    if not match or not title or item.get("isLocal") or item.get("is_local"):
        return None
    artists = []
    if isinstance(item.get("artists"), list):
        artists = [_text(artist.get("name")) for artist in item["artists"] if isinstance(artist, dict)]
        artists = [artist for artist in artists if artist]
    if not artists:
        subtitle = item.get("subtitle")
        # Spotify separates distinct artists with comma + NBSP. A normal comma
        # may be part of an artist's name (e.g. Earth, Wind & Fire).
        artists = [_text(name) for name in subtitle.split(",\u00a0")] if isinstance(subtitle, str) else []
        artists = [artist for artist in artists if artist]
    if not artists:
        return None
    milliseconds = item.get("duration")
    duration = (milliseconds / 1000 if isinstance(milliseconds, (int, float))
                and not isinstance(milliseconds, bool) and math.isfinite(milliseconds)
                and milliseconds > 0 else None)
    album = item.get("album")
    album = _text(album.get("name")) if isinstance(album, dict) else _text(album)
    return {
        "id": match[1], "title": title, "artist": _text(", ".join(artists)),
        "artists": artists, "album": album, "duration": duration,
        "spotifyUrl": "https://open.spotify.com/track/" + match[1],
    }


def _parse_embed(html, playlist_url):
    parser = _NextData()
    try:
        parser.feed(html)
        payload = json.loads("".join(parser.fragments))
        entity = payload["props"]["pageProps"]["state"]["data"]["entity"]
    except (ValueError, KeyError, TypeError, RecursionError):
        raise LibraryError(PUBLIC_ERROR, 502) from None
    playlist_id = playlist_url.rsplit("/", 1)[1]
    if (not isinstance(entity, dict) or entity.get("type") != "playlist"
            or entity.get("id") != playlist_id
            or entity.get("uri") not in (None, "spotify:playlist:" + playlist_id)
            or not isinstance(entity.get("trackList"), list)):
        raise LibraryError("Spotify returned unexpected playlist information. Try again later.", 502)
    entries = entity["trackList"]
    tracks = []
    skipped = 0
    unavailable = 0
    for entry in entries[:MAX_TRACKS]:
        track = _track_metadata(entry)
        if track is None:
            skipped += 1
            continue
        tracks.append(track)
        unavailable += entry.get("isPlayable") is False
    notes = [
        f"Spotify exposed {len(tracks)} song{'s' if len(tracks) != 1 else ''} in its public preview. "
        "Spotify does not provide a total here, so this may not be the whole playlist. "
        "Stillroom reads up to 100 visible entries."
    ]
    if len(entries) > MAX_TRACKS:
        notes.append(f"Only the first {MAX_TRACKS} visible entries were read.")
    if skipped:
        notes.append(f"Skipped {skipped} local file, non-song, or incomplete entr{'y' if skipped == 1 else 'ies'}.")
    if unavailable:
        notes.append(f"{unavailable} song{' is' if unavailable == 1 else 's are'} unavailable in Spotify's preview; their identifying information was kept for matching.")
    if not tracks:
        notes.append("There are no identifiable public songs to match in this playlist.")
    return {
        "title": _text(entity.get("title") or entity.get("name")) or "Spotify playlist",
        "url": playlist_url, "tracks": tracks, "complete": False, "total": None,
        "notes": notes,
    }


def read_spotify_playlist(url, cancel_event=None):
    """Return bounded, ordered public metadata; never claim unproven completeness."""
    normalized = normalize_spotify_playlist_url(url)
    result = _parse_embed(_fetch_embed(normalized, cancel_event), normalized)
    _cancelled(cancel_event)
    return result
