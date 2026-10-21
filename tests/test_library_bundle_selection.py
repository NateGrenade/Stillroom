"""Selection/snapshot API regressions; generated disposable libraries only."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

from tests import test_library_bundle as fixtures

export = fixtures.export
sha = fixtures.sha
read_bundle = fixtures.read_bundle


class SelectionTests(fixtures.ExportTests):
    def selected(self, mode="tracks", ids=None):
        return {"mode": mode, "trackIds": ids or [self.second, self.first]}

    def source_hashes(self):
        return {p.relative_to(self.root).as_posix(): sha(p) for p in self.root.rglob("*") if p.is_file()}

    def test_single_loop_preserves_original_bytes_and_bounds_without_playlists(self):
        before = self.source_hashes()
        result = export.LibraryExporter(self.root).export(self.output, selection={"mode": "track", "trackId": self.second})
        manifest, files = read_bundle(self.output)
        self.assertEqual((1, 0, 1), (result["tracks"], result["playlists"], result["files"]))
        self.assertEqual([self.second], [t["id"] for t in manifest["tracks"]])
        self.assertEqual([], manifest["playlists"])
        track = manifest["tracks"][0]
        self.assertEqual((.6, 1, .2, .8), (track["duration"], track["fileDuration"], track["loopStart"], track["loopEnd"]))
        self.assertEqual(self.audio[1].read_bytes(), files[track["audioFile"]])
        self.assertEqual(before, self.source_hashes())

    def test_complete_playlist_retains_id_name_exact_order_and_only_that_playlist(self):
        other = "d" * 32
        self.change("INSERT INTO playlists VALUES (?,?,?,?)", (other, "Other", "other", "2026-10-21"))
        self.change("INSERT INTO playlist_tracks VALUES (?,?,?)", (other, self.first, 0))
        export.export_library(self.root, self.output, selection={"mode": "playlist", "playlistId": self.playlist})
        manifest, files = read_bundle(self.output)
        self.assertEqual([self.second, self.first], [t["id"] for t in manifest["tracks"]])
        self.assertEqual([{"id": self.playlist, "name": "Evening", "trackIds": [self.second, self.first]}], manifest["playlists"])
        self.assertEqual(["audio/" + self.audio[1].name, "audio/" + self.audio[0].name], list(files))

    def test_custom_selection_order_is_exact_without_partial_or_fabricated_playlists(self):
        export.export_library(self.root, self.output, selection=self.selected())
        manifest, files = read_bundle(self.output)
        self.assertEqual([self.second, self.first], [t["id"] for t in manifest["tracks"]])
        self.assertEqual([], manifest["playlists"])
        self.assertEqual([t["audioFile"] for t in manifest["tracks"]], list(files))

    def test_explicit_all_is_byte_identical_to_legacy_all_and_keeps_empty_playlists(self):
        self.change("INSERT INTO playlists VALUES (?,?,?,?)", ("e" * 32, "Empty", "empty", "2026-10-22"))
        alternate = self.folder / "explicit.stillroomlibrary"
        export.export_library(self.root, self.output)
        export.export_library(self.root, alternate, selection={"mode": "all"})
        self.assertEqual(self.output.read_bytes(), alternate.read_bytes())
        self.assertEqual([], read_bundle(alternate)[0]["playlists"][1]["trackIds"])

    def test_selection_isolation_does_not_open_or_export_unselected_invalid_audio_metadata(self):
        self.audio[0].write_bytes(b"UNSELECTED-PRIVATE-MATERIAL")
        self.change("UPDATE tracks SET title=?,audio_filename=? WHERE id=?", ("bad\u202etitle", "../unsafe.wav", self.first))
        original = export.open_audio
        names = []
        def opened(fd, name):
            names.append(name)
            return original(fd, name)
        with mock.patch.object(export, "open_audio", side_effect=opened):
            export.export_library(self.root, self.output, selection={"mode": "tracks", "trackIds": [self.second]})
        self.assertTrue(names)
        self.assertEqual({self.audio[1].name}, set(names))
        self.assertNotIn(b"UNSELECTED-PRIVATE-MATERIAL", self.output.read_bytes())

    def test_selection_rejects_unknown_fields_roots_duplicates_types_and_partial_missing_ids(self):
        bad = [{"mode": "all", "root": str(self.root)}, {"mode": "all", "output": str(self.output)},
               {"mode": "tracks", "trackIds": []}, {"mode": "tracks", "trackIds": [self.first, self.first]},
               {"mode": "tracks", "trackIds": self.first}, {"mode": "tracks", "trackIds": [self.first, "f" * 32]},
               {"mode": "track", "trackId": "../private"}, {"mode": "playlist", "playlistId": "f" * 32},
               {"mode": "track", "trackId": self.first, "playlistId": self.playlist},
               {"mode": "unknown"}, {"mode": []}, "all", {}, {"mode": "track", "trackId": ""}]
        before = self.source_hashes()
        for selection in bad:
            with self.subTest(selection=selection):
                self.assert_refused(selection=selection)
        self.assertEqual(before, self.source_hashes())

    def test_selected_empty_or_duplicate_membership_playlist_fails_closed(self):
        self.change("INSERT INTO playlists VALUES (?,?,?,?)", ("e" * 32, "Empty", "empty", "2026-10-22"))
        self.assert_refused(selection={"mode": "playlist", "playlistId": "e" * 32})
        self.change("INSERT INTO playlist_tracks VALUES (?,?,?)", (self.playlist, self.first, 99))
        self.assert_refused(selection={"mode": "playlist", "playlistId": self.playlist})

    def test_preflight_reports_exact_counts_sizes_disk_and_no_output_or_source_changes(self):
        before = self.source_hashes()
        bound = export.LibraryExporter(self.root)
        review = bound.preflight(self.output, selection=self.selected(), settings={"playbackRate": 1.37, "reverb": .4})
        self.assertFalse(self.output.exists())
        self.assertEqual([], list(self.folder.glob(".pocket-export-*")))
        self.assertEqual((2, 0, 2), (review["tracks"], review["playlists"], review["files"]))
        self.assertEqual(sum(p.stat().st_size for p in self.audio), review["audioBytes"])
        self.assertEqual(review["bundleBytes"] + export.SAFETY_BYTES, review["requiredBytes"])
        self.assertGreaterEqual(review["availableBytes"], review["requiredBytes"])
        self.assertRegex(review["reviewToken"], r"\A[0-9a-f]{64}\Z")
        result = bound.export(self.output, selection=self.selected(), settings={"playbackRate": 1.37, "reverb": .4}, expected_review_token=review["reviewToken"])
        self.assertEqual(self.output.stat().st_size, review["bundleBytes"])
        self.assertEqual(review["reviewToken"], result["reviewToken"])
        self.assertEqual(before, self.source_hashes())

    def test_review_token_refuses_changed_metadata_selection_settings_and_malformed_tokens(self):
        bound = export.LibraryExporter(self.root)
        selection = {"mode": "track", "trackId": self.first}
        review = bound.preflight(self.output, selection=selection)
        self.assert_refused(selection={"mode": "track", "trackId": self.second}, expected_review_token=review["reviewToken"])
        self.assert_refused(selection=selection, settings={"reverb": .5}, expected_review_token=review["reviewToken"])
        for bad in ("", "z" * 64, 1, "f" * 65):
            self.assert_refused(selection=selection, expected_review_token=bad)
        self.change("UPDATE tracks SET title='Changed after review' WHERE id=?", (self.first,))
        self.assert_refused(selection=selection, expected_review_token=review["reviewToken"])

    def test_progress_is_bounded_phase_local_and_does_not_expose_extra_metadata(self):
        events = []
        export.export_library(self.root, self.output, selection=self.selected(), progress=events.append)
        self.assertEqual({"preparing", "copying", "verifying", "publishing"}, {e["phase"] for e in events})
        for phase in {e["phase"] for e in events}:
            selected = [e for e in events if e["phase"] == phase]
            self.assertEqual(sorted(e["completedBytes"] for e in selected), [e["completedBytes"] for e in selected])
        for event in events:
            self.assertEqual({"phase", "completedBytes", "totalBytes", "completedFiles", "totalFiles"}, set(event))
            self.assertTrue(0 <= event["completedBytes"] <= event["totalBytes"])
            self.assertTrue(0 <= event["completedFiles"] <= event["totalFiles"])

    def test_cooperative_cancellation_preparing_copying_and_final_publication_keeps_source(self):
        before = self.source_hashes()
        for phase in ("preparing", "copying", "publishing"):
            output = self.folder / (phase + ".stillroomlibrary")
            cancel = False
            def progress(event):
                nonlocal cancel
                if event["phase"] == phase:
                    cancel = True
            with self.assertRaises(export.ExportCancelled):
                export.LibraryExporter(self.root).export(output, selection=self.selected(), cancelled=lambda: cancel, progress=progress)
            self.assertFalse(output.exists())
            self.assertEqual([], list(self.folder.glob(".pocket-export-*.tmp")))
        self.assertEqual(before, self.source_hashes())

    def test_preflight_cancellation_and_low_destination_space_create_nothing(self):
        cancelled = False
        def progress(event):
            nonlocal cancelled
            cancelled = True
        with self.assertRaises(export.ExportCancelled):
            export.preflight_export(self.root, self.output, progress=progress, cancelled=lambda: cancelled)
        with mock.patch.object(export.shutil, "disk_usage", return_value=type("Usage", (), {"free": 0})()):
            with self.assertRaises(export.ExportError):
                export.preflight_export(self.root, self.output, selection=self.selected())
        self.assertFalse(self.output.exists())
        self.assertEqual([], list(self.folder.glob(".pocket-export-*")))

    def test_earlier_selected_audio_mutation_during_later_copy_is_refused(self):
        changed = False
        def progress(event):
            nonlocal changed
            if not changed and event["phase"] == "copying" and event["completedFiles"] == 1:
                changed = True
                with self.audio[0].open("r+b") as file:
                    file.seek(100); file.write(b"changed")
        self.assert_refused(progress=progress)
        self.assertTrue(changed)

    def test_preflight_rejects_earlier_audio_mutation_after_later_file_hash(self):
        changed = False
        def progress(event):
            nonlocal changed
            if not changed and event["phase"] == "preparing" and event["completedFiles"] == 2:
                changed = True
                with self.audio[0].open("r+b") as file:
                    file.seek(100); file.write(b"changed")
        with self.assertRaises(export.ExportError):
            export.preflight_export(self.root, self.output, progress=progress)
        self.assertFalse(self.output.exists())

    def test_database_change_at_last_progress_callback_is_refused(self):
        with contextlib.closing(sqlite3.connect(self.database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            def progress(event):
                if event["phase"] == "publishing":
                    writer.execute("UPDATE playlists SET name='Edited just before publication'")
                    writer.commit()
            self.assert_refused(progress=progress)

    def test_stopped_wal_database_snapshot_never_creates_source_sidecars(self):
        with contextlib.closing(sqlite3.connect(self.database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("UPDATE playlists SET name='Closed WAL library'")
            writer.commit()
        self.assertFalse(Path(str(self.database) + "-wal").exists())
        self.assertFalse(Path(str(self.database) + "-shm").exists())
        before = self.source_hashes()
        original = export.manifest_values
        snapshots = []
        def inspect(connection, *args):
            self.assertEqual(before, self.source_hashes())
            snapshot = Path(connection.execute("PRAGMA database_list").fetchone()[2])
            self.assertFalse(snapshot.is_relative_to(self.root))
            self.assertEqual(0o600, snapshot.stat().st_mode & 0o777)
            snapshots.append(snapshot.parent)
            return original(connection, *args)
        with mock.patch.object(export, "manifest_values", side_effect=inspect):
            export.export_library(self.root, self.output)
        self.assertEqual(before, self.source_hashes())
        self.assertTrue(snapshots)
        self.assertTrue(all(not p.exists() for p in snapshots))
        self.assertEqual("Closed WAL library", read_bundle(self.output)[0]["playlists"][0]["name"])

    def test_dangling_or_unexpected_sidecar_symlinks_are_rejected_without_following(self):
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.database) + suffix)
            sidecar.symlink_to(self.folder / "missing-sidecar")
            self.assert_refused()
            self.assertTrue(sidecar.is_symlink())
            sidecar.unlink()

    def test_private_snapshot_copy_failure_preserves_source_and_no_completed_output(self):
        before = self.source_hashes()
        with mock.patch.object(export, "copy_snapshot_file", side_effect=OSError("Injected local snapshot failure")):
            self.assert_refused()
        self.assertEqual(before, self.source_hashes())

    def test_staged_output_substitution_is_never_published_or_deleted_as_owned(self):
        sentinel = b"UNRELATED-FILE-MUST-SURVIVE"
        replacement = None
        def progress(event):
            nonlocal replacement
            if event["phase"] == "publishing":
                replacement = next(self.folder.glob(".pocket-export-*.tmp"))
                replacement.rename(self.folder / "owned-staged-backup")
                replacement.write_bytes(sentinel)
        with self.assertRaises(export.ExportError):
            export.export_library(self.root, self.output, progress=progress)
        self.assertFalse(self.output.exists())
        self.assertIsNotNone(replacement)
        self.assertEqual(sentinel, replacement.read_bytes())

    def test_no_caller_callbacks_run_between_final_staged_identity_and_publication(self):
        events = []
        original_stat, original_link = export.os.stat, export.os.link
        def inspected(path, *args, **kwargs):
            result = original_stat(path, *args, **kwargs)
            if str(path).startswith(".pocket-export-") and kwargs.get("follow_symlinks") is False:
                events.append("staged-identity")
            return result
        def linked(*args, **kwargs):
            events.append("publication")
            return original_link(*args, **kwargs)
        def cancelled():
            events.append("callback")
            return False
        with mock.patch.object(export.os, "stat", side_effect=inspected), mock.patch.object(export.os, "link", side_effect=linked):
            export.export_library(self.root, self.output, cancelled=cancelled)
        commit = events.index("publication")
        self.assertEqual("staged-identity", events[commit - 1])
        self.assertNotIn("callback", events[commit + 1:])

    def test_legacy_cli_and_selection_preflight_and_reviewed_cli_export(self):
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            self.assertEqual(0, export.main(["--root", str(self.root), "--output", str(self.output), "--preflight", "--playlist", self.playlist]))
        review = json.loads(captured.getvalue())
        self.assertFalse(self.output.exists())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, export.main(["--root", str(self.root), "--output", str(self.output), "--playlist", self.playlist, "--expected-review-token", review["reviewToken"]]))
        self.assertEqual([self.second, self.first], [t["id"] for t in read_bundle(self.output)[0]["tracks"]])
        alternate = self.folder / "legacy.stillroomlibrary"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, export.main(["--root", str(self.root), "--output", str(alternate)]))
        self.assertEqual(1, len(read_bundle(alternate)[0]["playlists"]))

    def test_cli_empty_explicit_track_fails_instead_of_exporting_all(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, export.main(["--root", str(self.root), "--output", str(self.output), "--track", ""]))
        self.assertFalse(self.output.exists())


def load_tests(loader, tests, pattern):
    # Reuse disposable fixture setup, without counting inherited baseline tests twice.
    return unittest.TestSuite(SelectionTests(name) for name in sorted(SelectionTests.__dict__) if name.startswith("test_"))


def load_tests(loader, tests, pattern):
    # The base fixture has its own module; run only newly declared selections here.
    return unittest.TestSuite(SelectionTests(name) for name in sorted(SelectionTests.__dict__) if name.startswith("test_"))
