#!/usr/bin/env python3
"""Export an explicit Stillroom library selection without re-encoding or secrets.

The destination is a new, self-contained .stillroomlibrary transfer file. This is
not an app release, a database backup, or a live synchronization mechanism.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import struct
import sys
import tempfile
import unicodedata
from urllib.parse import parse_qs, urlsplit
import uuid

MAGIC = b"STLRMB01"
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_AUDIO_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024 * 1024
MAX_TRACKS = 10000
CHUNK_BYTES = 1024 * 1024
EXTENSIONS = {"m4a", "mp3", "aac", "wav", "flac", "opus", "ogg", "webm"}
ID = re.compile(r"[0-9a-f]{32}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
FILENAME = re.compile(r"[0-9a-f]{32}\.(m4a|mp3|aac|wav|flac|opus|ogg|webm)\Z")
SAFETY_BYTES = 16 * 1024 * 1024
MAX_DATABASE_BYTES = 128 * 1024 * 1024
MAX_WAL_BYTES = 256 * 1024 * 1024


class ExportError(Exception):
    """An export failed safely; no existing output is replaced."""


def require(condition, message):
    if not condition:
        raise ExportError(message)


class ExportCancelled(ExportError):
    """Cooperative cancellation before atomic publication."""


def check_cancelled(cancelled):
    if cancelled():
        raise ExportCancelled("Export cancelled. No completed transfer file was created.")



def valid_text(value, field, *, limit=4096, required=False):
    require(isinstance(value, str), f"{field} must be text.")
    require(len(value) <= limit and (not required or bool(value.strip())), f"{field} is empty or too long.")
    # Includes DEL, C1 controls, bidi overrides and zero-width formatting controls.
    require(not any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value), f"{field} contains hidden/control characters.")
    return value


def valid_id(value, field):
    require(isinstance(value, str) and ID.fullmatch(value) is not None, f"{field} must be a stable Stillroom ID.")
    return value


def finite_number(value, field, *, minimum=0, maximum=21600):
    require(not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value), f"{field} must be a finite number.")
    require(minimum <= value <= maximum, f"{field} is outside the supported range.")
    return float(value)


def canonical_youtube(value):
    """Only retain a recognized video ID, never account/query/tracking data."""
    valid_text(value, "sourceUrl", limit=2048)
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
            return None
        host = (parsed.hostname or "").lower()
        video = None
        if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
            if parsed.path == "/watch":
                values = parse_qs(parsed.query).get("v", [])
                video = values[0] if len(values) == 1 else None
            else:
                parts = parsed.path.strip("/").split("/")
                if len(parts) == 2 and parts[0] in {"shorts", "embed", "live"}:
                    video = parts[1]
        elif host in {"youtu.be", "www.youtu.be"}:
            video = parsed.path.removeprefix("/")
        if video and re.fullmatch(r"[A-Za-z0-9_-]{11}", video):
            return "https://www.youtube.com/watch?v=" + video
    except ValueError:
        pass
    return None


def settings_values(value):
    if value is None:
        return None
    require(isinstance(value, dict) and set(value) <= {"playbackRate", "pitchChanges", "reverb"}, "Only playback rate, pitch behavior and reverb settings can transfer.")
    result = {"playbackRate": 1.0, "pitchChanges": True, "reverb": 0.0}
    if "playbackRate" in value:
        result["playbackRate"] = finite_number(value["playbackRate"], "playbackRate", minimum=.5, maximum=2)
    if "pitchChanges" in value:
        require(isinstance(value["pitchChanges"], bool), "pitchChanges must be true or false.")
        result["pitchChanges"] = value["pitchChanges"]
    if "reverb" in value:
        result["reverb"] = finite_number(value["reverb"], "reverb", maximum=1)
    return result


def reject_symlinks(path):
    """Reject symlinks in the explicit caller path rather than following aliases."""
    path = Path(path).absolute()
    for item in (path, *path.parents):
        require(not item.is_symlink(), "Choose a real folder/file rather than a symbolic link.")
    return path


def file_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def plain_file(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and not path.is_symlink(), "Expected a regular local file.")
    return info


def digest_file(path, cancelled):
    info = plain_file(path)
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        require(stat.S_ISREG(opened.st_mode) and file_identity(info) == file_identity(opened),
                "The library file changed while it was being opened.")
        while chunk := source.read(CHUNK_BYTES):
            check_cancelled(cancelled)
            digest.update(chunk)
    require(file_identity(info) == file_identity(path.lstat()), "The library changed during export. Try again after editing has finished.")
    return {"identity": file_identity(info), "sha256": digest.hexdigest()}


def database_fingerprint(database, cancelled):
    result = {}
    for path in (database, Path(str(database) + "-wal")):
        if os.path.lexists(path):
            limit = MAX_WAL_BYTES if path.name.endswith("-wal") else MAX_DATABASE_BYTES
            require(plain_file(path).st_size <= limit, "The desktop metadata snapshot is too large. Close Stillroom cleanly and try again.")
            result[path.name] = digest_file(path, cancelled)
    return result


def same_database_fingerprint(before, after):
    if before.keys() != after.keys():
        return False
    for name, old in before.items():
        new = after[name]
        if old == new:
            continue
        # Opening/checkpointing a WAL-mode SQLite read snapshot can touch an
        # already-empty WAL's timestamps without changing any database bytes.
        # Ignore ONLY those two times: retain device/inode/zero-size and the
        # SHA-256 guard. Main DB and all nonempty WAL identities remain strict.
        if (name.endswith("-wal") and old["identity"][2] == new["identity"][2] == 0
                and old["identity"][:3] == new["identity"][:3]
                and old["sha256"] == new["sha256"] == hashlib.sha256(b"").hexdigest()):
            continue
        return False
    return True


def audio_signature(header, extension):
    if extension == "m4a":
        return len(header) >= 12 and header[4:8] == b"ftyp"
    if extension == "mp3":
        return header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xff and header[1] & 0xe0 == 0xe0)
    if extension == "aac":
        return len(header) >= 2 and header[0] == 0xff and header[1] & 0xf6 == 0xf0
    if extension == "wav":
        return len(header) >= 12 and header[:4] in {b"RIFF", b"RF64"} and header[8:12] == b"WAVE"
    if extension == "flac":
        return header.startswith(b"fLaC")
    if extension in {"opus", "ogg"}:
        return header.startswith(b"OggS")
    if extension == "webm":
        return header.startswith(b"\x1a\x45\xdf\xa3")
    return False


def open_audio(audio_dir_fd, name):
    require(isinstance(name, str) and FILENAME.fullmatch(name) is not None, "A registered audio filename is unsafe or unsupported.")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=audio_dir_fd)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= MAX_AUDIO_BYTES, "Audio must be a regular local file of at most 512 MiB.")
        return os.fdopen(fd, "rb"), info
    except BaseException:
        os.close(fd)
        raise


def media_record(audio_dir_fd, row, cancelled, on_chunk=None):
    source, info = open_audio(audio_dir_fd, row["audio_filename"])
    digest = hashlib.sha256()
    byte_count = 0
    with source:
        require(audio_signature(source.read(16), row["audio_filename"].rsplit(".", 1)[1]), "An audio file does not match its registered format. Re-import or inspect it on desktop first.")
        source.seek(0)
        while chunk := source.read(CHUNK_BYTES):
            check_cancelled(cancelled)
            byte_count += len(chunk)
            require(byte_count <= MAX_AUDIO_BYTES, "An audio file grew past the export limit.")
            digest.update(chunk)
            if on_chunk is not None:
                on_chunk(len(chunk))
        require(file_identity(info) == file_identity(os.fstat(source.fileno())), "Audio changed during export. Try again after editing has finished.")
    sha = digest.hexdigest()
    require(isinstance(row["sha256"], str) and HASH.fullmatch(row["sha256"]) is not None, "The stored audio fingerprint is invalid.")
    require(type(row["file_size"]) is int and row["file_size"] == byte_count and row["sha256"] == sha, "An audio file no longer matches the desktop library. Repair it before exporting.")
    return {"path": "audio/" + row["audio_filename"], "byteCount": byte_count, "sha256": sha}, file_identity(info)


def track_values(row):
    track_id = valid_id(row["id"], "track id")
    require(FILENAME.fullmatch(row["audio_filename"]) is not None, "A registered audio filename is unsafe or unsupported.")
    kind = row["kind"]
    require(kind in {"original", "loop"}, "Track kind must be original or loop.")
    duration = finite_number(row["duration"], "duration", minimum=.000001)
    file_duration = finite_number(row["file_duration"], "fileDuration", minimum=.000001)
    start = finite_number(row["loop_start"], "loopStart")
    end = None if row["loop_end"] is None else finite_number(row["loop_end"], "loopEnd", minimum=.000001)
    effective_end = end if end is not None else file_duration
    require(start < effective_end <= file_duration, "Stored loop bounds exceed the recording.")
    expected_duration = effective_end - start if kind == "loop" else file_duration
    require(abs(duration - expected_duration) <= .01, "Stored playback duration disagrees with the loop/recording bounds.")
    tags_raw = row["tags_json"]
    require(isinstance(tags_raw, str) and len(tags_raw.encode("utf-8")) <= 65536, "Stored tags are too large or invalid.")
    try:
        tags = json.loads(tags_raw)
    except (ValueError, TypeError):
        raise ExportError("Stored tags are not valid JSON.") from None
    require(isinstance(tags, list) and len(tags) <= 32, "At most 32 tags may transfer per track.")
    tags = [valid_text(tag, "tag", limit=100, required=True) for tag in tags]
    require(len(set(tags)) == len(tags), "Duplicate tags must be resolved before export.")
    track = {"id": track_id, "title": valid_text(row["title"], "title", required=True),
             "artist": valid_text(row["artist"], "artist"), "album": valid_text(row["album"], "album"),
             "kind": kind, "duration": duration, "fileDuration": file_duration,
             "loopStart": start, "loopEnd": end, "tags": tags,
             "audioFile": "audio/" + row["audio_filename"]}
    source = canonical_youtube(row["source_url"])
    if source:
        track["sourceUrl"] = source
    if "added_at" in row.keys():
        track["addedAt"] = valid_text(row["added_at"], "addedAt", limit=100, required=True)
    return track


def copy_snapshot_file(source_path, target, expected, cancelled):
    descriptor = os.open(source_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        require(file_identity(os.fstat(source.fileno())) == expected["identity"], "The library changed before its metadata snapshot was copied.")
        target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        digest = hashlib.sha256()
        with os.fdopen(target_fd, "wb") as destination:
            remaining = expected["identity"][2]
            while remaining:
                check_cancelled(cancelled)
                chunk = source.read(min(CHUNK_BYTES, remaining))
                require(chunk, "The desktop metadata snapshot became incomplete.")
                destination.write(chunk); digest.update(chunk); remaining -= len(chunk)
            require(not source.read(1) and file_identity(os.fstat(source.fileno())) == expected["identity"] and digest.hexdigest() == expected["sha256"], "The library changed while its metadata snapshot was copied.")


class ReadSnapshotConnection(sqlite3.Connection):
    """Carries the exact original fingerprint used for this immutable read copy."""


@contextlib.contextmanager
def read_library(root, cancelled=lambda: False):
    database = root / "data" / "library.sqlite3"
    reject_symlinks(database); plain_file(database)
    wal, shm = Path(str(database) + "-wal"), Path(str(database) + "-shm")
    # Validate both names independently, including dangling symbolic links.
    for sidecar in (wal, shm):
        if os.path.lexists(sidecar):
            plain_file(sidecar)
    if os.path.lexists(wal):
        require(os.path.lexists(shm), "This library has a WAL without its shared-memory file. Close Stillroom cleanly before exporting.")
    before = database_fingerprint(database, cancelled)
    # SQLite mode=ro may create or alter WAL shared-memory state in the source
    # directory. Open only a bounded private DB/WAL copy; never let SQLite touch
    # the caller's root. Audio and unrelated files are not copied here.
    with tempfile.TemporaryDirectory(prefix="stillroom-export-snapshot-") as folder:
        snapshot = Path(folder) / database.name
        for name, expected in before.items():
            copy_snapshot_file(database.parent / name, Path(folder) / name, expected, cancelled)
        require(same_database_fingerprint(before, database_fingerprint(database, cancelled)), "The library changed while its metadata snapshot was prepared.")
        connection = sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True, timeout=10, factory=ReadSnapshotConnection)
        connection.source_fingerprint = before
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("BEGIN")
            require(connection.execute("PRAGMA user_version").fetchone()[0] in {1, 2}, "This desktop library schema is unsupported by this exporter.")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            require({"tracks", "playlists", "playlist_tracks"} <= tables, "The selected folder is not a complete Stillroom library.")
            require(connection.execute("PRAGMA quick_check").fetchall()[0][0] == "ok", "The desktop library needs an integrity check before export.")
            require(not connection.execute("PRAGMA foreign_key_check").fetchall(), "The desktop library has missing playlist references.")
            require(same_database_fingerprint(before, database_fingerprint(database, cancelled)), "The library changed before its read snapshot was established.")
            verify_database_identities(database, before)
            yield connection
        finally:
            connection.close()


def selection_values(value=None):
    """Strict data-only request; source/destination roots are never selectable."""
    if value is None:
        return {"mode": "all"}
    require(isinstance(value, dict), "Choose an export selection.")
    mode = value.get("mode")
    allowed = {"all": {"mode"}, "track": {"mode", "trackId"},
               "playlist": {"mode", "playlistId"}, "tracks": {"mode", "trackIds"}}
    require(isinstance(mode, str) and mode in allowed and set(value) == allowed[mode],
            "Choose all songs, a song, a complete playlist, or ordered song IDs; no paths or extra fields are accepted.")
    if mode == "track":
        return {"mode": mode, "trackId": valid_id(value["trackId"], "selected song id")}
    if mode == "playlist":
        return {"mode": mode, "playlistId": valid_id(value["playlistId"], "selected playlist id")}
    if mode == "tracks":
        ids = value["trackIds"]
        require(isinstance(ids, (list, tuple)) and 0 < len(ids) <= MAX_TRACKS,
                "Choose 1 to 10,000 songs in playback order.")
        ids = [valid_id(item, "selected song id") for item in ids]
        require(len(set(ids)) == len(ids), "Choose each song only once; duplicate IDs cannot preserve a distinct library entry.")
        return {"mode": mode, "trackIds": ids}
    return {"mode": "all"}


def notify(progress, phase, completed_bytes, total_bytes, completed_files, total_files):
    if progress is not None:
        progress({"phase": phase, "completedBytes": completed_bytes, "totalBytes": total_bytes,
                  "completedFiles": completed_files, "totalFiles": total_files})


def playlist_values(connection, row, available_ids=None):
    playlist_id = valid_id(row["id"], "playlist id")
    memberships = connection.execute("SELECT track_id,position FROM playlist_tracks WHERE playlist_id=? ORDER BY position", (playlist_id,)).fetchall()
    require(len(memberships) <= MAX_TRACKS, "A playlist contains too many tracks.")
    ids, positions = set(), set()
    for membership in memberships:
        track_id = valid_id(membership["track_id"], "playlist song id")
        position = membership["position"]
        require(track_id not in ids and (available_ids is None or track_id in available_ids), "A playlist has an invalid/duplicate track reference.")
        require(type(position) is int and position >= 0 and position not in positions, "A playlist has an invalid/duplicate position.")
        ids.add(track_id); positions.add(position)
    return {"id": playlist_id, "name": valid_text(row["name"], "playlist name", required=True),
            "trackIds": [item["track_id"] for item in memberships]}


def selected_rows(connection, selection):
    mode = selection["mode"]
    playlists = []
    if mode == "all":
        count = connection.execute("SELECT count(*) FROM tracks").fetchone()[0]
        require(0 < count <= MAX_TRACKS, "Choose a library containing 1 to 10,000 tracks.")
        rows = connection.execute("SELECT * FROM tracks ORDER BY added_at, rowid").fetchall()
        playlist_count = connection.execute("SELECT count(*) FROM playlists").fetchone()[0]
        require(playlist_count <= MAX_TRACKS, "This transfer has too many playlists.")
        available = {valid_id(row["id"], "track id") for row in rows}
        playlists = [playlist_values(connection, row, available) for row in connection.execute("SELECT * FROM playlists ORDER BY created_at, rowid")]
        require(len({item["id"] for item in playlists}) == len(playlists), "Duplicate playlist IDs are not supported.")
        return rows, playlists
    if mode == "playlist":
        row = connection.execute("SELECT * FROM playlists WHERE id=?", (selection["playlistId"],)).fetchone()
        require(row is not None, "The selected playlist no longer exists. Choose it again.")
        playlists = [playlist_values(connection, row)]
        ids = playlists[0]["trackIds"]
        require(ids, "The selected playlist is empty. Choose a playlist containing songs.")
    else:
        ids = [selection["trackId"]] if mode == "track" else selection["trackIds"]
    # Bounded parameter batches work on older SQLite builds as well as current macOS.
    records = {}
    for start in range(0, len(ids), 900):
        batch = ids[start:start + 900]
        for row in connection.execute("SELECT * FROM tracks WHERE id IN (" + ",".join("?" for _ in batch) + ")", batch):
            require(row["id"] not in records, "Duplicate source song IDs are not supported.")
            records[row["id"]] = row
    require(set(records) == set(ids), "A selected song is missing. Refresh the selection before exporting.")
    return [records[item] for item in ids], playlists


def manifest_values(connection, audio_dir_fd, settings, cancelled, selection=None, progress=None):
    selection = selection_values(selection)
    rows, playlists = selected_rows(connection, selection)
    orphan = connection.execute("SELECT count(*) FROM playlist_tracks WHERE playlist_id NOT IN (SELECT id FROM playlists)").fetchone()[0]
    require(orphan == 0, "A playlist membership points to a missing playlist.")
    expected_total = 0
    for row in rows:
        require(type(row["file_size"]) is int and 0 < row["file_size"] <= MAX_AUDIO_BYTES, "A registered audio size is invalid.")
        expected_total += row["file_size"]
    require(expected_total <= MAX_TOTAL_BYTES, "This transfer exceeds the 32 GiB prototype limit.")
    tracks, files, identities = [], [], {}
    seen_tracks, seen_files = set(), set()
    total, completed = 0, 0
    notify(progress, "preparing", 0, expected_total, 0, len(rows))
    check_cancelled(cancelled)
    def hashed(amount):
        nonlocal completed
        completed += amount
        notify(progress, "preparing", completed, expected_total, len(files), len(rows))
        check_cancelled(cancelled)
    for row in rows:
        check_cancelled(cancelled)
        track = track_values(row)
        require(track["id"] not in seen_tracks and track["audioFile"] not in seen_files, "Duplicate track IDs or audio references are not supported.")
        seen_tracks.add(track["id"]); seen_files.add(track["audioFile"])
        file, identity = media_record(audio_dir_fd, row, cancelled, hashed)
        total += file["byteCount"]
        require(total <= MAX_TOTAL_BYTES, "This transfer exceeds the 32 GiB prototype limit.")
        tracks.append(track); files.append(file); identities[file["path"]] = identity
        notify(progress, "preparing", completed, expected_total, len(files), len(rows))
    manifest = {"format": "stillroom-library", "version": 1, "tracks": tracks, "playlists": playlists, "files": files}
    preferences = settings_values(settings)
    if preferences is not None:
        manifest["settings"] = preferences
    encoded = json.dumps(manifest, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    require(len(encoded) <= MAX_MANIFEST_BYTES, "The transfer manifest exceeds the 8 MiB prototype limit.")
    return manifest, encoded, identities, total


def source_root(root):
    root = reject_symlinks(Path(root).expanduser())
    require(root.is_dir(), "The selected library folder does not exist.")
    root = root.resolve()
    audio = reject_symlinks(root / "audio")
    require(audio.is_dir(), "The selected library has no audio folder.")
    return root, audio


def output_path(root, output):
    output = Path(output).expanduser().absolute()
    require(output.suffix == ".stillroomlibrary", "Choose a .stillroomlibrary destination.")
    parent = reject_symlinks(output.parent)
    require(parent.is_dir(), "Choose an existing destination folder.")
    parent = parent.resolve()
    output = parent / output.name
    require(not output.is_relative_to(root), "Save the transfer outside the source library folder.")
    require(not output.exists() and not output.is_symlink(), "The destination already exists. Choose a new filename.")
    return output, parent


def review_token(encoded, selection, fingerprint):
    # Byte digests intentionally ignore timestamp-only churn of an empty SQLite WAL.
    source = {name: value["sha256"] for name, value in fingerprint.items()}
    detail = json.dumps({"selection": selection, "source": source}, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded + b"\0" + detail).hexdigest()


def verify_media_identities(audio_dir_fd, identities, cancelled):
    """Recheck every selected pathname after all hashing/copying callbacks."""
    for path, expected in identities.items():
        check_cancelled(cancelled)
        source, info = open_audio(audio_dir_fd, path.removeprefix("audio/"))
        with source:
            require(file_identity(info) == expected, "Selected audio changed during export preparation or copying. Review it again.")


def verify_database_identities(database, before):
    after = {}
    for path in (database, Path(str(database) + "-wal")):
        if os.path.lexists(path):
            info = plain_file(path)
            after[path.name] = {"identity": file_identity(info), "sha256": before.get(path.name, {}).get("sha256", "")}
    require(same_database_fingerprint(before, after), "The library changed immediately before publication. No export was created.")


def preflight_result(manifest, encoded, total, parent, output, selection, fingerprint):
    bundle_bytes = 12 + len(encoded) + total
    required = bundle_bytes + SAFETY_BYTES
    available = shutil.disk_usage(parent).free
    require(available >= required, "The destination needs more free space for this transfer and a 16 MiB safety margin.")
    return {"format": "stillroom-library", "version": 1, "selection": selection,
            "tracks": len(manifest["tracks"]), "playlists": len(manifest["playlists"]), "files": len(manifest["files"]),
            "audioBytes": total, "bundleBytes": bundle_bytes, "requiredBytes": required,
            "availableBytes": available, "reviewToken": review_token(encoded, selection, fingerprint),
            "output": str(output), "reencoded": False}


def _preflight(root, output, *, selection=None, settings=None, cancelled=lambda: False, progress=None):
    root, audio = source_root(root)
    output, parent = output_path(root, output)
    selection = selection_values(selection)
    check_cancelled(cancelled)
    audio_dir_fd = os.open(audio, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with read_library(root, cancelled) as connection:
            database = root / "data" / "library.sqlite3"
            before = connection.source_fingerprint
            manifest, encoded, identities, total = manifest_values(connection, audio_dir_fd, settings, cancelled, selection, progress)
            verify_media_identities(audio_dir_fd, identities, cancelled)
            check_cancelled(cancelled)
            require(same_database_fingerprint(before, database_fingerprint(database, cancelled)), "The library changed during preparation. Review it again after editing has finished.")
            return preflight_result(manifest, encoded, total, parent, output, selection, before)
    finally:
        os.close(audio_dir_fd)


def _export_library(root, output, *, selection=None, settings=None, cancelled=lambda: False, progress=None, expected_review_token=None):
    """Export a consistent selected read snapshot; publication never overwrites."""
    root, audio = source_root(root)
    output, parent = output_path(root, output)
    selection = selection_values(selection)
    if expected_review_token is not None:
        require(isinstance(expected_review_token, str) and HASH.fullmatch(expected_review_token), "Review this export selection again before saving it.")
    check_cancelled(cancelled)
    database = root / "data" / "library.sqlite3"
    temporary_name = ".pocket-export-" + uuid.uuid4().hex + ".tmp"
    directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        audio_dir_fd = os.open(audio, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except BaseException:
        os.close(directory_fd); raise
    temporary_identity = None
    try:
        with read_library(root, cancelled) as connection:
            original_database = connection.source_fingerprint
            manifest, encoded, identities, total = manifest_values(connection, audio_dir_fd, settings, cancelled, selection, progress)
            result = preflight_result(manifest, encoded, total, parent, output, selection, original_database)
            if expected_review_token is not None:
                require(result["reviewToken"] == expected_review_token, "The library or selection changed after review. Review it again before exporting.")
            check_cancelled(cancelled)
            descriptor = os.open(temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
            temporary_identity = file_identity(os.fstat(descriptor))
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(MAGIC + struct.pack(">I", len(encoded)) + encoded)
                completed = 0
                notify(progress, "copying", 0, total, 0, len(manifest["files"]))
                for index, file in enumerate(manifest["files"]):
                    check_cancelled(cancelled)
                    name = file["path"].removeprefix("audio/")
                    source, info = open_audio(audio_dir_fd, name)
                    digest, copied = hashlib.sha256(), 0
                    with source:
                        require(file_identity(info) == identities[file["path"]], "Audio changed after preparation. No completed export was created.")
                        while chunk := source.read(CHUNK_BYTES):
                            check_cancelled(cancelled)
                            copied += len(chunk); completed += len(chunk)
                            require(copied <= file["byteCount"], "Audio grew during export. Try again after editing has finished.")
                            digest.update(chunk); destination.write(chunk)
                            notify(progress, "copying", completed, total, index, len(manifest["files"]))
                            check_cancelled(cancelled)
                        require(file_identity(info) == file_identity(os.fstat(source.fileno())), "Audio changed during export.")
                    require(copied == file["byteCount"] and digest.hexdigest() == file["sha256"], "Audio changed during export. No completed transfer was created.")
                    notify(progress, "copying", completed, total, index + 1, len(manifest["files"]))
                notify(progress, "verifying", completed, total, len(manifest["files"]), len(manifest["files"]))
                check_cancelled(cancelled)
                destination.flush(); os.fsync(destination.fileno())
                byte_count = destination.tell()
                require(byte_count == result["bundleBytes"], "The transfer size could not be verified.")
                completed_identity = file_identity(os.fstat(destination.fileno()))
            notify(progress, "publishing", total, total, len(manifest["files"]), len(manifest["files"]))
            check_cancelled(cancelled)
            verify_media_identities(audio_dir_fd, identities, cancelled)
            require(same_database_fingerprint(original_database, database_fingerprint(database, cancelled)), "The library changed during export. Try again after editing has finished.")
            check_cancelled(cancelled)
            # Final callback-free identity sweep: no request/cancellation/progress
            # callback can substitute output or change earlier-verified sources
            # between this proof and the atomic publication commit.
            verify_media_identities(audio_dir_fd, identities, lambda: False)
            verify_database_identities(database, original_database)
            staged = os.stat(temporary_name, dir_fd=directory_fd, follow_symlinks=False)
            require(stat.S_ISREG(staged.st_mode) and file_identity(staged) == completed_identity, "The staged transfer changed. No export was published.")
            # Exclusive hard-link publication is atomic; no fallible callbacks
            # or cancellation checks run after this commit succeeds.
            os.link(temporary_name, output.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
        with contextlib.suppress(OSError):
            os.fsync(directory_fd)
        result.update(sourceAudioUnchanged=True, published=True)
        return result
    finally:
        if temporary_identity is not None:
            with contextlib.suppress(OSError):
                staged = os.stat(temporary_name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISREG(staged.st_mode) and file_identity(staged)[:2] == temporary_identity[:2]:
                    os.unlink(temporary_name, dir_fd=directory_fd)
        os.close(audio_dir_fd); os.close(directory_fd)


def normalized_errors(action):
    try:
        return action()
    except ExportError:
        raise
    except (OSError, sqlite3.Error, UnicodeError, ValueError, KeyError, TypeError) as error:
        raise ExportError("The export could not be completed. The source library and any existing destination are unchanged.") from error


class LibraryExporter:
    """Root-bound stdlib API. Construct with trusted app configuration, not HTTP data.

    Selection dictionaries accept IDs/modes only. Callbacks execute synchronously
    on the caller's worker thread and must never update UI directly. A progress
    callback may request cancellation; no callbacks run after publication commits.
    """
    def __init__(self, root):
        self._root = normalized_errors(lambda: source_root(root)[0])

    def preflight(self, output, *, selection=None, settings=None, cancelled=lambda: False, progress=None):
        return normalized_errors(lambda: _preflight(self._root, output, selection=selection, settings=settings, cancelled=cancelled, progress=progress))

    def export(self, output, *, selection=None, settings=None, cancelled=lambda: False, progress=None, expected_review_token=None):
        return normalized_errors(lambda: _export_library(self._root, output, selection=selection, settings=settings, cancelled=cancelled, progress=progress, expected_review_token=expected_review_token))


def preflight_export(root, output, **options):
    return LibraryExporter(root).preflight(output, **options)


def export_library(root, output, *, selection=None, settings=None, cancelled=lambda: False, progress=None, expected_review_token=None):
    """Backward-compatible all-library export, with optional explicit selection."""
    return LibraryExporter(root).export(output, selection=selection, settings=settings, cancelled=cancelled, progress=progress, expected_review_token=expected_review_token)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Explicit caller-authorized Stillroom library root (read only).")
    parser.add_argument("--output", type=Path, required=True, help="New .stillroomlibrary file outside the source root.")
    parser.add_argument("--settings", type=Path, help="Optional JSON containing only supported playback settings; never account settings.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--track", help="One stable song ID.")
    group.add_argument("--playlist", help="One complete saved playlist ID, preserving its order.")
    group.add_argument("--tracks", nargs="+", help="Stable song IDs in the requested order; no partial playlists are invented.")
    parser.add_argument("--preflight", action="store_true", help="Verify selection and free space without creating a transfer.")
    parser.add_argument("--expected-review-token", help="Reject export if a prior preflight selection/source changed.")
    args = parser.parse_args(argv)
    try:
        settings = None
        if args.settings is not None:
            path = reject_symlinks(args.settings)
            require(plain_file(path).st_size <= 4096, "The settings file is too large.")
            settings = json.loads(path.read_text())
        selection = {"mode": "track", "trackId": args.track} if args.track is not None else {"mode": "playlist", "playlistId": args.playlist} if args.playlist is not None else {"mode": "tracks", "trackIds": args.tracks} if args.tracks is not None else {"mode": "all"}
        exporter = LibraryExporter(args.root)
        options = {"selection": selection, "settings": settings}
        if args.preflight:
            require(args.expected_review_token is None, "A review token is only used when saving the reviewed transfer.")
            result = exporter.preflight(args.output, **options)
        else:
            result = exporter.export(args.output, expected_review_token=args.expected_review_token, **options)
        print(json.dumps(result, indent=2))
        return 0
    except (ExportError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
