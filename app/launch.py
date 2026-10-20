"""Start/stop a detached Stillroom server without depending on an open terminal."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
import webbrowser

from .server import DEFAULT_PORT, DEFAULT_ROOT, SERVER_CONTROL_FILE


def health(port):
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
            data = json.load(response)
        return data if isinstance(data, dict) and data.get("app") == "stillroom" else None
    except (OSError, ValueError, HTTPError, URLError):
        return None


def detached_process_options():
    """Detach without leaving a console window on packaged Windows builds."""
    if sys.platform == "win32":
        return {"creationflags": (getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                                  | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))}
    return {"start_new_session": True}


def read_control(root, port, running):
    try:
        record = json.loads((root / "data" / SERVER_CONTROL_FILE).read_text(encoding="utf-8"))
        if (isinstance(record, dict) and record.get("app") == "stillroom"
                and record.get("pid") == running.get("pid") and record.get("port") == port
                and record.get("root") == str(root)
                and isinstance(record.get("token"), str) and record["token"]):
            return record
    except (OSError, ValueError):
        pass
    return None


def stop_server(root, port, running):
    """Ask the exact local library to clean up before exiting."""
    pid = running.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        raise RuntimeError("Could not identify this service safely.")
    record = read_control(root, port, running)
    if record:
        request = Request(f"http://127.0.0.1:{port}/api/shutdown",
                          data=json.dumps({"token": record["token"]}).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
                result = json.load(response)
                if not result.get("success"):
                    raise RuntimeError("Stillroom did not accept the stop request.")
        except (OSError, ValueError, HTTPError, URLError) as exc:
            raise RuntimeError("Stillroom could not be stopped safely. Try Stop Stillroom again.") from exc
    else:
        # Health responses alone do not authenticate a process. Never send a
        # signal to a PID supplied by another service on the local port.
        raise RuntimeError("Stillroom could not verify this service safely. Close the Stillroom process manually, then reopen Stillroom.")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = health(port)
        same_server = current and current.get("pid") == pid and current.get("root") == str(root)
        if not same_server and (not record or read_control(root, port, running) != record):
            return
        time.sleep(0.1)
    raise RuntimeError("The service did not finish stopping. Wait a few seconds and try Stop Stillroom again.")


def main():
    parser = argparse.ArgumentParser(description="Open or stop your private Stillroom library.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-open", action="store_true", help="Start the service without opening a browser")
    parser.add_argument("--stop", action="store_true", help="Stop this library's local service")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    if not 1 <= args.port <= 65535:
        parser.error("Choose a port between 1 and 65535.")
    running = health(args.port)
    if running and Path(running.get("root", "")).resolve() != root:
        parser.error(f"Another Stillroom library is using port {args.port}. Stop that library first, or launch this one with --port and a different number.")
    if args.stop:
        if running:
            try:
                stop_server(root, args.port, running)
            except (RuntimeError, OSError) as exc:
                parser.error(str(exc))
            print("Stillroom stopped. Your library is saved.")
            return
        print("Stillroom is already stopped. Your library is saved.")
        return
    if not running:
        (root / "data").mkdir(parents=True, exist_ok=True)
        with (root / "data" / "server.log").open("ab", buffering=0) as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "app.server", "--root", str(root), "--port", str(args.port)],
                cwd=DEFAULT_ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                close_fds=True, **detached_process_options(),
            )
        for _ in range(60):
            running = health(args.port)
            if running:
                break
            if process.poll() is not None:
                # A simultaneous launch may have opened the same library first.
                running = health(args.port)
                if running:
                    break
                parser.error(f"Could not start Stillroom on port {args.port}. Another app may be using it. Read {root / 'data' / 'server.log'} for details, or try --port with a different number.")
            time.sleep(0.1)
        if not running:
            process.terminate()
            parser.error(f"Stillroom did not respond. Read {root / 'data' / 'server.log'} for details.")
        if Path(running.get("root", "")).resolve() != root:
            if process.poll() is None:
                process.terminate()
            parser.error(f"Another Stillroom library is using port {args.port}. Stop that library first, or launch this one with --port and a different number.")
    url = f"http://127.0.0.1:{args.port}"
    print(f"Stillroom is ready at {url}")
    if not args.no_open:
        webbrowser.open(url)


if __name__ == "__main__":
    main()
