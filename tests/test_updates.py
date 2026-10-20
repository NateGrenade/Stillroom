"""Update integrity, library preservation, rollback, and release authority."""
import io
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
import zipfile

from app import updates
from app.updates import UpdateError

TARGET = "macos-arm64"


def make_package(root, version="1.5.0", *, target=TARGET):
    root.mkdir(parents=True, exist_ok=True)
    files = {
        "app/__init__.py": f'__version__ = "{version}"\n',
        "app/server.py": "# packaged server\n",
        "app/updates.py": "# packaged updater\n",
        "public/index.html": f"Stillroom {version}",
        "Third-party licenses/license.txt": "license",
        "Start Stillroom.command": "#!/bin/sh\n",
        "Stop Stillroom.command": "#!/bin/sh\n",
        "Update Stillroom.command": "#!/bin/sh\n",
        "Read Me.txt": "hello",
        f"runtime/{target}/python/" + ("python.exe" if target == "windows-x64" else "bin/python3"): "test-runtime",
        updates.MANIFEST: json.dumps({"app": "Stillroom", "version": version, "platforms": [target], "empty_library": True}),
    }
    for name, value in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        path.chmod(0o755 if name.endswith("command") or "python3" in name else 0o644)
    seal(root)
    return root


def seal(root):
    (root / updates.CHECKSUMS).write_text("".join(
        f"{updates.digest(p)}  {p.relative_to(root).as_posix()}\n"
        for p in sorted(root.rglob("*")) if p.is_file() and p.name != updates.CHECKSUMS))


def archive_package(root, archive):
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in root.rglob("*"):
            if path.is_file():
                bundle.write(path, "Stillroom-1.5.0-Mac/" + path.relative_to(root).as_posix())
    return archive


def release(version="1.5.0", target="Mac"):
    name = f"Stillroom-{version}-{target}.zip"
    return {"tag_name": "v" + version, "draft": False, "prerelease": False,
            "assets": [{"name": name, "size": 123, "digest": "sha256:" + "a" * 64,
                        "browser_download_url": f"https://github.com/NateGrenade/Stillroom/releases/download/v{version}/{name}"}]}


class UpdateTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="Stillroom update tests ")
        self.root = Path(self.temporary.name).resolve()
        self.platform_patch = patch("app.updates.platform_id", return_value=TARGET)
        self.platform_patch.start()

    def tearDown(self):
        self.platform_patch.stop()
        self.temporary.cleanup()


