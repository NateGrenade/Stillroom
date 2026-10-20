"""Metadata-first recommendations, durable feedback, and disposable listening sessions.

No network or audio acquisition happens until refresh / listen is explicitly
requested. A recommendation import records ownership in the track transaction;
only that exact owned file can ever be removed by a negative recommendation.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

from .library import LibraryError, clean_text, number
from .import_sources import normalize_youtube_url
from .recommendation_catalog import canonical_key, discover_candidates, rank_candidates
from .storage_policy import native_opus, reject_quality_options

COOLDOWN = 24 * 3600
SESSION_TTL = 3600
COMPLETION_THRESHOLD = .95
LISTENS_TO_KEEP = 3
ACTIVE = {'preparing', 'ready'}


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def token(value, field):
    value = clean_text(value, field, 128, required=True)
    if not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise LibraryError(f'{field} must contain letters, numbers, underscores or hyphens.')
    return value


class Recommendations:
    def __init__(self, library, import_jobs, *, clock=time.time):
        self.library, self.import_jobs, self.clock = library, import_jobs, clock
        self.lock = threading.RLock()
        self.closed = False
        self.cancel = threading.Event()
        self.refresh_state = {'status': 'idle', 'message': 'Find songs based on your library.'}
        self.worker = None
        self.receipts = library.data / 'recommendation-pending'
        self.receipts.mkdir(exist_ok=True)
        with library.connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS recommendation_candidates (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS recommendation_sessions (
                    id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS recommendation_events (
                    session_id TEXT NOT NULL, event_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(session_id,event_id));
                CREATE TABLE IF NOT EXISTS recommendation_ownership (
                    track_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL, state TEXT NOT NULL);
            ''')
            self.candidates = {row['id']: json.loads(row['data']) for row in conn.execute('SELECT * FROM recommendation_candidates')}
            self.sessions = {row['id']: json.loads(row['data']) for row in conn.execute('SELECT * FROM recommendation_sessions')}
            conn.commit()
        # ImportJobs reclaims stopped-process preview folders. A new instance
        # cannot resume an old browser's audio; preserve only its neutral metadata.
        for session in self.sessions.values():
            if session['status'] in ACTIVE:
                candidate = self.candidates[session['candidateId']]
                existing = self._existing(candidate) if candidate['state'] == 'retained' else None
                if existing:
                    session.update(status='retained', track=existing, message='Kept in your library.')
                else:
                    session.update(status='limbo', message='Playback was interrupted. Listen again when you are ready.', track=None)
                    candidate.update(state='limbo', availableAt=self.clock() + COOLDOWN)
                    if candidate.get('rating') == 'up':
                        session['message'] = candidate['message'] = 'Keep requested. Listen again to finish saving this song.'
                self._save(candidate, session)
        self._cleanup_garbage()
        self._cleanup_pending()
        self.sweeper = threading.Thread(target=self._sweep, daemon=True)
        self.sweeper.start()

    def _save(self, candidate=None, session=None, conn=None):
        if conn is None:
            with self.library.connect() as connection:
                self._save(candidate, session, connection)
                connection.commit()
            return
        if candidate is not None:
            conn.execute('INSERT OR REPLACE INTO recommendation_candidates VALUES (?,?)',
                         (candidate['id'], json.dumps(candidate, allow_nan=False)))
        if session is not None:
            conn.execute('INSERT OR REPLACE INTO recommendation_sessions VALUES (?,?,?,?)',
                         (session['id'], session['candidateId'], session['requestId'], json.dumps(session, allow_nan=False)))

    def _candidate(self, identifier):
        candidate = self.candidates.get(identifier)
        if candidate is None:
            raise LibraryError('Recommendation not found. Find songs again.', 404)
        return candidate

    def _session(self, identifier):
        session = self.sessions.get(identifier)
        if session is None:
            raise LibraryError('Listening session not found. Listen to the song again.', 404)
        return session

    def _public_session(self, session):
        result = copy.deepcopy({key: value for key, value in session.items() if not key.startswith('_')})
        candidate = self._candidate(session['candidateId'])
        result.update(candidate=self._public_candidate(candidate), completedListens=candidate['completedListens'])
        return result

    @staticmethod
    def _public_candidate(candidate):
        return copy.deepcopy({key: value for key, value in candidate.items() if not key.startswith('_')})

    def _feedback(self):
        return [{**candidate, 'decision': candidate['rating']} for candidate in self.candidates.values() if candidate.get('rating')]

    def snapshot(self):
        with self.lock:
            self._expire()
            tracks = self.library.library()['tracks']
            available = [candidate for candidate in self.candidates.values() if candidate['state'] not in ('rejected', 'retained')
                         and candidate.get('availableAt', 0) <= self.clock()]
            ranked = rank_candidates(available, tracks, self._feedback())
            limbo = [self._public_candidate(candidate) for candidate in self.candidates.values() if candidate['state'] == 'limbo']
            retained = [self._public_candidate(candidate) for candidate in self.candidates.values() if candidate['state'] == 'retained']
            return {'candidates': ranked, 'limbo': sorted(limbo, key=lambda c: (c['availableAt'], c['id'])),
                    'retained': sorted(retained, key=lambda c: c['id']),
                    'rejectedCount': sum(c['state'] == 'rejected' for c in self.candidates.values()),
                    'refresh': copy.deepcopy(self.refresh_state),
                    'policy': {'completionThreshold': COMPLETION_THRESHOLD, 'completedListensToKeep': LISTENS_TO_KEEP,
                               'limboCooldownHours': COOLDOWN / 3600}}

    def delete_track(self, track_id):
        """Remove saved audio while keeping feedback and sessions consistent."""
        with self.lock:
            self._available()
            related_ids = {candidate['id'] for candidate in self.candidates.values()
                           if candidate.get('ownedTrackId') == track_id}
            related_ids.update(session['candidateId'] for session in self.sessions.values()
                               if (session.get('track') or {}).get('id') == track_id)
            candidates, sessions, jobs = {}, {}, set()
            for identifier, current in self.candidates.items():
                related = identifier in related_ids
                if not related and track_id not in current.get('seedIds', []):
                    continue
                candidate = copy.deepcopy(current)
                candidate['seedIds'] = [seed for seed in candidate.get('seedIds', []) if seed != track_id]
                if related:
                    candidate['ownedTrackId'] = None
                    candidate['completedListens'] = 0
                    if candidate['state'] != 'rejected':
                        candidate.update(state='limbo', availableAt=self.clock() + COOLDOWN)
                    candidate.pop('message', None)
                candidates[identifier] = candidate
            for identifier, current in self.sessions.items():
                if current['candidateId'] not in related_ids:
                    continue
                session = copy.deepcopy(current)
                if session.get('_job'):
                    jobs.add(session['_job'])
                session.update(status='limbo', track=None, _job=None,
                               message='Deleted from your library. Listen again when you want this song.')
                session.pop('_follows', None)
                sessions[identifier] = session

            def remove_references(conn, track):
                conn.execute('DELETE FROM recommendation_ownership WHERE track_id=?', (track['id'],))
                for candidate in candidates.values():
                    self._save(candidate=candidate, conn=conn)
                for session in sessions.values():
                    self._save(session=session, conn=conn)

            result = self.library.delete_track(track_id, _on_delete=remove_references)
            # Publish in-memory changes only after the track transaction succeeds.
            self.candidates.update(candidates)
            self.sessions.update(sessions)
            for job in jobs:
                with contextlib.suppress(LibraryError):
                    self.import_jobs.discard(job)
            return result

    def refresh(self, options):
        if not isinstance(options, dict):
            raise LibraryError('Recommendation options must be an object.')
        with self.lock:
            self._available()
            if self.worker and self.worker.is_alive():
                return self.snapshot()
            tracks = self.library.library()['tracks']
            if options.get('seedId'):
                tracks = [track for track in tracks if track['id'] == options['seedId']]
                if not tracks:
                    raise LibraryError('Choose a song in your library to find related songs.')
            if not tracks:
                raise LibraryError('Add a song to your library before finding recommendations.')
            self.refresh_state = {'status': 'working', 'message': 'Finding related song metadata…'}
            self.worker = threading.Thread(target=self._discover, args=(tracks,), daemon=True)
            self.worker.start()
            return self.snapshot()

    def _discover(self, tracks):
        try:
            candidates = discover_candidates(tracks, self.cancel, progress=self._discovery_progress)
            with self.lock:
                if self.closed:
                    return
                for candidate in sorted(candidates, key=lambda value: value['id']):
                    self._add_candidate(candidate)
                self.refresh_state = {'status': 'complete', 'message': 'Suggestions are ready. Audio is fetched only when you listen.'}
        except Exception as exc:
            with self.lock:
                self.refresh_state = {'status': 'error', 'message': str(exc) if isinstance(exc, LibraryError)
                                      else 'Recommendations could not be found. Try again.'}

    def _discovery_progress(self, *values):
        with self.lock:
            self.refresh_state['message'] = str(values[-1]) if values else 'Finding songs…'

    def _add_candidate(self, incoming):
        """Called only with trusted catalog output (also useful for fixture tests)."""
        candidate = copy.deepcopy(incoming)
        if not re.fullmatch(r'[0-9a-f]{32}', candidate.get('id', '')):
            raise LibraryError('The song catalog returned an invalid ID.')
        candidate['sourceUrl'] = normalize_youtube_url(candidate['sourceUrl'])
        equivalent = next((item for item in self.candidates.values() if item['sourceUrl'] == candidate['sourceUrl']
                           or canonical_key(item) == canonical_key(candidate)), None)
        if equivalent and equivalent['id'] != candidate['id']:
            # One lifecycle per recording, even when search finds another upload.
            equivalent['seedIds'] = sorted(set(equivalent.get('seedIds', [])) | set(candidate.get('seedIds', [])))
            self._save(equivalent)
            return
        previous = self.candidates.get(candidate['id'])
        if previous:
            candidate = {**candidate, **previous}  # Lifecycle and first-seen metadata remain stable.
        else:
            candidate.update(state='suggested', completedListens=0, rating=None, availableAt=0, ownedTrackId=None)
        self.candidates[candidate['id']] = candidate
        self._save(candidate)

    def _available(self):
        if self.closed:
            raise LibraryError('Stillroom is shutting down. Reopen it to continue.', 503)

    def _existing(self, candidate):
        for track in self.library.library()['tracks']:
            if track['kind'] != 'original':
                continue
            if canonical_key(track) == canonical_key(candidate) or (track.get('sourceUrl') == candidate['sourceUrl']
                    and track.get('loopStart', 0) == 0 and track.get('loopEnd') is None):
                return track
        return None

    def listen(self, identifier, options):
        if not isinstance(options, dict):
            raise LibraryError('Listening options must be an object.')
        reject_quality_options(options)
        request_id = token(options.get('requestId'), 'requestId')
        allow_native_opus = native_opus(options.get('allowNativeOpus', True))
        with self.lock:
            self._available()
            self._expire()
            candidate = self._candidate(identifier)
            for session in self.sessions.values():
                if request_id in session.get('_requests', [session['requestId']]):
                    if session['candidateId'] != identifier:
                        raise LibraryError('This requestId was already used for a different song.', 409)
                    self._sync(session)
                    return self._public_session(session)
            if candidate['state'] == 'rejected':
                raise LibraryError('This song was thumbs-downed and will not be recommended again.', 409)
            if any(other.get('rating') == 'down' and (canonical_key(other) == canonical_key(candidate)
                   or other['sourceUrl'] == candidate['sourceUrl']) for other in self.candidates.values()):
                raise LibraryError('This recording was already thumbs-downed.', 409)
            for session in self.sessions.values():
                if (session['candidateId'] == identifier and session['status'] in ACTIVE
                        and session.get('allowNativeOpus', True) == allow_native_opus):
                    session.setdefault('_requests', [session['requestId']]).append(request_id)
                    self._save(session=session)
                    self._sync(session)
                    return self._public_session(session)
            session = {'id': uuid.uuid4().hex, 'candidateId': identifier, 'requestId': request_id,
                       'allowNativeOpus': allow_native_opus,
                       'status': 'preparing', 'track': None, 'message': 'Preparing this song for a listen…',
                       '_job': None, '_touched': self.clock(), '_position': 0, '_reported': 0, '_listened': 0,
                       '_seeked': False, '_completed': False, '_lastProgress': self.clock()}
            session['_requests'] = [request_id]
            existing = self._existing(candidate)
            if existing:
                session.update(status='retained', track=existing, message='This recording is already in your library.')
                candidate['state'] = 'retained'
            else:
                self._prepare(candidate, session)
            self.sessions[session['id']] = session
            self._save(candidate, session)
            return self._public_session(session)

    def _prepare(self, candidate, session):
        job = self.import_jobs.create({'source': 'youtube', 'url': candidate['sourceUrl'], 'metadataMode': 'manual',
                                      'allowNativeOpus': session.get('allowNativeOpus', True),
                                      'metadata': {key: candidate.get(key, [] if key == 'tags' else '')
                                                   for key in ('title', 'artist', 'album', 'tags')}, 'exact': True, 'loop': False})
        session.update(status='preparing', _job=job['id'], _touched=self.clock(), track=None,
                       message='Preparing this song for a listen…', _position=0, _reported=0, _listened=0, _seeked=False)
        session.pop('_follows', None)

    def get(self, identifier):
        with self.lock:
            self._expire()
            session = self._session(identifier)
            session['_touched'] = self.clock()
            self._sync(session)
            return self._public_session(session)

    def _sync(self, session):
        if session['status'] != 'preparing':
            return
        if session.get('_follows'):
            active = self._session(session['_follows'])
            self._sync(active)
            session.update({key: copy.deepcopy(active[key]) for key in ('status', 'track', 'message')})
            self._save(session=session)
            return
        candidate = self._candidate(session['candidateId'])
        try:
            job = self.import_jobs.get(session['_job'])
            session['message'] = job.get('message', 'Preparing this song…')
            if job['status'] == 'ready':
                session.update(status='ready', track={**candidate, **job.get('metadata', {}),
                               'id': candidate['id'], 'audioUrl': job['audioUrl'], 'duration': job['duration'],
                               'fileDuration': job['fileDuration'], 'kind': 'original', 'storage': job.get('storage')}, _lastProgress=self.clock(),
                               message='Keep it, pass on it, or leave it unrated.')
                if candidate.get('rating') == 'up':
                    self._retain(candidate, session)
            elif job['status'] in ('error', 'cancelled'):
                raise LibraryError(job.get('error') or job.get('message') or 'This song could not be prepared.')
        except LibraryError as exc:
            session.update(status='error', message=str(exc), track=None)
            candidate.update(state='limbo', availableAt=self.clock() + COOLDOWN)
            if candidate.get('rating') == 'up':
                candidate['message'] = 'Keep requested. Listen again to retry saving this song.'
            self._discard(session)
        self._save(candidate, session)

    def _discard(self, session):
        if session.get('_job'):
            with contextlib.suppress(LibraryError):
                self.import_jobs.discard(session['_job'])
            session['_job'] = None

    def _neutral(self, candidate, session, message):
        self._discard(session)
        existing = self._existing(candidate) if candidate['state'] == 'retained' else None
        if existing:
            session.update(status='retained', track=existing, message='Kept in your library.')
            return
        session.update(status='limbo', track=None, message=message)
        candidate.update(state='limbo', availableAt=self.clock() + COOLDOWN)
        if candidate.get('rating') == 'up':
            session['message'] = candidate['message'] = 'Keep requested. Listen again to finish saving this song.'

    def _retain(self, candidate, session):
        candidate.pop('message', None)
        existing = self._existing(candidate)
        if existing:
            session.update(status='retained', track=existing, message='Kept in your library.')
            candidate['state'] = 'retained'
            self._save(candidate, session)
            self._discard(session)
            return
        job = self.import_jobs.get(session['_job'])
        if job['status'] != 'ready':
            raise LibraryError('Wait for this song to finish preparing before keeping it.', 409)
        source = self.import_jobs.audio(job['id'])
        previous_candidate, previous_session = copy.deepcopy(candidate), copy.deepcopy(session)
        receipt = None
        def before_import(source, destination):
            nonlocal receipt
            receipt = self.receipts / (destination.stem + '.json')
            data = {'app': 'stillroom-recommendation-import', 'filename': destination.name,
                    'sha256': file_hash(source)}
            # This receipt reaches disk before the audio rename. It remains
            # independent of the SQLite transaction until the import commits.
            with receipt.open('x', encoding='utf-8') as output:
                json.dump(data, output)
                output.flush()
                os.fsync(output.fileno())
            if os.name != 'nt':
                descriptor = os.open(self.receipts, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        def record_ownership(conn, track):
            filename = Path(track['audioUrl']).name
            digest = file_hash(self.library.audio / filename)
            conn.execute('INSERT INTO recommendation_ownership VALUES (?,?,?,?,?)',
                         (track['id'], candidate['id'], filename, digest, 'owned'))
            candidate.update(state='retained', ownedTrackId=track['id'])
            session.update(status='retained', track=track, message='Kept in your library.')
            self._save(candidate, session, conn)
        try:
            # Stage under ImportJobs' process-owned preview folder. Hard-killed
            # copies/conversions are reclaimed along with the stopped preview.
            self.library.import_file(source, job['metadata'], _on_import=record_ownership,
                                     _before_import=before_import, _staging_dir=source.parent,
                                     _provenance=job.get('storage'))
        except Exception as exc:
            candidate.clear(); candidate.update(previous_candidate)
            session.clear(); session.update(previous_session)
            if isinstance(exc, LibraryError) and exc.status == 409:
                # A normal import may have won a race, or already own identical
                # audio under other metadata. Reuse that copy without claiming it.
                existing = self._existing(candidate)
                if existing is None:
                    digest = (job.get('storage') or {}).get('source', {}).get('sha256') or file_hash(source)
                    with self.library.connect() as conn:
                        row = conn.execute('SELECT * FROM tracks WHERE sha256=? OR source_sha256=? ORDER BY added_at,id LIMIT 1', (digest, digest)).fetchone()
                        existing = self.library.track(row) if row else None
                if existing:
                    session.update(status='retained', track=existing, message='This audio is already in your library.')
                    candidate['state'] = 'retained'
                    self._save(candidate, session)
                    self._discard(session)
                    return
            raise
        finally:
            # On an ordinary exception Library removed its new destination. On
            # a hard crash this receipt survives for startup reconciliation.
            if receipt:
                self._cleanup_pending()
        self._discard(session)

    def _cleanup_pending(self):
        if self.receipts.is_symlink():
            return
        for receipt in self.receipts.glob('*.json'):
            if receipt.is_symlink():
                continue
            try:
                data = json.loads(receipt.read_text(encoding='utf-8'))
                filename = data.get('filename', '')
                if (data.get('app') != 'stillroom-recommendation-import'
                        or not re.fullmatch(r'[0-9a-f]{32}\.(mp3|m4a|aac|wav|flac|opus|ogg|webm)', filename)
                        or receipt.stem != Path(filename).stem):
                    continue
                with self.library.connect() as conn:
                    live = conn.execute('SELECT 1 FROM tracks WHERE audio_filename=?', (filename,)).fetchone()
                path = self.library.audio / filename
                if not live and path.exists():
                    if path.is_symlink() or file_hash(path) != data.get('sha256'):
                        continue
                    path.unlink()
                receipt.unlink()
            except (OSError, ValueError, TypeError, AttributeError):
                continue

    def _remove_owned(self, candidate, conn):
        identifier = candidate.get('ownedTrackId')
        if not identifier:
            return
        owner = conn.execute('SELECT * FROM recommendation_ownership WHERE track_id=? AND candidate_id=? AND state=?',
                             (identifier, candidate['id'], 'owned')).fetchone()
        track = conn.execute('SELECT * FROM tracks WHERE id=?', (identifier,)).fetchone()
        # Never infer deletion ownership from a title, source URL, hash duplicate,
        # or a matching pre-existing library record.
        if owner and track and owner['filename'] == track['audio_filename']:
            filename = owner['filename']
            path = self.library.audio / filename
            if (not re.fullmatch(r'[0-9a-f]{32}\.(mp3|m4a|aac|wav|flac|opus|ogg|webm)', filename)
                    or path.is_symlink() or path.parent.resolve() != self.library.audio.resolve()
                    or (path.exists() and file_hash(path) != owner['sha256'])):
                # Somebody replaced this copy outside Stillroom. Ownership no
                # longer proves those bytes belong to recommendations.
                candidate['ownedTrackId'] = None
                return
            conn.execute('DELETE FROM playlist_tracks WHERE track_id=?', (identifier,))
            conn.execute('DELETE FROM tracks WHERE id=?', (identifier,))
            conn.execute("UPDATE recommendation_ownership SET state='garbage' WHERE track_id=?", (identifier,))
        candidate['ownedTrackId'] = None

    def _cleanup_garbage(self):
        with self.library.connect() as conn:
            rows = list(conn.execute("SELECT * FROM recommendation_ownership WHERE state='garbage'"))
            for row in rows:
                filename = row['filename']
                if not re.fullmatch(r'[0-9a-f]{32}\.(mp3|m4a|aac|wav|flac|opus|ogg|webm)', filename):
                    continue
                path = self.library.audio / filename
                if path.is_symlink() or path.parent.resolve() != self.library.audio.resolve():
                    continue
                if conn.execute('SELECT 1 FROM tracks WHERE audio_filename=?', (filename,)).fetchone():
                    continue
                try:
                    if path.exists():
                        if file_hash(path) != row['sha256']:
                            continue
                        path.unlink()
                    conn.execute('DELETE FROM recommendation_ownership WHERE track_id=?', (row['track_id'],))
                except OSError:
                    continue  # Keep the receipt and retry on the next sweep/restart.
            conn.commit()

    def _progress(self, session, options):
        position = number(options.get('position', session['_position']), 'position')
        reported = number(options.get('listenedSeconds', session['_reported']), 'listenedSeconds')
        rate = number(options.get('playbackRate', 1), 'playbackRate')
        seeked = options.get('seeked', False)
        if position < 0 or reported < 0 or not .25 <= rate <= 4 or not isinstance(seeked, bool):
            raise LibraryError('Provide valid playback progress and playbackRate between 0.25 and 4.')
        elapsed = max(0, self.clock() - session['_lastProgress'])
        delta, heard = position - session['_position'], reported - session['_reported']
        # Client counts only intervals while the audio advances. Bound reports by
        # elapsed media time too, so seeking to the end cannot count as a listen.
        # The client reports its peak rate since this listen began, so lowering
        # playback speed within a reporting interval cannot reject a valid play.
        session['_maxRate'] = max(rate, session.get('_maxRate', .25))
        allowance = elapsed * session['_maxRate'] * 1.25 + .5
        if delta < -.1 or heard < -.1 or delta > allowance + .5 or delta > heard + 1:
            seeked = True
        session['_seeked'] |= seeked
        session['_listened'] += max(0, min(delta, heard, allowance))
        session.update(_position=position, _reported=reported, _lastProgress=self.clock())

    def event(self, identifier, options):
        if not isinstance(options, dict):
            raise LibraryError('Playback event must be an object.')
        event_id = token(options.get('eventId'), 'eventId')
        kind = options.get('type')
        if kind not in ('progress', 'complete', 'skip', 'up', 'down'):
            raise LibraryError('Choose progress, complete, skip, up or down.')
        payload = json.dumps(options, sort_keys=True, allow_nan=False)
        with self.lock:
            self._available()
            session = self._session(identifier)
            candidate = self._candidate(session['candidateId'])
            with self.library.connect() as conn:
                existing = conn.execute('SELECT payload FROM recommendation_events WHERE session_id=? AND event_id=?',
                                        (identifier, event_id)).fetchone()
            if existing:
                if existing['payload'] != payload:
                    raise LibraryError('This eventId was already used for a different event.', 409)
                return self._public_session(session)
            self._sync(session)
            session['_touched'] = self.clock()
            if kind == 'down':
                related_candidates = [item for item in self.candidates.values()
                                      if item['sourceUrl'] == candidate['sourceUrl'] or canonical_key(item) == canonical_key(candidate)]
                related_ids = {item['id'] for item in related_candidates}
                for item in related_candidates:
                    item.update(state='rejected', rating='down')
                for related in self.sessions.values():
                    if related['candidateId'] in related_ids:
                        self._discard(related)
                        related.update(status='rejected', track=None, message='Passed on. Similar suggestions will rank lower.')
                with self.library.connect() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    for item in related_candidates:
                        self._remove_owned(item, conn)
                        self._save(candidate=item, conn=conn)
                    for related in self.sessions.values():
                        if related['candidateId'] in related_ids:
                            self._save(session=related, conn=conn)
                    conn.execute('INSERT INTO recommendation_events VALUES (?,?,?)', (identifier, event_id, payload))
                    conn.commit()
                self._cleanup_garbage()
                return self._public_session(session)
            if candidate['state'] == 'rejected':
                return self._public_session(session)
            if kind == 'up':
                candidate['rating'] = 'up'
                if session['status'] == 'ready':
                    self._retain(candidate, session)
                elif session['status'] not in ('preparing', 'retained'):
                    existing = self._existing(candidate)
                    active = next((other for other in self.sessions.values() if other['candidateId'] == candidate['id']
                                   and other['status'] in ACTIVE and other.get('allowNativeOpus', True) == session.get('allowNativeOpus', True)), None)
                    if existing:
                        candidate['state'] = 'retained'
                        session.update(status='retained', track=existing, message='Kept in your library.')
                    elif active:
                        # An old tab's Keep action joins the current fetch; it
                        # must not start another download for the same recording.
                        session.update(status='preparing', _follows=active['id'], _job=None,
                                       message='Keep requested. Saving this song…')
                        self._sync(session)
                    else:
                        self._prepare(candidate, session)
            elif session['status'] in ACTIVE:
                if kind in ('progress', 'complete') and session['status'] == 'ready':
                    self._progress(session, options)
                if kind == 'complete' and session['status'] == 'ready' and not session['_completed']:
                    duration = session['track']['duration']
                    finished = (not session['_seeked'] and session['_listened'] >= duration * COMPLETION_THRESHOLD
                                and session['_position'] >= duration - min(.75, duration * .01))
                    session['_completed'] = True
                    if finished:
                        candidate['completedListens'] += 1
                    if finished and candidate['completedListens'] >= LISTENS_TO_KEEP:
                        try:
                            self._retain(candidate, session)
                        except Exception:
                            # Retry the same completion after a disk/DB error.
                            # This attempt has not been recorded as an event.
                            candidate['completedListens'] -= 1
                            session['_completed'] = False
                            raise
                    else:
                        self._neutral(candidate, session, 'Audio removed; song details saved for another listen.')
                elif kind == 'skip':
                    if candidate.get('rating') == 'up':
                        session['message'] = 'Keep requested. Saving will continue in the background.'
                    else:
                        self._neutral(candidate, session, 'Skipped without changing your preferences. Audio removed.')
            with self.library.connect() as conn:
                self._save(candidate, session, conn)
                conn.execute('INSERT INTO recommendation_events VALUES (?,?,?)', (identifier, event_id, payload))
                conn.commit()
            return self._public_session(session)

    def _expire(self):
        for session in self.sessions.values():
            if session['status'] in ACTIVE and self.clock() - session['_touched'] > SESSION_TTL:
                candidate = self._candidate(session['candidateId'])
                self._neutral(candidate, session, 'This listening session expired. Song details are saved for later.')
                self._save(candidate, session)

    def _sweep(self):
        while not self.cancel.wait(30):
            try:
                self._maintain()
            except Exception:
                # A temporary disk/DB failure must not permanently disable
                # cleanup or completion of a previously requested Keep.
                import logging
                logging.getLogger(__name__).exception('Recommendation maintenance will retry.')

    def _maintain(self):
        with self.lock:
            for session in self.sessions.values():
                if session['status'] == 'preparing' and self._candidate(session['candidateId']).get('rating') == 'up':
                    self._sync(session)
            self._expire()
            self._cleanup_garbage()

    def close(self):
        with self.lock:
            self.closed = True
            self.cancel.set()
            for session in self.sessions.values():
                if session['status'] in ACTIVE:
                    candidate = self._candidate(session['candidateId'])
                    self._neutral(candidate, session, 'Playback was interrupted. Song details are saved for later.')
                    self._save(candidate, session)
        if self.worker:
            self.worker.join(3)
        self.sweeper.join(1)
