"""Portable tool discovery and scoped process cleanup, without saved audio."""

import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import audio_prepare, import_sources, library, runtime


class RuntimeResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def tool(self, path, executable=True):
        path = self.root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test executable")
        path.chmod(0o755 if executable else 0o644)
        return str(path)

    def test_platform_matches_interpreter_architecture(self):
        for system, arch, expected in (
            ("darwin", "arm64", "macos-arm64"),
            ("darwin", "x86_64", "macos-x64"),
            ("win32", "AMD64", "windows-x64"),
            ("win32", "ARM64", None),
            ("linux", "x86_64", None),
        ):
            with self.subTest(system=system, arch=arch), patch.object(runtime.sys, "platform", system), patch.object(runtime.platform, "machine", return_value=arch):
                self.assertEqual(runtime.platform_id(), expected)

    def test_current_mac_bundle_wins_over_installed_tools(self):
        arm_tool = self.tool("runtime/macos-arm64/bin/ffmpeg")
        self.tool("runtime/macos-x64/bin/ffmpeg")
        with patch.object(runtime.sys, "platform", "darwin"), patch.object(runtime.platform, "machine", return_value="arm64"), patch.object(runtime.shutil, "which", return_value="/other/ffmpeg") as which:
            self.assertEqual(runtime.find_executable("ffmpeg", root=self.root), arm_tool)
        which.assert_not_called()

    def test_rosetta_uses_x64_bundle(self):
        self.tool("runtime/macos-arm64/bin/node")
        x64_tool = self.tool("runtime/macos-x64/bin/node")
        with patch.object(runtime.sys, "platform", "darwin"), patch.object(runtime.platform, "machine", return_value="x86_64"):
            self.assertEqual(runtime.find_executable("node", root=self.root), x64_tool)

    def test_never_falls_back_to_a_different_platform_bundle(self):
        self.tool("runtime/macos-arm64/bin/ffmpeg")
        self.tool("runtime/macos-x64/bin/ffmpeg.exe")
        with patch.object(runtime.sys, "platform", "win32"), patch.object(runtime.platform, "machine", return_value="AMD64"), patch.object(runtime.shutil, "which", return_value=None):
            self.assertIsNone(runtime.find_executable("ffmpeg", root=self.root))

    def test_windows_executable_suffix_does_not_require_posix_permission(self):
        expected = self.tool("runtime/windows-x64/bin/ffprobe.exe", executable=False)
        with patch.object(runtime.sys, "platform", "win32"), patch.object(runtime.platform, "machine", return_value="AMD64"):
            self.assertEqual(runtime.find_executable("ffprobe", root=self.root), expected)
            self.assertEqual(runtime.find_executable("ffprobe.exe", root=self.root), expected)

    def test_bundle_precedes_source_downloader_and_path(self):
        expected = self.tool("runtime/macos-arm64/bin/yt-dlp")
        self.tool(".tools/audio/bin/yt-dlp")
        with patch.object(runtime.sys, "platform", "darwin"), patch.object(runtime.platform, "machine", return_value="arm64"), patch.object(runtime.shutil, "which", return_value="/other/yt-dlp"):
            self.assertEqual(runtime.find_executable("yt-dlp", root=self.root), expected)

    def test_existing_source_downloader_is_retained(self):
        expected = self.tool(".tools/audio/bin/yt-dlp")
        with patch.object(runtime.sys, "platform", "darwin"), patch.object(runtime.shutil, "which", return_value="/other/yt-dlp"):
            self.assertEqual(runtime.find_executable("yt-dlp", root=self.root), expected)

    def test_source_mode_keeps_path_tools(self):
        with patch.object(runtime.sys, "platform", "linux"), patch.object(runtime.shutil, "which", return_value="/tools/ffmpeg"):
            self.assertEqual(runtime.find_executable("ffmpeg", root=self.root), "/tools/ffmpeg")

    @unittest.skipIf(sys.platform == "win32", "POSIX executable permission check")
    def test_nonexecutable_bundle_is_not_launched(self):
        self.tool("runtime/macos-arm64/bin/ffmpeg", executable=False)
        with patch.object(runtime.sys, "platform", "darwin"), patch.object(runtime.platform, "machine", return_value="arm64"), patch.object(runtime.shutil, "which", return_value="/installed/ffmpeg"):
            self.assertEqual(runtime.find_executable("ffmpeg", root=self.root), "/installed/ffmpeg")


