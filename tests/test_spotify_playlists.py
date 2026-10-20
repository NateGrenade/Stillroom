"""Public playlist reader contracts, including incomplete lists and network bounds."""

from __future__ import annotations

import io
from http.client import IncompleteRead
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import spotify_playlists as spotify
from app.library import LibraryError

PLAYLIST_ID = "3cEYpjA9oz9GiPac4AsH4n"
URL = "https://open.spotify.com/playlist/" + PLAYLIST_ID
TRACK_ID = "4rzfv0JLZfVhOhbSQ8o5jZ"


def track(**updates):
    item = {"uri": "spotify:track:" + TRACK_ID, "title": "A song", "subtitle": "An artist",
            "duration": 123456, "entityType": "track", "isPlayable": True,
            "audioPreview": {"url": "https://p.scdn.co/private-preview-do-not-retain"}}
    item.update(updates)
    return item


def page(entries=None, **updates):
    entity = {"id": PLAYLIST_ID, "uri": "spotify:playlist:" + PLAYLIST_ID,
              "type": "playlist", "title": "A playlist", "trackList": entries if entries is not None else [track()]}
    entity.update(updates)
    payload = {"props": {"pageProps": {"state": {"data": {"entity": entity}, "accessToken": "DO-NOT-KEEP"}}}}
    return "<html><script type='application/json' id='__NEXT_DATA__'>" + json.dumps(payload) + "</script></html>"


class SpotifyUrlTests(unittest.TestCase):
    def test_share_embed_and_locale_urls_are_normalized(self):
        for url in (URL, URL + "?si=tracking#fragment", " http://open.spotify.com/playlist/" + PLAYLIST_ID + " ",
                    "https://open.spotify.com:443/embed/playlist/" + PLAYLIST_ID + "/?utm_source=generator",
                    "https://OPEN.SPOTIFY.COM/intl-de/playlist/" + PLAYLIST_ID):
            with self.subTest(url=url):
                self.assertEqual(spotify.normalize_spotify_playlist_url(url), URL)

    def test_rejects_other_content_and_host_confusion(self):
        for url in (None, "", "spotify:playlist:" + PLAYLIST_ID, "https://spotify.link/unknown",
                    "https://open.spotify.com.evil.test/playlist/" + PLAYLIST_ID,
                    "https://open.spotify.com@localhost/playlist/" + PLAYLIST_ID,
                    "https://user:secret@open.spotify.com/playlist/" + PLAYLIST_ID,
                    "https://open.spotify.com:444/playlist/" + PLAYLIST_ID,
                    URL + "/other", URL + "\n?bad", URL + "\\evil", URL.replace("playlist", "album"),
                    "https://open.spotify.com/playlist/too-short", "file:///playlist/" + PLAYLIST_ID):
            with self.subTest(url=url), self.assertRaises(LibraryError):
                spotify.normalize_spotify_playlist_url(url)

    def test_redirects_cannot_change_origin_playlist_or_downgrade(self):
        redirects = spotify._PlaylistRedirects(URL)
        for destination in ("http://open.spotify.com/playlist/" + PLAYLIST_ID,
                            "https://example.org/", "https://127.0.0.1/", "https://accounts.spotify.com/login",
                            "https://open.spotify.com/playlist/37i9dQZF1DX4JAvHpjipBk"):
            with self.subTest(destination=destination), self.assertRaises(LibraryError):
                redirects.redirect_request(Request(URL), None, 302, "found", {}, destination)
        request = redirects.redirect_request(Request(URL), None, 302, "found", {}, URL + "?locale=en")
        self.assertEqual(request.full_url, URL + "?locale=en")