class PackageValidationTests(UpdateTestCase):
    def setUp(self):
        super().setUp()
        self.package = make_package(self.root / "release")
        self.zip = archive_package(self.package, self.root / "release.zip")

    def test_extract_verified_package_and_preserve_executable_bit(self):
        result, manifest = updates.extract_package(self.zip, self.root / "stage")
        self.assertEqual(manifest["version"], "1.5.0")
        self.assertEqual((result / "public/index.html").read_text(), "Stillroom 1.5.0")
        self.assertTrue((result / "Start Stillroom.command").stat().st_mode & stat.S_IXUSR)

    def test_expected_version_must_match(self):
        with self.assertRaisesRegex(UpdateError, "version"):
            updates.extract_package(self.zip, self.root / "stage", expected_version="1.6.0")
        self.assertFalse((self.root / "stage").exists())

    def test_reject_corrupt_file_even_if_manifest_valid(self):
        (self.package / "app/server.py").write_text("corruption")
        with self.assertRaisesRegex(UpdateError, "integrity"):
            updates.verify_package(self.package)

    def test_reject_missing_checksum_entry(self):
        (self.package / "app/extra.py").write_text("extra")
        with self.assertRaisesRegex(UpdateError, "file list"):
            updates.verify_package(self.package)

    def test_reject_checksum_for_missing_file(self):
        (self.package / "app/server.py").unlink()
        with self.assertRaises(UpdateError):
            updates.verify_package(self.package)

    def test_reject_private_library_folder(self):
        (self.package / "data").mkdir()
        (self.package / "data/library.sqlite3").write_bytes(b"private")
        seal(self.package)
        with self.assertRaisesRegex(UpdateError, "personal data"):
            updates.verify_package(self.package)

    def test_reject_app_manifest_version_disagreement(self):
        (self.package / "app/__init__.py").write_text('__version__ = "1.4.0"')
        seal(self.package)
        with self.assertRaisesRegex(UpdateError, "version"):
            updates.verify_package(self.package)

    def test_reject_other_platform(self):
        with self.assertRaisesRegex(UpdateError, "support"):
            updates.verify_package(self.package, target="windows-x64")

    def test_reject_source_development_tree(self):
        (self.package / "packaging").mkdir()
        with self.assertRaisesRegex(UpdateError, "development"):
            updates.installed_package(self.package)

    def test_reject_symlink_component(self):
        shutil.rmtree(self.package / "public")
        (self.root / "elsewhere").mkdir()
        (self.package / "public").symlink_to(self.root / "elsewhere", target_is_directory=True)
        with self.assertRaisesRegex(UpdateError, "linked"):
            updates.installed_package(self.package)

    def test_reject_nested_symlink(self):
        (self.package / "public/link").symlink_to(self.package / "app/server.py")
        with self.assertRaisesRegex(UpdateError, "linked"):
            updates.verify_package(self.package)

    def test_finder_metadata_does_not_prevent_extracted_package_bootstrap(self):
        (self.package / ".DS_Store").write_bytes(b"Finder folder preferences")
        (self.package / "public/.DS_Store").write_bytes(b"Finder folder preferences")
        self.assertEqual(updates.verify_package(self.package)["version"], "1.5.0")

    def test_reject_duplicate_checksum_entries(self):
        path = self.package / updates.CHECKSUMS
        path.write_text(path.read_text() + path.read_text().splitlines()[0] + "\n")
        with self.assertRaisesRegex(UpdateError, "duplicate"):
            updates.verify_package(self.package)

    def test_reject_unsafe_archive_names_before_writes(self):
        for bad in ("../escape", "/absolute", "Stillroom-1.5.0-Mac/../../escape",
                    "Stillroom-1.5.0-Mac/app/../escape", "Stillroom-1.5.0-Mac/app\\escape",
                    "Stillroom-1.5.0-Mac/app/C:bad", "Stillroom-1.5.0-Mac/app/CON.txt",
                    "Stillroom-1.5.0-Mac/app/trailing.", "Stillroom-1.5.0-Mac/app//bad"):
            with self.subTest(path=bad):
                with zipfile.ZipFile(self.root / "bad.zip", "w") as bundle:
                    bundle.writestr(bad, "no")
                with self.assertRaises(UpdateError):
                    updates.extract_package(self.root / "bad.zip", self.root / "badstage")
                self.assertFalse((self.root / "badstage").exists())
        self.assertFalse((self.root / "escape").exists())

    def test_reject_duplicate_and_case_colliding_archive_files(self):
        for pair in (("app/a", "app/a"), ("app/A", "app/a")):
            with self.subTest(pair=pair):
                with zipfile.ZipFile(self.root / "bad.zip", "w") as bundle:
                    import warnings
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        for name in pair:
                            bundle.writestr("Stillroom-1.5.0-Mac/" + name, "no")
                with self.assertRaisesRegex(UpdateError, "duplicate"):
                    updates.extract_package(self.root / "bad.zip", self.root / "badstage")

    def test_reject_archive_symlink_and_device(self):
        for kind in (stat.S_IFLNK, stat.S_IFCHR, stat.S_IFIFO):
            info = zipfile.ZipInfo("Stillroom-1.5.0-Mac/app/bad")
            info.create_system = 3
            info.external_attr = (kind | 0o777) << 16
            with zipfile.ZipFile(self.root / "bad.zip", "w") as bundle:
                bundle.writestr(info, "no")
            with self.assertRaisesRegex(UpdateError, "special"):
                updates.extract_package(self.root / "bad.zip", self.root / "badstage")

    def test_reject_personal_data_in_archive(self):
        with zipfile.ZipFile(self.zip, "a") as bundle:
            bundle.writestr("Stillroom-1.5.0-Mac/audio/my-song.mp3", "private")
        with self.assertRaisesRegex(UpdateError, "personal data"):
            updates.extract_package(self.zip, self.root / "stage")

    def test_bound_archive_expansion(self):
        with patch("app.updates.MAX_EXPANDED_BYTES", 1):
            with self.assertRaisesRegex(UpdateError, "too large"):
                updates.extract_package(self.zip, self.root / "stage")

    def test_bound_archive_file_count(self):
        with patch("app.updates.MAX_ENTRIES", 1):
            with self.assertRaisesRegex(UpdateError, "too large"):
                updates.extract_package(self.zip, self.root / "stage")


