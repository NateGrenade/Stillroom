"""Loopback-only HTTP service. Run with python3 -m app.server."""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import sqlite3
import sys
import tempfile
import threading
from urllib.parse import unquote, urlsplit

from . import __version__
from .library import Library, LibraryError, MAX_AUDIO_BYTES
from .import_jobs import ImportJobs, merge_metadata
from .import_sources import local_metadata
from .playlist_imports import PlaylistImports
from .recommendations import Recommendations
from .youtube_auth import YouTubeAuth, MAX_COOKIE_BYTES
from .updates import Updates, MAX_UPDATE_BYTES
from .sharing import Sharing
from .storage_policy import reject_quality_options
from .pocket_exports import PocketExports

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = 8768
MAX_JSON_BYTES = 128 * 1024
SERVER_CONTROL_FILE = "server-control.json"


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        if sys.platform == "win32":
            # Windows SO_REUSEADDR permits another process to bind this live
            # port. Exclusive ownership keeps simultaneous launches safe.
            self.allow_reuse_address = False
            self.allow_reuse_port = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, root=DEFAULT_ROOT, port=DEFAULT_PORT):
        self.library = Library(root)
        self._control_record = None
        super().__init__(("127.0.0.1", port), Handler)
        try:
            self.youtube_auth = YouTubeAuth(self.library.root)
            self.updates = Updates(self.library.root)
            self.mutation_lock = threading.RLock()
            self.sharing = Sharing(self.library, self.mutation_lock)
            self.pocket_exports = PocketExports(self.library)
            self.import_jobs = ImportJobs(self.library, youtube_auth=self.youtube_auth)
            self.playlist_imports = PlaylistImports(self.library, self.import_jobs, youtube_auth=self.youtube_auth)
            self.recommendations = Recommendations(self.library, self.import_jobs)
            # Publish only after binding succeeds. A losing simultaneous launch
            # must never replace the running server's shutdown credentials.
            record = {"app": "stillroom", "pid": os.getpid(),
                      "root": str(self.library.root), "port": self.server_port,
                      "token": secrets.token_urlsafe(32)}
            handle, temporary = tempfile.mkstemp(prefix=".server-control-", dir=self.library.data)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as output:
                    json.dump(record, output)
                    output.write("\n")
                os.replace(temporary, self.library.data / SERVER_CONTROL_FILE)
                self._control_record = record
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary)
        except Exception:
            self.server_close()
            raise

    def server_close(self):
        try:
            if hasattr(self, "pocket_exports"):
                self.pocket_exports.close()
            if hasattr(self, "sharing"):
                self.sharing.close()
            if hasattr(self, "recommendations"):
                self.recommendations.close()
            if hasattr(self, "playlist_imports"):
                self.playlist_imports.close()
            if hasattr(self, "import_jobs"):
                self.import_jobs.close()
            if hasattr(self, "updates"):
                self.updates.close()
        finally:
            super().server_close()
            if self._control_record:
                path = self.library.data / SERVER_CONTROL_FILE
                with contextlib.suppress(OSError, ValueError):
                    current = json.loads(path.read_text(encoding="utf-8"))
                    if current == self._control_record:
                        path.unlink()


