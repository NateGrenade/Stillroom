"""Song sharing checks using generated audio and disposable libraries only."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
import math
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.library import Library, LibraryError, executable
from app.server import Server
from app.sharing import Sharing, GENERIC_FILENAME, download_filename

_TemporaryDirectory = tempfile.TemporaryDirectory


def make_audio(path, duration=3):
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
        audio.writeframes(b"".join(struct.pack("<h", round(6000 * math.sin(2 * math.pi * 220 * n / 44100)))
                                   for n in range(round(duration * 44100))))


def inspect(path):
    return json.loads(subprocess.check_output([executable("ffprobe"), "-v", "error", "-show_format",
                                               "-show_streams", "-show_chapters", "-of", "json", str(path)]))


class SharingFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = _TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.wav"
        make_audio(self.source)
        self.library = Library(self.root / "library")
        self.lock = threading.RLock()
        self.sharing = Sharing(self.library, self.lock)
        self.track = self.library.import_file(self.source, {
            "title": "A song", "artist": "An artist", "album": "An album", "tags": ["private-tag"],
            "sourceUrl": "https://youtu.be/abcdefghijk?t=30&token=private-source-token",
        })
        self.exports = self.root / "exports"
        self.exports.mkdir()
        self.temp_patch = patch("app.sharing.tempfile.TemporaryDirectory",
                                side_effect=lambda **kwargs: _TemporaryDirectory(dir=self.exports, **kwargs))
        self.temp_patch.start()

    def tearDown(self):
        self.sharing.close()
        self.temp_patch.stop()
        self.temporary.cleanup()

    def export(self, include_info=True, track=None):
        track = track or self.track
        preview = self.sharing.preview(track["id"])
        return self.sharing.export(track["id"], include_info, preview["revision"])

    def audio_state(self):
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.library.audio.iterdir()}

    def assert_clean(self):
        # A client can finish reading Content-Length just before the handler
        # exits its response context. Wait for that owned cleanup to complete.
        self.assertTrue(self.sharing._idle.wait(2))
        self.assertEqual(list(self.exports.iterdir()), [])
        self.assertFalse(self.sharing.busy)


class SharingTests(SharingFixture):
    def test_preview_is_allowlisted_and_url_is_canonical_without_network(self):
        with patch("app.sharing.subprocess.Popen", side_effect=AssertionError("no media lookup for preview")):
            preview = self.sharing.preview(self.track["id"])
        self.assertEqual(set(preview), {"title", "artist", "album", "sourceUrl", "kind", "exportDuration", "filename", "revision"})
        self.assertEqual(preview["sourceUrl"], "https://www.youtube.com/watch?v=abcdefghijk")
        self.assertEqual(preview["exportDuration"], 3)
        self.assertRegex(preview["revision"], r"^[0-9a-f]{64}$")
        self.assertNotIn("private", json.dumps(preview))
        self.assertNotIn(str(self.root), json.dumps(preview))
        self.assertEqual(preview["filename"], "An artist - A song.m4a")

    def test_source_url_rejects_external_local_credential_and_malformed_links(self):
        for url in ("https://example.com/secret", "file:///private/secret", "https://user:pass@youtube.com/watch?v=abcdefghijk",
                    "https://youtube.com.evil.test/watch?v=abcdefghijk", "https://youtube.com:444/watch?v=abcdefghijk", "bad"):
            with self.subTest(url=url):
                with self.library.connect() as conn:
                    conn.execute("UPDATE tracks SET source_url=? WHERE id=?", (url, self.track["id"]))
                    conn.commit()
                self.assertEqual(self.sharing.preview(self.track["id"])["sourceUrl"], "")

    def test_export_is_fresh_aac_and_keeps_library_and_source_unchanged(self):
        before = self.library.library()
        audio = self.audio_state()
        original = self.source.read_bytes()
        for include_info in (True, False):
            with self.export(include_info) as exported:
                self.assertTrue(exported.path.is_file())
                self.assertNotEqual(exported.path.read_bytes(), original)
                result = inspect(exported.path)
                self.assertEqual(len(result["streams"]), 1)
                self.assertEqual(result["streams"][0]["codec_name"], "aac")
                self.assertAlmostEqual(float(result["format"]["duration"]), 3, delta=.05)
                tags = result["format"].get("tags", {})
                display = {key: tags[key] for key in ("title", "artist", "album") if key in tags}
                self.assertEqual(display, {"title": "A song", "artist": "An artist", "album": "An album"} if include_info else {})
                self.assertEqual(exported.filename, "An artist - A song.m4a" if include_info else GENERIC_FILENAME)
                self.assertEqual(exported.path.parent.stat().st_mode & 0o777, 0o700)
            self.assert_clean()
        self.assertEqual(self.library.library(), before)
        self.assertEqual(self.audio_state(), audio)
        self.assertEqual(self.source.read_bytes(), original)

    def test_original_ignores_loop_bounds_and_loop_uses_selected_bounds(self):
        original = self.library.import_file(self.source, {"title": "Whole", "loopStart": .5, "loopEnd": 1.25}, allow_duplicate=True)
        loop = self.library.import_file(self.source, {"title": "Loop", "kind": "loop", "loopStart": .5, "loopEnd": 1.75}, allow_duplicate=True)
        open_loop = self.library.import_file(self.source, {"title": "Open loop", "kind": "loop", "loopStart": 1}, allow_duplicate=True)
        for track, duration in ((original, 3), (loop, 1.25), (open_loop, 2)):
            with self.subTest(kind=track["title"]):
                self.assertAlmostEqual(self.sharing.preview(track["id"])["exportDuration"], duration)
                with self.export(False, track) as exported:
                    self.assertAlmostEqual(float(inspect(exported.path)["format"]["duration"]), duration, delta=.05)
                    self.assertAlmostEqual(exported.duration, duration)
                self.assert_clean()

    def test_whole_file_exports_reach_eof_when_container_duration_is_underestimated(self):
        loop = self.library.import_file(self.source, {"title": "Open loop", "kind": "loop", "loopStart": .5}, allow_duplicate=True)
        for track, expected in ((self.track, 3), (loop, 2.5)):
            with self.subTest(kind=track['kind']), patch.object(self.sharing, '_duration', return_value=2):
                with self.export(False, track) as exported:
                    self.assertAlmostEqual(float(inspect(exported.path)['format']['duration']), expected, delta=.05)
        self.assert_clean()

    def test_original_embedded_secrets_artwork_chapters_and_extra_audio_are_removed(self):
        metadata = self.root / "secret.ffmeta"
        metadata.write_text(";FFMETADATA1\ntitle=embedded-title-secret\nartist=embedded-artist-secret\nalbum=embedded-album-secret\n"
                            "comment=/Users/private/account-cookie-secret\ncreation_time=2020-01-02T03:04:05Z\n"
                            "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=2000\ntitle=private-chapter-secret\n")
        tagged = self.root / "tagged.m4a"
        subprocess.run([executable("ffmpeg"), "-v", "error", "-nostdin", "-y", "-i", str(self.source),
                        "-f", "ffmetadata", "-i", str(metadata), "-f", "lavfi", "-i", "color=c=red:s=16x16",
                        "-map", "0:a:0", "-map", "0:a:0", "-map", "2:v:0", "-map_metadata", "1",
                        "-map_chapters", "1", "-metadata:s:a:0", "title=private-stream-secret",
                        "-metadata:s:a:0", "language=fra", "-c:a", "aac", "-c:v", "png", "-frames:v", "1",
                        "-disposition:v:0", "attached_pic", str(tagged)], check=True)
        original = inspect(tagged)
        self.assertGreater(len(original["streams"]), 2)
        self.assertTrue(original["chapters"])
        track = self.library.import_file(tagged, {"title": "Display title", "artist": "Display artist", "album": "Display album"})
        for include_info in (False, True):
            with self.export(include_info, track) as exported:
                result = inspect(exported.path)
                self.assertEqual(len(result["streams"]), 1)
                self.assertEqual(result["streams"][0]["codec_type"], "audio")
                self.assertEqual(result["streams"][0]["codec_name"], "aac")
                self.assertEqual(result["chapters"], [])
                tags = result["format"].get("tags", {})
                self.assertLessEqual(set(tags), {"major_brand", "minor_version", "compatible_brands", "title", "artist", "album"})
                self.assertLessEqual(set(result["streams"][0].get("tags", {})), {"language", "handler_name"})
                self.assertEqual(result["streams"][0]["tags"], {"language": "und", "handler_name": "SoundHandler"})
                self.assertNotIn("secret", json.dumps(result))
                self.assertNotIn(b"secret", exported.path.read_bytes())
                self.assertNotIn(b"/Users/", exported.path.read_bytes())
                self.assertEqual(tags.get("title"), "Display title" if include_info else None)
            self.assert_clean()

    def test_filename_sanitizes_unicode_path_tricks_device_names_and_header_controls(self):
        for title in ('../../secret\\song\r\nX-Header: yes', 'CON', 'NUL.txt', 'LPT1', ' COM9 ', '  ...  ', 'café 音楽', 'a' * 300):
            with self.subTest(title=title):
                name = download_filename(title)
                self.assertTrue(name.isascii())
                self.assertTrue(name.endswith('.m4a'))
                self.assertLess(len(name), 130)
                self.assertNotRegex(name, r'[\\/:*?"<>|\x00-\x1f\x7f]')
                self.assertFalse(name.startswith('.'))
                self.assertNotRegex(name, r'^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)')
                self.assertEqual(download_filename(title, include_info=False), GENERIC_FILENAME)

    def test_invalid_ids_database_paths_and_symlinks_are_rejected(self):
        for track_id in ('../secret', 'A' * 32, 'f' * 31, self.track['id'] + '.wav'):
            with self.subTest(track_id=track_id), self.assertRaises(LibraryError):
                self.sharing.preview(track_id)
        filename = Path(self.track['audioUrl']).name
        stored = self.library.audio / filename
        original = stored.read_bytes()
        for invalid in ('../source.wav', '/private/source.wav', 'f' * 32 + '.wav', filename + ':secret', filename.upper()):
            with self.subTest(filename=invalid):
                with self.library.connect() as conn:
                    conn.execute('UPDATE tracks SET audio_filename=? WHERE id=?', (invalid, self.track['id']))
                    conn.commit()
                with self.assertRaises(LibraryError):
                    self.sharing.preview(self.track['id'])
        with self.library.connect() as conn:
            conn.execute('UPDATE tracks SET audio_filename=? WHERE id=?', (filename, self.track['id']))
            conn.commit()
        stored.unlink()
        stored.symlink_to(self.source)
        with self.assertRaises(LibraryError):
            self.sharing.preview(self.track['id'])
        stored.unlink()
        stored.write_bytes(original)
        moved = self.root / 'moved-audio'
        self.library.audio.rename(moved)
        self.library.audio.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(LibraryError):
            self.sharing.preview(self.track['id'])
        self.assertEqual(self.source.read_bytes(), original)
        self.assert_clean()

    def test_stale_revision_rejects_unreviewed_metadata_or_audio_changes(self):
        preview = self.sharing.preview(self.track['id'])
        self.library.update_track(self.track['id'], {'title': 'Changed'})
        with self.assertRaises(LibraryError) as caught, self.sharing.export(self.track['id'], True, preview['revision']):
            pass
        self.assertEqual(caught.exception.status, 409)
        preview = self.sharing.preview(self.track['id'])
        stored = self.library.audio / Path(self.track['audioUrl']).name
        stored.write_bytes(stored.read_bytes() + b'changed')
        with self.assertRaises(LibraryError) as caught, self.sharing.export(self.track['id'], False, preview['revision']):
            pass
        self.assertEqual(caught.exception.status, 409)
        self.assert_clean()

    def test_input_output_duration_and_time_limits_cleanup_and_release_slot(self):
        with patch('app.sharing.MAX_SHARE_BYTES', 100):
            with self.assertRaises(LibraryError) as caught:
                self.sharing.preview(self.track['id'])
            self.assertEqual(caught.exception.status, 413)
        with patch('app.sharing.MAX_EXPORT_SECONDS', 1):
            with self.assertRaises(LibraryError):
                self.sharing.preview(self.track['id'])
        with patch('app.sharing.ENCODE_TIMEOUT', -1):
            with self.assertRaises(LibraryError) as caught, self.export():
                pass
            self.assertEqual(caught.exception.status, 503)
        self.assert_clean()
        with patch.object(self.sharing, '_snapshot') as snapshot, patch.object(self.sharing, '_duration', return_value=3), patch('app.sharing.MAX_SHARE_BYTES', 100):
            # Avoid the input limit to exercise the independent output limit.
            snapshot.side_effect = lambda row, destination, revision: destination.write_bytes(self.source.read_bytes())
            with self.assertRaises(LibraryError) as caught, self.sharing.export(self.track['id'], False, '0' * 64):
                pass
            self.assertEqual(caught.exception.status, 413)
        self.assert_clean()
        with self.export(False):
            pass
        self.assert_clean()

    def test_subprocess_flags_disallow_network_playlist_handlers_and_original_stream_copy(self):
        calls = []
        original = self.sharing._run
        def record(arguments, **kwargs):
            calls.append(arguments)
            return original(arguments, **kwargs)
        with patch.object(self.sharing, '_run', side_effect=record), self.export(False):
            pass
        self.assertEqual(len(calls), 2)
        for command in calls:
            self.assertEqual(command[command.index('-protocol_whitelist') + 1], 'file')
            self.assertEqual(command[command.index('-format_whitelist') + 1], 'wav')
            self.assertEqual(command[command.index('-f') + 1], 'wav')
            self.assertNotIn('copy', command)
        encode = calls[1]
        self.assertEqual(encode[encode.index('-map') + 1], '0:a:0')
        self.assertEqual(encode[encode.index('-b:a') + 1], '256k')
        self.assertEqual(encode[encode.index('-map_metadata') + 1], '-1')
        self.assertEqual(encode[encode.index('-map_chapters') + 1], '-1')

    def test_playlist_disguised_as_stored_audio_is_not_followed(self):
        stored = self.library.audio / Path(self.track['audioUrl']).name
        stored.write_text('#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nhttps://example.test/secret\n')
        with self.assertRaises(LibraryError), self.export(False):
            pass
        self.assert_clean()

    def test_export_releases_global_lock_caps_concurrency_and_survives_song_delete(self):
        entered, resume = threading.Event(), threading.Event()
        original = self.sharing._duration
        def paused(source):
            entered.set()
            self.assertTrue(resume.wait(5))
            return original(source)
        def run_export():
            with self.export(False) as exported:
                return exported.path.read_bytes()
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(self.sharing, '_duration', side_effect=paused):
            future = pool.submit(run_export)
            self.assertTrue(entered.wait(5))
            try:
                self.assertTrue(self.lock.acquire(timeout=1))
                self.lock.release()
                with self.assertRaises(LibraryError) as caught, self.export(False):
                    pass
                self.assertEqual(caught.exception.status, 409)
                with self.lock:
                    self.library.delete_track(self.track['id'])
                resume.set()
                self.assertGreater(len(future.result(5)), 100)
            finally:
                resume.set()
        self.assert_clean()
        self.assertEqual(self.library.library()['tracks'], [])

    def test_disconnect_and_failed_encode_cleanup_without_leaking_errors(self):
        with self.assertRaises(ConnectionResetError):
            with self.export(False):
                raise ConnectionResetError('synthetic browser disconnected')
        self.assert_clean()
        with patch('app.sharing.subprocess.Popen', side_effect=OSError('/Users/private/session-cookie')):
            with self.assertRaises(LibraryError) as caught, self.export(False):
                pass
        self.assertNotIn('private', str(caught.exception))
        self.assert_clean()

    def test_close_cancels_owned_child_and_cleans_snapshot(self):
        started = threading.Event()
        original_popen = subprocess.Popen
        def slow_process(arguments, **kwargs):
            process = original_popen([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
            started.set()
            return process
        def run_export():
            with self.assertRaises(LibraryError), self.export(False):
                pass
        with ThreadPoolExecutor(max_workers=1) as pool, patch('app.sharing.subprocess.Popen', side_effect=slow_process):
            future = pool.submit(run_export)
            self.assertTrue(started.wait(5))
            self.sharing.close()
            future.result(5)
        self.assert_clean()
        with self.assertRaises(LibraryError):
            self.sharing.preview(self.track['id'])


class SharingHTTPTests(SharingFixture):
    def setUp(self):
        super().setUp()
        self.server = Server(self.library.root, 0)
        self.sharing = self.server.sharing
        self.logging = patch('app.server.Handler.log_message')
        self.logging.start()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = '/api/tracks/' + self.track['id']

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.logging.stop()
        super().tearDown()

    def request(self, method, suffix, value=None, *, headers=None, raw=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            body = (json.dumps(value) if value is not None else None) if raw is None else raw
            connection.request(method, self.base + suffix, body, {'Content-Type': 'application/json', **(headers or {})})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def body(self, include_info=True):
        return {'includeInfo': include_info, 'revision': self.sharing.preview(self.track['id'])['revision']}

    def test_preview_and_direct_m4a_download_with_both_information_choices(self):
        status, headers, raw = self.request('GET', '/share')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(json.loads(raw)['title'], 'A song')
        for flag in (False, True):
            status, headers, raw = self.request('POST', '/export', self.body(flag))
            self.assertEqual(status, 200)
            self.assertEqual(headers['Content-Type'], 'audio/mp4')
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertEqual(int(headers['Content-Length']), len(raw))
            filename = GENERIC_FILENAME if not flag else 'An artist - A song.m4a'
            self.assertEqual(headers['Content-Disposition'], 'attachment; filename="' + filename + '"')
            self.assertEqual(raw[4:8], b'ftyp')
            self.assertNotIn(b'private-source-token', raw)
        self.assert_clean()

    def test_get_head_export_and_invalid_routes_do_not_encode(self):
        with patch.object(self.sharing, 'export', side_effect=AssertionError('must not export')):
            for method in ('GET', 'HEAD'):
                self.assertEqual(self.request(method, '/export')[0], 404)
            previous = self.base
            for track_id in ('A' * 32, 'f' * 31, 'f' * 32):
                self.base = '/api/tracks/' + track_id
                self.assertEqual(self.request('GET', '/share')[0], 404)
            self.base = previous

    def test_cross_origin_preview_and_export_rejected_before_work(self):
        for extra in ({'Origin': 'https://evil.test'}, {'Sec-Fetch-Site': 'cross-site'}, {'Sec-Fetch-Site': 'same-site'}, {'Host': 'evil.test'}):
            with self.subTest(headers=extra), patch.object(self.sharing, 'preview') as preview, patch.object(self.sharing, 'export') as export:
                self.assertEqual(self.request('GET', '/share', headers=extra)[0], 403)
                self.assertEqual(self.request('POST', '/export', {'includeInfo': False, 'revision': '0' * 64}, headers=extra)[0], 403)
                preview.assert_not_called()
                export.assert_not_called()
        origin = 'http://127.0.0.1:' + str(self.server.server_port)
        self.assertEqual(self.request('GET', '/share', headers={'Origin': origin, 'Sec-Fetch-Site': 'same-origin'})[0], 200)

    def test_malformed_or_non_strict_export_requests_reject_without_encoding(self):
        body = self.body()
        cases = ('{', '[]', 'null', '{}', '{"includeInfo":true}',
                 json.dumps({**body, 'private': 'x'}), json.dumps({**body, 'includeInfo': 1}),
                 json.dumps({**body, 'includeInfo': None}), json.dumps({**body, 'revision': '../secret'}),
                 '{"includeInfo":true,"includeInfo":false,"revision":"' + body['revision'] + '"}',
                 '{"includeInfo":NaN,"revision":"' + body['revision'] + '"}')
        with patch.object(self.sharing, 'export', side_effect=AssertionError('must not encode')):
            for raw in cases:
                with self.subTest(raw=raw):
                    self.assertEqual(self.request('POST', '/export', raw=raw)[0], 400)
            self.assertEqual(self.request('POST', '/export', body, headers={'Content-Type': 'text/plain'})[0], 415)
            self.assertEqual(self.request('POST', '/export', raw=' ' * (128 * 1024 + 1))[0], 413)

    def test_stale_preview_and_installing_update_return_409(self):
        body = self.body()
        self.library.update_track(self.track['id'], {'title': 'Updated'})
        status, _, raw = self.request('POST', '/export', body)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(raw), {'error': 'This song changed. Reopen sharing to review its current details.'})
        with patch.object(self.server.updates, 'status', return_value={'state': 'installing'}):
            self.assertEqual(self.request('POST', '/export', self.body())[0], 409)
        self.assert_clean()

    def test_api_failures_do_not_expose_source_filename_or_internal_error(self):
        with patch('app.sharing.subprocess.Popen', side_effect=OSError('/Users/private/cookies.txt')):
            status, _, raw = self.request('POST', '/export', self.body(False))
        self.assertEqual(status, 503)
        self.assertEqual(set(json.loads(raw)), {'error'})
        self.assertNotIn(b'/Users/', raw)
        self.assertNotIn(Path(self.track['audioUrl']).name.encode(), raw)
        self.assert_clean()

    def test_update_install_waits_for_active_export(self):
        with self.export(False), patch.object(self.server.updates, 'install') as install:
            connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
            try:
                connection.request('POST', '/api/updates/install', '{}', {'Content-Type': 'application/json'})
                response = connection.getresponse()
                self.assertEqual(response.status, 409)
                response.read()
            finally:
                connection.close()
            install.assert_not_called()
        self.assert_clean()


if __name__ == '__main__':
    unittest.main()
