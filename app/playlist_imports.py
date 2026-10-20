"""Review Spotify matches or YouTube playlist entries before saving a batch."""
from __future__ import annotations

import contextlib
import copy
import json
import math
import os
import threading
import time
import uuid

from .library import LibraryError, clean_text
from .import_sources import normalize_youtube_url
from .spotify_playlists import read_spotify_playlist
from .youtube_playlists import read_youtube_playlist, normalize_youtube_playlist_url
from .playlist_matching import search_candidates
from .youtube_auth import YouTubeAuth
from .storage_policy import reject_quality_options, native_opus

MAX_ROWS = 100
ACTIVE = {'reading', 'matching', 'importing'}
SAVED = {'saved', 'existing'}


class PlaylistImports:
    def __init__(self, library, import_jobs, youtube_auth=None):
        self.library = library
        self.import_jobs = import_jobs
        self.youtube_auth = youtube_auth or getattr(import_jobs, "youtube_auth", None) or YouTubeAuth(library.root)
        self.sessions = {}
        self.lock = threading.RLock()
        self.closed = False

    @staticmethod
    def _public(session):
        result = copy.deepcopy({key: value for key, value in session.items() if not key.startswith('_')})
        for row in result['rows']:
            for key in list(row):
                if key.startswith('_'):
                    del row[key]
        return result

    def _get(self, identifier):
        session = self.sessions.get(identifier)
        if session is None:
            raise LibraryError('This playlist review has expired. Load the playlist again. Saved tracks are still in your library.', 404)
        return session

    def get(self, identifier):
        with self.lock:
            session = self._get(identifier)
            session['_touched'] = time.monotonic()
            self._refresh_existing(session)
            return self._public(session)

    def _refresh_existing(self, session):
        tracks = self.library.library()['tracks']
        track_ids = {track['id'] for track in tracks}
        for row in session['rows']:
            if row['status'] in SAVED and row.get('trackId') not in track_ids:
                row.update(status='error', message='This track was deleted from your library. Select it again to reimport.')
                row.pop('trackId', None)
                row.pop('_savedFull', None)
            row['existingTracks'] = [{key: track[key] for key in ('id', 'title', 'artist', 'duration', 'audioUrl', 'sourceUrl')}
                                     for track in tracks if track['kind'] == 'original'
                                     and track['title'].casefold() == row['track']['title'].casefold()
                                     and track['artist'].casefold() == row['track']['artist'].casefold()]

    def _prune(self):
        for identifier, session in list(self.sessions.items()):
            if not session.get('_worker') or not session['_worker'].is_alive():
                if time.monotonic() - session['_touched'] > 3600:
                    del self.sessions[identifier]
        if len(self.sessions) >= 8:
            raise LibraryError('Close an older playlist review and wait for it to expire, or restart Stillroom before loading more playlists.', 429)

    def _available(self, owner=None):
        if self.closed:
            raise LibraryError('Stillroom is shutting down. Reopen it to continue.', 503)
        if any(s is not owner and s.get('_worker') and s['_worker'].is_alive() for s in self.sessions.values()):
            raise LibraryError('Another playlist is being checked or imported. Finish or stop it first.', 429)

    def create(self, options):
        if not isinstance(options, dict) or not isinstance(options.get('url'), str) or len(options['url']) > 2048:
            raise LibraryError('Paste a Spotify or YouTube playlist link.')
        source = options.get('source', 'spotify')
        if source not in ('spotify', 'youtube'):
            raise LibraryError('Choose a Spotify or YouTube playlist.')
        reject_quality_options(options)
        allow_native_opus = native_opus(options.get('allowNativeOpus', True))
        url = normalize_youtube_playlist_url(options['url']) if source == 'youtube' else options['url']
        enabled = options.get('useYouTubeAccount', False)
        if source != 'youtube' and enabled:
            raise LibraryError('Account sessions are available for YouTube playlist imports. Spotify matching uses public search.')
        youtube_session = self.youtube_auth.snapshot(enabled)
        with self.lock:
            self._available()
            self._prune()
            identifier = uuid.uuid4().hex
            session = {'id': identifier, 'source': source, 'status': 'reading', 'title': '', 'url': '', 'notes': [],
                       'complete': False, 'total': None, 'rows': [], 'message': f"Reading the {'YouTube' if source == 'youtube' else 'public Spotify'} playlist…",
                       'useYouTubeAccount': youtube_session is not None,
                       'allowNativeOpus': allow_native_opus, '_youtube_session': youtube_session,
                       '_cancel': threading.Event(), '_touched': time.monotonic()}
            self.sessions[identifier] = session
            worker = threading.Thread(target=self._match, args=(session, url), daemon=True)
            session['_worker'] = worker
            worker.start()
            return self._public(session)

    def _match(self, session, url):
        try:
            direct = session['source'] == 'youtube'
            reader = read_youtube_playlist if direct else read_spotify_playlist
            auth = {'youtube_session': session['_youtube_session']} if direct and session.get('_youtube_session') is not None else {}
            playlist = reader(url, cancel_event=session['_cancel'], **auth)
            with self.lock:
                session.update({key: playlist.get(key) for key in ('title', 'url', 'notes', 'complete', 'total')})
                session['title'] = clean_text(session['title'], 'Playlist name')[:120] or ('YouTube playlist' if direct else 'Spotify playlist')
                session['rows'] = [{'id': str(i), 'track': track, 'status': 'pending', 'message': 'Waiting to find matches.', 'candidates': []}
                                   for i, track in enumerate(playlist['tracks'][:MAX_ROWS])]
                if len(playlist['tracks']) > MAX_ROWS:
                    session['complete'] = False
                    session['notes'].append('Only the first 100 publicly visible tracks can be reviewed at once.')
                if direct:
                    for row in session['rows']:
                        track = row['track']
                        if not track.get('available'):
                            row.update(status='skipped', message=track.get('message') or 'Unavailable video; it will be skipped.')
                            continue
                        candidate = {'url': track['youtubeUrl'], 'title': track['title'], 'channel': track['artist'],
                                     'duration': track.get('duration'), 'score': 100, 'confidence': 'direct',
                                     'reasons': ['This is the video listed in the YouTube playlist.']}
                        row.update(status='matched', candidates=[candidate], message='Ready to import this playlist video.')
                    session.update(status='review', message='Review the playlist videos, then import the tracks you select.')
                    return
                session['status'] = 'matching'
            for i, row in enumerate(session['rows']):
                if session['_cancel'].is_set():
                    break
                with self.lock:
                    row.update(status='matching', message='Searching YouTube…')
                    session['message'] = f"Finding matches for {i + 1} of {len(session['rows'])} tracks…"
                try:
                    candidates = search_candidates(row['track'], cancel_event=session['_cancel'])
                    with self.lock:
                        row.update(candidates=candidates, status='matched' if candidates else 'unmatched',
                                   message='Review the recording before importing.' if candidates else 'No match found. Paste a YouTube link or skip this track.')
                except LibraryError as exc:
                    with self.lock:
                        row.update(status='error', message=str(exc))
            with self.lock:
                session.update(status='review', message='Search stopped. Review the matches already found.' if session['_cancel'].is_set()
                               else 'Review the YouTube recordings, then import the tracks you select.')
        except Exception as exc:
            with self.lock:
                session.update(status='cancelled' if session['_cancel'].is_set() else 'error',
                               message=str(exc) if isinstance(exc, LibraryError) else 'This playlist could not be read. Try another public playlist link.')
        finally:
            with self.lock:
                session['_touched'] = time.monotonic()

    def start_import(self, identifier, options):
        if not isinstance(options, dict):
            raise LibraryError('Choose the tracks to import.')
        title = clean_text(options.get('title', ''), 'Playlist name', 120, required=True)
        choices = options.get('selections')
        reject_quality_options(options)
        if 'allowNativeOpus' in options:
            native_opus(options['allowNativeOpus'])
        if not isinstance(choices, list) or not 1 <= len(choices) <= MAX_ROWS:
            raise LibraryError('Select between 1 and 100 tracks to import.')
        with self.lock:
            session = self._get(identifier)
            if 'allowNativeOpus' in options and options['allowNativeOpus'] != session['allowNativeOpus']:
                raise LibraryError('This playlist uses the playback support checked when it was loaded. Load it again to change playback support.')
            self._available(session)
            if session['status'] not in ('review', 'complete', 'cancelled') or session['_worker'].is_alive():
                raise LibraryError('Wait for the playlist search or current import to finish.', 409)
            self._refresh_existing(session)
            rows = {row['id']: row for row in session['rows']}
            selected = {}
            for choice in choices:
                if not isinstance(choice, dict) or not isinstance(choice.get('rowId'), str) or choice['rowId'] not in rows:
                    raise LibraryError('Choose a track from this playlist review.')
                if choice['rowId'] in selected:
                    raise LibraryError('A track can only be selected once.')
                if session['source'] == 'youtube' and rows[choice['rowId']]['track'].get('available') is False:
                    raise LibraryError('Unavailable playlist videos cannot be selected. Choose another track.')
                # Custom links are an explicit user override. Canonicalization still
                # limits every acquisition to one YouTube video.
                if 'existingTrackId' in choice:
                    self._refresh_existing(session)
                    identifier = choice['existingTrackId']
                    if not isinstance(identifier, str) or identifier not in {t['id'] for t in rows[choice['rowId']].get('existingTracks', [])}:
                        raise LibraryError('Choose a matching existing copy shown in this playlist review.')
                    if choice.get('url'):
                        raise LibraryError('Choose either an existing copy or a YouTube recording.')
                    selected[choice['rowId']] = {'existingTrackId': identifier}
                else:
                    selected[choice['rowId']] = {'url': normalize_youtube_url(choice.get('url', ''))}
            selected = {identifier: url for identifier, url in selected.items() if rows[identifier]['status'] not in SAVED}
            if not selected:
                return self._public(session)  # Idempotent retry after a completed batch.
            if not session.get('playlistId'):
                # Never merge an import into an unrelated existing playlist.
                playlist = self.library.create_playlist(title)
                session['playlistId'] = playlist['id']
            else:
                self.library.update_playlist(session['playlistId'], {'name': title})
            session['title'] = title
            previous_rows = copy.deepcopy(session['rows'])
            for row in session['rows']:
                if row['id'] in selected:
                    choice = selected[row['id']]
                    row.update(selectedUrl=choice.get('url'), selectedExistingTrackId=choice.get('existingTrackId'), status='pending', message='Waiting to import…')
                elif row['status'] not in SAVED:
                    row.update(status='skipped', message=row['track'].get('message') or 'Not selected.')
            session['_cancel'] = threading.Event()
            session.update(status='importing', message=f'Importing {len(selected)} selected tracks…')
            try:
                self._persist(session)
            except OSError as exc:
                session.update(status='review', rows=previous_rows, message='Could not save import progress. Check free space and folder permissions, then retry.')
                raise LibraryError(session['message'], 500) from exc
            worker = threading.Thread(target=self._import, args=(session, selected), daemon=True)
            session['_worker'] = worker
            worker.start()
            return self._public(session)

    def _existing(self, session, row, url):
        # Automatic reuse is limited to whole recordings saved by this batch.
        # Pre-existing tracks may be trimmed; the user chooses those explicitly.
        proven = {item.get('trackId') for item in session['rows']
                  if item.get('_savedFull') and item.get('selectedUrl') == url}
        return next((track for track in self.library.library()['tracks']
                     if track['id'] in proven and track['title'].casefold() == row['track']['title'].casefold()
                     and track['artist'].casefold() == row['track']['artist'].casefold()), None)

    def _persist_progress(self, session):
        try:
            self._persist(session)
        except OSError:
            note = 'Saved audio is safe, but the import receipt could not be updated. Check free space and folder permissions.'
            if note not in session['notes']:
                session['notes'].append(note)

    def _attach(self, session, row, track, existing=False):
        # Preserve playlist order when a failed earlier item is retried later.
        order = [track['id'] if item is row else item.get('trackId') for item in session['rows']]
        self.library.add_to_playlist_id(session['playlistId'], track['id'], [value for value in order if value])
        with self.lock:
            row.update(status='existing' if existing else 'saved', trackId=track['id'],
                       message='Used the matching track already in your library.' if existing else 'Added to the playlist.')
            row['actualSourceUrl'] = track['sourceUrl']
            self._persist_progress(session)

    def _import(self, session, selected):
        try:
            for row in session['rows']:
                if row['id'] not in selected:
                    continue
                if session['_cancel'].is_set():
                    break
                job_id = None
                try:
                    with self.lock:
                        row.update(status='importing', message='Preparing the complete recording…')
                        session['message'] = 'Importing ' + row['track']['title'] + '…'
                    choice = selected[row['id']]
                    url = choice.get('url')
                    if choice.get('existingTrackId'):
                        existing = next((track for track in self.library.library()['tracks'] if track['id'] == choice['existingTrackId']), None)
                        if existing is None:
                            raise LibraryError('This existing track was deleted. Choose another recording.')
                    else:
                        existing = self._existing(session, row, url)
                    if existing:
                        self._attach(session, row, existing, existing=True)
                        continue
                    source = row['track']
                    auth = {'youtube_session': session['_youtube_session']} if session.get('_youtube_session') is not None else {}
                    job = self.import_jobs.create({'source': 'youtube', 'url': url, 'metadataMode': 'manual',
                                                  'metadata': {'title': source['title'], 'artist': source['artist'], 'album': source.get('album', '')},
                                                  'exact': True, 'loop': False, 'useYouTubeAccount': bool(auth),
                                                  'allowNativeOpus': session['allowNativeOpus']}, **auth)
                    job_id = job['id']
                    with self.lock:
                        session['_job'] = job_id
                    while job['status'] in ('queued', 'working'):
                        if session['_cancel'].wait(.15):
                            self.import_jobs.discard(job_id)
                            break
                        job = self.import_jobs.get(job_id)
                        with self.lock:
                            row['message'] = job['message']
                    if session['_cancel'].is_set():
                        with self.lock:
                            row.update(status='skipped', message='Stopped before saving.')
                        break
                    if job['status'] != 'ready':
                        raise LibraryError(job.get('error') or job.get('message') or 'The recording could not be prepared.')
                    expected = source.get('duration')
                    if isinstance(expected, (int, float)) and math.isfinite(expected) and expected > 0:
                        if abs(job['duration'] - expected) > max(10, expected * .05):
                            source_name = 'YouTube' if session['source'] == 'youtube' else 'Spotify'
                            raise LibraryError(f"This recording is {round(job['duration'])} seconds; {source_name} lists {round(expected)} seconds. Choose a closer match.")
                    try:
                        track = self.import_jobs.commit(job_id, {})['track']
                    except LibraryError as exc:
                        if exc.status == 409:
                            raise LibraryError('A matching title or audio file is already in your library. Choose Use existing copy from the recording list, or skip this song.', 409) from exc
                        raise
                    row['_savedFull'] = True
                    row['trackId'] = track['id']
                    self._attach(session, row, track)
                except Exception as exc:
                    with self.lock:
                        row.update(status='error', message=str(exc) if isinstance(exc, LibraryError) else 'This track could not be saved. Try it again.')
                finally:
                    if job_id:
                        with contextlib.suppress(LibraryError):
                            self.import_jobs.discard(job_id)
                    with self.lock:
                        session.pop('_job', None)
            with self.lock:
                for row in session['rows']:
                    if row['status'] in ('pending', 'importing'):
                        row.update(status='skipped', message='Stopped before saving.')
                count = sum(row['status'] in SAVED for row in session['rows'])
                errors = sum(row['status'] == 'error' for row in session['rows'])
                session.update(status='cancelled' if session['_cancel'].is_set() else 'complete',
                               message=f'{count} tracks in the playlist.' + (f' {errors} could not be imported; choose another match or retry.' if errors else '')
                               + (' Import stopped; saved tracks were kept.' if session['_cancel'].is_set() else ''))
                self._persist_progress(session)
        except Exception:
            with self.lock:
                session.update(status='error', message='The playlist import stopped unexpectedly. Saved tracks are still in your library.')
        finally:
            with self.lock:
                session['_touched'] = time.monotonic()

    def _persist(self, session):
        # Metadata-only import receipts preserve both Spotify identification and
        # actual YouTube provenance. Private data/ is excluded from portable ZIPs.
        folder = self.library.data / 'playlist-imports'
        folder.mkdir(exist_ok=True)
        destination = folder / (session['id'] + '.json')
        temporary = destination.with_suffix('.tmp')
        temporary.write_text(json.dumps(self._public(session), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(temporary, destination)

    def cancel(self, identifier):
        with self.lock:
            session = self._get(identifier)
            session['_cancel'].set()
            if session['status'] in ACTIVE:
                session['message'] = 'Stopping… Saved tracks will be kept.'
            return self._public(session)

    def close(self):
        with self.lock:
            self.closed = True
            workers = []
            for session in self.sessions.values():
                session['_cancel'].set()
                if session.get('_worker'):
                    workers.append(session['_worker'])
        deadline = time.monotonic() + 3
        for worker in workers:
            worker.join(max(0, deadline - time.monotonic()))