class Handler(BaseHTTPRequestHandler):
    server_version = "Stillroom/" + __version__
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; font-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
        super().end_headers()

    def valid_request(self, mutation=False):
        host = self.headers.get("Host", "")
        try:
            parsed = urlsplit("http://" + host)
            if parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.username or parsed.password:
                raise ValueError()
            if parsed.path or parsed.query or parsed.fragment or parsed.port not in (None, self.server.server_port):
                raise ValueError()
            if not host or len(self.headers.get_all("Host", [])) != 1:
                raise ValueError()
        except ValueError:
            raise LibraryError("This service only accepts local browser requests.", 403)
        if mutation:
            origin = self.headers.get("Origin")
            if origin and origin.rstrip("/").lower() != ("http://" + host).lower():
                raise LibraryError("Cross-origin changes are not allowed.", 403)
            if self.headers.get("Sec-Fetch-Site", "none") not in ("same-origin", "none"):
                raise LibraryError("Cross-origin changes are not allowed.", 403)

    def json_response(self, value, status=200, head=False):
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def error(self, error, head=False):
        self.close_connection = True
        self.json_response({"error": str(error)}, getattr(error, "status", 500), head)

    def dispatch(self, method):
        try:
            self.valid_request(method in ("POST", "PATCH", "DELETE"))
            path = unquote(urlsplit(self.path).path)
            if "\x00" in path or "\\" in path or any(part in ("..", ".") for part in path.split("/")):
                raise LibraryError("Path not found.", 404)
            if method in ("GET", "HEAD"):
                self.read_request(path, method == "HEAD")
            else:
                export_match = re.fullmatch(r"/api/tracks/([0-9a-f]{32})/export", path)
                if method == "POST" and export_match:
                    # Sharing snapshots under mutation_lock, then releases it
                    # before encoding and streaming a potentially long song.
                    return self.export_audio(export_match[1])
                with self.server.mutation_lock:
                    if path != "/api/shutdown" and self.server.updates.status().get("state") == "installing":
                        raise LibraryError("Stillroom is installing an update. Please wait for it to restart.", 409)
                    self.write_request(path, method)
        except LibraryError as exc:
            self.error(exc, method == "HEAD")
        except (ConnectionError, socket.timeout):
            self.close_connection = True
        except sqlite3.Error:
            self.error(LibraryError("The library is busy or could not be saved. Please try again.", 503), method == "HEAD")
        except Exception:
            import traceback
            traceback.print_exc()
            self.error(LibraryError("The library could not complete this request.", 500), method == "HEAD")

    def do_GET(self):
        self.dispatch("GET")

    def do_HEAD(self):
        self.dispatch("HEAD")

    def do_POST(self):
        self.dispatch("POST")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def do_OPTIONS(self):
        self.error(LibraryError("Cross-origin requests are not supported.", 405))

    def read_request(self, path, head):
        if path == "/api/health":
            return self.json_response({"app": "stillroom", "version": __version__, "root": str(self.server.library.root), "pid": os.getpid()}, head=head)
        if path == "/api/library":
            return self.json_response(self.server.library.library(), head=head)
        if path == "/api/pocket-export/devices":
            self.valid_request(mutation=True)
            return self.json_response(self.server.pocket_exports.devices(), head=head)
        if path == "/api/pocket-export/jobs":
            self.valid_request(mutation=True)
            return self.json_response({"jobs": self.server.pocket_exports.jobs()}, head=head)
        pocket_match = re.fullmatch(r"/api/pocket-export/jobs/([0-9a-f]{32})", path)
        if pocket_match:
            self.valid_request(mutation=True)
            return self.json_response({"job": self.server.pocket_exports.get(pocket_match[1])}, head=head)
        if path == "/api/settings":
            return self.json_response({"youtube": self.server.youtube_auth.status(), "updates": self.server.updates.status()}, head=head)
        if path == "/api/updates":
            return self.json_response(self.server.updates.status(), head=head)
        if path == "/api/recommendations":
            return self.json_response(self.server.recommendations.snapshot(), head=head)
        share_match = re.fullmatch(r"/api/tracks/([0-9a-f]{32})/share", path)
        if share_match:
            self.valid_request(mutation=True)
            return self.json_response(self.server.sharing.preview(share_match[1]), head=head)
        recommendation_match = re.fullmatch(r"/api/recommendation-sessions/([0-9a-f]{32})", path)
        if recommendation_match:
            return self.json_response({"session": self.server.recommendations.get(recommendation_match[1])}, head=head)
        playlist_match = re.fullmatch(r"/api/playlist-imports/([0-9a-f]{32})", path)
        if playlist_match:
            return self.json_response({"session": self.server.playlist_imports.get(playlist_match[1])}, head=head)
        match = re.fullmatch(r"/api/import-jobs/([0-9a-f]{32})(/audio)?", path)
        if match:
            if match[2]:
                audio = self.server.import_jobs.audio(match[1])
                return self.send_file(audio.parent, audio.name, head)
            return self.json_response({"job": self.server.import_jobs.get(match[1])}, head=head)
        if path.startswith("/api/"):
            raise LibraryError("API endpoint not found.", 404)
        if path.startswith("/audio/"):
            filename = path[len("/audio/"):]
            if not re.fullmatch(r"[0-9a-f]{32}\.(?:mp3|m4a|aac|wav|flac|opus|ogg|webm)", filename):
                raise LibraryError("Audio not found.", 404)
            return self.send_file(self.server.library.audio, filename, head, audio=True)
        filename = "index.html" if path == "/" else path.lstrip("/")
        return self.send_file(self.server.library.public, filename, head)

    def send_file(self, base, filename, head, audio=False):
        target = (base / filename).resolve()
        if not target.is_relative_to(base.resolve()) or any(part.startswith(".") for part in Path(filename).parts) or not target.is_file():
            raise LibraryError("File not found.", 404)
        try:
            handle = target.open("rb")
        except OSError:
            raise LibraryError("File not found.", 404)
        with handle:
            size = os.fstat(handle.fileno()).st_size
            start, end = 0, size - 1
            status = 200
            range_header = self.headers.get("Range")
            if range_header:
                match = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", range_header.strip())
                valid = bool(match and (match[1] or match[2]) and size)
                if valid:
                    if match[1]:
                        start = int(match[1])
                        end = min(int(match[2]), size - 1) if match[2] else size - 1
                    else:
                        suffix = int(match[2])
                        start, end = max(0, size - suffix), size - 1
                        valid = suffix > 0
                    valid = valid and start < size and start <= end
                if not valid:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    return
                status = 206
            content_types = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".wav": "audio/wav", ".flac": "audio/flac", ".opus": "audio/ogg", ".ogg": "audio/ogg", ".webm": "audio/webm", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".html": "text/html; charset=utf-8"}
            content_type = content_types.get(target.suffix.lower()) or mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(max(0, end - start + 1)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "private, max-age=86400, immutable" if audio else "no-cache")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if not head:
                handle.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = handle.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

    def content_length(self, maximum):
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
            raise LibraryError("Provide a single Content-Length header.", 411)
        try:
            raw_length = self.headers["Content-Length"]
            if not re.fullmatch(r"[0-9]{1,12}", raw_length):
                raise ValueError()
            length = int(raw_length)
        except (ValueError, TypeError):
            raise LibraryError("Invalid Content-Length.")
        if length > maximum:
            raise LibraryError("The upload is too large.", 413)
        return length

    def json_body(self, *, strict=False):
        if self.headers.get_content_type() != "application/json":
            raise LibraryError("Use application/json for this request.", 415)
        length = self.content_length(MAX_JSON_BYTES)
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError()
            def object_pairs(pairs):
                result = {}
                for key, value in pairs:
                    if strict and key in result:
                        raise ValueError()
                    result[key] = value
                return result
            result = json.loads(raw.decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError()), object_pairs_hook=object_pairs)
        except (ValueError, UnicodeDecodeError):
            raise LibraryError("The request body is not valid JSON.")
        if not isinstance(result, dict):
            raise LibraryError("The request body must be a JSON object.")
        return result

    def export_audio(self, track_id):
        body = self.json_body(strict=True)
        if (set(body) != {"includeInfo", "revision"} or type(body["includeInfo"]) is not bool or
                not isinstance(body["revision"], str) or not re.fullmatch(r"[0-9a-f]{64}", body["revision"])):
            raise LibraryError("Provide only includeInfo as true or false and the current sharing revision.")
        def check_ready():
            if self.server.updates.status().get("state") == "installing":
                raise LibraryError("Stillroom is installing an update. Please wait for it to restart.", 409)
        with self.server.sharing.export(track_id, body["includeInfo"], body["revision"], check_ready=check_ready) as exported:
            with exported.path.open("rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                self.send_response(200)
                self.send_header("Content-Type", "audio/mp4")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", 'attachment; filename="' + exported.filename + '"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                while chunk := handle.read(256 * 1024):
                    if self.server.sharing.closed:
                        self.close_connection = True
                        return
                    self.wfile.write(chunk)

    def write_request(self, path, method):
        library = self.server.library
        if method == "POST" and path == "/api/pocket-export/review":
            return self.json_response(self.server.pocket_exports.review(self.json_body(strict=True)))
        if method == "POST" and path == "/api/pocket-export/jobs":
            return self.json_response({"job": self.server.pocket_exports.start(self.json_body(strict=True))}, 202)
        pocket_check = re.fullmatch(r"/api/pocket-export/jobs/([0-9a-f]{32})/check", path)
        if method == "POST" and pocket_check:
            if self.json_body(strict=True):
                raise LibraryError("Checking an existing transfer does not accept options.")
            return self.json_response({"job": self.server.pocket_exports.check_transfer(pocket_check[1])})
        pocket_match = re.fullmatch(r"/api/pocket-export/jobs/([0-9a-f]{32})", path)
        if method == "DELETE" and pocket_match:
            if self.headers.get("Content-Length", "0") != "0" or self.headers.get("Transfer-Encoding"):
                raise LibraryError("Cancelling an export does not accept a request body.")
            return self.json_response({"job": self.server.pocket_exports.cancel(pocket_match[1])})
        if method == "POST" and path == "/api/settings/youtube":
            return self.json_response(self.server.youtube_auth.configure(self.json_body()))
        if path == "/api/settings/youtube/cookies":
            if method == "POST":
                if self.headers.get_content_type() != "application/octet-stream":
                    raise LibraryError("Upload the cookie file as application/octet-stream.", 415)
                length = self.content_length(MAX_COOKIE_BYTES)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise LibraryError("The cookie upload ended before the file was complete.")
                return self.json_response(self.server.youtube_auth.upload_cookies(raw))
            if method == "DELETE":
                if self.headers.get("Content-Length", "0") != "0" or self.headers.get("Transfer-Encoding"):
                    raise LibraryError("Removing an account connection does not accept a request body.")
                return self.json_response(self.server.youtube_auth.clear_cookies())
        if method == "POST" and path.startswith("/api/updates/"):
            action = path.rsplit("/", 1)[-1]
            if action == "package":
                return self.upload_update()
            body = self.json_body()
            if action == "check":
                return self.json_response(self.server.updates.check(auto=body.get("auto") is True), 202)
            if action == "download":
                return self.json_response(self.server.updates.download(), 202)
            if action == "install":
                with self.server.import_jobs.lock:
                    active = any(job.get("status") in ("queued", "working", "ready", "saving") for job in self.server.import_jobs.jobs.values())
                with self.server.playlist_imports.lock:
                    active = active or any(session.get("status") in ("reading", "matching", "importing") for session in self.server.playlist_imports.sessions.values())
                active = active or self.server.sharing.busy or self.server.pocket_exports.busy
                if active:
                    raise LibraryError("Finish or cancel your music imports and song downloads before installing an update.", 409)
                return self.json_response(self.server.updates.install(self.server.server_port), 202)
        if method == "POST" and path == "/api/recommendations/refresh":
            return self.json_response(self.server.recommendations.refresh(self.json_body()), 202)
        recommendation_match = re.fullmatch(r"/api/recommendations/([0-9a-f]{32})/listen", path)
        if method == "POST" and recommendation_match:
            return self.json_response({"session": self.server.recommendations.listen(recommendation_match[1], self.json_body())}, 202)
        event_match = re.fullmatch(r"/api/recommendation-sessions/([0-9a-f]{32})/events", path)
        if method == "POST" and event_match:
            return self.json_response({"session": self.server.recommendations.event(event_match[1], self.json_body())})
        if method == "POST" and path == "/api/shutdown":
            token = self.json_body().get("token")
            record = self.server._control_record
            if not isinstance(token, str) or not token.isascii() or not record or not secrets.compare_digest(token, record["token"]):
                raise LibraryError("This shutdown request is not authorized.", 403)
            self.json_response({"success": True})
            self.wfile.flush()
            # shutdown() waits for serve_forever(), so it cannot run on that
            # loop's thread. The main finally block then cleans up previews.
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if method == "POST" and path == "/api/import":
            return self.import_audio()
        if method == "POST" and path == "/api/playlist-imports":
            return self.json_response({"session": self.server.playlist_imports.create(self.json_body())}, 202)
        playlist_match = re.fullmatch(r"/api/playlist-imports/([0-9a-f]{32})(/import)?", path)
        if playlist_match:
            if method == "POST" and playlist_match[2]:
                return self.json_response({"session": self.server.playlist_imports.start_import(playlist_match[1], self.json_body())}, 202)
            if method == "DELETE" and not playlist_match[2]:
                if self.headers.get("Content-Length", "0") != "0" or self.headers.get("Transfer-Encoding"):
                    raise LibraryError("Stopping a playlist import does not accept a request body.")
                return self.json_response({"session": self.server.playlist_imports.cancel(playlist_match[1])})
        if method == "POST" and path == "/api/import-jobs":
            return self.json_response({"job": self.server.import_jobs.create(self.json_body())}, 202)
        if method == "POST" and path == "/api/import-jobs/file":
            return self.import_audio(prepare=True)
        match = re.fullmatch(r"/api/import-jobs/([0-9a-f]{32})(/commit)?", path)
        if match:
            if method == "POST" and match[2]:
                return self.json_response(self.server.import_jobs.commit(match[1], self.json_body()), 201)
            if method == "DELETE" and not match[2]:
                if self.headers.get("Content-Length", "0") != "0" or self.headers.get("Transfer-Encoding"):
                    raise LibraryError("Closing a preview does not accept a request body.")
                return self.json_response(self.server.import_jobs.discard(match[1]))
        if method == "POST" and path == "/api/playlists":
            body = self.json_body()
            return self.json_response(library.create_playlist(body.get("name")), 201)
        track_match = re.fullmatch(r"/api/tracks/([0-9a-f]{32})", path)
        if method == "PATCH" and track_match:
            return self.json_response({"track": library.update_track(track_match[1], self.json_body())})
        if method == "DELETE" and track_match:
            if self.headers.get("Content-Length", "0") != "0" or self.headers.get("Transfer-Encoding"):
                raise LibraryError("Song deletion does not accept a request body.")
            return self.json_response(self.server.recommendations.delete_track(track_match[1]))
        match = re.fullmatch(r"/api/playlists/([0-9a-f]{32})", path)
        if match:
            if method == "PATCH":
                return self.json_response(library.update_playlist(match[1], self.json_body()))
            if method == "DELETE":
                length = self.headers.get("Content-Length", "0")
                if length != "0" or self.headers.get("Transfer-Encoding"):
                    raise LibraryError("Playlist deletion does not accept a request body.")
                return self.json_response(library.delete_playlist(match[1]))
        raise LibraryError("API endpoint not found.", 404)

    def upload_update(self):
        if self.headers.get_content_type() not in ("application/octet-stream", "application/zip"):
            raise LibraryError("Choose a Stillroom ZIP update.", 415)
        length = self.content_length(MAX_UPDATE_BYTES)
        if not length:
            raise LibraryError("The update file is empty.")
        handle, temporary = tempfile.mkstemp(prefix=".update-upload-", dir=self.server.library.data)
        transferred = False
        try:
            with os.fdopen(handle, "wb") as output:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise LibraryError("The update upload ended before the file was complete.")
                    output.write(chunk)
                    remaining -= len(chunk)
            status = self.server.updates.stage(Path(temporary))
            transferred = True
            return self.json_response(status, 202)
        finally:
            if not transferred:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary)

    def import_audio(self, prepare=False):
        content_type = self.headers.get_content_type()
        if content_type != "application/octet-stream" and not content_type.startswith("audio/"):
            raise LibraryError("Upload an audio file with an audio content type or application/octet-stream.", 415)
        length = self.content_length(MAX_AUDIO_BYTES)
        if length == 0:
            raise LibraryError("The audio file is empty.")
        filename = unquote(self.headers.get("X-Filename", ""))
        if not filename or len(filename) > 255 or any(c in filename for c in ("/", "\\", "\x00")) or filename in (".", ".."):
            raise LibraryError("Provide a filename without a folder in X-Filename.")
        metadata = {}
        encoded = self.headers.get("X-Track-Metadata")
        if encoded:
            if len(encoded) > 16384:
                raise LibraryError("Track metadata is too large.")
            try:
                metadata = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
            except (ValueError, binascii.Error, UnicodeDecodeError):
                raise LibraryError("X-Track-Metadata must be base64-encoded UTF-8 JSON.")
            if not isinstance(metadata, dict):
                raise LibraryError("Track metadata must be a JSON object.")
        reject_quality_options(metadata)
        handle, temporary = tempfile.mkstemp(prefix="upload-", dir=self.server.import_jobs.session)
        try:
            with os.fdopen(handle, "wb") as output:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise LibraryError("The audio upload ended before the file was complete.")
                    output.write(chunk)
                    remaining -= len(chunk)
            if prepare:
                job = self.server.import_jobs.create(metadata, upload=temporary, filename=filename)
                return self.json_response({"job": job}, 202)
            allow_native_opus = metadata.pop("allowNativeOpus", True)
            if "metadataMode" in metadata:
                details = merge_metadata(local_metadata(temporary, filename=filename), {
                    "metadataMode": metadata.pop("metadataMode"), "metadata": metadata,
                })
                metadata = {**metadata, **details}
            track = self.server.library.import_file(temporary, metadata, filename=filename,
                                                    allow_native_opus=allow_native_opus)
            self.json_response({"track": track}, 201)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description="Run Stillroom on this computer only.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    server = Server(args.root, args.port)
    def stop_service(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, stop_service)
    print(f"Stillroom is listening at http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