class PortableProcessTests(unittest.TestCase):
    def test_windows_options_hide_windows_and_isolate_cancellable_tools(self):
        with patch.object(runtime.sys, "platform", "win32"):
            plain = runtime.subprocess_options()
            grouped = runtime.subprocess_options(new_process_group=True)
        self.assertEqual(plain, {"creationflags": 0x08000000})
        self.assertEqual(grouped, {"creationflags": 0x08000200})
        self.assertNotIn("start_new_session", grouped)

    def test_posix_options_use_an_owned_session(self):
        with patch.object(runtime.sys, "platform", "darwin"):
            self.assertEqual(runtime.subprocess_options(), {})
            self.assertEqual(runtime.subprocess_options(new_process_group=True), {"start_new_session": True})

    def test_windows_stops_only_owned_pid_and_descendants(self):
        process = Mock(pid=12345)
        process.poll.side_effect = [None, 0]
        with patch.object(runtime.sys, "platform", "win32"), patch.object(runtime.subprocess, "run") as run:
            runtime.stop_process_tree(process)
        command = run.call_args.args[0]
        self.assertEqual(command[1:], ["/PID", "12345", "/T", "/F"])
        self.assertTrue(command[0].endswith("taskkill.exe"))
        self.assertNotIn("/IM", command)
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        process.kill.assert_not_called()
        process.wait.assert_called_once_with()

    def test_failed_windows_taskkill_still_reaps_direct_child(self):
        process = Mock(pid=12345)
        process.poll.return_value = None
        with patch.object(runtime.sys, "platform", "win32"), patch.object(runtime.subprocess, "run", side_effect=OSError("missing")):
            runtime.stop_process_tree(process)
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with()

    @unittest.skipIf(not hasattr(os, "killpg"), "POSIX group termination")
    def test_posix_stops_only_owned_process_group(self):
        process = Mock(pid=12345)
        process.poll.return_value = None
        with patch.object(runtime.sys, "platform", "darwin"), patch.object(runtime.os, "killpg") as kill:
            runtime.stop_process_tree(process)
        kill.assert_called_once_with(12345, signal.SIGKILL)
        process.wait.assert_called_once_with()

    def test_exited_process_is_reaped_without_termination(self):
        process = Mock(pid=12345)
        process.poll.return_value = 0
        with patch.object(runtime.sys, "platform", "win32"), patch.object(runtime.subprocess, "run") as run:
            runtime.stop_process_tree(process)
        run.assert_not_called()
        process.kill.assert_not_called()
        process.wait.assert_called_once_with()

    def test_youtube_runner_passes_portable_creation_options(self):
        process = Mock()
        process.poll.return_value = 0
        with patch.object(runtime.sys, "platform", "win32"), patch.object(import_sources.subprocess, "Popen", return_value=process) as start:
            result = import_sources._run_tool(["downloader.exe"], timeout=1)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(start.call_args.kwargs["creationflags"], 0x08000200)
        self.assertNotIn("start_new_session", start.call_args.kwargs)

    def test_audio_runner_passes_portable_creation_options(self):
        process = Mock(returncode=0)
        process.poll.return_value = 0
        process.communicate.return_value = (b"audio", b"")
        with patch.object(runtime.sys, "platform", "win32"), patch.object(audio_prepare.subprocess, "Popen", return_value=process) as start:
            self.assertEqual(audio_prepare._run(["ffmpeg.exe"], binary=True), b"audio")
        self.assertEqual(start.call_args.kwargs["creationflags"], 0x08000200)
        self.assertNotIn("start_new_session", start.call_args.kwargs)
        self.assertIsNone(start.call_args.kwargs["encoding"])

    def test_tool_text_uses_utf8_for_international_file_names(self):
        # FFmpeg diagnostics and JSON use UTF-8 even on a Windows ANSI locale.
        process = Mock(returncode=0)
        process.poll.return_value = 0
        process.communicate.return_value = ("音楽", "")
        with patch.object(audio_prepare.subprocess, "Popen", return_value=process) as start:
            self.assertEqual(audio_prepare._run(["ffmpeg"]), "音楽")
        self.assertEqual(start.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(start.call_args.kwargs["errors"], "replace")

    def test_library_probe_hides_tool_window(self):
        info = {"streams": [{"codec_type": "audio", "codec_name": "mp3"}], "format": {"duration": "1", "format_name": "mp3"}}
        result = Mock(returncode=0, stdout=json.dumps(info))
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "example.mp3"
            source.write_bytes(b"synthetic fixture; decoding is mocked")
            with patch.object(runtime.sys, "platform", "win32"), patch.object(library, "executable", return_value="ffprobe.exe"), patch.object(library.subprocess, "run", return_value=result) as run:
                self.assertEqual(library.probe(source), (1, ".mp3"))
        self.assertEqual(run.call_args.kwargs["creationflags"], 0x08000000)
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")


if __name__ == "__main__":
    unittest.main()
