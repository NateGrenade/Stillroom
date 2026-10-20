"""Conservative song matching, hostile extractor output and search-only checks."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import import_sources
from app import playlist_matching as matching
from app.library import LibraryError


TRACK = {"title": "Silver Moon", "artists": ["Elena Reed"], "artist": "Elena Reed", "album": "Daylight", "duration": 200.7}
ENTRY = {"id": "BaW_jenozKc", "title": "Elena Reed - Silver Moon (Official Audio)", "channel": "Elena Reed - Topic", "duration": 201, "ie_key": "Youtube", "url": "https://www.youtube.com/watch?v=BaW_jenozKc"}


class CandidateTests(unittest.TestCase):
    def candidate(self, fields=None, track=None):
        return matching._candidate(TRACK if track is None else track, dict(ENTRY, **(fields or {})))

    def test_matching_title_artist_and_duration_are_strong(self):
        result = self.candidate()
        self.assertEqual(result["confidence"], "strong")
        self.assertEqual(result["score"], 100)
        self.assertEqual(result["url"], ENTRY["url"])
        self.assertEqual(len(result["reasons"]), 3)

    def test_wrong_artist_with_identical_title_and_duration_needs_review(self):
        result = self.candidate({"title": "Other Singer - Silver Moon", "channel": "Other Singer"})
        self.assertEqual(result["confidence"], "review")
        self.assertLess(result["score"], 75)

    def test_unrelated_channel_and_album_words_do_not_make_a_match_strong(self):
        result = self.candidate({"channel": "Fans of Elena Reed"})
        self.assertEqual(result["confidence"], "review")
        result = self.candidate({"title": "Elena Reed - Silver Moon Daylight"})
        self.assertEqual(result["confidence"], "review")

    def test_wrong_versions_never_strong_even_with_same_duration(self):
        for version in ("Live", "Live at Wembley", "Remix", "Cover", "Sped Up", "Slowed + Reverb", "Instrumental", "Remastered", "Acoustic", "Extended", "Radio Edit", "Demo"):
            with self.subTest(version=version):
                result = self.candidate({"title": "Elena Reed - Silver Moon (" + version + ")"})
                self.assertEqual(result["confidence"], "review")
                self.assertTrue(any("version" in reason for reason in result["reasons"]))

    def test_expected_version_must_be_present(self):
        track = dict(TRACK, title="Silver Moon - Live")
        self.assertEqual(self.candidate(track=track)["confidence"], "review")
        result = self.candidate({"title": "Elena Reed - Silver Moon - Live (Official Audio)"}, track)
        self.assertEqual(result["confidence"], "strong")

    def test_title_containing_live_is_not_itself_live_recording(self):
        track = dict(TRACK, title="Live Forever")
        result = self.candidate({"title": "Elena Reed - Live Forever (Official Audio)"}, track)
        self.assertEqual(result["confidence"], "strong")
        result = self.candidate({"title": "Elena Reed - Live Forever (Live)"}, track)
        self.assertEqual(result["confidence"], "review")

    def test_missing_or_different_duration_needs_review(self):
        for value in (None, True, -1, 0, "201", float("nan"), float("inf"), 230, 21601):
            with self.subTest(duration=value):
                self.assertEqual(self.candidate({"duration": value})["confidence"], "review")
        self.assertEqual(self.candidate(track=dict(TRACK, duration=None))["confidence"], "review")

    def test_missing_artist_and_missing_collaborator_need_review(self):
        for track in (dict(TRACK, artists=[], artist=""), dict(TRACK, artists=["Elena Reed", "Marcus Hill"])):
            with self.subTest(track=track):
                self.assertEqual(self.candidate(track=track)["confidence"], "review")

    def test_shared_words_and_additional_song_title_never_strong(self):
        for title in ("Elena Reed - Moon Silver", "Elena Reed - Silver Moon Rising", "Elena Reed - Silver"):
            with self.subTest(title=title):
                self.assertEqual(self.candidate({"title": title})["confidence"], "review")

    def test_diacritics_punctuation_and_topic_channels(self):
        track = dict(TRACK, title="L'été", artists=["Éléna Reed"])
        result = self.candidate({"title": "L’ete (Official Audio)", "channel": "Elena ReedVEVO"}, track)
        self.assertEqual(result["confidence"], "strong")

    def test_hostile_fields_and_nonpublic_results_are_skipped(self):
        for fields in ({"id": "../../file"}, {"id": []}, {"title": {}}, {"_type": "playlist"}, {"ie_key": "Generic"}, {"url": "file:///etc/passwd"}, {"url": "https://evil.example/BaW_jenozKc"}, {"webpage_url": "https://youtube.com/watch?v=abcdefghijk"}, {"url": []}, {"is_live": True}, {"live_status": "is_upcoming"}, {"availability": "private"}):
            with self.subTest(fields=fields):
                self.assertIsNone(self.candidate(fields))
        self.assertIsNone(matching._candidate(TRACK, []))

    def test_display_fields_are_bounded_and_control_characters_removed(self):
        result = self.candidate({"title": "Elena Reed\n" + "x" * 900, "channel": ["untrusted"], "duration": 201})
        self.assertEqual(len(result["title"]), 300)
        self.assertNotIn("\n", result["title"])
        self.assertEqual(result["channel"], "")


class SearchTests(unittest.TestCase):
    def run_search(self, info, track=TRACK):
        with patch.object(import_sources, "_downloader", return_value="/tools/yt-dlp"), patch.object(import_sources, "executable", side_effect=lambda name: "/tools/" + name), patch.object(import_sources, "_run_tool", return_value=subprocess.CompletedProcess([], 0, json.dumps(info), "")) as run:
            result = matching.search_candidates(track)
        return result, run

    def test_search_is_metadata_only_uses_safe_flags_and_bounded_runner(self):
        result, run = self.run_search({"entries": [ENTRY]})
        command = run.call_args.args[0]
        self.assertEqual(len(result), 1)
        for option in ("--ignore-config", "--no-config-locations", "--no-plugin-dirs", "--no-cookies", "--no-cookies-from-browser", "--no-remote-components", "--flat-playlist", "--skip-download"):
            self.assertIn(option, command)
        self.assertEqual(command[command.index("--use-extractors") + 1], "youtube:search")
        self.assertEqual(command[-2:], ["--", "ytsearch5:Silver Moon Elena Reed audio"])
        self.assertEqual(run.call_args.kwargs["timeout"], matching.SEARCH_TIMEOUT)
        self.assertNotIn("--no-simulate", command)

    def test_scores_sort_then_stable_url_and_duplicates_are_removed(self):
        lower = dict(ENTRY, title="Other Singer - Silver Moon", channel="Other Singer", id="abcdefghijk", url="https://youtu.be/abcdefghijk")
        result, _ = self.run_search({"entries": [lower, ENTRY, ENTRY, None]})
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["url"], ENTRY["url"])
        self.assertEqual(result[0]["confidence"], "strong")

    def test_invalid_json_and_shapes_have_clear_errors(self):
        for info in ({}, {"entries": {}}, [], {"entries": [None] * 101}):
            with self.subTest(info=info), self.assertRaises(LibraryError):
                self.run_search(info)
        with patch.object(import_sources, "_base_command", return_value=["yt-dlp", "--use-extractors", "youtube"]), patch.object(import_sources, "_run_tool", return_value=subprocess.CompletedProcess([], 0, "not JSON", "")):
            with self.assertRaisesRegex(LibraryError, "unreadable"):
                matching.search_candidates(TRACK)

    def test_network_error_does_not_expose_third_party_details(self):
        with patch.object(import_sources, "_base_command", return_value=["yt-dlp", "--use-extractors", "youtube"]), patch.object(import_sources, "_run_tool", return_value=subprocess.CompletedProcess([], 1, "", "https://host/?secret=value")):
            with self.assertRaises(LibraryError) as caught:
                matching.search_candidates(TRACK)
        self.assertNotIn("secret", str(caught.exception))

    def test_cancellation_event_passes_through(self):
        event = threading.Event()
        with patch.object(import_sources, "_base_command", return_value=["yt-dlp", "--use-extractors", "youtube"]), patch.object(import_sources, "_run_tool", side_effect=LibraryError("Preparation cancelled.")) as run:
            with self.assertRaisesRegex(LibraryError, "cancelled"):
                matching.search_candidates(TRACK, event)
        self.assertIs(run.call_args.kwargs["cancel_event"], event)

    def test_invalid_track_does_not_start_tool(self):
        with patch.object(import_sources, "_run_tool") as run:
            for track in (None, [], {}, {"title": []}, {"title": "\n"}):
                with self.subTest(track=track), self.assertRaises(LibraryError):
                    matching.search_candidates(track)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
