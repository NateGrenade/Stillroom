"""Deterministic recommendation metadata, feedback, dedup and safe discovery."""

from __future__ import annotations

import copy
import itertools
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import recommendation_catalog as catalog
from app import import_sources, playlist_matching
from app.library import LibraryError


def song(title="Silver Moon", artist="Elena Reed", video="aaaaaaaaaaa", **fields):
    return dict({"id": video, "title": title, "artist": artist, "album": "Daylight",
                 "duration": 200, "tags": ["indie", "dream pop"],
                 "sourceUrl": "https://www.youtube.com/watch?v=" + video, "seedIds": []}, **fields)


class IdentityTests(unittest.TestCase):
    def test_presentation_labels_unicode_and_artist_prefix_are_same_recording(self):
        original = song("L'été", "Éléna Reed")
        for title in ("L’ete", "Elena Reed - L'ete (Official Audio)", "L'ete [Official Music Video]", "L'ete Official Audio"):
            with self.subTest(title=title):
                self.assertEqual(catalog.canonical_key(original), catalog.canonical_key(song(title, "Elena Reed - Topic")))

    def test_versions_and_meaningful_title_words_are_not_conflated(self):
        original = song()
        versions = [song("Silver Moon (" + version + ")") for version in
                    ("Live", "Live at Wembley", "RAC Remix", "Cover", "Sped Up", "Slowed + Reverb", "Instrumental", "Remastered", "Acoustic", "Extended", "Radio Edit", "Demo")]
        keys = [catalog.canonical_key(track) for track in [original, *versions, song("Silver Moon Rising")]]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertNotEqual(catalog.canonical_key(original), catalog.canonical_key(song(artist="Other Singer")))

    def test_unknown_artists_do_not_conflate_same_title_by_different_sources(self):
        self.assertNotEqual(catalog.canonical_key(song(artist="")), catalog.canonical_key(song(artist="", video="bbbbbbbbbbb")))
        self.assertEqual(catalog.canonical_key(None), "")
        self.assertEqual(catalog.canonical_key({}), "")

    def test_actual_titles_audio_and_live_forever_are_preserved(self):
        self.assertIn("|audio|", catalog.canonical_key(song("Audio")))
        self.assertTrue(catalog.canonical_key(song("Live Forever")).endswith("|live forever|"))


