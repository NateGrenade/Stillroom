"""Durable library operations shared by the HTTP service and import command."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
import uuid

from .runtime import find_executable, subprocess_options

MAX_AUDIO_BYTES = 512 * 1024 * 1024
SCHEMA_VERSION = 2
# Only self-contained media demuxers belong here. In particular, HLS, DASH,
# concat and playlist formats can open resources named inside an uploaded file.
LOCAL_AUDIO_FORMATS = (
    "aac,ac3,aiff,amr,ape,asf,au,avi,caf,dsf,eac3,flac,flv,"
    "matroska,webm,mov,mp4,m4a,3gp,3g2,mj2,mp3,mpeg,mpegts,"
    "ogg,oma,tta,w64,wav,wv"
)


class LibraryError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def executable(name):
    found = find_executable(name)
    if found:
        return found
    raise LibraryError(f"{name} is required for audio imports. Restore the bundled audio tools or install the missing tool, then try again.", 503)


def clean_text(value, field, limit=300, required=False):
    if not isinstance(value, str):
        raise LibraryError(f"{field} must be text.")
    value = value.strip()
    if (required and not value) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise LibraryError(f"{field} must contain {'1 to ' if required else 'at most '}{limit} characters without control characters.")
    return value


def number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise LibraryError(f"{field} must be a finite number.")
    return float(value)


def metadata_values(metadata, fallback_title, file_duration):
    if not isinstance(metadata, dict):
        raise LibraryError("Track metadata must be a JSON object.")
    title = clean_text(metadata.get("title", fallback_title), "title", required=True)
    artist = clean_text(metadata.get("artist", ""), "artist")
    album = clean_text(metadata.get("album", ""), "album")
    kind = metadata.get("kind", "original")
    if kind not in ("original", "loop"):
        raise LibraryError("kind must be original or loop.")
    start = number(metadata.get("loopStart", 0), "loopStart")
    end = metadata.get("loopEnd")
    if end is not None:
        end = number(end, "loopEnd")
    effective_end = file_duration if end is None else end
    if start < 0 or effective_end > file_duration or start >= effective_end:
        raise LibraryError("Loop bounds must satisfy 0 <= loopStart < loopEnd <= fileDuration.")
    tags = metadata.get("tags", [])
    if not isinstance(tags, list) or len(tags) > 32:
        raise LibraryError("tags must be a list of at most 32 text labels.")
    tags = list(dict.fromkeys(clean_text(tag, "tag", 100, required=True) for tag in tags))
    source = clean_text(metadata.get("sourceUrl", ""), "sourceUrl", 2048)
    if source:
        from urllib.parse import urlsplit
        try:
            parsed = urlsplit(source)
            if parsed.scheme not in ("https", "http") or not parsed.netloc:
                raise ValueError()
        except ValueError:
            raise LibraryError("sourceUrl must be an http or https URL.")
    return {
        "title": title, "artist": artist, "album": album, "kind": kind,
        "loopStart": start, "loopEnd": end, "tags": tags, "sourceUrl": source,
        "duration": effective_end - start if kind == "loop" else file_duration,
    }


def local_audio_input_options(path=None, *, for_probe=False):
    """Restrict tools to local media bytes, never playlists or remote protocols.

    MOV external tracks are disabled by FFmpeg by default. Make this explicit
    for probes and byte-recognized MOV inputs too; ffmpeg rejects MOV-private
    options on other demuxers. A filename extension is never used to select a
    format. The demuxer whitelist remains enforced for every input.
    """
    options = ["-protocol_whitelist", "file", "-format_whitelist", LOCAL_AUDIO_FORMATS]
    mov = for_probe
    if path is not None and not mov:
        try:
            with open(path, "rb") as source:
                header = source.read(8)
            atom_size = int.from_bytes(header[:4], "big")
            mov = (len(header) == 8 and (atom_size == 1 or 8 <= atom_size <= MAX_AUDIO_BYTES)
                   and header[4:8] in (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip", b"pnot", b"uuid"))
        except OSError:
            pass  # The tool reports unreadable input; no protocol is enabled.
    if mov:
        options.extend(["-enable_drefs", "0", "-use_absolute_path", "0"])
    return options


def _webm_doctype(path):
    """Read the bounded EBML header; FFprobe names Matroska and WebM alike."""
    try:
        with Path(path).open("rb") as source:
            header = source.read(4096)
        def vint(offset, identifier=False):
            first = header[offset]
            width = next((width for width in range(1, 9) if first & (1 << (8-width))), None)
            if width is None or offset+width > len(header) or (identifier and width > 4):
                raise ValueError()
            value = int.from_bytes(header[offset:offset+width], "big")
            return (value if identifier else value & ((1 << (7*width))-1)), offset+width
        if header[:4] != b"\x1a\x45\xdf\xa3":
            return False
        length, cursor = vint(4)
        finish = cursor+length
        if finish > len(header):
            return False
        doctype = None
        while cursor < finish:
            element, cursor = vint(cursor, True)
            size, cursor = vint(cursor)
            if cursor+size > finish:
                return False
            if element == 0x4282:
                if doctype is not None:
                    return False
                doctype = header[cursor:cursor+size]
            cursor += size
        return cursor == finish and doctype == b"webm"
    except (OSError, ValueError, IndexError):
        return False


def inspect_audio(path):
    try:
        source = Path(path).resolve()
        if not source.is_file():
            raise LibraryError("Choose an existing audio file.")
        if source.stat().st_size > MAX_AUDIO_BYTES:
            raise LibraryError("Audio imports are limited to 512 MB.", 413)
        result = subprocess.run(
            [executable("ffprobe"), "-v", "error", *local_audio_input_options(for_probe=True),
             "-show_entries", "format=format_name,duration,bit_rate:stream=codec_type,codec_name,duration,bit_rate",
             "-of", "json", str(source)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, **subprocess_options(),
        )
        info = json.loads(result.stdout) if result.returncode == 0 else {}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        raise LibraryError("The audio file could not be inspected.")
    if not isinstance(info, dict) or not isinstance(info.get("streams"), list) or not isinstance(info.get("format", {}), dict):
        raise LibraryError("The audio file could not be inspected.")
    streams = [s for s in info["streams"] if isinstance(s, dict) and s.get("codec_type") == "audio"]
    if not streams:
        raise LibraryError("The file does not contain playable audio.")
    try:
        duration = float(info.get("format", {}).get("duration") or streams[0].get("duration", 0))
    except (ValueError, TypeError):
        duration = 0
    if not math.isfinite(duration) or duration <= 0:
        raise LibraryError("The audio file has no valid duration.")
    codec = streams[0].get("codec_name", "")
    format_name = info.get("format", {}).get("format_name", "")
    if not isinstance(codec, str) or not re.fullmatch(r"[a-zA-Z0-9_]{1,80}", codec) or not isinstance(format_name, str):
        raise LibraryError("The audio file could not be inspected.")
    formats = {value for value in format_name.split(",") if re.fullmatch(r"[a-zA-Z0-9_]{1,80}", value)}
    if not formats:
        raise LibraryError("The audio file could not be inspected.")
    extension = None
    if "mp3" in formats and codec == "mp3":
        extension = ".mp3"
    elif "wav" in formats and codec.startswith("pcm_"):
        extension = ".wav"
    elif "flac" in formats and codec == "flac":
        extension = ".flac"
    elif formats.intersection({"mov", "mp4", "m4a"}) and codec in ("aac", "alac"):
        extension = ".m4a"
    elif "aac" in formats and codec == "aac":
        extension = ".aac"
    elif "ogg" in formats and codec == "opus":
        extension = ".opus"
    elif "webm" in formats and codec == "opus" and _webm_doctype(source):
        extension = ".webm"
    # Opus is preserved only when the container is audio-only. Other containers
    # can be remuxed by the storage policy without decoding the audio.
    video = any(isinstance(s, dict) and s.get("codec_type") == "video" for s in info["streams"])
    if codec == "opus" and video:
        extension = None
    try:
        bitrate = int(streams[0].get("bit_rate") or 0)
    except (ValueError, TypeError, OverflowError):
        bitrate = 0
    estimated = bitrate <= 0
    size = source.stat().st_size
    if estimated:
        bitrate = round(size * 8 / duration)
    return {"duration": duration, "extension": extension, "codec": codec,
            "container": "webm" if "webm" in formats and _webm_doctype(source) else "matroska" if "matroska" in formats else "mp4" if "mov" in formats else sorted(formats)[0],
            "bitrate": bitrate if 0 < bitrate <= 10**10 else None, "bitrateEstimated": estimated,
            "bytes": size, "video": video}


def probe(path):
    info = inspect_audio(path)
    return info["duration"], info["extension"]


class Library:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.data = self.root / "data"
        self.audio = self.root / "audio"
        self.public = self.root / "public"
        self.data.mkdir(parents=True, exist_ok=True)
        self.audio.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data / "library.sqlite3"
        with self.connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise LibraryError("This library was created by a newer Stillroom version.", 503)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tracks (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, artist TEXT NOT NULL,
                    album TEXT NOT NULL, duration REAL NOT NULL, file_duration REAL NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('original','loop')),
                    audio_filename TEXT NOT NULL UNIQUE, loop_start REAL NOT NULL,
                    loop_end REAL, tags_json TEXT NOT NULL, source_url TEXT NOT NULL,
                    added_at TEXT NOT NULL, sha256 TEXT NOT NULL, file_size INTEGER NOT NULL,
                    title_key TEXT NOT NULL, artist_key TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tracks_hash ON tracks(sha256);
                CREATE INDEX IF NOT EXISTS tracks_name ON tracks(title_key, artist_key, kind);
                CREATE TABLE IF NOT EXISTS playlists (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS playlist_tracks (
                    playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                    track_id TEXT NOT NULL REFERENCES tracks(id), position INTEGER NOT NULL,
                    PRIMARY KEY(playlist_id, track_id), UNIQUE(playlist_id, position)
                );
            """)
            conn.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tracks)")}
            if "source_sha256" not in columns:
                conn.execute("ALTER TABLE tracks ADD COLUMN source_sha256 TEXT NOT NULL DEFAULT ''")
            if "storage_json" not in columns:
                conn.execute("ALTER TABLE tracks ADD COLUMN storage_json TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS tracks_source_hash ON tracks(source_sha256)")
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
        self.deletions = self.data / "audio-deletions"
        self._recover_deletions()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return contextlib.closing(_ConnectionContext(conn))

    @staticmethod
    def track(row):
        return {
            "id": row["id"], "title": row["title"], "artist": row["artist"],
            "album": row["album"], "duration": row["duration"], "fileDuration": row["file_duration"],
            "kind": row["kind"], "audioUrl": "/audio/" + row["audio_filename"],
            "loopStart": row["loop_start"], "loopEnd": row["loop_end"],
            "tags": json.loads(row["tags_json"]), "sourceUrl": row["source_url"], "addedAt": row["added_at"],
            "storage": json.loads(row["storage_json"]) if "storage_json" in row.keys() and row["storage_json"] else None,
        }

    @staticmethod
    def playlist(conn, row):
        return {"id": row["id"], "name": row["name"], "createdAt": row["created_at"],
                "trackIds": [r[0] for r in conn.execute(
                    "SELECT track_id FROM playlist_tracks WHERE playlist_id=? ORDER BY position", (row["id"],))]}

    def library(self):
        with self.connect() as conn:
            conn.execute("BEGIN")
            tracks = [self.track(row) for row in conn.execute("SELECT * FROM tracks ORDER BY added_at, rowid")]
            playlists = [self.playlist(conn, row) for row in conn.execute("SELECT * FROM playlists ORDER BY created_at, rowid")]
            return {"tracks": tracks, "playlists": playlists}

    def import_file(self, path, metadata=None, allow_duplicate=False, filename=None, *, allow_native_opus=True, _provenance=None, _on_import=None, _before_import=None, _staging_dir=None):
        from .storage_policy import prepare_original, native_opus, file_hash
        native_opus(allow_native_opus)
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise LibraryError("Choose an existing audio file.")
        if source.stat().st_size > MAX_AUDIO_BYTES:
            raise LibraryError("Audio imports are limited to 512 MB.", 413)
        title_name = Path(filename or source.name).stem
        task_temp = None
        converted_temp = None
        destination = None
        try:
            handle, task_temp = tempfile.mkstemp(prefix=".import-", dir=_staging_dir or self.audio)
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(handle, "wb") as output, source.open("rb") as input_file:
                while True:
                    chunk = input_file.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_AUDIO_BYTES:
                        raise LibraryError("Audio imports are limited to 512 MB.", 413)
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if not size:
                raise LibraryError("The audio file is empty.")
            duration, extension = probe(task_temp)
            values = metadata_values({} if metadata is None else metadata, title_name, duration)
            incoming_hash = digest.hexdigest()
            if _provenance is None:
                if values["kind"] == "loop" and extension in (None, ".opus"):
                    raise LibraryError("Prepare this loop in the import preview so its exact boundaries can be verified and saved as lossless FLAC.")
                if values["kind"] == "loop" and not allow_native_opus and inspect_audio(task_temp)["codec"] == "opus":
                    raise LibraryError("Prepare this loop in the import preview so browser-compatible audio can retain verified sample boundaries.")
                stored = prepare_original(task_temp, _staging_dir or self.audio, allow_native_opus=allow_native_opus)
                converted_temp = stored["file"] if Path(stored["file"]) != Path(task_temp) else None
                storage = stored["storage"]
                if converted_temp is not None:
                    os.unlink(task_temp)
                    task_temp, converted_temp = converted_temp, None
                duration, extension = probe(task_temp)
                values = metadata_values({} if metadata is None else metadata, title_name, duration)
            else:
                # Only preview owners supply this internal argument. Verify the
                # exact preview bytes before using its earlier source identity.
                storage = json.loads(json.dumps(_provenance, allow_nan=False))
                if (not isinstance(storage, dict) or storage.get("stored", {}).get("sha256") != incoming_hash
                        or storage.get("stored", {}).get("bytes") != size or extension is None):
                    raise LibraryError("The prepared audio changed. Prepare this track again.", 409)
            size = os.path.getsize(task_temp)
            stored_hash = file_hash(task_temp)
            original_hash = storage["source"]["sha256"]
            duplicate_hash = stored_hash if storage["transformation"] == "edited_lossless" else original_hash
            # Windows FlushFileBuffers requires a writable handle.
            with open(task_temp, "rb+") as converted_file:
                os.fsync(converted_file.fileno())
            track_id = uuid.uuid4().hex
            audio_filename = track_id + extension
            destination = self.audio / audio_filename
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if not allow_duplicate:
                    duplicate = conn.execute(
                        "SELECT title FROM tracks WHERE sha256 IN (?,?) OR (title_key=? AND artist_key=? AND kind=?) LIMIT 1",
                        (duplicate_hash, stored_hash, values["title"].casefold(), values["artist"].casefold(), values["kind"]),
                    ).fetchone()
                    if duplicate is None and storage["transformation"] != "edited_lossless":
                        # A whole recording and a selection from it are different
                        # saved audio. Source identity only deduplicates whole
                        # imports, including compatibility conversions.
                        duplicate = next((row for row in conn.execute(
                            "SELECT title,storage_json FROM tracks WHERE source_sha256=?", (original_hash,))
                            if row["storage_json"] and json.loads(row["storage_json"]).get("transformation") != "edited_lossless"), None)
                    if duplicate:
                        raise LibraryError(f"This audio or title is already in the library: {duplicate['title']}. For a different excerpt, edit its title before saving.", 409)
                if _before_import is not None:
                    _before_import(Path(task_temp), destination)
                os.replace(task_temp, destination)
                task_temp = None
                conn.execute("""INSERT INTO tracks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    track_id, values["title"], values["artist"], values["album"], values["duration"], duration,
                    values["kind"], audio_filename, values["loopStart"], values["loopEnd"], json.dumps(values["tags"]),
                    values["sourceUrl"], now(), stored_hash, size,
                    values["title"].casefold(), values["artist"].casefold(),
                    original_hash, json.dumps(storage, allow_nan=False),
                ))
                row = conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
                # Internal lifecycle owners can persist provenance in this same
                # transaction. A callback failure rolls back both records and
                # removes only the new destination created by this import.
                if _on_import is not None:
                    _on_import(conn, self.track(row))
                conn.commit()
                destination = None
                return self.track(row)
        finally:
            for item in (task_temp, converted_temp, destination):
                if item is not None:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(item)

    def update_track(self, track_id, changes):
        """Change display metadata without changing the saved audio or identity."""
        fields = ("title", "artist", "album")
        if not isinstance(changes, dict) or not changes or any(key not in fields for key in changes):
            raise LibraryError("Provide title, artist and/or album to edit a song's info.")
        values = {field: clean_text(value, field, required=field == "title")
                  for field, value in changes.items()}
        with self.connect() as conn:
            # Serialize with imports, other edits and deletions. An edit that
            # waits for a deletion must return 404, never recreate the song.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
            if row is None:
                raise LibraryError("Song not found. It may already have been deleted.", 404)
            updated = {field: values.get(field, row[field]) for field in fields}
            conn.execute("""UPDATE tracks SET title=?, artist=?, album=?, title_key=?, artist_key=? WHERE id=?""",
                         (updated["title"], updated["artist"], updated["album"],
                          updated["title"].casefold(), updated["artist"].casefold(), track_id))
            result = self.track(conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone())
            conn.commit()
            return result

    def _recover_deletions(self):
        """Finish committed deletes, or restore audio after an interrupted save."""
        if not self.deletions.exists() or self.deletions.is_symlink() or self.audio.is_symlink():
            return
        with self.connect() as conn:
            # Another process may currently be between its audio move and DB
            # commit. Wait for that transaction before interpreting its receipt.
            conn.execute("BEGIN IMMEDIATE")
            for receipt in self.deletions.glob("*.json"):
                if receipt.is_symlink() or not re.fullmatch(r"[0-9a-f]{32}", receipt.stem):
                    continue
                try:
                    data = json.loads(receipt.read_text(encoding="utf-8"))
                    filename = data.get("filename", "")
                    if (data.get("app") != "stillroom-track-deletion" or
                            not re.fullmatch(re.escape(receipt.stem) + r"\.(mp3|m4a|aac|wav|flac|opus|ogg|webm)", filename)):
                        continue
                    staged = self.deletions / (receipt.stem + ".audio")
                    original = self.audio / filename
                    if staged.is_symlink() or original.is_symlink():
                        continue
                    row = conn.execute("SELECT audio_filename FROM tracks WHERE id=?", (receipt.stem,)).fetchone()
                    if row:
                        if row["audio_filename"] != filename:
                            continue
                        if staged.exists():
                            if original.exists():
                                continue  # Never overwrite an independently restored copy.
                            os.replace(staged, original)
                    else:
                        staged.unlink(missing_ok=True)
                    receipt.unlink(missing_ok=True)
                except (OSError, ValueError, TypeError, AttributeError):
                    # A locked file or temporary permission issue can be retried
                    # on the next launch without making the library unavailable.
                    continue

    def delete_track(self, track_id, *, _on_delete=None):
        """Delete one saved track and its private copy, never its upload source.

        The receipt is written before moving audio. A failed transaction puts
        the copy back; startup recovery handles a process interrupted mid-delete.
        Lifecycle owners may update their records in the same transaction.
        """
        receipt = staged = original = None
        receipt_created = False
        committed = False
        try:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
                if row is None:
                    raise LibraryError("Song not found. It may already have been deleted.", 404)
                filename = row["audio_filename"]
                original = self.audio / filename
                if (not re.fullmatch(r"[0-9a-f]{32}", row["id"])
                        or not re.fullmatch(re.escape(row["id"]) + r"\.(mp3|m4a|aac|wav|flac|opus|ogg|webm)", filename)
                        or self.audio.is_symlink() or original.is_symlink()
                        or (original.exists() and not original.is_file())):
                    raise LibraryError("This song's audio location has changed. Restore its library copy before deleting it.", 409)
                if original.exists():
                    self.deletions.mkdir(exist_ok=True)
                    if self.deletions.is_symlink():
                        raise LibraryError("The library's deletion folder has changed. Restore it before deleting songs.", 409)
                    receipt = self.deletions / (row["id"] + ".json")
                    staged = self.deletions / (row["id"] + ".audio")
                    if receipt.exists() or staged.exists() or staged.is_symlink():
                        raise LibraryError("An earlier deletion needs recovery. Restart Stillroom and try again.", 409)
                    with receipt.open("x", encoding="utf-8") as output:
                        receipt_created = True
                        json.dump({"app": "stillroom-track-deletion", "filename": filename}, output)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(original, staged)
                conn.execute("DELETE FROM playlist_tracks WHERE track_id=?", (track_id,))
                conn.execute("DELETE FROM tracks WHERE id=?", (track_id,))
                if _on_delete is not None:
                    _on_delete(conn, self.track(row))
                conn.commit()
                committed = True
        except OSError as exc:
            raise LibraryError("The song could not be deleted. Check folder permissions and try again.", 503) from exc
        finally:
            if not committed and receipt_created:
                # Keep the receipt if restoration itself fails; startup recovery
                # can retry safely while the database still contains the track.
                try:
                    if staged is not None and staged.exists() and not original.exists():
                        os.replace(staged, original)
                    if staged is None or not staged.exists():
                        receipt.unlink(missing_ok=True)
                except OSError:
                    pass
        cleanup_pending = False
        if staged is not None:
            try:
                staged.unlink(missing_ok=True)
                receipt.unlink(missing_ok=True)
            except OSError:
                cleanup_pending = True
        return {"success": True, "trackId": track_id, "cleanupPending": cleanup_pending}

    def create_playlist(self, name):
        name = clean_text(name, "Playlist name", 120, required=True)
        with self.connect() as conn:
            try:
                playlist_id = uuid.uuid4().hex
                conn.execute("INSERT INTO playlists VALUES (?,?,?,?)", (playlist_id, name, name.casefold(), now()))
                conn.commit()
            except sqlite3.IntegrityError:
                raise LibraryError("A playlist with that name already exists.", 409)
            return self.playlist(conn, conn.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)).fetchone())

    def update_playlist(self, playlist_id, changes):
        if not isinstance(changes, dict) or not changes or any(key not in ("name", "trackIds") for key in changes):
            raise LibraryError("Provide name and/or trackIds to update a playlist.")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)).fetchone()
            if row is None:
                raise LibraryError("Playlist not found.", 404)
            if "name" in changes:
                name = clean_text(changes["name"], "Playlist name", 120, required=True)
                try:
                    conn.execute("UPDATE playlists SET name=?, name_key=? WHERE id=?", (name, name.casefold(), playlist_id))
                except sqlite3.IntegrityError:
                    raise LibraryError("A playlist with that name already exists.", 409)
            if "trackIds" in changes:
                ids = changes["trackIds"]
                if not isinstance(ids, list) or len(ids) > 10000 or any(not isinstance(i, str) for i in ids):
                    raise LibraryError("trackIds must be a list of track IDs.")
                if len(set(ids)) != len(ids):
                    raise LibraryError("A playlist cannot contain the same track twice.")
                for track_id in ids:
                    if not conn.execute("SELECT 1 FROM tracks WHERE id=?", (track_id,)).fetchone():
                        raise LibraryError("One or more tracks are missing from the library.")
                conn.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (playlist_id,))
                conn.executemany("INSERT INTO playlist_tracks VALUES (?,?,?)", ((playlist_id, track_id, position) for position, track_id in enumerate(ids)))
            result = self.playlist(conn, conn.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)).fetchone())
            conn.commit()
            return result

    def delete_playlist(self, playlist_id):
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
            if not cursor.rowcount:
                raise LibraryError("Playlist not found.", 404)
            conn.commit()
        return {"success": True}

    def add_to_playlist_id(self, playlist_id, track_id, preferred_order=()):
        """Append to an existing playlist, keeping this batch in source order."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)).fetchone()
            if row is None:
                raise LibraryError("The destination playlist was deleted. Start a new playlist import.", 404)
            if not conn.execute("SELECT 1 FROM tracks WHERE id=?", (track_id,)).fetchone():
                raise LibraryError("Track not found.", 404)
            current = self.playlist(conn, row)["trackIds"]
            if track_id not in current:
                current.append(track_id)
            ordered = list(dict.fromkeys(identifier for identifier in preferred_order if identifier in current))
            ordered.extend(identifier for identifier in current if identifier not in ordered)
            conn.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (playlist_id,))
            conn.executemany("INSERT INTO playlist_tracks VALUES (?,?,?)", ((playlist_id, identifier, position) for position, identifier in enumerate(ordered)))
            result = self.playlist(conn, row)
            conn.commit()
            return result

    def add_to_playlist(self, name, track_id):
        name = clean_text(name, "Playlist name", 120, required=True)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM playlists WHERE name_key=?", (name.casefold(),)).fetchone()
            if row is None:
                playlist_id = uuid.uuid4().hex
                conn.execute("INSERT INTO playlists VALUES (?,?,?,?)", (playlist_id, name, name.casefold(), now()))
                row = conn.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)).fetchone()
            playlist_id = row["id"]
            exists = conn.execute("SELECT 1 FROM playlist_tracks WHERE playlist_id=? AND track_id=?", (playlist_id, track_id)).fetchone()
            if not exists:
                position = conn.execute("SELECT COALESCE(MAX(position), -1)+1 FROM playlist_tracks WHERE playlist_id=?", (playlist_id,)).fetchone()[0]
                try:
                    conn.execute("INSERT INTO playlist_tracks VALUES (?,?,?)", (playlist_id, track_id, position))
                except sqlite3.IntegrityError:
                    raise LibraryError("Track not found.", 404)
            result = self.playlist(conn, row)
            conn.commit()
            return result


class _ConnectionContext:
    """sqlite's context manager commits/rolls back but does not close itself."""
    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def close(self):
        self.connection.rollback()
        self.connection.close()
