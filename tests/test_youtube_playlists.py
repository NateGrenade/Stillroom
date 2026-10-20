"""Public playlist review is bounded, metadata-only, and preserves source order."""

import json
import subprocess
import threading
import unittest
from unittest.mock import patch

from app.library import LibraryError
from app.youtube_playlists import normalize_youtube_playlist_url, read_youtube_playlist


PLAYLIST_ID = 'PL' + 'a' * 32
URL = 'https://www.youtube.com/playlist?list=' + PLAYLIST_ID
VIDEO_ID = 'abcdefghijk'
VIDEO = 'https://www.youtube.com/watch?v=' + VIDEO_ID
BASE = ['yt-dlp', '--ignore-config', '--no-config-locations', '--no-plugin-dirs',
        '--no-cookies', '--no-cookies-from-browser', '--no-playlist', '--use-extractors', 'youtube']


def entry(identifier=VIDEO_ID, **changes):
    return {'id': identifier, 'url': 'https://www.youtube.com/watch?v=' + identifier,
            'title': 'A recording', 'channel': 'A creator', 'duration': 180,
            '_type': 'url', 'ie_key': 'Youtube', **changes}


def playlist(entries=None, **changes):
    return {'id': PLAYLIST_ID, '_type': 'playlist', 'title': 'An ordered playlist',
            'playlist_count': 2, 'entries': entries if entries is not None else [entry(), entry('12345678901')], **changes}