class RankingTests(unittest.TestCase):
    def test_exact_score_explains_every_available_feature(self):
        seed = song("Silver Moon")
        candidate = song("Silver Moon Rising", video="bbbbbbbbbbb")
        result = catalog.rank_candidates([candidate], [seed], [])[0]
        self.assertEqual(result["score"], 96.67)  # 40 + 25 + 10 + 2/3*10 + 15
        for reason in ("Same artist: +40.", "Shared tags: +25.", "Same album: +10.", "Similar duration: +15."):
            self.assertIn(reason, result["reasons"])
        self.assertRegex(result["id"], r"^[0-9a-f]{32}$")

    def test_all_input_permutations_keep_scores_reasons_and_ties_stable(self):
        seeds = [song(), song("New Stars", "Marcus Hill", "ccccccccccc")]
        candidates = [song("Red Sky", video="bbbbbbbbbbb"), song("Blue Sky", "Marcus Hill", "ddddddddddd"), song("Late Night", "Other", "eeeeeeeeeee")]
        feedback = [dict(song("Another", video="fffffffffff"), verdict="up"), dict(song("Different", "Other", "ggggggggggg"), decision="down")]
        expected = catalog.rank_candidates(candidates, seeds, feedback)
        for order in itertools.permutations(candidates):
            for seed_order in itertools.permutations(seeds):
                self.assertEqual(catalog.rank_candidates(order, seed_order, list(reversed(feedback))), expected)

    def test_exact_sources_and_owned_recording_aliases_are_excluded(self):
        owned = song()
        candidates = [song("Completely Different", "Other", sourceUrl="https://youtu.be/aaaaaaaaaaa?t=5"),
                      song("Elena Reed - Silver Moon (Official Audio)", video="bbbbbbbbbbb"),
                      song("Silver Moon (Live)", video="ccccccccccc")]
        result = catalog.rank_candidates(candidates, [owned], [])
        self.assertEqual([row["title"] for row in result], ["Silver Moon (Live)"])

    def test_source_alias_bridges_recording_aliases_and_combines_provenance(self):
        candidates = [song("One", video="aaaaaaaaaaa", seedIds=["seed-1"]),
                      song("Two", video="aaaaaaaaaaa", seedIds=["seed-2"]),
                      song("Two", video="bbbbbbbbbbb", seedIds=["seed-3"])]
        expected = catalog.rank_candidates(candidates, [], [])
        self.assertEqual(len(expected), 1)
        self.assertEqual(expected[0]["seedIds"], ["seed-1", "seed-2", "seed-3"])
        for order in itertools.permutations(candidates):
            self.assertEqual(catalog.rank_candidates(order, [], []), expected)
        self.assertEqual(catalog.rank_candidates(candidates, [song("Two", video="zzzzzzzzzzz")], []), [])

    def test_down_suppresses_aliases_and_repeated_feedback_does_not_compound(self):
        disliked = dict(song("Bad Song", video="bbbbbbbbbbb"), rating="down")
        alias = song("Elena Reed - Bad Song (Official Audio)", video="ccccccccccc")
        candidate = song("New Song", video="ddddddddddd")
        seed = song("Owned", tags=[])
        base = catalog.rank_candidates([candidate], [seed], [])[0]["score"]
        once = catalog.rank_candidates([alias, candidate], [seed], [disliked])
        many = catalog.rank_candidates([alias, candidate], [seed], [disliked, disliked, dict(alias, verdict="down")])
        self.assertEqual(len(once), 1)
        self.assertEqual(once, many)
        self.assertLess(once[0]["score"], base)
        self.assertIn("Disliked “Bad Song” shares artist, tags: -35.", once[0]["reasons"])

    def test_like_increases_rank_and_neutral_records_have_no_effect(self):
        candidate = song("New Song", video="bbbbbbbbbbb", tags=[])
        seed = song("Owned", tags=[], album="")
        base = catalog.rank_candidates([candidate], [seed], [])
        liked = dict(song("Good Song", video="ccccccccccc", tags=[]), decision="up")
        positive = catalog.rank_candidates([candidate], [seed], [liked])
        self.assertEqual(positive[0]["score"], base[0]["score"] + 12)
        for value in (None, "neutral", "skip", "completed", ""):
            self.assertEqual(catalog.rank_candidates([candidate], [seed], [dict(liked, decision=value)]), base)

    def test_conflicting_feedback_aliases_favor_explicit_rejection(self):
        candidate = song("Bad Song", video="bbbbbbbbbbb")
        positive = dict(candidate, verdict="up")
        negative = dict(candidate, verdict="down")
        self.assertEqual(catalog.rank_candidates([candidate], [], [positive, negative]), [])
        self.assertEqual(catalog.rank_candidates([candidate], [], [negative, positive]), [])

    def test_dislike_adjusts_version_without_artist_overlap_and_feedback_is_capped(self):
        candidate = song("New Song (Live)", video="bbbbbbbbbbb", tags=[])
        seed = song("Owned", album="", tags=[])
        dislike = dict(song("Other (Live)", "Other Artist", "ccccccccccc", tags=[]), verdict="down")
        base = catalog.rank_candidates([candidate], [seed], [])[0]["score"]
        adjusted = catalog.rank_candidates([candidate], [seed], [dislike])[0]
        self.assertEqual(adjusted["score"], base - 8)
        votes = [dict(song("Bad " + str(i), video=str(i) * 11), verdict="down") for i in range(8)]
        capped = catalog.rank_candidates([candidate], [seed], votes)[0]
        self.assertEqual(capped["score"], 0)
        self.assertIn("Feedback adjustment capped at -60.", capped["reasons"])

    def test_missing_or_hostile_metadata_does_not_score_or_escape(self):
        candidate = song("Lonely", artist="", album="", tags=[], duration=None)
        result = catalog.rank_candidates([None, {}, song(sourceUrl="file:///etc/passwd"), candidate], [{"title": "Other"}], [])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["score"], 0)
        self.assertEqual(result[0]["reasons"], ["No matching library metadata: +0."])
        for duration in (float("nan"), float("inf"), True, "200", -1):
            row = catalog.rank_candidates([dict(candidate, duration=duration)], [], [])[0]
            self.assertIsNone(row["duration"])

    def test_inputs_are_unchanged_and_lifecycle_fields_are_preserved(self):
        candidates, tracks, feedback = [song("New", video="bbbbbbbbbbb", status="limbo")], [song()], []
        before = copy.deepcopy((candidates, tracks, feedback))
        result = catalog.rank_candidates(candidates, tracks, feedback)
        self.assertEqual(before, (candidates, tracks, feedback))
        self.assertEqual(result[0]["status"], "limbo")


