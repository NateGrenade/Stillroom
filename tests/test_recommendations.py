"""Durable recommendation lifecycle; every database and audio file is isolated."""
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest
import uuid
import wave
from unittest.mock import patch

from app.library import Library, LibraryError
from app.import_jobs import ImportJobs
from app.recommendations import Recommendations, COOLDOWN, SESSION_TTL


class PreviewJobs:
    """Controllable download boundary; retention uses real Library/file/SQLite IO."""
    def __init__(self, root):
        self.root, self.jobs, self.creates, self.discards = root, {}, 0, []
        self.ready = True
        self.failure = False

    def create(self, options):
        self.creates += 1
        identifier = uuid.uuid4().hex
        path = self.root / (identifier + '.wav')
        with wave.open(str(path), 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(struct.pack('<h', self.creates) * 8000 * 4)
        job = {'id': identifier, 'status': 'ready' if self.ready else 'working', 'duration': 4,
               'fileDuration': 4, 'metadata': {**options['metadata'], 'sourceUrl': options['url']},
               'audioUrl': f'/api/import-jobs/{identifier}/audio', 'message': 'Ready', '_file': path}
        self.jobs[identifier] = job
        return copy.deepcopy(job)

    def get(self, identifier):
        return copy.deepcopy(self.jobs[identifier])

    def audio(self, identifier):
        return self.jobs[identifier]['_file']

    def discard(self, identifier):
        self.discards.append(identifier)
        self.jobs[identifier]['status'] = 'cancelled'
        self.jobs[identifier]['_file'].unlink(missing_ok=True)


def candidate(video='aaaaaaaaaaa', title='A song'):
    url = 'https://www.youtube.com/watch?v=' + video
    return {'id': hashlib.sha256(url.encode()).hexdigest()[:32], 'title': title, 'artist': 'An artist',
            'album': '', 'tags': ['calm'], 'duration': 4, 'sourceUrl': url, 'seedIds': []}


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.library = Library(self.root / 'library')
        self.jobs = PreviewJobs(self.root)
        self.now = 1000
        self.service = Recommendations(self.library, self.jobs, clock=lambda: self.now)
        self.song = candidate()
        self.service._add_candidate(self.song)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def listen(self, request_id=None):
        session = self.service.listen(self.song['id'], {'requestId': request_id or uuid.uuid4().hex})
        return self.service.get(session['id'])

    def event(self, session, kind, **options):
        return self.service.event(session['id'], {'eventId': uuid.uuid4().hex, 'type': kind, **options})

    def complete(self, session, **options):
        self.now += 4
        return self.event(session, 'complete', position=4, listenedSeconds=4, **options)

    def test_snapshot_has_metadata_without_downloading(self):
        self.assertEqual(self.jobs.creates, 0)
        self.assertEqual(len(self.service.snapshot()['candidates']), 1)
        self.assertEqual(self.library.library()['tracks'], [])
        self.assertEqual(self.jobs.creates, 0)

    def test_preparation_and_reprepare_keep_browser_capability_without_quality_options(self):
        with patch.object(self.jobs, 'create', wraps=self.jobs.create) as create:
            session = self.service.listen(self.song['id'], {'requestId': uuid.uuid4().hex, 'allowNativeOpus': False})
            self.assertEqual(create.call_args.args[0]['allowNativeOpus'], False)
            self.assertNotIn('storageProfile', create.call_args.args[0])
            session = self.service.get(session['id'])
            session = self.event(session, 'skip')
            self.event(session, 'up')
            self.assertEqual(create.call_count, 2)
            self.assertEqual(create.call_args.args[0]['allowNativeOpus'], False)
            self.assertNotIn('storageProfile', create.call_args.args[0])

    def test_different_browser_capability_does_not_reuse_incompatible_preview(self):
        first = self.service.listen(self.song['id'], {'requestId': uuid.uuid4().hex, 'allowNativeOpus': True})
        second = self.service.listen(self.song['id'], {'requestId': uuid.uuid4().hex, 'allowNativeOpus': False})
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(self.jobs.creates, 2)

    def test_removed_quality_option_fails_before_preparation(self):
        with self.assertRaises(LibraryError):
            self.service.listen(self.song['id'], {'requestId': uuid.uuid4().hex, 'storageProfile': 'compact128'})
        self.assertEqual(self.jobs.creates, 0)

    def test_malformed_browser_capability_fails_before_preparation(self):
        with self.assertRaises(LibraryError):
            self.service.listen(self.song['id'], {'requestId': uuid.uuid4().hex, 'allowNativeOpus': 'false'})
        self.assertEqual(self.jobs.creates, 0)

    def test_refresh_is_metadata_only_and_singleflight(self):
        seed = self.listen()
        self.event(seed, 'up')
        started, release = threading.Event(), threading.Event()
        def discover(tracks, cancel, progress=None):
            started.set(); release.wait(2)
            return [candidate('bbbbbbbbbbb', 'Second song')]
        count = self.jobs.creates
        with patch('app.recommendations.discover_candidates', side_effect=discover) as search:
            self.service.refresh({})
            self.assertTrue(started.wait(1))
            self.service.refresh({})
            release.set()
            self.service.worker.join(2)
        self.assertEqual(search.call_count, 1)
        self.assertEqual(self.jobs.creates, count)
        self.assertEqual(self.service.snapshot()['refresh']['status'], 'complete')
        self.assertEqual(len(self.service.snapshot()['candidates']), 1)

    def test_skip_discards_audio_without_feedback(self):
        session = self.listen()
        path = self.jobs.audio(self.service.sessions[session['id']]['_job'])
        session = self.event(session, 'skip')
        self.assertEqual(session['status'], 'limbo')
        self.assertFalse(path.exists())
        self.assertEqual(session['completedListens'], 0)
        self.assertIsNone(session['candidate']['rating'])
        self.assertEqual(self.service._feedback(), [])

    def test_complete_unrated_discards_and_refetches_next_time(self):
        session = self.complete(self.listen())
        self.assertEqual(session['status'], 'limbo')
        self.assertEqual(session['completedListens'], 1)
        self.assertEqual(self.service.snapshot()['candidates'], [])
        self.assertEqual(len(self.service.snapshot()['limbo']), 1)
        self.assertEqual(self.listen()['status'], 'ready')
        self.assertEqual(self.jobs.creates, 2)

    def test_three_full_unrated_listens_retains_without_explicit_feedback(self):
        for index in range(3):
            session = self.complete(self.listen())
            self.assertEqual(session['completedListens'], index + 1)
        self.assertEqual(session['status'], 'retained')
        self.assertEqual(len(self.library.library()['tracks']), 1)
        self.assertTrue((self.library.root / session['track']['audioUrl'].lstrip('/')).is_file())
        self.assertEqual(self.service._feedback(), [])
        self.assertEqual(self.service.snapshot()['candidates'], [])

    def test_incomplete_seeked_or_impossible_completion_does_not_count(self):
        for options in ({'position': 4, 'listenedSeconds': 1}, {'position': 4, 'listenedSeconds': 4, 'seeked': True}):
            session = self.listen()
            self.now += 4
            session = self.event(session, 'complete', **options)
            self.assertEqual(session['completedListens'], 0)
        session = self.listen()
        session = self.event(session, 'complete', position=4, listenedSeconds=4)
        self.assertEqual(session['completedListens'], 0)
        self.assertEqual(self.service._feedback(), [])

    def test_progress_and_complete_require_no_seek_and_count_once(self):
        session = self.listen()
        self.now += 2
        self.event(session, 'progress', position=2, listenedSeconds=2)
        self.now += 2
        session = self.event(session, 'complete', position=4, listenedSeconds=4)
        self.assertEqual(session['completedListens'], 1)
        session = self.event(session, 'complete', position=4, listenedSeconds=4)
        self.assertEqual(session['completedListens'], 1)

    def test_pause_time_is_not_counted_as_listening(self):
        session = self.listen()
        self.now += 100
        self.event(session, 'progress', position=1, listenedSeconds=1)
        self.now += 1
        session = self.event(session, 'complete', position=4, listenedSeconds=2)
        self.assertEqual(session['completedListens'], 0)

    def test_two_times_rate_can_finish_in_half_wall_time(self):
        session = self.listen()
        self.now += 2
        session = self.event(session, 'complete', position=4, listenedSeconds=4, playbackRate=2)
        self.assertEqual(session['completedListens'], 1)

    def test_half_speed_finishes_after_twice_the_wall_time_and_counts_once(self):
        session = self.listen()
        self.now += 8
        session = self.event(session, 'complete', position=4, listenedSeconds=4, playbackRate=.5)
        self.assertEqual(session['completedListens'], 1)
        session = self.event(session, 'complete', position=4, listenedSeconds=4, playbackRate=.5)
        self.assertEqual(session['completedListens'], 1)

    def test_mixed_slow_fast_and_slow_progress_credits_only_heard_media_seconds(self):
        session = self.listen()
        self.now += 2
        self.event(session, 'progress', position=1, listenedSeconds=1, playbackRate=.5)
        self.now += 1
        self.event(session, 'progress', position=3, listenedSeconds=3, playbackRate=2)
        self.now += 2
        session = self.event(session, 'complete', position=4, listenedSeconds=4, playbackRate=2)
        self.assertEqual(session['completedListens'], 1)

    def test_up_retains_and_down_deletes_only_owned_copy(self):
        session = self.event(self.listen(), 'up')
        path = self.library.root / session['track']['audioUrl'].lstrip('/')
        self.assertTrue(path.exists())
        self.assertEqual(session['candidate']['rating'], 'up')
        playlist = self.library.create_playlist('Saved')
        self.library.add_to_playlist_id(playlist['id'], session['track']['id'])
        session = self.event(session, 'down')
        self.assertEqual(session['status'], 'rejected')
        self.assertFalse(path.exists())
        self.assertEqual(self.library.library()['tracks'], [])
        self.assertEqual(self.library.library()['playlists'][0]['trackIds'], [])
        self.assertEqual(self.service._feedback()[0]['decision'], 'down')
        with self.assertRaises(LibraryError):
            self.listen()

    def test_library_delete_clears_retention_without_dislike_or_background_resave(self):
        session = self.event(self.listen(), 'up')
        track = session['track']
        self.library.add_to_playlist('Saved', track['id'])
        self.service.delete_track(track['id'])
        self.service._maintain()
        current = self.service.get(session['id'])
        self.assertEqual(current['status'], 'limbo')
        self.assertIsNone(current['track'])
        self.assertIsNone(current['candidate']['ownedTrackId'])
        self.assertEqual(current['candidate']['rating'], 'up')
        self.assertEqual(self.service.snapshot()['retained'], [])
        self.assertEqual(self.library.library()['tracks'], [])
        self.assertEqual(self.library.library()['playlists'][0]['trackIds'], [])
        with self.library.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM recommendation_ownership').fetchone()[0], 0)
        self.service.close()
        self.service = Recommendations(self.library, self.jobs, clock=lambda: self.now)
        self.assertIsNone(self.service.get(session['id'])['track'])
        self.assertEqual(self.service.snapshot()['retained'], [])
        self.assertEqual(self.library.library()['tracks'], [])

    def test_library_delete_clears_reused_track_and_seed_references(self):
        preview = self.listen()
        source = self.jobs.audio(self.service.sessions[preview['id']]['_job'])
        track = self.library.import_file(source, {key: self.song[key] for key in ('title', 'artist', 'sourceUrl')})
        retained = self.event(preview, 'up')
        self.assertIsNone(retained['candidate']['ownedTrackId'])
        other = candidate('bbbbbbbbbbb', 'Seeded song')
        other['seedIds'] = [track['id'], 'keep-this-seed']
        self.service._add_candidate(other)
        self.service.delete_track(track['id'])
        self.assertIsNone(self.service.get(retained['id'])['track'])
        self.assertEqual(self.service.candidates[other['id']]['seedIds'], ['keep-this-seed'])
        self.assertEqual(self.service.snapshot()['retained'], [])

    def test_library_delete_failure_preserves_recommendation_and_audio(self):
        session = self.event(self.listen(), 'up')
        previous = copy.deepcopy(self.service.candidates)
        original_save = self.service._save
        def failed_save(candidate=None, session=None, conn=None):
            if conn is not None:
                raise OSError('simulated disk failure')
            return original_save(candidate, session, conn)
        with patch.object(self.service, '_save', side_effect=failed_save):
            with self.assertRaises(LibraryError):
                self.service.delete_track(session['track']['id'])
        self.assertEqual(self.service.candidates, previous)
        self.assertEqual(self.service.get(session['id'])['status'], 'retained')
        self.assertEqual(self.library.library()['tracks'], [session['track']])
        self.assertTrue((self.library.audio / Path(session['track']['audioUrl']).name).is_file())

    def test_down_during_preparation_cancels_job(self):
        self.jobs.ready = False
        session = self.listen()
        path = self.jobs.audio(self.service.sessions[session['id']]['_job'])
        self.assertEqual(self.event(session, 'down')['status'], 'rejected')
        self.assertFalse(path.exists())
        self.assertEqual(self.library.library()['tracks'], [])

    def test_up_during_preparation_retains_when_ready(self):
        self.jobs.ready = False
        session = self.listen()
        self.event(session, 'up')
        self.jobs.jobs[self.service.sessions[session['id']]['_job']]['status'] = 'ready'
        session = self.service.get(session['id'])
        self.assertEqual(session['status'], 'retained')
        self.assertEqual(len(self.library.library()['tracks']), 1)

    def test_explicit_keep_finishes_without_browser_polling_or_after_skip(self):
        self.jobs.ready = False
        session = self.listen()
        self.event(session, 'up')
        self.assertEqual(self.event(session, 'skip')['status'], 'preparing')
        self.jobs.jobs[self.service.sessions[session['id']]['_job']]['status'] = 'ready'
        self.service._maintain()
        self.assertEqual(self.service.sessions[session['id']]['status'], 'retained')
        self.assertEqual(len(self.library.library()['tracks']), 1)

    def test_interrupted_keep_preserves_feedback_and_clear_retry_instruction(self):
        self.jobs.ready = False
        session = self.listen()
        self.event(session, 'up')
        self.service.close()
        self.service = Recommendations(self.library, self.jobs, clock=lambda: self.now)
        item = self.service.snapshot()['limbo'][0]
        self.assertEqual(item['rating'], 'up')
        self.assertIn('finish saving', item['message'])
        self.assertEqual(self.jobs.creates, 1)
        self.jobs.ready = True
        self.assertEqual(self.listen()['status'], 'retained')

    def test_up_after_limbo_fetches_again_then_retains(self):
        session = self.complete(self.listen())
        session = self.event(session, 'up')
        self.assertEqual(session['status'], 'preparing')
        session = self.service.get(session['id'])
        self.assertEqual(session['status'], 'retained')
        self.assertEqual(self.jobs.creates, 2)
        self.assertEqual(len(self.library.library()['tracks']), 1)

    def test_preexisting_track_is_reused_and_never_deleted(self):
        session = self.listen()
        job = self.jobs.get(self.service.sessions[session['id']]['_job'])
        owned = self.library.import_file(job['_file'], job['metadata'])
        path = self.library.root / owned['audioUrl'].lstrip('/')
        original_bytes = path.read_bytes()
        kept = self.event(session, 'up')
        self.assertEqual(kept['track']['id'], owned['id'])
        self.assertIsNone(kept['candidate']['ownedTrackId'])
        self.event(kept, 'down')
        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual(self.library.library()['tracks'][0]['id'], owned['id'])

    def test_down_preserves_replaced_file_and_library_record(self):
        session = self.event(self.listen(), 'up')
        path = self.library.root / session['track']['audioUrl'].lstrip('/')
        path.write_bytes(b'replaced user audio')
        self.event(session, 'down')
        self.assertEqual(path.read_bytes(), b'replaced user audio')
        self.assertEqual(len(self.library.library()['tracks']), 1)

    def test_down_preserves_symlink_target(self):
        session = self.event(self.listen(), 'up')
        path = self.library.root / session['track']['audioUrl'].lstrip('/')
        original = self.root / 'original.wav'
        original.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(original)
        self.event(session, 'down')
        self.assertTrue(original.exists())
        self.assertTrue(path.is_symlink())
        self.assertEqual(len(self.library.library()['tracks']), 1)

    def test_requests_and_events_are_idempotent_including_concurrent_aliases(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            sessions = list(pool.map(lambda i: self.listen(f'request-{i}'), range(8)))
        self.assertEqual(len({s['id'] for s in sessions}), 1)
        self.assertEqual(self.jobs.creates, 1)
        session = sessions[0]
        payload = {'eventId': 'same-event', 'type': 'up'}
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.service.event(session['id'], payload), range(8)))
        self.assertEqual(len(self.library.library()['tracks']), 1)
        self.assertEqual({r['status'] for r in results}, {'retained'})
        for i in range(8):
            self.assertEqual(self.listen(f'request-{i}')['id'], session['id'])
        self.assertEqual(self.jobs.creates, 1)
        with self.assertRaises(LibraryError):
            self.service.event(session['id'], {**payload, 'type': 'down'})

    def test_repeat_request_after_neutral_does_not_download_again(self):
        session = self.listen('fixed-request')
        self.event(session, 'skip')
        self.assertEqual(self.listen('fixed-request')['status'], 'limbo')
        self.assertEqual(self.jobs.creates, 1)

    def test_old_tab_keep_joins_current_download_and_reuses_retained_audio(self):
        old = self.event(self.listen(), 'skip')
        self.jobs.ready = False
        current = self.listen()
        result = self.event(old, 'up')
        self.assertEqual(result['status'], 'preparing')
        self.assertEqual(self.jobs.creates, 2)
        self.jobs.jobs[self.service.sessions[current['id']]['_job']]['status'] = 'ready'
        self.service._maintain()
        self.assertEqual(self.service.get(old['id'])['status'], 'retained')
        self.assertEqual(self.service.get(current['id'])['status'], 'retained')
        self.assertEqual(len(self.library.library()['tracks']), 1)
        self.assertEqual(self.event(old, 'up')['status'], 'retained')
        self.assertEqual(self.jobs.creates, 2)

    def test_restart_preserves_feedback_counts_and_receipts(self):
        self.complete(self.listen())
        pending = self.listen('pending')
        self.service.close()
        self.service = Recommendations(self.library, self.jobs, clock=lambda: self.now)
        result = self.service.get(pending['id'])
        self.assertEqual(result['status'], 'limbo')
        self.assertEqual(result['completedListens'], 1)
        self.assertEqual(self.jobs.creates, 2)
        kept = self.event(self.listen(), 'up')
        self.service.close()
        self.service = Recommendations(self.library, self.jobs, clock=lambda: self.now)
        self.assertEqual(self.service._feedback()[0]['decision'], 'up')
        self.assertEqual(self.service.get(kept['id'])['status'], 'retained')
        self.event(kept, 'down')
        self.assertEqual(self.library.library()['tracks'], [])

    def test_expiration_cleans_audio_without_feedback(self):
        session = self.listen()
        self.now += SESSION_TTL + 1
        self.assertEqual(self.service.get(session['id'])['status'], 'limbo')
        self.assertEqual(self.service._feedback(), [])
        self.assertEqual(len(self.jobs.discards), 1)

    def test_limbo_resurfaces_only_after_cooldown_and_keeps_stable_ranking(self):
        self.event(self.listen(), 'skip')
        self.assertEqual(self.service.snapshot()['candidates'], [])
        self.now += COOLDOWN + 1
        first = self.service.snapshot()['candidates']
        self.assertEqual(len(first), 1)
        self.assertEqual(first, self.service.snapshot()['candidates'])

    def test_rejected_alias_cannot_be_manually_replayed(self):
        self.event(self.listen(), 'down')
        alias = candidate('bbbbbbbbbbb')
        self.service._add_candidate(alias)
        with self.assertRaises(LibraryError):
            self.service.listen(alias['id'], {'requestId': 'alias'})
        self.assertEqual(self.jobs.creates, 1)

    def test_failed_ownership_callback_rolls_back_import_and_new_audio(self):
        session = self.listen()
        original_save = self.service._save
        def fail(candidate=None, session=None, conn=None):
            if conn is not None and candidate and candidate['state'] == 'retained':
                raise RuntimeError('simulated persistence failure')
            return original_save(candidate, session, conn)
        with patch.object(self.service, '_save', side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.event(session, 'up')
        self.assertEqual(self.library.library()['tracks'], [])
        self.assertEqual(list(self.library.audio.iterdir()), [])
        self.assertEqual(self.service.get(session['id'])['status'], 'ready')
        self.assertEqual(self.event(session, 'up')['status'], 'retained')

    def test_preparation_error_keeps_only_metadata(self):
        self.jobs.ready = False
        session = self.listen()
        job = self.jobs.jobs[self.service.sessions[session['id']]['_job']]
        job.update(status='error', error='Source unavailable')
        result = self.service.get(session['id'])
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['message'], 'Source unavailable')
        self.assertFalse(job['_file'].exists())
        self.assertEqual(self.service._feedback(), [])

    def test_progress_validation_rejects_invalid_types(self):
        session = self.listen()
        for value in (True, '4', float('inf'), -1):
            with self.subTest(value=value), self.assertRaises((LibraryError, ValueError)):
                self.event(session, 'progress', position=value)
        self.assertEqual(self.service.get(session['id'])['status'], 'ready')

    def test_catalog_aliases_share_one_lifecycle(self):
        self.complete(self.listen())
        alias = candidate('bbbbbbbbbbb')
        self.service._add_candidate(alias)
        self.assertEqual(len(self.service.candidates), 1)
        self.assertEqual(self.service.candidates[self.song['id']]['completedListens'], 1)

    def test_legacy_active_and_limbo_aliases_are_rejected_together(self):
        first = self.listen()
        alias = {**candidate('bbbbbbbbbbb'), 'state': 'suggested', 'completedListens': 0,
                 'rating': None, 'availableAt': 0, 'ownedTrackId': None}
        self.service.candidates[alias['id']] = alias
        self.service._save(alias)
        second = self.service.listen(alias['id'], {'requestId': 'alias-session'})
        self.service.get(second['id'])
        self.event(first, 'down')
        self.assertEqual(self.service.get(second['id'])['status'], 'rejected')
        self.assertEqual(self.event(second, 'up')['status'], 'rejected')
        self.assertEqual(self.complete(second)['status'], 'rejected')
        self.assertEqual(self.service.snapshot()['limbo'], [])
        self.assertEqual(self.library.library()['tracks'], [])

    def test_identical_preowned_audio_with_different_metadata_is_reused(self):
        session = self.listen()
        job = self.jobs.get(self.service.sessions[session['id']]['_job'])
        owned = self.library.import_file(job['_file'], {'title': 'My file', 'artist': 'Unknown'})
        kept = self.event(session, 'up')
        self.assertEqual(kept['track']['id'], owned['id'])
        self.assertIsNone(kept['candidate']['ownedTrackId'])
        self.event(kept, 'down')
        self.assertEqual(self.library.library()['tracks'], [owned])

    def test_failed_third_completion_can_be_retried_without_double_count(self):
        self.complete(self.listen())
        self.complete(self.listen())
        session = self.listen()
        self.now += 4
        event = {'eventId': 'last-complete', 'type': 'complete', 'position': 4, 'listenedSeconds': 4}
        with patch.object(self.library, 'import_file', side_effect=LibraryError('Disk full', 503)):
            with self.assertRaises(LibraryError):
                self.service.event(session['id'], event)
        self.assertEqual(self.service.get(session['id'])['completedListens'], 2)
        result = self.service.event(session['id'], event)
        self.assertEqual(result['status'], 'retained')
        self.assertEqual(result['completedListens'], 3)
        self.assertEqual(self.service.event(session['id'], event)['completedListens'], 3)

    def test_restart_reclaims_only_receipted_orphans(self):
        orphan = self.library.audio / (uuid.uuid4().hex + '.wav')
        orphan.write_bytes(b'owned orphan')
        unowned = self.library.audio / (uuid.uuid4().hex + '.wav')
        unowned.write_bytes(b'original without receipt')
        receipt = self.service.receipts / (orphan.stem + '.json')
        receipt.write_text(json.dumps({'app': 'stillroom-recommendation-import', 'filename': orphan.name,
                                      'sha256': hashlib.sha256(orphan.read_bytes()).hexdigest()}))
        self.service.close()
        self.service = Recommendations(self.library, self.jobs, clock=lambda: self.now)
        self.assertFalse(orphan.exists())
        self.assertFalse(receipt.exists())
        self.assertEqual(unowned.read_bytes(), b'original without receipt')

    def test_pending_receipt_preserves_committed_audio_and_changed_files(self):
        session = self.event(self.listen(), 'up')
        saved = self.library.root / session['track']['audioUrl'].lstrip('/')
        changed = self.library.audio / (uuid.uuid4().hex + '.wav')
        changed.write_bytes(b'new user data')
        for path, expected in ((saved, hashlib.sha256(saved.read_bytes()).hexdigest()), (changed, 'old-hash')):
            (self.service.receipts / (path.stem + '.json')).write_text(json.dumps({
                'app': 'stillroom-recommendation-import', 'filename': path.name, 'sha256': expected}))
        self.service._cleanup_pending()
        self.assertTrue(saved.exists())
        self.assertEqual(changed.read_bytes(), b'new user data')
        self.assertFalse((self.service.receipts / (saved.stem + '.json')).exists())
        self.assertTrue((self.service.receipts / (changed.stem + '.json')).exists())

    def test_import_receipt_exists_before_audio_move_and_is_removed_on_success(self):
        session = self.listen()
        real_replace = __import__('os').replace
        observed = []
        def replace(source, destination):
            if Path(destination).parent == self.library.audio:
                receipt = self.service.receipts / (Path(destination).stem + '.json')
                observed.append(receipt.is_file())
            return real_replace(source, destination)
        with patch('app.library.os.replace', side_effect=replace):
            self.event(session, 'up')
        self.assertEqual(observed, [True])
        self.assertEqual(list(self.service.receipts.iterdir()), [])

    def test_real_import_jobs_preview_retention_and_cleanup(self):
        self.service.close()
        jobs = ImportJobs(self.library)
        self.service = Recommendations(self.library, jobs, clock=lambda: self.now)
        source_job = self.jobs.create({'metadata': self.song, 'url': self.song['sourceUrl']})
        source = self.jobs.audio(source_job['id'])
        def acquired(url, directory, progress=None, cancel_event=None):
            destination = directory / 'download.wav'
            destination.write_bytes(source.read_bytes())
            return {'file': destination, 'metadata': self.song}
        try:
            with patch('app.import_jobs.acquire_youtube', side_effect=acquired):
                session = self.listen()
                for _ in range(200):
                    session = self.service.get(session['id'])
                    if session['status'] != 'preparing':
                        break
                    threading.Event().wait(.02)
            self.assertEqual(session['status'], 'ready', session)
            session = self.event(session, 'up')
            self.assertEqual(session['status'], 'retained')
            self.assertEqual(len(self.library.library()['tracks']), 1)
            self.assertEqual(list(jobs.session.glob('*/prepared*')), [])
            self.event(session, 'down')
            self.assertEqual(list(self.library.audio.iterdir()), [])
        finally:
            self.service.close()
            jobs.close()


if __name__ == '__main__':
    unittest.main()
