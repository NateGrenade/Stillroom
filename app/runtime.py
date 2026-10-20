"""Resolve portable audio tools and manage only Stillroom's child processes.

Packaged tools live below runtime/<platform>/bin. Source checkouts can continue
to use their local downloader and installed tools without changing the library.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys


APP_ROOT = Path(__file__).resolve().parents[1]
# Windows-only subprocess constants are absent when testing on macOS/Linux.
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def platform_id():
    """Select tools matching the running interpreter, including Rosetta x64."""
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        if machine in ("arm64", "aarch64"):
            return "macos-arm64"
        if machine in ("x86_64", "amd64"):
            return "macos-x64"
    elif sys.platform == "win32" and machine in ("x86_64", "amd64"):
        return "windows-x64"
    return None


def bundled_bin(root=None):
    """Return only this platform's directory; never select another architecture."""
    target = platform_id()
    return Path(APP_ROOT if root is None else root) / "runtime" / target / "bin" if target else None


def _usable(path):
    return path.is_file() and (sys.platform == "win32" or os.access(path, os.X_OK))


def find_executable(name, *, root=None):
    """Find a bundled executable before source-checkout and installed tools."""
    root = Path(APP_ROOT if root is None else root)
    filename = name + ".exe" if sys.platform == "win32" and not name.lower().endswith(".exe") else name
    directory = bundled_bin(root)
    if directory is not None:
        candidate = directory / filename
        if _usable(candidate):
            return str(candidate)
    if name in ("yt-dlp", "yt-dlp.exe"):
        candidate = root / ".tools" / "audio" / "bin" / filename
        if _usable(candidate):
            return str(candidate)
    found = shutil.which(filename)
    if found:
        return found
    if sys.platform == "darwin":
        for folder in ("/opt/homebrew/bin", "/usr/local/bin"):
            candidate = Path(folder) / filename
            if _usable(candidate):
                return str(candidate)
    return None


def subprocess_options(*, new_process_group=False):
    """Hide Windows tool windows and optionally isolate a cancellable child tree."""
    if sys.platform == "win32":
        flags = CREATE_NO_WINDOW
        if new_process_group:
            flags |= CREATE_NEW_PROCESS_GROUP
        return {"creationflags": flags}
    return {"start_new_session": True} if new_process_group else {}


def stop_process_tree(process):
    """Stop and reap a child started with new_process_group=True.

    Windows taskkill receives only the owned child's PID. No executable-name
    matching is used, so another Stillroom instance's tools remain untouched.
    """
    if process.poll() is None:
        if sys.platform == "win32":
            taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=10,
                    **subprocess_options(),
                )
            # A process may exit while taskkill is starting. If taskkill itself
            # was unavailable, at least terminate and reap the direct child.
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
    process.wait()