class ReleaseTests(unittest.TestCase):
    def test_select_mac_release_and_digest(self):
        chosen = updates.release_for_platform(release(), TARGET)
        self.assertEqual(chosen["filename"], "Stillroom-1.5.0-Mac.zip")
        self.assertEqual(chosen["sha256"], "a" * 64)

    def test_select_windows_release(self):
        chosen = updates.release_for_platform(release(target="Windows-x64"), "windows-x64")
        self.assertTrue(chosen["filename"].endswith("Windows-x64.zip"))

    def test_checksum_fallback(self):
        data = release()
        data["assets"][0].pop("digest")
        data["assets"].append({"name": "SHA256SUMS.txt", "browser_download_url": "https://github.com/NateGrenade/Stillroom/releases/download/v1.5.0/SHA256SUMS.txt"})
        with patch("app.updates._read_remote", return_value=("b" * 64 + "  Stillroom-1.5.0-Mac.zip\n").encode()):
            self.assertEqual(updates.release_for_platform(data, TARGET)["sha256"], "b" * 64)

    def test_missing_digest_and_checksum_refused(self):
        data = release()
        data["assets"][0].pop("digest")
        with self.assertRaisesRegex(UpdateError, "checksum"):
            updates.release_for_platform(data, TARGET)

    def test_untrusted_release_asset_url_refused(self):
        for url in ("http://github.com/NateGrenade/Stillroom/releases/download/v1.5.0/app.zip",
                    "https://github.com/attacker/repo/releases/download/v1.5.0/app.zip",
                    "https://github.com.evil.test/NateGrenade/Stillroom/releases/download/app.zip",
                    "https://example.com/app.zip", "file:///tmp/app.zip", "https://user:pass@github.com/NateGrenade/Stillroom/releases/download/app.zip"):
            with self.subTest(url=url):
                data = release()
                data["assets"][0]["browser_download_url"] = url
                with self.assertRaises(UpdateError):
                    updates.release_for_platform(data, TARGET)

    def test_redirect_cannot_leave_github_release_hosts(self):
        self.assertEqual(updates._allowed_url("https://release-assets.githubusercontent.com/test"), "https://release-assets.githubusercontent.com/test")
        for url in ("http://release-assets.githubusercontent.com/a", "https://example.org/payload", "https://api.github.com/other"):
            with self.assertRaises(UpdateError):
                updates._allowed_url(url)

    def test_numeric_version_order_and_validation(self):
        self.assertGreater(updates.version_tuple("1.10.0"), updates.version_tuple("1.9.9"))
        for value in (None, "nightly", "1.2.3-rc1", "1.2", "1.2.3/evil"):
            with self.assertRaises(UpdateError):
                updates.version_tuple(value)