class SpotifyMetadataTests(unittest.TestCase):
    def test_order_duplicates_and_public_metadata_are_preserved(self):
        html = page([track(title="First"), track(title="Second"), track(title="First")])
        result = spotify._parse_embed(html, URL)
        self.assertEqual([t["title"] for t in result["tracks"]], ["First", "Second", "First"])
        self.assertEqual(result["tracks"][0], {
            "id": TRACK_ID, "title": "First", "artist": "An artist", "artists": ["An artist"],
            "album": "", "duration": 123.456, "spotifyUrl": "https://open.spotify.com/track/" + TRACK_ID,
        })
        self.assertEqual(result["url"], URL)
        self.assertEqual(result["title"], "A playlist")
        self.assertNotIn("preview-do-not-retain", json.dumps(result))
        self.assertNotIn("DO-NOT-KEEP", json.dumps(result))

    def test_no_total_means_completeness_is_unknown_even_for_short_list(self):
        result = spotify._parse_embed(page(), URL)
        self.assertFalse(result["complete"])
        self.assertIsNone(result["total"])
        self.assertIn("may not be the whole playlist", result["notes"][0])

    def test_large_list_is_bounded_without_claiming_completion(self):
        result = spotify._parse_embed(page([track(title=str(i)) for i in range(105)]), URL)
        self.assertEqual(len(result["tracks"]), 100)
        self.assertEqual(result["tracks"][-1]["title"], "99")
        self.assertFalse(result["complete"])
        self.assertTrue(any("first 100" in note for note in result["notes"]))

    def test_artist_separator_does_not_split_commas_inside_names(self):
        entries = [track(subtitle="Earth, Wind & Fire"), track(subtitle="First Artist,\u00a0Second Artist")]
        result = spotify._parse_embed(page(entries), URL)
        self.assertEqual(result["tracks"][0]["artists"], ["Earth, Wind & Fire"])
        self.assertEqual(result["tracks"][1]["artists"], ["First Artist", "Second Artist"])

    def test_structured_artist_and_album_names_are_used_when_present(self):
        result = spotify._parse_embed(page([track(artists=[{"name": "Artist"}, {}, {"name": "Guest"}],
                                                  album={"name": "Release"})]), URL)
        self.assertEqual(result["tracks"][0]["artists"], ["Artist", "Guest"])
        self.assertEqual(result["tracks"][0]["album"], "Release")

    def test_local_missing_and_non_song_entries_are_reported(self):
        entries = [None, track(uri="spotify:local:artist:album:title:123"), track(entityType="episode"),
                   track(title=""), track(subtitle=""), track(is_local=True), track()]
        result = spotify._parse_embed(page(entries), URL)
        self.assertEqual(len(result["tracks"]), 1)
        self.assertTrue(any("Skipped 6" in note for note in result["notes"]))

    def test_unplayable_spotify_song_can_still_be_identified_for_matching(self):
        result = spotify._parse_embed(page([track(isPlayable=False)]), URL)
        self.assertEqual(len(result["tracks"]), 1)
        self.assertTrue(any("unavailable" in note for note in result["notes"]))

    def test_empty_list_is_explicit_and_does_not_raise(self):
        result = spotify._parse_embed(page([]), URL)
        self.assertEqual(result["tracks"], [])
        self.assertTrue(any("no identifiable" in note for note in result["notes"]))

    def test_durations_are_seconds_or_unknown_never_invalid_numbers(self):
        for value in (None, "123", True, -20, 0, float("inf"), float("nan")):
            with self.subTest(value=value):
                result = spotify._parse_embed(page([track(duration=value)]), URL)
                self.assertIsNone(result["tracks"][0]["duration"])

    def test_text_is_sanitized_and_does_not_leak_page_properties(self):
        result = spotify._parse_embed(page([track(title="  Song\n Name\x00  ", subtitle="x" * 400)]), URL)
        self.assertEqual(result["tracks"][0]["title"], "Song Name")
        self.assertEqual(len(result["tracks"][0]["artist"]), 300)

    def test_identity_and_page_shape_are_verified(self):
        for html in ("<html>Unavailable</html>", "<script id='__NEXT_DATA__'>bad json</script>",
                     "<script id='__NEXT_DATA__'>{\"props\":{\"pageProps\":{\"status\":404}}}</script>",
                     page(id="37i9dQZF1DX4JAvHpjipBk"), page(uri="spotify:playlist:other"),
                     page(type="album"), page(trackList={"items": []})):
            with self.subTest(html=html[:100]), self.assertRaises(LibraryError):
                spotify._parse_embed(html, URL)


class Response(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}


class SpotifyNetworkTests(unittest.TestCase):
    def test_only_public_embed_is_requested_without_account_headers(self):
        opener = Mock()
        opener.open.return_value = Response(page().encode())
        with patch.object(spotify, "build_opener", return_value=opener):
            result = spotify.read_spotify_playlist(URL + "?si=tracking")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, URL.replace("/playlist/", "/embed/playlist/"))
        self.assertNotIn("Authorization", request.headers)
        self.assertNotIn("Cookie", request.headers)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], spotify.REQUEST_TIMEOUT)
        self.assertEqual(len(result["tracks"]), 1)

    def test_content_length_and_stream_size_both_enforce_bound(self):
        for headers in ({"Content-Length": "500"}, {}):
            with self.subTest(headers=headers):
                opener = Mock()
                opener.open.return_value = Response(b"x" * 500, headers)
                with patch.object(spotify, "build_opener", return_value=opener), patch.object(spotify, "MAX_PAGE_BYTES", 100):
                    with self.assertRaises(LibraryError) as raised:
                        spotify.read_spotify_playlist(URL)
                self.assertEqual(raised.exception.status, 413)

    def test_http_network_and_timeout_errors_are_actionable(self):
        for error, expected in ((HTTPError(URL, 404, "no", {}, None), 502),
                                (HTTPError(URL, 429, "slow", {}, None), 429),
                                (URLError("failed"), 502), (IncompleteRead(b"partial"), 502), (TimeoutError(), 504)):
            with self.subTest(error=error):
                opener = Mock()
                opener.open.side_effect = error
                with patch.object(spotify, "build_opener", return_value=opener), self.assertRaises(LibraryError) as raised:
                    spotify.read_spotify_playlist(URL)
                self.assertEqual(raised.exception.status, expected)

    def test_cancel_before_network_does_not_connect(self):
        event = threading.Event()
        event.set()
        with patch.object(spotify, "build_opener") as opener, self.assertRaisesRegex(LibraryError, "cancelled"):
            spotify.read_spotify_playlist(URL, event)
        opener.assert_not_called()

    def test_cancel_during_read_does_not_parse_or_return_partial_data(self):
        event = threading.Event()
        response = Response(page().encode())
        original_read = response.read
        def cancel_read(length):
            event.set()
            return original_read(length)
        response.read = cancel_read
        opener = Mock()
        opener.open.return_value = response
        with patch.object(spotify, "build_opener", return_value=opener), self.assertRaisesRegex(LibraryError, "cancelled"):
            spotify.read_spotify_playlist(URL, event)

    def test_total_time_is_bounded_even_if_bytes_keep_arriving(self):
        opener = Mock()
        opener.open.return_value = Response(page().encode())
        with patch.object(spotify, "build_opener", return_value=opener), patch.object(spotify.time, "monotonic", side_effect=[0, 100]):
            with self.assertRaises(LibraryError) as raised:
                spotify.read_spotify_playlist(URL)
        self.assertEqual(raised.exception.status, 504)


if __name__ == "__main__":
    unittest.main()
