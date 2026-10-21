"""Meaningful tests for the read-only desktop transfer path; temporary fixtures only."""
from pathlib import Path
import contextlib
import hashlib
import importlib.util
import json
import os
import sqlite3
import struct
import tempfile
import unittest
from unittest import mock
import wave

WORK = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pocket_export", WORK / "Desktop" / "export_library.py")
export = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(export)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_bundle(path):
    with path.open("rb") as stream:
        assert stream.read(8) == export.MAGIC
        manifest_size = struct.unpack(">I", stream.read(4))[0]
        manifest = json.loads(stream.read(manifest_size))
        files = {item["path"]: stream.read(item["byteCount"]) for item in manifest["files"]}
        assert not stream.read(1)
        return manifest, files


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="transfer-export-test-", dir=WORK / "Tests")
        self.folder = Path(self.temporary.name).resolve()
        self.root = self.folder / "Dev"
        (self.root / "audio").mkdir(parents=True)
        (self.root / "data").mkdir()
        # These must never be walked/copied by the exporter.
        (self.root / "data" / ".youtube-auth").mkdir()
        (self.root / "data" / ".youtube-auth" / "cookies.txt").write_text("DO-NOT-COPY-ACCOUNT-SECRET")
        (self.root / "runtime").mkdir()
        (self.root / "runtime" / "tool").write_text("DO-NOT-COPY-RUNTIME")
        self.database = self.root / "data" / "library.sqlite3"
        self.first, self.second, self.playlist = "a" * 32, "b" * 32, "c" * 32
        self.audio = []
        for index, identifier in enumerate((self.first, self.second)):
            path = self.root / "audio" / (identifier + ".wav")
            with wave.open(str(path), "wb") as sound:
                sound.setnchannels(1)
                sound.setsampwidth(2)
                sound.setframerate(8000)
                sound.writeframes(struct.pack("<h", 100 + index) * 8000)
            self.audio.append(path)
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            db.executescript('''
                PRAGMA foreign_keys=ON;
                CREATE TABLE tracks (
                    id TEXT PRIMARY KEY,title TEXT,artist TEXT,album TEXT,duration REAL,file_duration REAL,
                    kind TEXT,audio_filename TEXT,loop_start REAL,loop_end REAL,tags_json TEXT,source_url TEXT,
                    added_at TEXT,sha256 TEXT,file_size INTEGER,title_key TEXT,artist_key TEXT,
                    source_sha256 TEXT,storage_json TEXT);
                CREATE TABLE playlists(id TEXT PRIMARY KEY,name TEXT,name_key TEXT,created_at TEXT);
                CREATE TABLE playlist_tracks(playlist_id TEXT REFERENCES playlists(id),track_id TEXT REFERENCES tracks(id),position INTEGER);
                PRAGMA user_version=2;
            ''')
            for index, (identifier, path) in enumerate(zip((self.first, self.second), self.audio)):
                kind = "loop" if index else "original"
                start, end = (.2, .8) if index else (0, None)
                duration = .6 if index else 1
                db.execute("INSERT INTO tracks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    identifier, "Song " + str(index), "Artist", "Album", duration, 1, kind, path.name,
                    start, end, '["calm","piano"]', 'https://www.youtube.com/watch?v=abcdefghijk&list=SECRET#private',
                    "2026-10-20T00:00:00Z", sha(path), path.stat().st_size, "DO-NOT-COPY-TITLE-KEY", "key", "", '{"secret":"DO-NOT-COPY-PROVENANCE"}'))
            db.execute("INSERT INTO playlists VALUES (?,?,?,?)", (self.playlist, "Evening", "evening", "2026-10-20T00:00:00Z"))
            # Deliberately nonconsecutive positions: order survives ordinary deletions.
            db.executemany("INSERT INTO playlist_tracks VALUES (?,?,?)", [(self.playlist, self.second, 4), (self.playlist, self.first, 9)])
        self.output = self.folder / "sample.stillroomlibrary"

    def tearDown(self):
        self.temporary.cleanup()

    def change(self, sql, args=()):
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            db.execute(sql, args)

    def assert_refused(self, **kwargs):
        with self.assertRaises(export.ExportError):
            export.export_library(self.root, self.output, **kwargs)
        self.assertFalse(self.output.exists())
        self.assertEqual([], list(self.folder.glob(".pocket-export-*.tmp")))

    def test_exact_media_ids_metadata_loop_bounds_playlist_order_and_no_secrets(self):
        before = {path: sha(path) for path in [self.database, *self.audio]}
        result = export.export_library(self.root, self.output)
        manifest, files = read_bundle(self.output)
        self.assertEqual(2, result["tracks"])
        self.assertEqual(1, result["playlists"])
        self.assertFalse(result["reencoded"])
        self.assertEqual("stillroom-library", manifest["format"])
        self.assertEqual(1, manifest["version"])
        self.assertEqual([self.second, self.first], manifest["playlists"][0]["trackIds"])
        self.assertEqual([self.first, self.second], [track["id"] for track in manifest["tracks"]])
        loop = manifest["tracks"][1]
        self.assertEqual((.6, 1, .2, .8), (loop["duration"], loop["fileDuration"], loop["loopStart"], loop["loopEnd"]))
        self.assertEqual("2026-10-20T00:00:00Z", loop["addedAt"])
        self.assertEqual("https://www.youtube.com/watch?v=abcdefghijk", loop["sourceUrl"])
        self.assertEqual({path: sha(path) for path in before}, before)
        for path in self.audio:
            self.assertEqual(path.read_bytes(), files["audio/" + path.name])
        bundle = self.output.read_bytes()
        for value in (b"DO-NOT-COPY", b"cookies", b"runtime", b"storage_json", b"SECRET", b"library.sqlite3"):
            self.assertNotIn(value, bundle)
        self.assertEqual(0o600, self.output.stat().st_mode & 0o777)

    def test_safe_preferences_only_and_explicit_defaults(self):
        export.export_library(self.root, self.output, settings={"playbackRate": 1.13})
        manifest, _ = read_bundle(self.output)
        self.assertEqual({"playbackRate": 1.13, "pitchChanges": True, "reverb": 0}, manifest["settings"])

    def test_account_preferences_rejected(self):
        self.assert_refused(settings={"mode": "browser", "cookieId": "private"})

    def test_nonfinite_and_invalid_preferences_rejected(self):
        for settings in ({"playbackRate": float("nan")}, {"reverb": 1.1}, {"pitchChanges": 1}, {"playbackRate": .49}):
            self.assert_refused(settings=settings)

    def test_unsafe_and_private_source_urls_are_omitted(self):
        for url in ("https://example.com/music?access_token=SECRET", "https://user:SECRET@youtube.com/watch?v=abcdefghijk", "https://youtube.com.evil/watch?v=abcdefghijk"):
            self.change("UPDATE tracks SET source_url=?", (url,))
            result = export.canonical_youtube(url)
            self.assertIsNone(result)
        export.export_library(self.root, self.output)
        manifest, _ = read_bundle(self.output)
        self.assertTrue(all("sourceUrl" not in track for track in manifest["tracks"]))

    def test_existing_destination_not_overwritten(self):
        self.output.write_text("keep me")
        with self.assertRaises(export.ExportError):
            export.export_library(self.root, self.output)
        self.assertEqual("keep me", self.output.read_text())

    def test_low_space_fails_before_output_and_keeps_source(self):
        before = sha(self.database), [sha(path) for path in self.audio]
        usage = type("Usage", (), {"free": 10})()
        with mock.patch.object(export.shutil, "disk_usage", return_value=usage):
            self.assert_refused()
        self.assertEqual(before, (sha(self.database), [sha(path) for path in self.audio]))

    def test_traversal_audio_filename_rejected(self):
        self.change("UPDATE tracks SET audio_filename=? WHERE id=?", ("../private.wav", self.first))
        self.assert_refused()

    def test_bad_stable_id_rejected(self):
        self.change("UPDATE tracks SET id=? WHERE id=?", ("../secret", self.first))
        self.assert_refused()

    def test_missing_audio_rejected(self):
        self.audio[0].rename(self.folder / "retained-audio.wav")
        self.assert_refused()

    def test_audio_symlink_rejected(self):
        retained = self.folder / "retained-audio.wav"
        self.audio[0].rename(retained)
        self.audio[0].symlink_to(retained)
        self.assert_refused()

    def test_fifo_audio_entry_rejected_without_blocking(self):
        retained = self.folder / "retained-audio.wav"
        self.audio[0].rename(retained)
        os.mkfifo(self.audio[0], 0o600)
        self.assert_refused()

    def test_root_symlink_rejected(self):
        alias = self.folder / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(export.ExportError):
            export.export_library(alias, self.output)

    def test_changed_bytes_or_recorded_size_rejected(self):
        self.audio[0].write_bytes(self.audio[0].read_bytes() + b"extra")
        self.assert_refused()

    def test_non_audio_payload_rejected_even_with_matching_hash(self):
        self.audio[0].write_bytes(b"PRIVATE-COOKIE-DATA")
        self.change("UPDATE tracks SET sha256=?,file_size=? WHERE id=?", (sha(self.audio[0]), self.audio[0].stat().st_size, self.first))
        self.assert_refused()

    def test_invalid_loop_bounds_and_duration_rejected(self):
        self.change("UPDATE tracks SET loop_end=2 WHERE id=?", (self.second,))
        self.assert_refused()
        self.change("UPDATE tracks SET loop_end=.8,duration=.9 WHERE id=?", (self.second,))
        self.assert_refused()

    def test_hidden_metadata_and_oversized_tags_rejected(self):
        self.change("UPDATE tracks SET title=? WHERE id=?", ("safe\u202ename", self.first))
        self.assert_refused()
        self.change("UPDATE tracks SET title='okay',tags_json=? WHERE id=?", (json.dumps(["x"] * 33), self.first))
        self.assert_refused()

    def test_duplicate_playlist_membership_rejected(self):
        self.change("INSERT INTO playlist_tracks VALUES (?,?,?)", (self.playlist, self.first, 10))
        self.assert_refused()

    def test_orphan_membership_rejected(self):
        self.change("INSERT INTO playlist_tracks VALUES (?,?,?)", ("d" * 32, self.first, 0))
        self.assert_refused()

    def test_unsupported_schema_rejected(self):
        self.change("PRAGMA user_version=999")
        self.assert_refused()

    def test_live_wal_snapshot_includes_uncheckpointed_metadata(self):
        with contextlib.closing(sqlite3.connect(self.database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("UPDATE playlists SET name='WAL snapshot'")
            writer.commit()
            self.assertTrue(Path(str(self.database) + "-wal").exists())
            before = sha(self.database), sha(Path(str(self.database) + "-wal"))
            export.export_library(self.root, self.output)
            manifest, _ = read_bundle(self.output)
            self.assertEqual("WAL snapshot", manifest["playlists"][0]["name"])
            self.assertEqual(before, (sha(self.database), sha(Path(str(self.database) + "-wal"))))

    def test_empty_wal_timestamp_churn_keeps_byte_guards_and_exports(self):
        original = export.manifest_values
        with contextlib.closing(sqlite3.connect(self.database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            wal = Path(str(self.database) + "-wal")
            self.assertEqual(0, wal.stat().st_size)
            before = sha(self.database), sha(wal)
            def touched(*args):
                result = original(*args)
                info = wal.stat()
                os.utime(wal, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
                return result
            with mock.patch.object(export, "manifest_values", side_effect=touched):
                export.export_library(self.root, self.output)
            self.assertEqual(before, (sha(self.database), sha(wal)))
            manifest, _ = read_bundle(self.output)
            self.assertEqual([self.second, self.first], manifest["playlists"][0]["trackIds"])

    def test_database_changes_during_export_refused_without_partial_output(self):
        original = export.manifest_values
        with contextlib.closing(sqlite3.connect(self.database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.assertEqual(0, Path(str(self.database) + "-wal").stat().st_size)
            def mutate(*args):
                result = original(*args)
                writer.execute("UPDATE playlists SET name='Edited while exporting'")
                writer.commit()
                return result
            with mock.patch.object(export, "manifest_values", side_effect=mutate):
                self.assert_refused()

    def test_manifest_and_media_size_bounds_fail_before_publication(self):
        with mock.patch.object(export, "MAX_MANIFEST_BYTES", 100):
            self.assert_refused()
        with mock.patch.object(export, "MAX_AUDIO_BYTES", 100):
            self.assert_refused()

    def test_output_inside_source_refused(self):
        with self.assertRaises(export.ExportError):
            export.export_library(self.root, self.root / "copy.stillroomlibrary")

    def test_cancel_before_work_and_during_copy_keeps_source_and_no_output(self):
        before = sha(self.database), [sha(path) for path in self.audio]
        self.assert_refused(cancelled=lambda: True)
        original = export.open_audio
        reads = 0
        def opened(*args):
            nonlocal reads
            reads += 1
            return original(*args)
        with mock.patch.object(export, "open_audio", side_effect=opened):
            self.assert_refused(cancelled=lambda: reads >= 3)
        self.assertEqual(before, (sha(self.database), [sha(path) for path in self.audio]))

    def test_audio_mutation_between_hash_and_copy_rejected(self):
        original = export.manifest_values
        def mutate(*args):
            result = original(*args)
            with self.audio[0].open("r+b") as file:
                file.seek(100)
                file.write(b"changed")
            return result
        with mock.patch.object(export, "manifest_values", side_effect=mutate):
            self.assert_refused()

    def test_exclusive_publication_race_keeps_winner(self):
        original = os.link
        def competing(src, dst, **kwargs):
            self.output.write_bytes(b"winner")
            return original(src, dst, **kwargs)
        with mock.patch.object(export.os, "link", side_effect=competing):
            with self.assertRaises(export.ExportError):
                export.export_library(self.root, self.output)
        self.assertEqual(b"winner", self.output.read_bytes())
        self.assertEqual([], list(self.folder.glob(".pocket-export-*.tmp")))

    def test_authorized_personal_named_fixture_is_read_only_and_exports(self):
        # Disposable stand-in only: never inspect Nathan's real Personal root.
        personal = self.folder / "Stillroom Personal"
        self.root.rename(personal)
        before = {p.relative_to(personal).as_posix(): sha(p) for p in personal.rglob("*") if p.is_file()}
        result = export.export_library(personal, self.output)
        self.assertEqual(2, result["tracks"])
        self.assertEqual(before, {p.relative_to(personal).as_posix(): sha(p) for p in personal.rglob("*") if p.is_file()})

    def test_named_personal_destination_outside_bound_source_is_no_longer_artificially_blocked(self):
        personal = self.folder / "Stillroom Personal"
        personal.mkdir()
        output = personal / "authorized-transfer.stillroomlibrary"
        export.export_library(self.root, output)
        self.assertTrue(output.is_file())
        self.assertEqual(2, len(read_bundle(output)[0]["tracks"]))


if __name__ == "__main__":
    unittest.main()