class ManagerTests(UpdateTestCase):
    def setUp(self):
        super().setUp()
        self.old = make_package(self.root / "old", "1.4.0")
        self.new = make_package(self.root / "new", "1.5.0")
        self.manager = updates.Updates(self.old)

    def tearDown(self):
        self.manager.close()
        if self.manager._worker:
            self.manager._worker.join(5)
        super().tearDown()

    def join(self):
        self.manager._worker.join(5)
        self.assertFalse(self.manager._worker.is_alive())
        return self.manager.status()

    def test_source_copy_is_check_only(self):
        source = updates.Updates(self.root / "source")
        self.assertFalse(source.status()["canInstall"])
        for action in (source.download, lambda: source.stage(self.root / "upload.zip"), source.install):
            with self.assertRaisesRegex(UpdateError, "Development"):
                action()

    def test_check_new_release_and_daily_auto_throttle(self):
        with patch("app.updates._read_remote", return_value=json.dumps(release()).encode()) as remote:
            self.manager.check()
            self.assertEqual(self.join()["state"], "available")
            self.manager.check(auto=True)
            self.assertEqual(remote.call_count, 1)

    def test_empty_repository_has_clear_message(self):
        with patch("app.updates._read_remote", side_effect=HTTPError(updates.RELEASE_API, 404, "No releases", {}, None)):
            self.manager.check()
            status = self.join()
        self.assertEqual(status["state"], "current")
        self.assertIn("No public release", status["message"])

    def test_stage_owns_upload_and_requires_newer_version(self):
        archive = archive_package(self.new, self.root / "upload.zip")
        self.manager.stage(archive)
        status = self.join()
        self.assertEqual(status["state"], "ready", status)
        self.assertFalse(archive.exists())
        self.assertEqual(status["latestVersion"], "1.5.0")
        self.assertNotIn("packagePath", status)
        old_zip = archive_package(self.old, self.root / "old.zip")
        self.manager.stage(old_zip)
        self.assertEqual(self.join()["state"], "error")
        self.assertFalse(old_zip.exists())

    def test_download_hash_mismatch_never_stages(self):
        self.manager._release = updates.release_for_platform(release(), TARGET)
        with patch("app.updates._request", return_value=io.BytesIO(b"x" * 123)):
            self.manager.download()
            status = self.join()
        self.assertEqual(status["state"], "error")
        self.assertIn("checksum", status["message"])
        self.assertIsNone(self.manager._pending)
        self.assertEqual(list(self.manager.cache.glob("download-*")), [])

    def test_verified_download_stages_matching_version(self):
        archive = archive_package(self.new, self.root / "source.zip")
        body = archive.read_bytes()
        chosen = updates.release_for_platform(release(), TARGET)
        chosen.update(size=len(body), sha256=updates.digest(archive))
        self.manager._release = chosen
        with patch("app.updates._request", return_value=io.BytesIO(body)):
            self.manager.download()
            status = self.join()
        self.assertEqual(status["state"], "ready", status)

    def test_install_runs_staged_runtime_not_running_runtime(self):
        self.manager.stage(archive_package(self.new, self.root / "upload.zip"))
        self.join()
        pending = self.manager._pending
        with patch("app.updates.subprocess.Popen") as process:
            status = self.manager.install(12345)
        self.assertEqual(status["state"], "installing")
        args, kwargs = process.call_args
        self.assertEqual(args[0][0], str(updates.package_python(pending)))
        self.assertEqual(kwargs["cwd"], pending)
        self.assertIn(str(self.old), args[0])
        self.assertIn("12345", args[0])
        self.assertIn("--non-interactive", args[0])

    def test_completed_stage_cleanup_waits_for_helper_exit(self):
        stage = self.manager.cache / ("stage-" + "a" * 32)
        stage.mkdir(parents=True)
        updates._write_json(self.manager.cache / "cleanup.json", {"pid": 12345, "stage": str(stage)})
        with patch("app.import_jobs.process_exists", return_value=True):
            self.manager._cleanup_completed_stage()
        self.assertTrue(stage.exists())
        with patch("app.import_jobs.process_exists", return_value=False):
            self.manager._cleanup_completed_stage()
        self.assertFalse(stage.exists())

    def test_completed_stage_cleanup_never_deletes_outside_cache(self):
        outside = self.root / ("stage-" + "b" * 32)
        outside.mkdir()
        updates._write_json(self.manager.cache / "cleanup.json", {"pid": 12345, "stage": str(outside)})
        with patch("app.import_jobs.process_exists", return_value=False):
            self.manager._cleanup_completed_stage()
        self.assertTrue(outside.exists())

    def test_install_without_staged_update_rejected(self):
        with self.assertRaisesRegex(UpdateError, "Download or select"):
            self.manager.install()

    def test_closed_manager_rejects_new_jobs(self):
        self.manager.close()
        with self.assertRaisesRegex(UpdateError, "shutting down"):
            self.manager.check()


