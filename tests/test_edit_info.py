"""Metadata edits use temporary libraries and locally generated audio only."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

from app.library import Library, LibraryError
from app.server import Server


def make_audio(path, sample=100):
    with wave.open(str(path), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(sample.to_bytes(2, 'little', signed=True) * 16000)


class EditInfoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source.wav'
        make_audio(self.source)
        self.library = Library(self.root / 'library')
        self.track = self.library.import_file(self.source, {
            'title': 'Imported title', 'artist': 'Original artist', 'album': 'Original album',
            'kind': 'loop', 'loopStart': 0.25, 'loopEnd': 1.75,
            'tags': ['focus', 'calm'], 'sourceUrl': 'https://www.youtube.com/watch?v=abcdefghijk',
        })

    def tearDown(self):
        self.temporary.cleanup()

    def stored_row(self):
        with self.library.connect() as conn:
            return dict(conn.execute('SELECT * FROM tracks WHERE id=?', (self.track['id'],)).fetchone())

    def test_edit_persists_and_preserves_audio_loop_source_identity_and_playlists(self):
        playlist = self.library.add_to_playlist('Saved', self.track['id'])
        before = self.stored_row()
        audio = self.library.root / self.track['audioUrl'].lstrip('/')
        original_hash = hashlib.sha256(audio.read_bytes()).hexdigest()
        changes = {'title': '  Corrected title  ', 'artist': ' New Artist ', 'album': ' New Album '}
        updated = self.library.update_track(self.track['id'], changes)
        expected = {**self.track, **{key: value.strip() for key, value in changes.items()}}
        self.assertEqual(updated, expected)
        self.assertEqual(Library(self.library.root).library(), {'tracks': [expected], 'playlists': [playlist]})
        after = self.stored_row()
        for key in set(before) - {'title', 'artist', 'album', 'title_key', 'artist_key'}:
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(after['title_key'], 'corrected title')
        self.assertEqual(after['artist_key'], 'new artist')
        self.assertEqual(hashlib.sha256(audio.read_bytes()).hexdigest(), original_hash)
        self.assertEqual(audio.read_bytes(), self.source.read_bytes())

    def test_partial_patch_can_clear_artist_and_album_without_resetting_title(self):
        updated = self.library.update_track(self.track['id'], {'artist': ' ', 'album': ''})
        self.assertEqual(updated, {**self.track, 'artist': '', 'album': ''})
        self.assertEqual(self.library.update_track(self.track['id'], {'title': 'Renamed'}),
                         {**updated, 'title': 'Renamed'})

    def test_metadata_with_markup_sql_and_unicode_is_stored_literally(self):
        changes = {'title': '<img src=x onerror="alert(1)">',
                   'artist': "Robert'); DELETE FROM tracks; --", 'album': 'Café — 雨 🎵'}
        updated = self.library.update_track(self.track['id'], changes)
        self.assertEqual(updated, {**self.track, **changes})
        self.assertEqual(Library(self.library.root).library()['tracks'], [updated])

    def test_invalid_patch_is_atomic_and_cannot_modify_other_track_fields(self):
        before = self.stored_row()
        invalid = [None, [], 'title', {}, {'unknown': 'x'}, {'title': ''}, {'title': ' '},
                   {'title': 123}, {'artist': None}, {'album': False}, {'album': 'x' * 301},
                   {'title': 'Valid', 'artist': 'bad\nartist'}, {'title': 'Valid', 'album': '\x00'},
                   {'id': 'b' * 32}, {'sourceUrl': 'https://example.com'}, {'kind': 'original'},
                   {'loopStart': 1}, {'tags': []}, {'audioUrl': '/audio/replaced.wav'}, {'addedAt': 'today'}]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(LibraryError) as caught:
                self.library.update_track(self.track['id'], changes)
            self.assertEqual(caught.exception.status, 400)
            self.assertEqual(self.stored_row(), before)
        updated = self.library.update_track(self.track['id'], {'title': 'x' * 300, 'artist': 'y' * 300, 'album': 'z' * 300})
        self.assertEqual(len(updated['title']), 300)

    def test_missing_and_deleted_tracks_return_404(self):
        for identifier in ('f' * 32, self.track['id']):
            if identifier == self.track['id']:
                self.library.delete_track(identifier)
            with self.assertRaises(LibraryError) as caught:
                self.library.update_track(identifier, {'title': 'Cannot restore'})
            self.assertEqual(caught.exception.status, 404)
        self.assertEqual(self.library.library()['tracks'], [])

    def test_name_duplicate_checks_follow_new_metadata_without_changing_audio_hash_identity(self):
        self.library.update_track(self.track['id'], {'title': 'New name', 'artist': 'New artist'})
        other = self.root / 'different.wav'
        make_audio(other, sample=200)
        with self.assertRaises(LibraryError) as caught:
            self.library.import_file(other, {'title': ' new NAME ', 'artist': ' new Artist ', 'kind': 'loop'})
        self.assertEqual(caught.exception.status, 409)
        # The old display name is free; the original audio remains a duplicate.
        self.library.import_file(other, {'title': self.track['title'], 'artist': self.track['artist'], 'kind': 'loop'})
        with self.assertRaises(LibraryError) as caught:
            self.library.import_file(self.source, {'title': 'Another display name'})
        self.assertEqual(caught.exception.status, 409)

    def test_parallel_partial_edits_do_not_lose_each_others_fields(self):
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(self.library.update_track, self.track['id'], changes)
                       for changes in ({'title': 'New title'}, {'artist': 'New artist'})]
            for future in futures:
                future.result(timeout=5)
        self.assertEqual(self.library.library()['tracks'], [{**self.track, 'title': 'New title', 'artist': 'New artist'}])

    def test_edit_keeps_recommendation_feedback_sessions_and_ownership_history(self):
        from app.recommendations import Recommendations
        from tests.test_recommendations import PreviewJobs, candidate

        service = Recommendations(self.library, PreviewJobs(self.root))
        try:
            song = candidate()
            service._add_candidate(song)
            session = service.get(service.listen(song['id'], {'requestId': 'edit-history'})['id'])
            session = service.event(session['id'], {'eventId': 'keep-before-edit', 'type': 'up'})
            retained = session['track']
            self.assertEqual(session['status'], 'retained')
            tables = ('recommendation_candidates', 'recommendation_sessions',
                      'recommendation_events', 'recommendation_ownership')

            def history():
                with self.library.connect() as conn:
                    return {table: [tuple(row) for row in conn.execute('SELECT * FROM ' + table)] for table in tables}

            before = history()
            updated = self.library.update_track(retained['id'], {'title': 'My preferred name', 'artist': ''})
            self.assertEqual(updated['sourceUrl'], retained['sourceUrl'])
            self.assertEqual(history(), before)
            self.assertEqual(service.snapshot()['retained'][0]['rating'], 'up')
            self.assertEqual(service.snapshot()['retained'][0]['ownedTrackId'], retained['id'])
        finally:
            service.close()

    def test_edit_waiting_for_delete_cannot_resurrect_track(self):
        deleting = threading.Event()
        release = threading.Event()

        def deletion_in_progress(conn, track):
            deleting.set()
            self.assertTrue(release.wait(5))

        with ThreadPoolExecutor(max_workers=2) as workers:
            deletion = workers.submit(self.library.delete_track, self.track['id'], _on_delete=deletion_in_progress)
            try:
                self.assertTrue(deleting.wait(5))
                edit = workers.submit(self.library.update_track, self.track['id'], {'title': 'Too late'})
            finally:
                release.set()
            self.assertTrue(deletion.result(timeout=5)['success'])
            with self.assertRaises(LibraryError) as caught:
                edit.result(timeout=5)
            self.assertEqual(caught.exception.status, 404)
        self.assertEqual(self.library.library()['tracks'], [])
        self.assertEqual(list(self.library.audio.iterdir()), [])


class EditInfoHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source.wav'
        make_audio(self.source)
        self.server = Server(self.root / 'library', 0)
        self.track = self.server.library.import_file(self.source, {'title': 'Before', 'artist': 'Artist'})
        self.path = '/api/tracks/' + self.track['id']
        self.logging = patch('app.server.Handler.log_message')
        self.logging.start()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.logging.stop()
        self.temporary.cleanup()

    def request(self, value, *, path=None, headers=None, raw=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request('PATCH', path or self.path, json.dumps(value) if raw is None else raw,
                               {'Content-Type': 'application/json', **(headers or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            connection.close()

    def test_patch_returns_current_track_and_persists_across_new_library_instance(self):
        changes = {'title': 'Corrected', 'artist': '', 'album': 'Album'}
        status, body, headers = self.request(changes)
        self.assertEqual(status, 200)
        self.assertEqual(body, {'track': {**self.track, **changes}})
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(Library(self.server.library.root).library()['tracks'], [body['track']])

    def test_invalid_json_content_type_and_patch_never_change_saved_track(self):
        for value in ([], None, 'bad', {}, {'title': 'Good', 'artist': None}, {'audioUrl': '/other.wav'}):
            with self.subTest(value=value):
                self.assertEqual(self.request(value)[0], 400)
        self.assertEqual(self.request(None, raw='{invalid')[0], 400)
        self.assertEqual(self.request({'title': 'Wrong content type'}, headers={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.server.library.library()['tracks'], [self.track])

    def test_cross_origin_and_foreign_host_requests_are_rejected_before_edit(self):
        for headers in ({'Origin': 'https://unrelated.example'}, {'Sec-Fetch-Site': 'cross-site'},
                        {'Host': 'unrelated.example'}):
            with self.subTest(headers=headers):
                status, _, returned_headers = self.request({'title': 'Blocked'}, headers=headers)
                self.assertEqual(status, 403)
                self.assertNotIn('Access-Control-Allow-Origin', returned_headers)
        self.assertEqual(self.server.library.library()['tracks'], [self.track])

    def test_missing_deleted_and_malformed_route_ids_return_404(self):
        for path in ('/api/tracks/' + 'f' * 32, '/api/tracks/nope', '/api/tracks/' + self.track['id'] + '/extra'):
            self.assertEqual(self.request({'title': 'Missing'}, path=path)[0], 404)
        self.server.recommendations.delete_track(self.track['id'])
        self.assertEqual(self.request({'title': 'Deleted'})[0], 404)
        self.assertEqual(self.server.library.library()['tracks'], [])

    def test_installing_update_blocks_metadata_mutation(self):
        with patch.object(self.server.updates, 'status', return_value={'state': 'installing'}):
            self.assertEqual(self.request({'title': 'Too late'})[0], 409)
        self.assertEqual(self.server.library.library()['tracks'], [self.track])


if __name__ == '__main__':
    unittest.main()
