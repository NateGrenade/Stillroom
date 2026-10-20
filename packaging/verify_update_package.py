"""Exercise an old portable release -> new release update in a disposable library.

This never opens or modifies the developer's real Stillroom music library.
Both input ZIPs are local release artifacts; no YouTube/account access is used.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import ProxyHandler, Request, build_opener
import wave
import zipfile

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))
from app.updates import Updates, extract_package, package_python, safe_relative, digest


def call(port, path, method="GET", data=None, headers=None, timeout=30):
    if isinstance(data, dict):
        data = json.dumps(data).encode()
        headers = {"Content-Type": "application/json", **(headers or {})}
    request = Request(f"http://127.0.0.1:{port}" + path, data=data, method=method, headers=headers or {})
    with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
        return json.load(response)


def unpack_old(archive, parent):
    roots = set()
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            relative = safe_relative(entry.filename)
            roots.add(relative.parts[0])
            output = parent.joinpath(*relative.parts)
            if entry.is_dir():
                output.mkdir(parents=True, exist_ok=True)
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(entry) as source, output.open("wb") as sink:
                    import shutil
                    shutil.copyfileobj(source, sink)
                output.chmod(0o755 if (entry.external_attr >> 16) & 0o111 else 0o644)
    assert len(roots) == 1
    return (parent / roots.pop()).resolve()


def launcher(root, port, stop=False):
    return subprocess.run([str(package_python(root)), "-E", "-s", "-B", "-m", "app.launch", "--root", str(root),
        "--port", str(port), "--no-open", *( ["--stop"] if stop else [])], cwd=root,
        capture_output=True, text=True, timeout=40)


def verify(old_archive, new_archive, online=False, local_upload=False):
    if online and local_upload:
        raise ValueError("Choose either online download or local ZIP upload.")
    report = {"oldArchive": str(old_archive), "newArchive": str(new_archive), "checks": [],
              "online": online, "localUpload": local_upload,
              "browserStorage": "Same local origin and port preserved; browser localStorage was not directly exercised."}
    def check(condition, label):
        if not condition:
            raise AssertionError(label)
        report["checks"].append(label)
    with tempfile.TemporaryDirectory(prefix="Stillroom isolated upgrade ") as temp:
        workspace = Path(temp).resolve()
        old = unpack_old(old_archive, workspace / "old")
        new, manifest = (None, None) if online else extract_package(new_archive, workspace / "new")
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        try:
            launch = launcher(old, port)
            check(launch.returncode == 0, "old release starts on an isolated random port")
            original_health = call(port, "/api/health")
            sample = io.BytesIO()
            with wave.open(sample, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(8000)
                wav.writeframes(b"\x00\x00" * 8000)
            metadata = base64.b64encode(json.dumps({"title": "Synthetic update preservation test", "artist": "Stillroom QA", "tags": ["test"]}).encode()).decode()
            track = call(port, "/api/import", "POST", sample.getvalue(), {"Content-Type": "audio/wav", "X-Filename": "synthetic.wav", "X-Track-Metadata": metadata})["track"]
            playlist = call(port, "/api/playlists", "POST", {"name": "Preserve this playlist"})
            call(port, "/api/playlists/" + playlist["id"], "PATCH", {"trackIds": [track["id"]]})
            before = call(port, "/api/library")
            check(len(before["tracks"]) == 1 and len(before["playlists"]) == 1, "old release saves a real synthetic audio file and playlist")
            audio_hashes = {p.name: digest(p) for p in (old / "audio").iterdir() if p.is_file()}
            marker_files = {"data/update-test-settings.json": b'{"volume":0.37,"theme":"dark"}',
                            "imports/update-test-original.wav": sample.getvalue(),
                            "personal-note.txt": b"preserve unmanaged file"}
            for relative, body in marker_files.items():
                (old / relative).parent.mkdir(parents=True, exist_ok=True)
                (old / relative).write_bytes(body)
            started = time.monotonic()
            if online:
                manager = Updates(old)
                manager.check()
                manager._worker.join(60)
                status = manager.status()
                check(status["state"] == "available", "live GitHub release check finds a newer portable release")
                manifest = {"version": status["latestVersion"]}
                manager.download()
                manager._worker.join(600)
                status = manager.status()
                check(status["state"] == "ready", "real GitHub download passes release digest and full package verification")
                report["publishedRelease"] = status["releaseUrl"]
                manager.install(port)
                deadline = time.monotonic() + 180
                result = {}
                while time.monotonic() < deadline:
                    with contextlib.suppress(OSError, ValueError):
                        result = json.loads((old / "data/.updates/status.json").read_text())
                        if result.get("state") in {"error", "current"}:
                            break
                    time.sleep(0.25)
                manager.close()
                report["helperOutput"] = (old / "data/.updates/updater.log").read_text()[-6000:]
                check(result.get("state") == "current", "real staged bundled helper installs and restarts after online download")
                # The QA process owns the helper (the production parent exits
                # during restart). Reap our child so cleanup can verify exit.
                cleanup = json.loads((old / "data/.updates/cleanup.json").read_text())
                if hasattr(os, "waitpid"):
                    with contextlib.suppress(ChildProcessError):
                        os.waitpid(cleanup["pid"], 0)
                fresh_manager = Updates(old)
                fresh_manager._cleanup_completed_stage()
                check(not list((old / "data/.updates").glob("stage-*")), "completed online staging files are cleaned after the helper exits")
                fresh_manager.close()
            elif local_upload:
                old_manifest = json.loads((old / "Component manifest.json").read_text())
                check(original_health["version"] == old_manifest["version"], f"local ZIP update starts from the actual {old_manifest['version']} server")
                check(call(port, "/api/updates")["canInstall"], "old portable server exposes the in-app updater")
                # Stream the same ZIP that the Settings file picker uploads;
                # only the running old app may stage or start this update.
                with new_archive.open("rb") as upload:
                    status = call(port, "/api/updates/package", "POST", upload,
                        {"Content-Type": "application/zip", "Content-Length": str(new_archive.stat().st_size)}, timeout=180)
                check(status["state"] in {"staging", "ready"}, "old server accepts the release ZIP through its upload API")
                deadline = time.monotonic() + 180
                while status["state"] == "staging" and time.monotonic() < deadline:
                    time.sleep(0.25)
                    status = call(port, "/api/updates")
                check(status["state"] == "ready", "old server verifies the uploaded package and reaches ready: " + status.get("message", ""))
                check(status["latestVersion"] == manifest["version"], "old server recognizes the uploaded release version")
                report["uploadStatus"] = status
                status = call(port, "/api/updates/install", "POST", {})
                check(status["state"] == "installing", "install API starts the bundled helper from the uploaded package")
                deadline = time.monotonic() + 180
                result = {}
                while time.monotonic() < deadline:
                    with contextlib.suppress(OSError, ValueError):
                        result = json.loads((old / "data/.updates/status.json").read_text())
                        if result.get("state") in {"error", "current"}:
                            break
                    time.sleep(0.25)
                report["helperOutput"] = (old / "data/.updates/updater.log").read_text()[-6000:]
                check(result.get("state") == "current", "uploaded package helper installs and restarts successfully: " + result.get("message", ""))
                # The old server, rather than this QA process, spawned the
                # helper. Wait for its exit before exercising normal cleanup.
                from app.import_jobs import process_exists
                deadline = time.monotonic() + 30
                cleanup = None
                while time.monotonic() < deadline:
                    with contextlib.suppress(OSError, ValueError):
                        cleanup = json.loads((old / "data/.updates/cleanup.json").read_text())
                    if cleanup and not process_exists(cleanup["pid"]) and not (old / "data/.updates/install.lock").exists():
                        break
                    time.sleep(0.25)
                check(cleanup is not None and not process_exists(cleanup["pid"]), "server-launched update helper exits after successful restart")
                fresh_manager = Updates(old)
                try:
                    fresh_manager._cleanup_completed_stage()
                    check(not list((old / "data/.updates").glob("stage-*")), "completed uploaded staging files are cleaned after the helper exits")
                finally:
                    fresh_manager.close()
            else:
                command = [str(package_python(new)), "-E", "-s", "-B", "-m", "app.updates", "--apply-to", str(old), "--port", str(port), "--non-interactive"]
                update = subprocess.run(command, cwd=new, capture_output=True, text=True, timeout=180)
                report["helperOutput"] = (update.stdout + update.stderr)[-6000:]
                check(update.returncode == 0, "real bundled helper replaces old release and exits successfully")
            report["updateSeconds"] = round(time.monotonic() - started, 2)
            health = call(port, "/api/health")
            check(health["version"] == manifest["version"], "restarted service reports the new release version")
            check(health["pid"] != original_health["pid"], "old server is stopped and a new process is running")
            check(health["root"] == str(old), "library stays in the exact original folder")
            after = call(port, "/api/library")
            check(after["tracks"] == before["tracks"], "saved track IDs and all track metadata survive exactly")
            check(after["playlists"] == before["playlists"], "playlist IDs, names, track order, and membership survive exactly")
            check({p.name: digest(p) for p in (old / "audio").iterdir() if p.is_file()} == audio_hashes, "all audio files remain byte-for-byte identical")
            for relative, body in marker_files.items():
                check((old / relative).read_bytes() == body, f"preserved {relative}")
            check(not list((old / "data/.updates").glob("backup-*")), "previous app backup is removed after successful restart verification")
            check(not (old / "data/.updates/install.lock").exists(), "installer lock is released")
            settings = call(port, "/api/settings")
            check(isinstance(settings, dict), "new settings API responds after the update")
            updates = call(port, "/api/updates")
            check(updates["canInstall"] and updates["currentVersion"] == manifest["version"], "updated portable copy enables subsequent in-app updates")
            report.update(success=True, newVersion=manifest["version"], checkCount=len(report["checks"]))
        finally:
            with contextlib.suppress(Exception):
                launcher(old, port, stop=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", required=True, type=Path)
    parser.add_argument("--new", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--online", action="store_true", help="Download the published GitHub release and exercise its real in-app update helper")
    mode.add_argument("--local-upload", action="store_true", help="Upload the new ZIP to the old portable server and install through its in-app update API (requires 1.5.0 or newer)")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify(args.old.resolve(), args.new.resolve(), args.online, args.local_upload)
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
