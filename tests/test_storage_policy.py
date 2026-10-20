"""Future-import storage policy, with generated media and disposable libraries."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from app.audio_prepare import prepare_audio, SAMPLE_RATE
from app.import_jobs import ImportJobs
from app.library import Library, LibraryError, executable, inspect_audio
from app.storage_policy import prepare_original
from test_backend import make_wave


class StoragePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'tone.wav'
        make_wave(self.source, duration=3)
        self.library = Library(self.root / 'library')

    def tearDown(self):
        self.temp.cleanup()

    def encode(self, name, codec, *arguments):
        path = self.root / name
        subprocess.run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-y', '-i', str(self.source),
                        '-c:a', codec, *arguments, str(path)], check=True, capture_output=True)
        return path

    def test_compatible_compressed_sources_keep_exact_bytes_and_both_hashes(self):
        for name, codec in (('tone.mp3', 'libmp3lame'), ('tone.webm', 'libopus')):
            with self.subTest(name=name):
                source = self.encode(name, codec)
                track = self.library.import_file(source, {'title': name}, allow_duplicate=True)
                stored = self.library.root / track['audioUrl'].lstrip('/')
                self.assertEqual(stored.read_bytes(), source.read_bytes())
                storage = track['storage']
                self.assertNotIn('profile', storage)
                self.assertEqual(storage['transformation'], 'preserved')
                self.assertEqual(storage['source']['sha256'], hashlib.sha256(source.read_bytes()).hexdigest())
                self.assertEqual(storage['source'], storage['stored'])
                self.assertEqual(storage['stored']['bytes'], stored.stat().st_size)
                self.assertEqual(storage['stored']['codec'], inspect_audio(stored)['codec'])
                self.assertTrue(self.library.delete_track(track['id'])['success'])

    def test_conversion_stores_actual_output_hash_and_deduplicates_source(self):
        source = self.encode('tone.aiff', 'pcm_s16be')
        track = self.library.import_file(source)
        storage = track['storage']
        stored = self.library.root / track['audioUrl'].lstrip('/')
        self.assertEqual(storage['source']['codec'], 'pcm_s16be')
        self.assertEqual(storage['stored']['codec'], 'aac')
        self.assertEqual(storage['transformation'], 'transcoded')
        self.assertNotEqual(storage['source']['sha256'], storage['stored']['sha256'])
        with self.library.connect() as conn:
            row = conn.execute('SELECT sha256,source_sha256,file_size FROM tracks').fetchone()
        self.assertEqual(row['sha256'], hashlib.sha256(stored.read_bytes()).hexdigest())
        self.assertEqual(row['source_sha256'], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(row['file_size'], stored.stat().st_size)
        with self.assertRaises(LibraryError) as caught:
            self.library.import_file(source, {'title': 'Different title'})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(Library(self.library.root).library()['tracks'], [track])

    def test_compatible_lossless_sources_keep_exact_bytes(self):
        for source in (self.source, self.encode('lossless.flac', 'flac')):
            with self.subTest(source=source.name):
                result = prepare_original(source, self.root / 'output')
                self.assertEqual(Path(result['file']).read_bytes(), source.read_bytes())
                self.assertEqual(result['storage']['transformation'], 'preserved')
                self.assertNotIn('profile', result['storage'])

    def test_low_bitrate_lossy_source_is_not_upconverted(self):
        source = self.encode('small.webm', 'libopus', '-b:a', '64k')
        result = prepare_original(source, self.root / 'output')
        self.assertEqual(Path(result['file']).read_bytes(), source.read_bytes())
        self.assertEqual(result['storage']['transformation'], 'preserved')
        self.assertIn('unchanged', result['storage']['note'])

    def test_matroska_opus_is_byte_detected_and_remuxed_without_encoding(self):
        source = self.encode('not-webm.mka', 'libopus', '-f', 'matroska')
        disguised = self.root / 'pretending.webm'
        disguised.write_bytes(source.read_bytes())
        self.assertIsNone(inspect_audio(disguised)['extension'])
        result = prepare_original(disguised, self.root / 'output')
        self.assertEqual(result['storage']['source']['container'], 'matroska')
        self.assertEqual(result['storage']['stored']['codec'], 'opus')
        self.assertEqual(result['storage']['transformation'], 'remuxed')
        self.assertAlmostEqual(result['duration'], inspect_audio(source)['duration'], delta=.05)
        def packets(path):
            return subprocess.check_output([executable('ffmpeg'), '-v', 'error', '-i', str(path),
                                            '-map', '0:a:0', '-c:a', 'copy', '-f', 'hash', '-hash', 'sha256', '-'])
        self.assertEqual(packets(source), packets(result['file']))

    def test_ogg_opus_remux_keeps_audio_packets_without_reencoding(self):
        source = self.encode('tone.opus', 'libopus')
        result = prepare_original(source, self.root / 'output')
        self.assertEqual(Path(result['file']).suffix, '.webm')
        self.assertEqual(result['storage']['transformation'], 'remuxed')
        self.assertEqual(result['storage']['source']['container'], 'ogg')
        self.assertEqual(result['storage']['stored']['container'], 'webm')
        def packets(path):
            return subprocess.check_output([executable('ffmpeg'), '-v', 'error', '-i', str(path),
                                            '-map', '0:a:0', '-c:a', 'copy', '-f', 'hash', '-hash', 'sha256', '-'])
        self.assertEqual(packets(source), packets(result['file']))

    def test_browser_opus_fallback_records_actual_conversion_and_source(self):
        source = self.encode('tone.webm', 'libopus', '-b:a', '96k')
        result = prepare_original(source, self.root / 'fallback', allow_native_opus=False)
        self.assertEqual(Path(result['file']).suffix, '.m4a')
        storage = result['storage']
        self.assertEqual(storage['source']['codec'], 'opus')
        self.assertEqual(storage['stored']['codec'], 'aac')
        self.assertEqual(storage['transformation'], 'transcoded')
        self.assertIn('does not report native Opus', storage['note'])
        self.assertLess(storage['stored']['bitrate'], 130000)

    def test_native_opus_capability_is_strictly_boolean(self):
        for value in ('false', None, 0, 1, [], {}):
            with self.subTest(value=value), self.assertRaises(LibraryError):
                prepare_original(self.source, self.root / 'output', allow_native_opus=value)

    def test_trim_and_loop_keep_sample_verified_lossless_bounds(self):
        for looping in (False, True):
            result = prepare_audio(self.source, self.root / str(looping),
                                   {'start': .25, 'end': 1.75, 'exact': True, 'loop': looping})
            self.assertEqual(Path(result['file']).suffix, '.flac')
            self.assertEqual(result['storage']['transformation'], 'edited_lossless')
            self.assertNotIn('profile', result['storage'])
            self.assertAlmostEqual(result['duration'], 1.5, delta=2/SAMPLE_RATE)
            self.assertEqual(result['analysis']['appliedStart'], .25)
            self.assertEqual(result['analysis']['appliedEnd'], 1.75)

    def test_distinct_excerpts_from_same_source_can_both_be_saved(self):
        tracks = []
        for index, (start, end) in enumerate(((.2, 1.2), (.4, 1.7))):
            prepared = prepare_audio(self.source, self.root / str(index), {'start': start, 'end': end, 'exact': True})
            tracks.append(self.library.import_file(prepared['file'], {'title': str(index), **prepared['metadata']},
                                                   _provenance=prepared['storage']))
        self.assertEqual(len(self.library.library()['tracks']), 2)
        self.assertEqual(tracks[0]['storage']['source']['sha256'], tracks[1]['storage']['source']['sha256'])
        self.assertNotEqual(tracks[0]['storage']['stored']['sha256'], tracks[1]['storage']['stored']['sha256'])
        whole = self.library.import_file(self.source, {'title': 'Complete recording'})
        self.assertEqual(whole['storage']['transformation'], 'preserved')
        self.assertEqual(len(self.library.library()['tracks']), 3)

    def test_preview_identity_is_verified_before_commit(self):
        prepared = prepare_audio(self.source, self.root / 'prepared', {'start': .2, 'end': 1, 'exact': True})
        Path(prepared['file']).write_bytes(self.source.read_bytes())
        with self.assertRaises(LibraryError) as caught:
            self.library.import_file(prepared['file'], _provenance=prepared['storage'])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.library.library()['tracks'], [])
        self.assertEqual(list(self.library.audio.iterdir()), [])

    def test_removed_quality_options_rejected_before_acquisition_or_probe(self):
        jobs = ImportJobs(self.library)
        try:
            for profile in ('source', 'compact192', 'compact160', 'compact128', '', None, True, {}, ['source']):
                with self.subTest(profile=profile):
                    with patch('app.import_jobs.acquire_youtube') as acquire:
                        with self.assertRaises(LibraryError):
                            jobs.create({'url': 'https://www.youtube.com/watch?v=abcdefghijk', 'storageProfile': profile})
                        acquire.assert_not_called()
                    with patch('app.audio_prepare.probe') as probe:
                        with self.assertRaises(LibraryError):
                            prepare_audio(self.source, self.root / 'prepared', {'storageProfile': profile})
                        probe.assert_not_called()
        finally:
            jobs.close()

    def test_cancelled_compatibility_conversion_removes_its_output(self):
        source = self.encode('tone.aiff', 'pcm_s16be')
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(LibraryError):
            prepare_original(source, self.root / 'output', cancel_event=cancelled)
        self.assertEqual(list((self.root / 'output').iterdir()), [])

    def test_schema_two_reopen_preserves_historical_quality_provenance_and_audio(self):
        track = self.library.import_file(self.source, {'title': 'Historical import'})
        playlist = self.library.create_playlist('Saved history')
        playlist = self.library.update_playlist(playlist['id'], {'trackIds': [track['id']]})
        historical = {**track['storage'], 'profile': 'compact128', 'note': 'Historical conversion details.'}
        stored_json = json.dumps(historical, separators=(',', ':'))
        with self.library.connect() as conn:
            conn.execute('UPDATE tracks SET storage_json=? WHERE id=?', (stored_json, track['id']))
            conn.commit()
            before = dict(conn.execute('SELECT * FROM tracks').fetchone())
        audio = self.library.root / track['audioUrl'].lstrip('/')
        original_bytes = audio.read_bytes()
        reopened = Library(self.library.root)
        result = reopened.library()
        self.assertEqual(result['tracks'][0]['storage'], historical)
        self.assertEqual(result['playlists'], [playlist])
        with reopened.connect() as conn:
            self.assertEqual(dict(conn.execute('SELECT * FROM tracks').fetchone()), before)
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 2)
        self.assertEqual(audio.read_bytes(), original_bytes)
        self.assertTrue(reopened.delete_track(track['id'])['success'])

    def test_v1_migration_keeps_audio_rows_and_unknown_provenance(self):
        # Construct the prior schema without opening a real library.
        legacy = self.root / 'legacy'
        (legacy / 'audio').mkdir(parents=True)
        (legacy / 'data').mkdir()
        filename = 'f' * 32 + '.wav'
        (legacy / 'audio' / filename).write_bytes(self.source.read_bytes())
        with sqlite3.connect(legacy / 'data' / 'library.sqlite3') as conn:
            conn.executescript('''CREATE TABLE tracks (
                id TEXT PRIMARY KEY,title TEXT NOT NULL,artist TEXT NOT NULL,album TEXT NOT NULL,
                duration REAL NOT NULL,file_duration REAL NOT NULL,kind TEXT NOT NULL,
                audio_filename TEXT NOT NULL UNIQUE,loop_start REAL NOT NULL,loop_end REAL,
                tags_json TEXT NOT NULL,source_url TEXT NOT NULL,added_at TEXT NOT NULL,
                sha256 TEXT NOT NULL,file_size INTEGER NOT NULL,title_key TEXT NOT NULL,artist_key TEXT NOT NULL);
                PRAGMA user_version=1;''')
            conn.execute('INSERT INTO tracks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         ('f'*32, 'Legacy', '', '', 3, 3, 'original', filename, 0, None,
                          '[]', '', '2020-01-01T00:00:00Z', 'historical-source-hash', self.source.stat().st_size, 'legacy', ''))
        library = Library(legacy)
        track = library.library()['tracks'][0]
        self.assertEqual(track['title'], 'Legacy')
        self.assertIsNone(track['storage'])
        self.assertEqual((legacy / 'audio' / filename).read_bytes(), self.source.read_bytes())
        with library.connect() as conn:
            row = conn.execute('SELECT * FROM tracks').fetchone()
            self.assertEqual(row['sha256'], 'historical-source-hash')
            self.assertEqual(row['source_sha256'], '')
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
