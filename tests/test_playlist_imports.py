"""Playlist review never saves audio until selection; batches are cancellable."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app.library import Library, LibraryError
from app.playlist_imports import PlaylistImports
from test_backend import make_wave

VIDEO='https://www.youtube.com/watch?v=abcdefghijk'
OTHER='https://www.youtube.com/watch?v=12345678901'


def playlist():
    return {'title':'A playlist','url':'https://open.spotify.com/playlist/'+'A'*22,
            'notes':['Public list may be incomplete.'],'complete':False,'total':None,
            'tracks':[{'id':str(i),'title':f'Song {i}','artist':'Artist','artists':['Artist'],'album':'Album',
                       'duration':2,'spotifyUrl':'https://open.spotify.com/track/'+'A'*21+str(i)} for i in range(3)]}


class FakeJobs:
    def __init__(self, library):
        self.library=library;self.jobs={};self.calls=[];self.fail=set();self.block=False;self.discarded=[]
    def create(self, options):
        self.calls.append(options);identifier=str(len(self.calls));title=options['metadata']['title']
        self.jobs[identifier]={'id':identifier,'status':'working' if self.block else 'error' if title in self.fail else 'ready',
                               'duration':2,'message':'Test preparation','error':'Test download failed','options':options}
        return self.get(identifier)
    def get(self, identifier): return copy.deepcopy(self.jobs[identifier])
    def discard(self, identifier):
        self.discarded.append(identifier)
        if self.jobs[identifier]['status']!='complete':self.jobs[identifier]['status']='cancelled'
    def commit(self, identifier, changes):
        job=self.jobs[identifier];options=job['options'];source=self.library.root/(identifier+'.wav')
        make_wave(source,duration=2,frequency=220+len(self.calls)*100)
        metadata={**options['metadata'],'sourceUrl':options['url']}
        track=self.library.import_file(source,metadata)
        job['status']='complete';return {'track':track}


class PlaylistImportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.library=Library(Path(self.temp.name));self.jobs=FakeJobs(self.library)
        self.manager=PlaylistImports(self.library,self.jobs)
        self.read=patch('app.playlist_imports.read_spotify_playlist',return_value=playlist());self.read.start()
        self.search=patch('app.playlist_imports.search_candidates',return_value=[{'url':VIDEO,'title':'Candidate','channel':'Artist','duration':2,'score':100,'confidence':'strong','reasons':[]}]);self.search.start()
    def tearDown(self):
        self.manager.close();self.read.stop();self.search.stop();self.temp.cleanup()
    def settle(self, identifier):
        self.manager.sessions[identifier]['_worker'].join(6)
        self.assertFalse(self.manager.sessions[identifier]['_worker'].is_alive())
        return self.manager.get(identifier)
    def review(self):
        session=self.manager.create({'url':playlist()['url']});return self.settle(session['id'])
    def start(self, session, rows=('0','1','2'),url=VIDEO,title='New playlist'):
        self.manager.start_import(session['id'],{'title':title,'selections':[{'rowId':r,'url':url} for r in rows]})
        return self.settle(session['id'])
    def test_review_is_metadata_only_and_preserves_partial_notice(self):
        session=self.review()
        self.assertEqual(session['status'],'review');self.assertFalse(session['complete']);self.assertIsNone(session['total'])
        self.assertEqual(len(session['rows']),3);self.assertEqual(self.jobs.calls,[])
        self.assertEqual(self.library.library()['tracks'],[]);self.assertEqual(self.library.library()['playlists'],[])
        session['rows'].clear();self.assertEqual(len(self.manager.get(session['id'])['rows']),3)

    def test_playlist_playback_capability_is_frozen_for_new_jobs_and_retry(self):
        created = self.manager.create({'url': playlist()['url'], 'allowNativeOpus': False})
        session = self.settle(created['id'])
        self.assertNotIn('storageProfile', session)
        self.assertFalse(session['allowNativeOpus'])
        with self.assertRaises(LibraryError):
            self.manager.start_import(session['id'], {'title': 'Changed', 'allowNativeOpus': True,
                                                     'selections': [{'rowId': '0', 'url': VIDEO}]})
        self.assertEqual(self.jobs.calls, [])
        self.jobs.fail.add('Song 0')
        session = self.start(session, ('0',))
        self.jobs.fail.clear()
        self.start(session, ('0',))
        self.assertEqual([call['allowNativeOpus'] for call in self.jobs.calls], [False, False])
        self.assertTrue(all('storageProfile' not in call for call in self.jobs.calls))

    def test_removed_quality_options_are_rejected_before_playlist_read_or_import(self):
        for profile in ('source', 'compact128', None, True):
            with self.subTest(profile=profile), patch('app.playlist_imports.read_spotify_playlist') as read:
                with self.assertRaises(LibraryError):
                    self.manager.create({'url': playlist()['url'], 'storageProfile': profile})
                read.assert_not_called()
        session = self.review()
        with self.assertRaises(LibraryError):
            self.manager.start_import(session['id'], {'title': 'Stale client', 'storageProfile': 'compact128',
                                                     'selections': [{'rowId': '0', 'url': VIDEO}]})
        self.assertEqual(self.jobs.calls, [])
    def test_selected_import_keeps_spotify_metadata_and_youtube_provenance(self):
        session=self.start(self.review(),('0','2'))
        self.assertEqual(session['status'],'complete');self.assertEqual([r['status'] for r in session['rows']],['saved','skipped','saved'])
        library=self.library.library();self.assertEqual(len(library['tracks']),2)
        self.assertEqual({t['title'] for t in library['tracks']},{'Song 0','Song 2'})
        self.assertTrue(all(t['sourceUrl']==VIDEO and t['artist']=='Artist' for t in library['tracks']))
        self.assertEqual(library['playlists'][0]['trackIds'],[session['rows'][0]['trackId'],session['rows'][2]['trackId']])
        receipt=json.loads((self.library.data/'playlist-imports'/f"{session['id']}.json").read_text())
        self.assertEqual(receipt['rows'][0]['track']['spotifyUrl'],playlist()['tracks'][0]['spotifyUrl'])
        self.assertNotIn('_cancel',receipt)
    def test_retry_is_idempotent_and_failures_do_not_abort_other_rows(self):
        self.jobs.fail.add('Song 0');session=self.start(self.review())
        self.assertEqual([r['status'] for r in session['rows']],['error','saved','saved'])
        self.jobs.fail.clear();session=self.start(session)
        self.assertEqual([r['status'] for r in session['rows']],['saved','saved','saved'])
        self.assertEqual(self.library.library()['playlists'][0]['trackIds'],[r['trackId'] for r in session['rows']])
        count=len(self.jobs.calls);self.start(session);self.assertEqual(len(self.jobs.calls),count)
    def test_invalid_selection_never_creates_playlist(self):
        session=self.review()
        for selections in ([{'rowId':'99','url':VIDEO}],[{'rowId':'0','url':'http://127.0.0.1/private'}],
                           [{'rowId':'0','url':VIDEO},{'rowId':'0','url':VIDEO}],[]):
            with self.assertRaises(LibraryError):self.manager.start_import(session['id'],{'title':'Name','selections':selections})
        self.assertEqual(self.library.library()['playlists'],[])
    def test_existing_playlist_name_is_never_implicitly_merged(self):
        self.library.create_playlist('New playlist');session=self.review()
        with self.assertRaises(LibraryError):self.start(session)
        self.assertEqual(self.jobs.calls,[]);self.assertEqual(len(self.library.library()['playlists']),1)
    def test_explicit_custom_link_is_used(self):
        session=self.start(self.review(),('0',),url=OTHER)
        self.assertEqual(session['rows'][0]['status'],'saved');self.assertEqual(self.jobs.calls[0]['url'],OTHER)
    def test_existing_copy_requires_explicit_selection(self):
        first=self.start(self.review(),('0',));count=len(self.jobs.calls)
        second=self.review();existing=second['rows'][0]['existingTracks'][0]
        self.assertEqual(existing['id'],first['rows'][0]['trackId'])
        self.manager.start_import(second['id'],{'title':'Other playlist','selections':[{'rowId':'0','existingTrackId':existing['id']}]})
        second=self.settle(second['id'])
        self.assertEqual(second['rows'][0]['status'],'existing');self.assertEqual(len(self.jobs.calls),count)
        self.assertEqual(first['rows'][0]['trackId'],second['rows'][0]['trackId'])
    def test_trimmed_existing_copy_is_never_silently_reused(self):
        self.start(self.review(),('0',));second=self.review()
        self.manager.sessions[second['id']]['rows'][0]['track']['duration']=10
        result=self.start(second,('0',),title='Other playlist')
        self.assertEqual(result['rows'][0]['status'],'error')
        self.assertIn('Use existing copy',result['rows'][0]['message'])
        self.assertEqual(len(self.jobs.calls),2)
    def test_initial_receipt_failure_leaves_review_retryable(self):
        session=self.review()
        with patch.object(self.manager,'_persist',side_effect=OSError('Disk full')):
            with self.assertRaises(LibraryError):self.start(session,('0',))
        self.assertEqual(self.manager.get(session['id'])['status'],'review')
        self.assertEqual(self.jobs.calls,[])
        self.assertEqual(self.start(session,('0',))['status'],'complete')
    def test_existing_selection_cannot_reference_unrelated_track(self):
        first=self.start(self.review(),('0',));second=self.review()
        with self.assertRaises(LibraryError):
            self.manager.start_import(second['id'],{'title':'Other','selections':[{'rowId':'1','existingTrackId':first['rows'][0]['trackId']}]})

    def test_wrong_duration_is_not_saved(self):
        session=self.review();self.manager.sessions[session['id']]['rows'][0]['track']['duration']=180
        result=self.start(session,('0',));self.assertEqual(result['rows'][0]['status'],'error')
        self.assertIn('closer match',result['rows'][0]['message']);self.assertEqual(self.library.library()['tracks'],[])
    def test_stop_cancels_current_preparation_without_saving(self):
        session=self.review();self.jobs.block=True
        self.manager.start_import(session['id'],{'title':'New playlist','selections':[{'rowId':'0','url':VIDEO}]})
        deadline=time.monotonic()+2
        while not self.jobs.calls and time.monotonic()<deadline:time.sleep(.01)
        self.manager.cancel(session['id']);result=self.settle(session['id'])
        self.assertEqual(result['status'],'cancelled');self.assertEqual(self.library.library()['tracks'],[])
        self.assertTrue(self.jobs.discarded)
    def test_stop_matching_leaves_reviewable_results(self):
        started=threading.Event()
        def searching(track,cancel_event=None):
            started.set();cancel_event.wait(2);raise LibraryError('Cancelled')
        with patch('app.playlist_imports.search_candidates',side_effect=searching):
            session=self.manager.create({'url':playlist()['url']});self.assertTrue(started.wait(2))
            with self.assertRaises(LibraryError):self.manager.create({'url':playlist()['url']})
            self.manager.cancel(session['id']);result=self.settle(session['id'])
        self.assertEqual(result['status'],'review');self.assertEqual(self.jobs.calls,[])

    def youtube_review(self):
        data = playlist()
        data.update(url='https://www.youtube.com/playlist?list=PL'+'a'*32, complete=True, total=3, notes=[])
        for i, track in enumerate(data['tracks']):
            track.pop('spotifyUrl')
            track.update(youtubeUrl=VIDEO if i != 1 else '', available=i != 1)
        data['tracks'][1]['message'] = 'This video is private; it will be skipped.'
        with patch('app.playlist_imports.read_youtube_playlist', return_value=data) as reader, \
                patch('app.playlist_imports.read_spotify_playlist') as spotify, \
                patch('app.playlist_imports.search_candidates') as search:
            session = self.manager.create({'source':'youtube','url':data['url']})
            session = self.settle(session['id'])
            reader.assert_called_once()
            spotify.assert_not_called()
            search.assert_not_called()
        return session

    def test_youtube_review_is_direct_metadata_only_with_disabled_unavailable_rows(self):
        session = self.youtube_review()
        self.assertEqual(session['source'], 'youtube')
        self.assertEqual(session['status'], 'review')
        self.assertTrue(session['complete'])
        self.assertEqual([row['status'] for row in session['rows']], ['matched', 'skipped', 'matched'])
        self.assertEqual(session['rows'][0]['candidates'][0]['confidence'], 'direct')
        self.assertEqual(session['rows'][0]['candidates'][0]['url'], VIDEO)
        self.assertEqual(session['rows'][1]['candidates'], [])
        self.assertEqual(self.jobs.calls, [])
        self.assertEqual(self.library.library()['tracks'], [])
        self.assertEqual(self.library.library()['playlists'], [])

    def test_youtube_selected_import_preserves_order_and_receipt_source(self):
        session = self.start(self.youtube_review(), ('2','0'))
        self.assertEqual(session['status'], 'complete')
        self.assertEqual([row['status'] for row in session['rows']], ['saved', 'skipped', 'saved'])
        self.assertIn('private', session['rows'][1]['message'])
        self.assertEqual([call['metadata']['title'] for call in self.jobs.calls], ['Song 0','Song 2'])
        tracks = self.library.library()['tracks']
        self.assertTrue(all(t['sourceUrl'] == VIDEO for t in tracks))
        self.assertEqual(self.library.library()['playlists'][0]['trackIds'],
                         [session['rows'][0]['trackId'], session['rows'][2]['trackId']])
        receipt = json.loads((self.library.data/'playlist-imports'/f"{session['id']}.json").read_text())
        self.assertEqual(receipt['source'], 'youtube')
        self.assertEqual(receipt['rows'][0]['track']['youtubeUrl'], VIDEO)

    def test_youtube_unavailable_row_cannot_be_imported(self):
        session = self.youtube_review()
        with self.assertRaisesRegex(LibraryError, 'Unavailable'):
            self.start(session, ('1',))
        self.assertEqual(self.jobs.calls, [])
        self.assertEqual(self.library.library()['playlists'], [])

    def test_source_defaults_to_spotify_and_unknown_source_is_rejected(self):
        self.assertEqual(self.review()['source'], 'spotify')
        with self.assertRaises(LibraryError):
            self.manager.create({'url':playlist()['url'], 'source':'example'})

    def test_cancel_youtube_read_creates_no_library_items(self):
        started = threading.Event()
        def reading(url, cancel_event=None):
            started.set()
            cancel_event.wait(2)
            raise LibraryError('Playlist reading cancelled.')
        with patch('app.playlist_imports.read_youtube_playlist', side_effect=reading):
            session = self.manager.create({'source':'youtube', 'url':'https://www.youtube.com/playlist?list=PL'+'a'*32})
            self.assertTrue(started.wait(2))
            self.manager.cancel(session['id'])
            result = self.settle(session['id'])
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(self.jobs.calls, [])
        self.assertEqual(self.library.library()['playlists'], [])

    def test_deleted_saved_track_can_be_selected_for_reimport(self):
        session = self.start(self.youtube_review(), ('0',))
        original_id = session['rows'][0]['trackId']
        self.library.delete_track(original_id)
        refreshed = self.manager.get(session['id'])
        self.assertEqual(refreshed['rows'][0]['status'], 'error')
        self.assertNotIn('trackId', refreshed['rows'][0])
        result = self.start(refreshed, ('0',))
        self.assertEqual(result['rows'][0]['status'], 'saved')
        self.assertNotEqual(result['rows'][0]['trackId'], original_id)
        self.assertEqual(len(self.jobs.calls), 2)
        self.assertEqual(self.library.library()['playlists'][0]['trackIds'], [result['rows'][0]['trackId']])


if __name__=='__main__':unittest.main()
