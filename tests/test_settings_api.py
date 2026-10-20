"""HTTP account/update contract and races, using only temporary synthetic data."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app.server import Server
from app.updates import UpdateError, MAX_UPDATE_BYTES
from app.youtube_auth import MAX_COOKIE_BYTES

COOKIE_FIXTURE = ("# Netscape HTTP Cookie File\n"
                  ".youtube.com\tTRUE\t/\tTRUE\t1999999999\tSID\tfictional-private-cookie\n"
                  ".example.com\tTRUE\t/\tTRUE\t1999999999\tSID\tunrelated-cookie\n").encode()
VIDEO = 'https://www.youtube.com/watch?v=abcdefghijk'


class SettingsAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'library'
        self.server = Server(self.root, 0)
        self.logging = patch('app.server.Handler.log_message')
        self.logging.start()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.logging.stop()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw else None, dict(response.getheaders())
        finally:
            connection.close()

    def json(self, path, value=None, headers=None):
        return self.request('POST', path, json.dumps({} if value is None else value),
                            {'Content-Type': 'application/json', **(headers or {})})

    def upload(self, value=COOKIE_FIXTURE, headers=None):
        return self.request('POST', '/api/settings/youtube/cookies', value,
                            {'Content-Type': 'application/octet-stream', **(headers or {})})

    def test_settings_default_off_and_updates_snapshot_are_safe_to_read(self):
        with patch('subprocess.Popen') as process:
            status, body, headers = self.request('GET', '/api/settings')
            self.assertEqual(status, 200)
            self.assertEqual(body['youtube']['mode'], 'off')
            self.assertFalse(body['youtube']['hasCookies'])
            self.assertFalse(body['updates']['canInstall'])
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertEqual(self.request('GET', '/api/updates')[1], body['updates'])
            self.assertNotIn(str(self.root), json.dumps(body))
            self.assertFalse(self.server.youtube_auth.directory.exists())
            process.assert_not_called()

    def test_configured_browser_is_shared_by_managers_but_never_implicitly_used(self):
        with patch('subprocess.Popen') as process:
            status, body, _ = self.json('/api/settings/youtube', {'mode': 'browser', 'browser': 'firefox'})
            self.assertEqual(status, 200)
            self.assertEqual(body['mode'], 'browser')
            self.assertEqual(body['browser'], 'firefox')
            self.assertEqual(self.request('GET', '/api/settings')[1]['youtube'], body)
            self.assertIs(self.server.import_jobs.youtube_auth, self.server.youtube_auth)
            self.assertIs(self.server.playlist_imports.youtube_auth, self.server.youtube_auth)
            self.assertIsNone(self.server.youtube_auth.snapshot(False))
            process.assert_not_called()

    def test_cookie_upload_and_forget_do_not_return_secrets_or_allow_reads(self):
        status, body, _ = self.upload()
        self.assertEqual(status, 200)
        self.assertEqual(body['mode'], 'cookies')
        self.assertTrue(body['hasCookies'])
        self.assertNotIn('fictional-private-cookie', json.dumps(body))
        self.assertNotIn(str(self.root), json.dumps(body))
        cookie = next(self.server.youtube_auth.directory.glob('cookies-*.txt'))
        self.assertNotIn(b'unrelated-cookie', cookie.read_bytes())
        self.assertEqual(self.request('GET', '/api/settings/youtube/cookies')[0], 404)
        self.assertEqual(self.request('GET', '/data/.youtube-auth/' + cookie.name)[0], 404)
        status, body, _ = self.request('DELETE', '/api/settings/youtube/cookies')
        self.assertEqual(status, 200)
        self.assertFalse(body['hasCookies'])
        self.assertEqual(body['mode'], 'off')
        self.assertFalse(cookie.exists())

    def test_invalid_upload_preserves_existing_session_without_echoing_input(self):
        self.upload()
        cookie = next(self.server.youtube_auth.directory.glob('cookies-*.txt'))
        before = cookie.read_bytes()
        invalid = b'# Netscape HTTP Cookie File\nmalformed-fictional-secret'
        status, body, _ = self.upload(invalid)
        self.assertEqual(status, 400)
        self.assertNotIn('malformed-fictional-secret', json.dumps(body))
        self.assertEqual(cookie.read_bytes(), before)
        self.assertTrue(self.request('GET', '/api/settings')[1]['youtube']['hasCookies'])
        for headers, expected in (({'Content-Type': 'application/json'}, 415),
                                  ({'Content-Length': str(MAX_COOKIE_BYTES + 1)}, 413)):
            status, body, _ = self.upload(b'', headers=headers)
            self.assertEqual(status, expected)
        status, _, _ = self.request('DELETE', '/api/settings/youtube/cookies', b'x')
        self.assertEqual(status, 400)
        self.assertTrue(cookie.exists())

    def test_browser_configuration_rejects_passwords_paths_and_unknown_browsers(self):
        for body in ({'mode': 'browser', 'browser': 'firefox:/Users/private'},
                     {'mode': 'browser', 'browser': 'unknown'}, {'mode': 'password', 'password': 'fictional-password'},
                     {'mode': 'cookies', 'path': '/private/file'}):
            status, response, _ = self.json('/api/settings/youtube', body)
            self.assertEqual(status, 400)
            self.assertNotIn('fictional-password', json.dumps(response))
            self.assertNotIn('/Users/private', json.dumps(response))
        self.assertEqual(self.server.youtube_auth.status()['mode'], 'off')

    def test_cross_origin_mutations_never_reach_account_or_update_managers(self):
        routes = [('POST', '/api/settings/youtube', b'{}', 'application/json'),
                  ('POST', '/api/settings/youtube/cookies', COOKIE_FIXTURE, 'application/octet-stream'),
                  ('DELETE', '/api/settings/youtube/cookies', None, None),
                  ('POST', '/api/updates/check', b'{}', 'application/json'),
                  ('POST', '/api/updates/download', b'{}', 'application/json'),
                  ('POST', '/api/updates/package', b'fake zip', 'application/zip'),
                  ('POST', '/api/updates/install', b'{}', 'application/json')]
        with patch.object(self.server.updates, 'check') as check, \
                patch.object(self.server.updates, 'stage') as stage, \
                patch.object(self.server.updates, 'install') as install:
            for guard in ({'Origin': 'https://unrelated.example'}, {'Sec-Fetch-Site': 'cross-site'}):
                for method, path, body, content_type in routes:
                    headers = {**guard, **({'Content-Type': content_type} if content_type else {})}
                    with self.subTest(path=path, guard=guard):
                        status, _, response_headers = self.request(method, path, body, headers)
                        self.assertEqual(status, 403)
                        self.assertNotIn('Access-Control-Allow-Origin', response_headers)
            check.assert_not_called()
            stage.assert_not_called()
            install.assert_not_called()
        self.assertEqual(self.server.youtube_auth.status()['mode'], 'off')
        self.assertFalse(self.server.youtube_auth.directory.exists())
        self.assertEqual(list(self.root.glob('data/.update-upload-*')), [])

    def test_update_check_and_download_route_to_async_manager_without_network(self):
        with patch.object(self.server.updates, 'check', return_value={'state': 'checking'}) as check:
            self.assertEqual(self.json('/api/updates/check', {'auto': True})[:2], (202, {'state': 'checking'}))
            check.assert_called_once_with(auto=True)
        with patch.object(self.server.updates, 'check', return_value={'state': 'checking'}) as check:
            self.assertEqual(self.json('/api/updates/check')[0], 202)
            check.assert_called_once_with(auto=False)
        with patch.object(self.server.updates, 'download', return_value={'state': 'downloading'}) as download:
            self.assertEqual(self.json('/api/updates/download')[:2], (202, {'state': 'downloading'}))
            download.assert_called_once_with()

    def test_source_copy_cannot_stage_or_install_and_rejected_upload_is_deleted(self):
        status, body, _ = self.request('POST', '/api/updates/package', b'fixture zip', {'Content-Type': 'application/zip'})
        self.assertEqual(status, 409)
        self.assertIn('Development files are protected', body['error'])
        self.assertEqual(list(self.root.glob('data/.update-upload-*')), [])
        self.assertEqual(self.json('/api/updates/install')[0], 409)
        self.assertEqual(self.json('/api/updates/download')[0], 409)
        self.assertEqual(self.server.library.library()['tracks'], [])

    def test_busy_stager_rejection_also_cleans_uploaded_file(self):
        captured = []
        def reject(path):
            captured.append(path)
            self.assertEqual(path.read_bytes(), b'fixture zip')
            raise UpdateError('An update action is already in progress.', 409)
        with patch.object(self.server.updates, 'stage', side_effect=reject):
            self.assertEqual(self.request('POST', '/api/updates/package', b'fixture zip', {'Content-Type': 'application/zip'})[0], 409)
        self.assertEqual(len(captured), 1)
        self.assertFalse(captured[0].exists())

    def test_invalid_update_upload_does_not_create_temp_files(self):
        for body, headers, expected in ((b'', {'Content-Type': 'application/zip'}, 400),
                (b'zip', {'Content-Type': 'text/plain'}, 415),
                (b'', {'Content-Type': 'application/zip', 'Content-Length': str(MAX_UPDATE_BYTES + 1)}, 413)):
            status, _, _ = self.request('POST', '/api/updates/package', body, headers)
            self.assertEqual(status, expected)
        self.assertEqual(list(self.root.glob('data/.update-upload-*')), [])

    def test_install_blocks_active_preparations_previews_and_playlist_batches(self):
        with patch.object(self.server.updates, 'install', return_value={'state': 'installing'}) as install:
            try:
                for status in ('queued', 'working', 'ready', 'saving'):
                    self.server.import_jobs.jobs['fixture'] = {'status': status}
                    response, body, _ = self.json('/api/updates/install')
                    self.assertEqual(response, 409, status)
                    self.assertIn('Finish or cancel', body['error'])
                    self.server.import_jobs.jobs.clear()
                for status in ('reading', 'matching', 'importing'):
                    self.server.playlist_imports.sessions['fixture'] = {'status': status}
                    self.assertEqual(self.json('/api/updates/install')[0], 409, status)
                    self.server.playlist_imports.sessions.clear()
                install.assert_not_called()
                self.assertEqual(self.json('/api/updates/install')[0], 202)
                install.assert_called_once_with(self.server.server_port)
            finally:
                self.server.import_jobs.jobs.clear()
                self.server.playlist_imports.sessions.clear()

    def test_install_gate_blocks_new_import_after_install_decision_under_same_lock(self):
        entered, release = threading.Event(), threading.Event()
        def install(port):
            entered.set()
            if not release.wait(3):
                raise AssertionError('Test did not release install operation')
            return self.server.updates._set(state='installing')
        with patch.object(self.server.updates, 'install', side_effect=install), \
                patch.object(self.server.import_jobs, 'create') as create, \
                ThreadPoolExecutor(max_workers=2) as workers:
            installing = workers.submit(self.json, '/api/updates/install')
            try:
                self.assertTrue(entered.wait(2))
                importing = workers.submit(self.json, '/api/import-jobs', {'source': 'youtube', 'url': VIDEO})
                time.sleep(.05)
                self.assertFalse(importing.done(), 'Import must wait while install holds the mutation lock')
            finally:
                release.set()
            self.assertEqual(installing.result(timeout=3)[0], 202)
            response, body, _ = importing.result(timeout=3)
            self.assertEqual(response, 409)
            self.assertIn('installing an update', body['error'])
            create.assert_not_called()
        self.assertEqual(self.request('GET', '/api/updates')[0], 200)
        # Shutdown authorization remains available while other writes are gated.
        self.assertIn('not authorized', self.json('/api/shutdown', {'token': 'invalid'})[1]['error'])


if __name__ == '__main__':
    unittest.main()
