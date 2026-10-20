"""Verified portable updates that replace app files while leaving a library in place.

The only network authority is the public Stillroom GitHub release repository.
Package checksums detect corruption; GitHub HTTPS plus its release digest binds
online downloads to that release. A local ZIP is an explicitly trusted update
file selected by the user, not an independently signed software artifact.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid
import zipfile

from . import __version__
from .library import LibraryError
from .runtime import platform_id

REPOSITORY = "NateGrenade/Stillroom"
RELEASE_URL = "https://github.com/" + REPOSITORY + "/releases"
RELEASE_API = "https://api.github.com/repos/" + REPOSITORY + "/releases/latest"
MAX_UPDATE_BYTES = 1024 * 1024 * 1024
MAX_EXPANDED_BYTES = 3 * 1024 * 1024 * 1024
MAX_ENTRIES = 50000
MANIFEST = "Component manifest.json"
CHECKSUMS = "Checksums.sha256"
MANAGED_DIRS = frozenset({"app", "public", "runtime", "Third-party licenses"})
MANAGED_FILES = frozenset({MANIFEST, CHECKSUMS, "Read Me.txt", "Start Stillroom.command",
    "Stop Stillroom.command", "Update Stillroom.command", "Start Stillroom.cmd",
    "Stop Stillroom.cmd", "Update Stillroom.cmd"})
MANAGED = MANAGED_DIRS | MANAGED_FILES
BUSY = frozenset({"checking", "downloading", "staging", "installing"})


class UpdateError(LibraryError):
    pass


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,6}\.\d{1,6}\.\d{1,6}", value):
        raise UpdateError("The update has an unsupported version number.")
    return tuple(int(part) for part in value.split("."))


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_relative(name):
    if (not isinstance(name, str) or not name or "\\" in name or "\x00" in name
            or any(ord(c) < 32 for c in name) or name.startswith("/")):
        raise UpdateError("The update contains an unsafe file path.")
    parts = name.rstrip("/").split("/")
    for part in parts:
        if (part in ("", ".", "..") or part.endswith((" ", "."))
                or any(c in part for c in ':<>"|?*')
                or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)):
            raise UpdateError("The update contains an unsafe file path.")
    return PurePosixPath(*parts)


def checksum_entries(text):
    result = {}
    if len(text) > 12 * 1024 * 1024:
        raise UpdateError("The update checksum list is too large.")
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *](.+)", line)
        if not match:
            raise UpdateError("The update checksum list is malformed.")
        path = str(safe_relative(match[2]))
        if path in result:
            raise UpdateError("The update checksum list contains duplicate files.")
        result[path] = match[1].lower()
    return result


def _read_manifest(root):
    path = root / MANIFEST
    try:
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise ValueError()
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("app") != "Stillroom"
                or not isinstance(data.get("platforms"), list)
                or not data["platforms"]
                or any(p not in {"macos-arm64", "macos-x64", "windows-x64"} for p in data["platforms"])):
            raise ValueError()
        version_tuple(data.get("version"))
        return data
    except (OSError, ValueError, TypeError) as exc:
        raise UpdateError("Choose an extracted Stillroom release folder with its component manifest.") from exc


def installed_package(root, target=None):
    """Reject source checkouts and unrelated folders before any managed writes."""
    root = Path(root).resolve()
    data = _read_manifest(root)
    target = platform_id() if target is None else target
    if not target or target not in data["platforms"]:
        raise UpdateError("This release does not support this computer.")
    if any((root / name).exists() for name in (".git", "packaging", "tests", "sources")):
        raise UpdateError("Install updates in an extracted Stillroom release, not its development folder.")
    for name in MANAGED:
        if (root / name).is_symlink():
            raise UpdateError("The app folder contains a linked component. Use a regular extracted release folder.")
    if not all((root / name).is_dir() for name in ("app", "public", "runtime")):
        raise UpdateError("The selected folder is not a complete Stillroom release.")
    if not (root / "app" / "server.py").is_file():
        raise UpdateError("The selected folder is not a complete Stillroom release.")
    return data


def verify_package(root, *, target=None, expected_version=None):
    """Validate a fresh package completely, including every executable byte."""
    root = Path(root).resolve()
    manifest = installed_package(root, target)
    if manifest.get("empty_library") is not True:
        raise UpdateError("Only an empty-library Stillroom release can be installed as an update.")
    if expected_version and manifest["version"] != expected_version:
        raise UpdateError("The downloaded package version does not match its GitHub release.")
    for child in root.iterdir():
        if child.name == ".DS_Store" and child.is_file() and not child.is_symlink():
            continue
        if child.name not in MANAGED:
            raise UpdateError("The update contains personal data or an unexpected top-level file.")
    checksum_path = root / CHECKSUMS
    if checksum_path.stat().st_size > 12 * 1024 * 1024:
        raise UpdateError("The update checksum list is too large.")
    checks = checksum_entries(checksum_path.read_text(encoding="utf-8"))
    files = {}
    names = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.name == ".DS_Store" and path.is_file() and not path.is_symlink():
            continue
        safe_relative(relative)
        key = relative.casefold()
        if key in names or path.is_symlink():
            raise UpdateError("The update contains linked or conflicting files.")
        names.add(key)
        if path.is_file() and relative != CHECKSUMS:
            files[relative] = path
        elif not path.is_file() and not path.is_dir():
            raise UpdateError("The update contains an unsupported file type.")
    if set(checks) != set(files):
        raise UpdateError("The update file list does not match its checksums.")
    for relative, path in files.items():
        if digest(path) != checks[relative]:
            raise UpdateError("The update failed its file integrity check. Download a fresh copy.")
    init_text = (root / "app" / "__init__.py").read_text(encoding="utf-8")
    app_version = re.search(r'(?m)^__version__\s*=\s*[\"\']([^\"\']+)[\"\']', init_text)
    if not app_version or app_version[1] != manifest["version"]:
        raise UpdateError("The update app version does not match its manifest.")
    if not (root / "app" / "updates.py").is_file():
        raise UpdateError("This older package does not support safe updating.")
    package_python(root, target)
    return manifest


def extract_package(archive, destination, *, target=None, expected_version=None):
    archive, destination = Path(archive), Path(destination)
    if archive.stat().st_size > MAX_UPDATE_BYTES:
        raise UpdateError("This update file exceeds the 1 GB limit.")
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(archive) as bundle:
            entries = bundle.infolist()
            if len(entries) > MAX_ENTRIES or sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
                raise UpdateError("The update archive is too large to extract safely.")
            if shutil.disk_usage(destination).free < sum(entry.file_size for entry in entries) + 64 * 1024 * 1024:
                raise UpdateError("There is not enough free disk space to unpack this update safely.")
            seen, roots = set(), set()
            for entry in entries:
                relative = safe_relative(entry.filename)
                key = str(relative).casefold()
                if key in seen:
                    raise UpdateError("The update archive contains duplicate file paths.")
                seen.add(key)
                roots.add(relative.parts[0])
                mode = (entry.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(mode)
                if file_type not in (0, stat.S_IFREG, stat.S_IFDIR) or entry.flag_bits & 1:
                    raise UpdateError("Linked, special, or encrypted update files are unsupported.")
                if len(relative.parts) > 1 and relative.parts[1] not in MANAGED:
                    raise UpdateError("The update contains personal data or an unexpected component.")
            if len(roots) != 1 or not re.fullmatch(r"Stillroom-\d+\.\d+\.\d+-(?:Mac|Windows-x64)", next(iter(roots))):
                raise UpdateError("Choose the original Stillroom release ZIP.")
            for entry in entries:
                relative = safe_relative(entry.filename)
                output = destination.joinpath(*relative.parts)
                if entry.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                    continue
                output.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(entry) as source, output.open("xb") as sink:
                    shutil.copyfileobj(source, sink, 1024 * 1024)
                # Preserve executability, never setuid/setgid or writable-to-all.
                mode = (entry.external_attr >> 16) & 0o777
                output.chmod(0o755 if mode & 0o111 else 0o644)
        package = destination / next(iter(roots))
        manifest = verify_package(package, target=target, expected_version=expected_version)
        return package, manifest
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def package_python(root, target=None):
    target = platform_id() if target is None else target
    if target not in {"macos-arm64", "macos-x64", "windows-x64"}:
        raise UpdateError("This computer is not supported by portable Stillroom updates.")
    relative = "python.exe" if target == "windows-x64" else "bin/python3"
    path = Path(root) / "runtime" / target / "python" / relative
    if not path.is_file() or path.is_symlink():
        raise UpdateError("The update is missing its bundled Python runtime.")
    return path


def _allowed_url(url, *, initial=False):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise UpdateError("The update URL is not an approved HTTPS address.")
    host = parsed.hostname
    if url == RELEASE_API:
        return url
    if host == "github.com" and unquote(parsed.path).startswith("/" + REPOSITORY + "/releases/download/"):
        return url
    if not initial and host in {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}:
        return url
    raise UpdateError("Updates must come from the Stillroom GitHub releases page.")


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _allowed_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _request(url):
    _allowed_url(url, initial=True)
    opener = build_opener(_SafeRedirect())
    return opener.open(Request(url, headers={"User-Agent": "Stillroom/" + __version__,
        "Accept": "application/vnd.github+json" if url == RELEASE_API else "application/octet-stream"}), timeout=30)


def _read_remote(url, limit):
    with _request(url) as response:
        body = response.read(limit + 1)
    if len(body) > limit:
        raise UpdateError("The release response is larger than expected.")
    return body


def release_for_platform(release, target=None):
    target = platform_id() if target is None else target
    if target not in {"macos-arm64", "macos-x64", "windows-x64"}:
        raise UpdateError("Online updates support Mac and Windows portable releases.")
    if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
        raise UpdateError("No stable Stillroom release is available.")
    tag = release.get("tag_name", "")
    version = tag[1:] if isinstance(tag, str) and tag.startswith("v") else tag
    version_tuple(version)
    filename = f"Stillroom-{version}-{'Windows-x64' if target == 'windows-x64' else 'Mac'}.zip"
    assets = release.get("assets", [])
    matching = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == filename]
    if len(matching) != 1:
        raise UpdateError("This release does not yet have a download for your computer.")
    asset = matching[0]
    size = asset.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_UPDATE_BYTES:
        raise UpdateError("The release download size is invalid.")
    url = _allowed_url(asset.get("browser_download_url", ""), initial=True)
    sha = asset.get("digest", "")
    if not isinstance(sha, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", sha):
        sums = [a for a in assets if isinstance(a, dict) and a.get("name") in {"SHA256SUMS.txt", "SHA256SUMS"}]
        if len(sums) != 1:
            raise UpdateError("This release has no verifiable download checksum yet.")
        content = _read_remote(sums[0].get("browser_download_url", ""), 1024 * 1024).decode("utf-8")
        sha = "sha256:" + checksum_entries(content).get(filename, "")
        if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", sha):
            raise UpdateError("This release has no checksum for your download.")
    return {"version": version, "filename": filename, "url": url, "sha256": sha[7:].lower(), "size": size,
            "releaseUrl": RELEASE_URL + "/tag/" + str(tag)}


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class Updates:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.cache = self.root / "data" / ".updates"
        self._lock = threading.RLock()
        self._worker = None
        self._closed = False
        self._release = None
        self._pending = None
        self._last_check = 0
        try:
            manifest = installed_package(self.root)
            self._can_install = True
            self._current = manifest["version"]
        except UpdateError:
            self._can_install = False
            self._current = __version__
        self._state = {"state": "idle", "currentVersion": self._current, "latestVersion": None,
            "message": "Check for a new Stillroom release.", "canInstall": self._can_install,
            "releaseUrl": RELEASE_URL, "progress": None, "filename": None}
        if not self._can_install:
            self._state["message"] = "This is a development copy. Release checks work; install updates in an extracted release."
        try:
            saved = json.loads((self.cache / "status.json").read_text(encoding="utf-8"))
            self._last_check = float(saved.get("lastCheck", 0))
            if saved.get("state") in {"installing", "error", "current"}:
                self._state.update({key: saved[key] for key in ("state", "message", "latestVersion") if key in saved})
                if saved.get("latestVersion") == self._current:
                    self._state.update(state="current", message="Stillroom is up to date. Your library and settings were preserved.")
                elif saved.get("state") == "installing":
                    self._state.update(state="error", message="The last update did not finish. Check data/.updates/updater.log before trying again.")
        except (OSError, ValueError, TypeError):
            pass
        self._cleanup_completed_stage()

    def _cleanup_completed_stage(self):
        """A later run cleans a successful stage only after its helper exits."""
        record_path = self.cache / "cleanup.json"
        try:
            from .import_jobs import process_exists
            record = json.loads(record_path.read_text(encoding="utf-8"))
            pid = record.get("pid")
            if (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1
                    or (self.cache / "install.lock").exists() or process_exists(pid)):
                return
            stage = Path(record["stage"])
            if (stage.is_symlink() or stage.resolve().parent != self.cache.resolve()
                    or not re.fullmatch(r"stage-[0-9a-f]{32}", stage.name)):
                return
            if stage.is_dir():
                shutil.rmtree(stage)
            record_path.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError, KeyError):
            pass

    def status(self):
        with self._lock:
            return dict(self._state)

    def _set(self, **changes):
        with self._lock:
            self._state.update(changes)
            _write_json(self.cache / "status.json", {**self._state, "lastCheck": self._last_check})
            return dict(self._state)

    def _start(self, state, message, work):
        with self._lock:
            if self._closed:
                raise UpdateError("Stillroom is shutting down. Reopen it to update.", 409)
            if self._state["state"] in BUSY:
                raise UpdateError("An update action is already in progress.", 409)
            self._set(state=state, message=message, progress=None)
            def run():
                try:
                    work()
                except Exception as exc:
                    message = str(exc) if isinstance(exc, UpdateError) else "The update could not be completed. Check your connection or choose a fresh Stillroom release ZIP."
                    self._set(state="error", message=message, progress=None)
            self._worker = threading.Thread(target=run, daemon=True, name="stillroom-update")
            self._worker.start()
            return self.status()

    def check(self, auto=False):
        self._cleanup_completed_stage()
        with self._lock:
            if auto and (self._state["state"] in BUSY or time.time() - self._last_check < 86400):
                return self.status()
        def work():
            self._last_check = time.time()
            try:
                release = json.loads(_read_remote(RELEASE_API, 2 * 1024 * 1024))
            except HTTPError as exc:
                if exc.code == 404:
                    self._release = None
                    self._set(state="current", message="No public release has been published yet.", latestVersion=None)
                    return
                if exc.code == 403:
                    raise UpdateError("GitHub is temporarily limiting update checks. Try again later.") from exc
                raise
            self._release = release_for_platform(release)
            newer = version_tuple(self._release["version"]) > version_tuple(self._current)
            self._set(state="available" if newer else "current", latestVersion=self._release["version"],
                filename=self._release["filename"], releaseUrl=self._release["releaseUrl"],
                message="A new Stillroom release is available." if newer else "You have the latest Stillroom release.")
        return self._start("checking", "Checking GitHub for a new release…", work)

    def _require_installable(self):
        if not self._can_install:
            raise UpdateError("Use an extracted Stillroom release to install updates. Development files are protected.", 409)

    def _stage(self, archive, expected_version=None):
        self._set(state="staging", message="Verifying the update and its bundled files…", progress=None)
        destination = self.cache / ("stage-" + uuid.uuid4().hex)
        package, manifest = extract_package(archive, destination, expected_version=expected_version)
        if version_tuple(manifest["version"]) <= version_tuple(self._current):
            shutil.rmtree(destination)
            raise UpdateError("Choose a Stillroom version newer than the one installed.")
        previous = self._pending
        self._pending = package
        if previous and previous.parent.parent == self.cache and previous.parent.name.startswith("stage-"):
            shutil.rmtree(previous.parent, ignore_errors=True)
        self._set(state="ready", latestVersion=manifest["version"], progress=100,
            message="The update is ready. Install and restart to keep your library and settings in this folder.")

    def stage(self, archive):
        self._require_installable()
        archive = Path(archive)
        def work():
            try:
                self._stage(archive)
            finally:
                archive.unlink(missing_ok=True)
        return self._start("staging", "Checking the selected update file…", work)

    def download(self):
        self._require_installable()
        with self._lock:
            release = dict(self._release) if self._release else None
        if not release or version_tuple(release["version"]) <= version_tuple(self._current):
            raise UpdateError("Check for a newer release before downloading an update.", 409)
        def work():
            self.cache.mkdir(parents=True, exist_ok=True)
            handle, name = tempfile.mkstemp(prefix="download-", suffix=".zip", dir=self.cache)
            archive = Path(name)
            received, last_progress = 0, -1
            sha = hashlib.sha256()
            try:
                with os.fdopen(handle, "wb") as sink, _request(release["url"]) as response:
                    while True:
                        if self._closed:
                            raise UpdateError("The update download stopped when Stillroom closed. Download it again after reopening.")
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        received += len(block)
                        if received > release["size"] or received > MAX_UPDATE_BYTES:
                            raise UpdateError("The update download exceeds its declared size.")
                        sink.write(block)
                        sha.update(block)
                        progress = int(100 * received / release["size"])
                        if progress != last_progress:
                            self._set(progress=progress)
                            last_progress = progress
                if received != release["size"] or sha.hexdigest() != release["sha256"]:
                    raise UpdateError("The update download failed its release checksum. Please try again.")
                self._stage(archive, release["version"])
            finally:
                archive.unlink(missing_ok=True)
        return self._start("downloading", "Downloading the new Stillroom release…", work)

    def install(self, port=8768):
        self._require_installable()
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise UpdateError("The app port is invalid.")
        with self._lock:
            if self._state["state"] != "ready" or not self._pending:
                raise UpdateError("Download or select an update before installing it.", 409)
            package = self._pending
            # The helper validates the entire package again before touching the app.
            python = package_python(package)
            self._set(state="installing", message="Installing and restarting Stillroom. This page will reconnect…", progress=None)
            try:
                with (self.cache / "updater.log").open("ab", buffering=0) as log:
                    subprocess.Popen([str(python), "-E", "-s", "-B", "-m", "app.updates", "--apply-to", str(self.root),
                        "--port", str(port), "--non-interactive"], cwd=package,
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True,
                        **detached_options())
            except OSError as exc:
                self._set(state="error", message="Could not start the update helper. Your current app is unchanged.")
                raise UpdateError("Could not start the update helper. Your current app is unchanged.") from exc
            return self.status()

    def close(self):
        self._closed = True


def detached_options():
    if sys.platform == "win32":
        return {"creationflags": 0x00000008 | 0x00000200}
    return {"start_new_session": True}


def _remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def replace_managed(source, target, backup):
    """Move only allowlisted app components; retain an exact rollback journal."""
    source, target, backup = Path(source), Path(target), Path(backup)
    backup.mkdir(parents=True, exist_ok=False)
    changed = []
    try:
        for name in sorted(MANAGED):
            old, new = target / name, source / name
            if not old.exists() and not new.exists():
                continue
            existed = old.exists()
            if existed:
                old.rename(backup / name)
            changed.append((name, existed))
            if new.is_dir():
                shutil.copytree(new, old)
            elif new.is_file():
                shutil.copy2(new, old)
        return changed
    except BaseException:
        restore_managed(target, backup, changed)
        raise


def restore_managed(target, backup, changed):
    for name, existed in reversed(changed):
        _remove(target / name)
        if existed:
            saved = backup / name
            # Copy back so failed upgrades retain a recoverable original backup.
            if saved.is_dir():
                shutil.copytree(saved, target / name)
            else:
                shutil.copy2(saved, target / name)


def _managed_size(root):
    total = 0
    for name in MANAGED:
        component = root / name
        entries = [component, *component.rglob("*")] if component.is_dir() else [component]
        for path in entries:
            if path.is_symlink():
                raise UpdateError("An app component contains a linked file. Use a regular extracted release before updating.")
            if path.is_file():
                total += path.stat().st_size
    return total


def _start_and_verify(root, version, port):
    from .launch import health
    command = [str(package_python(root)), "-E", "-s", "-B", "-m", "app.launch", "--root", str(root), "--port", str(port), "--no-open"]
    result = subprocess.run(command, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=45, **({"creationflags": 0x08000000} if sys.platform == "win32" else {}))
    running = health(port)
    if (result.returncode or not running or running.get("version") != version
            or running.get("root") != str(root)):
        raise UpdateError("The updated app did not restart correctly.")


def _stop_exact(root, port):
    from .launch import health, read_control, stop_server
    from .import_jobs import process_exists
    from .server import SERVER_CONTROL_FILE
    running = health(port)
    if not running:
        # A still-existing control record means a service may be starting/closing.
        # Do not replace its runtime until the user has stopped it completely.
        control = root / "data" / SERVER_CONTROL_FILE
        if control.exists():
            try:
                record = json.loads(control.read_text(encoding="utf-8"))
                pid = record.get("pid")
                if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
                    raise ValueError()
                if process_exists(pid):
                    raise UpdateError("Stillroom is still starting or stopping. Stop it, then try the update again.")
            except (ValueError, TypeError, OSError):
                raise UpdateError("Could not verify that Stillroom has stopped safely.")
        return
    if running.get("root") != str(root):
        raise UpdateError("Another library is running on this port. Stop it before updating this folder.")
    if not read_control(root, port, running):
        raise UpdateError("The running app cannot be stopped safely. Use its Stop Stillroom launcher first.")
    stop_server(root, port, running)
    deadline = time.monotonic() + 15
    while process_exists(running["pid"]):
        if time.monotonic() >= deadline:
            raise UpdateError("Stillroom has not finished stopping. Wait a moment and try again.")
        time.sleep(0.1)


def apply_update(source, target, port=8768):
    source, target = Path(source).resolve(), Path(target).expanduser().resolve()
    new = verify_package(source)
    old = installed_package(target)
    if source == target or target in source.parents and not (target / "data" / ".updates") in source.parents:
        raise UpdateError("Choose the existing app folder separately from the new release folder.")
    if source in target.parents:
        raise UpdateError("The old app folder cannot be inside the new release.")
    if version_tuple(new["version"]) <= version_tuple(old["version"]):
        raise UpdateError("This update must be newer than the installed release.")
    required = _managed_size(source) + _managed_size(target) + 64 * 1024 * 1024
    if shutil.disk_usage(target).free < required:
        raise UpdateError("There is not enough free disk space to update and retain a safe rollback copy. Free some space, then try again.")
    cache = target / "data" / ".updates"
    cache.mkdir(parents=True, exist_ok=True)
    # An exclusive lock protects two launchers attempting the same update.
    lock = cache / "install.lock"
    try:
        handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise UpdateError("Another update is running, or an interrupted update needs recovery. See data/.updates/updater.log.") from exc
    with os.fdopen(handle, "w") as stream:
        stream.write(str(os.getpid()))
    status_path = cache / "status.json"
    backup = cache / ("backup-" + old["version"] + "-" + uuid.uuid4().hex)
    changed = None
    stopped = False
    try:
        _write_json(status_path, {"state": "installing", "latestVersion": new["version"], "message": "Installing and restarting Stillroom…"})
        # Allow the initiating HTTP request to return before stopping its server.
        time.sleep(0.5)
        _stop_exact(target, port)
        stopped = True
        changed = replace_managed(source, target, backup)
        try:
            _start_and_verify(target, new["version"], port)
        except Exception:
            _stop_exact(target, port)
            restore_managed(target, backup, changed)
            changed = None
            raise UpdateError("The new app could not start, so the previous version was restored. Its backup is kept in data/.updates.")
        shutil.rmtree(backup)
        _write_json(status_path, {"state": "current", "latestVersion": new["version"],
            "message": "Stillroom was updated. Your songs, playlists, and settings were preserved."})
        if source.parent.parent == cache and re.fullmatch(r"stage-[0-9a-f]{32}", source.parent.name):
            _write_json(cache / "cleanup.json", {"stage": str(source.parent), "pid": os.getpid()})
        return new["version"]
    except Exception as exc:
        if stopped and changed is None:
            # Replacement failures restore in replace_managed; bring that old
            # app back as well. Verification failures already attempt restart.
            with contextlib.suppress(Exception):
                _start_and_verify(target, old["version"], port)
        message = str(exc) if isinstance(exc, UpdateError) else "The update failed. The previous app was restored where possible; keep data/.updates for recovery."
        _write_json(status_path, {"state": "error", "latestVersion": new["version"], "message": message})
        raise
    finally:
        lock.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Update an existing Stillroom folder while preserving its library and settings.")
    parser.add_argument("--apply-to", type=Path, help="The existing Stillroom release folder")
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Choose a port between 1 and 65535.")
    target = args.apply_to
    if target is None:
        if args.non_interactive:
            parser.error("An existing app folder is required.")
        print("This updates your existing Stillroom folder and preserves its songs, playlists, and settings.")
        print("Paste the old Stillroom folder path below (not the new release folder).")
        value = input("Existing Stillroom folder: ").strip()
        # Finder terminal drag-and-drop quotes spaces. Only parse one path, never execute it.
        if sys.platform != "win32":
            import shlex
            try:
                pieces = shlex.split(value)
                if len(pieces) == 1:
                    value = pieces[0]
            except ValueError:
                pass
        else:
            value = value.strip('"')
        if not value:
            parser.error("No folder was selected.")
        target = Path(value)
    try:
        version = apply_update(Path(__file__).resolve().parents[1], target, args.port)
    except Exception as exc:
        parser.exit(1, f"Update stopped: {exc}\nYour music library remains in the existing app folder.\n")
    print(f"Updated to Stillroom {version}. Continue using your existing app folder; its old app files have been replaced.")
    if not args.non_interactive:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{args.port}")


if __name__ == "__main__":
    main()
