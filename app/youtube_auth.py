"""Explicit, per-import YouTube sessions; never store passwords or browser cookies.

Only cookies deliberately uploaded by the user are stored. Browser extraction
belongs to yt-dlp and is only invoked after an import opts in. A snapshot freezes
that import's method/browser/upload, so a settings change cannot switch accounts
halfway through a playlist.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import uuid

from .library import LibraryError

MAX_COOKIE_BYTES = 2 * 1024 * 1024
_BROWSER_NAMES = {"firefox": "Firefox", "chrome": "Chrome", "edge": "Edge", "brave": "Brave"}
_COOKIE_ID = re.compile(r"[a-f0-9]{32}\Z")


@dataclass(frozen=True)
class YouTubeSession:
    mode: str
    browser: str = ""
    cookie_id: str = ""
    _manager: object = field(default=None, repr=False, compare=False)

    @contextmanager
    def command_options(self):
        if self.mode == "browser":
            # Allowlisted browser name only; no profile paths or arbitrary args.
            if self.browser not in self._manager.browser_names:
                raise LibraryError("Choose a supported browser for this computer.")
            yield ["--cookies-from-browser", self.browser]
        elif self.mode == "cookies":
            # yt-dlp writes its cookie jar. Give it an isolated working copy and
            # destroy that copy after this acquisition, including on failure.
            content = self._manager._uploaded(self.cookie_id)
            with tempfile.TemporaryDirectory(prefix=".session-", dir=self._manager.directory) as folder:
                path = Path(folder) / "cookies.txt"
                _write_private(path, content)
                yield ["--cookies", str(path)]
        else:
            raise LibraryError("Choose a YouTube account session before importing.")


def _read_private(path, maximum):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if path.is_symlink():
        raise LibraryError("The YouTube account settings must be regular local files.")
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as source:
            import stat
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise LibraryError("The YouTube account settings must be regular local files.")
            content = source.read(maximum + 1)
    except FileNotFoundError:
        raise
    except OSError:
        raise LibraryError("YouTube account settings could not be read. Check folder permissions.", 500) from None
    if len(content) > maximum:
        raise LibraryError("The YouTube account settings exceed the file size limit.")
    return content


def _write_private(path, content):
    descriptor, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _cookie_bytes(content):
    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_COOKIE_BYTES:
        raise LibraryError("Choose a Netscape cookies.txt file smaller than 2 MB.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise LibraryError("The cookie file must be a UTF-8 Netscape cookies.txt export.") from None
    lines = text.splitlines()
    if not lines or not re.fullmatch(r"# (?:Netscape )?HTTP Cookie File", lines[0].strip()):
        raise LibraryError("Choose a Netscape cookies.txt export, not a browser database or JSON file.")
    kept = []
    for line in lines[1:]:
        if not line.strip() or line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue
        fields = line.removeprefix("#HttpOnly_").split("\t")
        if len(fields) != 7:
            raise LibraryError("This Netscape cookie file contains an invalid row. Export it again.")
        domain, include_subdomains, path, secure, expiry, name, value = fields
        if (not re.fullmatch(r"\.?(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}", domain) or include_subdomains not in ("TRUE", "FALSE")
                or secure not in ("TRUE", "FALSE") or not path.startswith("/")
                or not re.fullmatch(r"\d{1,16}", expiry) or not name
                or any(ord(c) < 32 or ord(c) == 127 for c in "".join(fields))):
            raise LibraryError("This Netscape cookie file contains an invalid row. Export it again.")
        host = domain.lstrip(".").lower()
        # Discard cookies for every unrelated site and comments that could
        # contain exporter metadata. Never save the original unfiltered upload.
        if host == "youtube.com" or host.endswith(".youtube.com") or host == "google.com" or host.endswith(".google.com"):
            kept.append(line)
    if not kept:
        raise LibraryError("This file has no YouTube or Google cookies. Export the signed-in YouTube session.")
    return ("# Netscape HTTP Cookie File\n" + "\n".join(kept) + "\n").encode("utf-8")


class YouTubeAuth:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.directory = self.root / "data" / ".youtube-auth"
        self.lock = threading.RLock()
        self.browser_names = dict(_BROWSER_NAMES)
        if sys.platform == "darwin":
            self.browser_names["safari"] = "Safari"

    def _check_directory(self, create=False):
        for path in (self.root / "data", self.directory):
            if path.is_symlink():
                raise LibraryError("YouTube account settings need a local folder, not a shortcut.")
        if create:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.directory, 0o700)

    def _config(self):
        self._check_directory()
        try:
            raw = json.loads(_read_private(self.directory / "settings.json", 4096))
        except FileNotFoundError:
            return {"mode": "off", "browser": "firefox", "cookieId": ""}
        except (ValueError, TypeError):
            raise LibraryError("YouTube account settings are unreadable. Choose your account method again.") from None
        if not isinstance(raw, dict):
            raise LibraryError("YouTube account settings are unreadable. Choose your account method again.")
        mode, browser, cookie_id = raw.get("mode", "off"), raw.get("browser", "firefox"), raw.get("cookieId", "")
        if (not isinstance(mode, str) or mode not in ("off", "browser", "cookies")
                or not isinstance(browser, str) or browser not in self.browser_names
                or not isinstance(cookie_id, str) or cookie_id and not _COOKIE_ID.fullmatch(cookie_id)):
            raise LibraryError("YouTube account settings are unreadable. Choose your account method again.")
        return {"mode": mode, "browser": browser, "cookieId": cookie_id}

    def _save(self, config):
        self._check_directory(create=True)
        _write_private(self.directory / "settings.json", (json.dumps(config) + "\n").encode("utf-8"))

    def _cookie_path(self, cookie_id):
        if not isinstance(cookie_id, str) or not _COOKIE_ID.fullmatch(cookie_id):
            raise LibraryError("Upload a YouTube cookie file before importing with this account method.")
        return self.directory / ("cookies-" + cookie_id + ".txt")

    def _uploaded(self, cookie_id):
        with self.lock:
            self._check_directory()
            try:
                content = _read_private(self._cookie_path(cookie_id), MAX_COOKIE_BYTES)
            except FileNotFoundError:
                raise LibraryError("This import's cookie file was removed or replaced. Load the playlist or prepare the track again.", 409) from None
            return _cookie_bytes(content)

    def status(self):
        with self.lock:
            config = self._config()
            cookie_id = config["cookieId"]
            path = self._cookie_path(cookie_id) if cookie_id else None
            present = bool(path and not path.is_symlink() and path.is_file())
            return {"mode": config["mode"], "browser": config["browser"], "hasCookies": present,
                    "browsers": [{"id": key, "label": label} for key, label in self.browser_names.items()]}

    def configure(self, options):
        if not isinstance(options, dict) or set(options) - {"mode", "browser"}:
            raise LibraryError("Choose an account method and browser; passwords and file paths are not accepted.")
        mode = options.get("mode")
        if mode not in ("off", "browser", "cookies"):
            raise LibraryError("Choose Off, Browser session, or Uploaded cookies.")
        with self.lock:
            config = self._config()
            browser = options.get("browser", config["browser"])
            if not isinstance(browser, str) or browser not in self.browser_names:
                raise LibraryError("Choose a supported browser for this computer.")
            if mode == "cookies":
                self._uploaded(config["cookieId"])
            config.update(mode=mode, browser=browser)
            self._save(config)
            return self.status()

    def upload_cookies(self, content):
        content = _cookie_bytes(content)
        with self.lock:
            config = self._config()
            previous = config["cookieId"]
            cookie_id = uuid.uuid4().hex
            self._check_directory(create=True)
            path = self._cookie_path(cookie_id)
            _write_private(path, content)
            try:
                config.update(mode="cookies", cookieId=cookie_id)
                self._save(config)
            except Exception:
                path.unlink(missing_ok=True)
                raise
            if previous:
                self._cookie_path(previous).unlink(missing_ok=True)
            return self.status()

    def clear_cookies(self):
        with self.lock:
            config = self._config()
            previous = config["cookieId"]
            config.update(cookieId="", mode="off" if config["mode"] == "cookies" else config["mode"])
            self._save(config)
            if previous:
                self._cookie_path(previous).unlink(missing_ok=True)
            return self.status()

    def snapshot(self, enabled=False):
        if not isinstance(enabled, bool):
            raise LibraryError("Use YouTube account must be true or false.")
        if not enabled:
            return None
        with self.lock:
            config = self._config()
            if config["mode"] == "off":
                raise LibraryError("Choose a YouTube account method first, or turn off Use YouTube account for this import.")
            if config["mode"] == "cookies":
                self._uploaded(config["cookieId"])
            return YouTubeSession(config["mode"], config["browser"], config["cookieId"], self)