class ApplyTests(UpdateTestCase):
    def setUp(self):
        super().setUp()
        self.old = make_package(self.root / "old", "1.4.0")
        self.new = make_package(self.root / "new", "1.5.0")
        self.private = {"data/library.sqlite3": b"songs and playlists", "audio/song.mp3": b"audio bytes",
                        "imports/custom.wav": b"original", "data/youtube-cookies.txt": b"private session",
                        "my-notes.txt": b"unmanaged"}
        for name, body in self.private.items():
            path = self.old / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        self.sleep = patch("app.updates.time.sleep")
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        super().tearDown()

    def assert_private_preserved(self):
        for name, body in self.private.items():
            self.assertEqual((self.old / name).read_bytes(), body)

    def test_success_preserves_all_private_files_restarts_same_root_port_removes_old_backup(self):
        with patch("app.updates._stop_exact") as stop, patch("app.updates._start_and_verify") as start:
            result = updates.apply_update(self.new, self.old, 12345)
        self.assertEqual(result, "1.5.0")
        self.assert_private_preserved()
        self.assertIn('"1.5.0"', (self.old / "app/__init__.py").read_text())
        stop.assert_called_once_with(self.old, 12345)
        start.assert_called_once_with(self.old, "1.5.0", 12345)
        self.assertEqual(list((self.old / "data/.updates").glob("backup-*")), [])
        self.assertFalse((self.old / "data/.updates/install.lock").exists())
        self.assertEqual(json.loads((self.old / "data/.updates/status.json").read_text())["state"], "current")

    def test_failed_restart_restores_previous_app_and_preserves_backup(self):
        def start(root, version, port):
            if version == "1.5.0":
                raise UpdateError("startup failed")
        with patch("app.updates._stop_exact"), patch("app.updates._start_and_verify", side_effect=start) as restart:
            with self.assertRaisesRegex(UpdateError, "previous version was restored"):
                updates.apply_update(self.new, self.old)
        self.assert_private_preserved()
        self.assertIn('"1.4.0"', (self.old / "app/__init__.py").read_text())
        self.assertTrue(list((self.old / "data/.updates").glob("backup-*")))
        self.assertIn(unittest.mock.call(self.old, "1.4.0", 8768), restart.call_args_list)

    def test_copy_failure_rolls_back_and_restarts_old_app(self):
        actual_copytree = shutil.copytree
        def copytree(source, target, *args, **kwargs):
            if Path(source) == self.new / "public":
                raise OSError("disk full")
            return actual_copytree(source, target, *args, **kwargs)
        with patch("app.updates._stop_exact"), patch("app.updates._start_and_verify") as restart, patch("app.updates.shutil.copytree", side_effect=copytree):
            with self.assertRaisesRegex(OSError, "disk full"):
                updates.apply_update(self.new, self.old)
        self.assert_private_preserved()
        self.assertIn('"1.4.0"', (self.old / "app/__init__.py").read_text())
        self.assertEqual((self.old / "public/index.html").read_text(), "Stillroom 1.4.0")
        restart.assert_called_once_with(self.old, "1.4.0", 8768)

    def test_stop_failure_never_replaces_any_app_file(self):
        with patch("app.updates._stop_exact", side_effect=UpdateError("cannot stop")), patch("app.updates.replace_managed") as replace:
            with self.assertRaisesRegex(UpdateError, "cannot stop"):
                updates.apply_update(self.new, self.old)
        replace.assert_not_called()
        self.assert_private_preserved()
        self.assertIn('"1.4.0"', (self.old / "app/__init__.py").read_text())

    def test_not_enough_rollback_space_refuses_before_stopping(self):
        with patch("app.updates.shutil.disk_usage", return_value=Mock(free=1)), patch("app.updates._stop_exact") as stop:
            with self.assertRaisesRegex(UpdateError, "disk space"):
                updates.apply_update(self.new, self.old)
        stop.assert_not_called()
        self.assert_private_preserved()

    def test_lock_prevents_two_concurrent_installers(self):
        lock = self.old / "data/.updates/install.lock"
        lock.parent.mkdir()
        lock.write_text("123")
        with patch("app.updates._stop_exact") as stop:
            with self.assertRaisesRegex(UpdateError, "Another update"):
                updates.apply_update(self.new, self.old)
        stop.assert_not_called()
        self.assertTrue(lock.exists())

    def test_new_package_tamper_is_detected_before_stop(self):
        (self.new / "app/server.py").write_text("changed after staging")
        with patch("app.updates._stop_exact") as stop:
            with self.assertRaisesRegex(UpdateError, "integrity"):
                updates.apply_update(self.new, self.old)
        stop.assert_not_called()

    def test_no_downgrade(self):
        newer = make_package(self.root / "too-new", "2.0.0")
        with patch("app.updates._stop_exact") as stop:
            with self.assertRaisesRegex(UpdateError, "newer"):
                updates.apply_update(self.new, newer)
        stop.assert_not_called()

    def test_server_identity_mismatch_never_stops_other_library(self):
        with patch("app.launch.health", return_value={"root": str(self.root / "unrelated"), "pid": 1234}), patch("app.launch.stop_server") as stop:
            with self.assertRaisesRegex(UpdateError, "Another library"):
                updates._stop_exact(self.old, 8768)
        stop.assert_not_called()

    def test_server_missing_control_token_never_signals_pid(self):
        running = {"root": str(self.old), "pid": 1234}
        with patch("app.launch.health", return_value=running), patch("app.launch.read_control", return_value=None), patch("app.launch.stop_server") as stop:
            with self.assertRaisesRegex(UpdateError, "cannot be stopped safely"):
                updates._stop_exact(self.old, 8768)
        stop.assert_not_called()

    def test_still_running_control_pid_prevents_replacement_even_without_health(self):
        (self.old / "data/server-control.json").write_text(json.dumps({"pid": 1234}))
        with patch("app.launch.health", return_value=None), patch("app.import_jobs.process_exists", return_value=True):
            with self.assertRaisesRegex(UpdateError, "still starting or stopping"):
                updates._stop_exact(self.old, 8768)


if __name__ == "__main__":
    unittest.main()