class DiscoveryTests(unittest.TestCase):
    def test_seed_selection_is_bounded_stable_and_does_not_overweight_owned_loop(self):
        tracks = [song("One", "A", "aaaaaaaaaaa"), song("One", "A", "aaaaaaaaaaa", id="loop", kind="loop"),
                  song("Two", "B", "bbbbbbbbbbb"), song("Three", "B", "ccccccccccc"),
                  song("Four", "C", "ddddddddddd"), song("Five", "D", "eeeeeeeeeee")]
        with patch.object(playlist_matching, "search_candidates", return_value=[]) as search:
            catalog.discover_candidates(tracks)
            queries = [call.args[0]["title"] for call in search.call_args_list]
            self.assertEqual(queries, ["B", "A", "C"])
            search.reset_mock()
            catalog.discover_candidates(list(reversed(tracks)))
            self.assertEqual([call.args[0]["title"] for call in search.call_args_list], queries)

    def test_single_seed_strips_promo_excludes_owned_and_never_copies_genre(self):
        seed = song()
        entries = [{"title": "Elena Reed - Another Song (Official Audio)", "channel": "Elena Reed - Topic", "duration": 203, "url": "https://youtu.be/bbbbbbbbbbb"},
                   {"title": "Silver Moon", "channel": "Elena Reed - Topic", "duration": 200, "url": "https://youtu.be/ccccccccccc"}]
        progress = []
        with patch.object(playlist_matching, "search_candidates", return_value=entries) as search:
            result = catalog.discover_candidates([seed], progress=progress.append)
        self.assertEqual(search.call_count, 1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "Another Song")
        self.assertEqual(result[0]["artist"], "Elena Reed")
        self.assertEqual(result[0]["tags"], [])
        self.assertEqual(result[0]["album"], "")
        self.assertEqual(result[0]["seedIds"], [seed["id"]])
        self.assertEqual(len(progress), 1)

    def test_extra_results_compilations_and_long_recordings_are_excluded(self):
        entries = [{"title": title, "channel": "Elena Reed", "duration": duration,
                    "url": "https://youtu.be/" + str(index) * 11} for index, (title, duration) in enumerate([
                        ("Full Album", 200), ("Best of Elena Reed", 200), ("Long song", 901), ("New Song", 200), ("Another Song", 200), ("Sixth song", 200)])]
        with patch.object(playlist_matching, "search_candidates", return_value=entries):
            result = catalog.discover_candidates([song()])
        self.assertEqual({row["title"] for row in result}, {"New Song", "Another Song"})

    def test_unrelated_search_hits_are_rejected_even_when_duration_matches(self):
        seed = song("Nowhere Around (Credits)", "Max Specter", duration=100)
        entries = [
            {"title": "PREMIERE⚡Max Duret - Specter [Art Bei Ton]", "channel": "Electric Shapes", "duration": 100, "url": "https://youtu.be/bbbbbbbbbbb"},
            {"title": "Just Asking at Night", "channel": "Max Specter", "duration": 250, "url": "https://youtu.be/ccccccccccc"},
            {"title": "No, I'm Not a Human Theme // Max Specter (Slowed and reverb)", "channel": "astrealoves_it", "duration": 229, "url": "https://youtu.be/ddddddddddd"},
        ]
        with patch.object(playlist_matching, "search_candidates", return_value=entries):
            result = catalog.discover_candidates([seed])
        self.assertEqual(len(result), 2)
        self.assertEqual({row["artist"] for row in result}, {"Max Specter"})
        self.assertIn("No, I'm Not a Human Theme (Slowed and reverb)", {row["title"] for row in result})
        self.assertTrue(all(row["score"] >= 40 for row in result))

    def test_artist_names_in_prose_do_not_get_guessed_from_query(self):
        entry = {"title": "I like Max Specter a lot", "channel": "Music Fan", "duration": 200, "url": "https://youtu.be/bbbbbbbbbbb"}
        with patch.object(playlist_matching, "search_candidates", return_value=[entry]):
            self.assertEqual(catalog.discover_candidates([song(artist="Max Specter")]), [])

    def test_cancelled_discovery_stops_before_search_and_after_last_search(self):
        event = threading.Event()
        event.set()
        with patch.object(playlist_matching, "search_candidates") as search:
            with self.assertRaisesRegex(LibraryError, "cancelled"):
                catalog.discover_candidates([song()], event)
            search.assert_not_called()
        event.clear()
        def finish_cancel(*args, **kwargs):
            self.assertIs(kwargs["cancel_event"], event)
            event.set()
            return []
        with patch.object(playlist_matching, "search_candidates", side_effect=finish_cancel):
            with self.assertRaisesRegex(LibraryError, "cancelled"):
                catalog.discover_candidates([song()], event)

    def test_empty_library_and_network_errors_surface(self):
        with patch.object(playlist_matching, "search_candidates", side_effect=LibraryError("YouTube search is unavailable.", 502)) as search:
            with self.assertRaisesRegex(LibraryError, "Add a song"):
                catalog.discover_candidates([])
            search.assert_not_called()
            with self.assertRaisesRegex(LibraryError, "unavailable"):
                catalog.discover_candidates([song()])

    def test_actual_search_command_is_metadata_only_and_bounded(self):
        entry = {"id": "bbbbbbbbbbb", "title": "Another Song", "channel": "Elena Reed - Topic", "duration": 201}
        tool_result = subprocess.CompletedProcess([], 0, json.dumps({"entries": [entry]}), "")
        with patch.object(import_sources, "_downloader", return_value="/tools/yt-dlp"), \
                patch.object(import_sources, "executable", side_effect=lambda name: "/tools/" + name), \
                patch.object(import_sources, "_run_tool", return_value=tool_result) as run:
            result = catalog.discover_candidates([song()])
        self.assertEqual(len(result), 1)
        self.assertEqual(run.call_count, 1)
        command = run.call_args.args[0]
        for option in ("--flat-playlist", "--skip-download", "--no-cookies", "--no-cookies-from-browser", "--ignore-config", "--no-plugin-dirs", "--no-remote-components"):
            self.assertIn(option, command)
        self.assertEqual(command[-2:], ["--", "ytsearch5:Elena Reed audio"])
        self.assertNotIn("--no-simulate", command)
        self.assertEqual(run.call_args.kwargs["timeout"], playlist_matching.SEARCH_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