class YouTubePlaylistTests(unittest.TestCase):
    def read(self, data=None, stderr='', returncode=0, cancel_event=None):
        result = subprocess.CompletedProcess([], returncode, json.dumps(playlist() if data is None else data), stderr)
        with patch('app.youtube_playlists.import_sources._base_command', return_value=BASE), \
                patch('app.youtube_playlists.import_sources._run_tool', return_value=result) as run:
            output = read_youtube_playlist(URL, cancel_event=cancel_event)
        return output, run

    def test_supported_links_normalize_to_one_playlist_without_tracking(self):
        for url in (URL, 'http://youtube.com/playlist/?list=' + PLAYLIST_ID,
                    'https://music.youtube.com/playlist?list=' + PLAYLIST_ID + '&si=tracking',
                    VIDEO + '&list=' + PLAYLIST_ID + '&index=15&t=3',
                    'https://m.youtube.com/watch?v=' + VIDEO_ID + '&list=' + PLAYLIST_ID,
                    'https://youtu.be/' + VIDEO_ID + '?list=' + PLAYLIST_ID + '#fragment'):
            with self.subTest(url=url):
                self.assertEqual(normalize_youtube_playlist_url(url), URL)

    def test_unsafe_and_non_playlist_links_are_rejected_before_tool_start(self):
        for url in ('https://example.com/playlist?list=' + PLAYLIST_ID,
                    'https://youtube.com.evil.test/playlist?list=' + PLAYLIST_ID,
                    'https://user:secret@youtube.com/playlist?list=' + PLAYLIST_ID,
                    'https://youtube.com:8443/playlist?list=' + PLAYLIST_ID,
                    'file:///playlist?list=' + PLAYLIST_ID, VIDEO,
                    URL + '&list=' + PLAYLIST_ID, URL.replace(PLAYLIST_ID, 'x'),
                    URL + '\n--exec=bad', URL + '\\bad',
                    'https://youtube.com/channel/UC' + 'x' * 22 + '?list=' + PLAYLIST_ID,
                    'https://youtube.com/watch?list=' + PLAYLIST_ID,
                    'https://youtu.be/invalid?list=' + PLAYLIST_ID):
            with self.subTest(url=url), patch('app.youtube_playlists.import_sources._run_tool') as run:
                with self.assertRaises(LibraryError):
                    read_youtube_playlist(url)
                run.assert_not_called()

    def test_direct_entries_preserve_order_and_do_not_use_audio_downloading(self):
        result, run = self.read()
        self.assertEqual([track['id'] for track in result['tracks']], [VIDEO_ID, '12345678901'])
        self.assertTrue(all(track['available'] for track in result['tracks']))
        self.assertEqual(result['tracks'][0]['youtubeUrl'], VIDEO)
        self.assertEqual(result['tracks'][0]['artist'], 'A creator')
        self.assertEqual(result['url'], URL)
        self.assertEqual(result['total'], 2)
        self.assertTrue(result['complete'])
        command = run.call_args.args[0]
        for flag in ('--flat-playlist', '--skip-download', '--simulate', '--no-cookies', '--no-cookies-from-browser', '--ignore-config'):
            self.assertIn(flag, command)
        self.assertNotIn('--no-playlist', command)
        self.assertNotIn('--no-simulate', command)
        self.assertNotIn('--format', command)
        self.assertEqual(command[command.index('--playlist-end') + 1], '101')
        self.assertEqual(command[-2:], ['--', URL])
        self.assertEqual(command[command.index('--use-extractors') + 1], 'youtube:tab,youtube:playlist')
        self.assertEqual(run.call_args.kwargs['timeout'], 120)

    def test_private_deleted_and_live_entries_remain_as_skipped_positions(self):
        data = playlist([entry(), entry(title='[Private video]'), entry(availability='needs_auth'),
                         None, entry(title='[Deleted video]'), entry(is_live=True), entry(duration=21601)], playlist_count=7)
        result, _ = self.read(data)
        self.assertEqual(len(result['tracks']), 7)
        self.assertEqual([t['available'] for t in result['tracks']], [True, False, False, False, False, False, False])
        self.assertTrue(all(not t['youtubeUrl'] for t in result['tracks'][1:]))
        self.assertIn('6 unavailable', ' '.join(result['notes']))

    def test_untrusted_entry_links_cannot_be_used_as_download_targets(self):
        data = playlist([entry(url='https://example.com/private'), entry(webpage_url='http://127.0.0.1/file'),
                         entry(url='https://www.youtube.com/watch?v=12345678901'), entry(ie_key='Generic'),
                         entry(id='invalid'), entry(_type='playlist')], playlist_count=6)
        result, _ = self.read(data)
        self.assertTrue(all(not t['available'] and not t['youtubeUrl'] for t in result['tracks']))

    def test_missing_optional_duration_and_artist_do_not_block_review(self):
        result, _ = self.read(playlist([entry(duration=None, channel=None)], playlist_count=1))
        track = result['tracks'][0]
        self.assertTrue(track['available'])
        self.assertIsNone(track['duration'])
        self.assertEqual(track['artist'], '')

    def test_track_bound_reports_partial_list_and_known_total(self):
        result, _ = self.read(playlist([entry() for _ in range(101)], playlist_count=500))
        self.assertEqual(len(result['tracks']), 100)
        self.assertFalse(result['complete'])
        self.assertEqual(result['total'], 500)
        self.assertIn('first 100', ' '.join(result['notes']))

    def test_unknown_total_uses_extra_entry_to_report_truncation(self):
        result, _ = self.read(playlist([entry() for _ in range(101)], playlist_count=None))
        self.assertFalse(result['complete'])
        self.assertIsNone(result['total'])

    def test_unknown_total_and_extractor_warnings_never_claim_completeness(self):
        for data, stderr in ((playlist(playlist_count=None), ''),
                             (playlist(), 'WARNING: Pagination stopped early'),
                             (playlist(), 'ERROR: One playlist page failed')):
            with self.subTest(stderr=stderr):
                result, _ = self.read(data, stderr=stderr)
                self.assertFalse(result['complete'])
                self.assertIn('may be incomplete', ' '.join(result['notes']))

    def test_generated_mixes_are_rejected_with_save_as_playlist_guidance(self):
        with self.assertRaisesRegex(LibraryError, 'regular playlist'):
            normalize_youtube_playlist_url('https://youtube.com/watch?v=' + VIDEO_ID + '&list=RD' + VIDEO_ID)

    def test_omitted_entries_are_reported_without_inventing_rows(self):
        result, _ = self.read(playlist([entry()], playlist_count=3))
        self.assertFalse(result['complete'])
        self.assertEqual(len(result['tracks']), 1)
        self.assertIn('Hidden videos', ' '.join(result['notes']))

    def test_empty_public_playlist_is_a_valid_empty_review(self):
        result, _ = self.read(playlist([], playlist_count=0))
        self.assertEqual(result['tracks'], [])
        self.assertTrue(result['complete'])

    def test_wrong_playlist_or_unexpected_payload_is_rejected(self):
        for data in ([], {'entries': []}, playlist(id='PL' + 'b' * 32), playlist(_type='video'), playlist(entries={})):
            with self.subTest(data=data), self.assertRaises(LibraryError):
                self.read(data)

    def test_bot_error_is_actionable_and_sanitized(self):
        with self.assertRaises(LibraryError) as error:
            self.read(stderr="ERROR: Sign in to confirm you're not a bot. See https://youtube.com/token?secret=123", returncode=1)
        self.assertIn('not a bot', str(error.exception))
        self.assertNotIn('secret', str(error.exception))
        self.assertNotIn('audio could not', str(error.exception))

    def test_cancel_event_is_forwarded_and_late_cancellation_is_honored(self):
        cancel = threading.Event()
        _, run = self.read(cancel_event=cancel)
        self.assertIs(run.call_args.kwargs['cancel_event'], cancel)
        cancel.set()
        with self.assertRaisesRegex(LibraryError, 'cancelled'):
            self.read(cancel_event=cancel)


if __name__ == '__main__':
    unittest.main()
