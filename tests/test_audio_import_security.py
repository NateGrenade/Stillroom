"""Untrusted imports use disposable media only; no network or personal library."""

from __future__ import annotations

import math
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import audio_prepare, import_sources, library
from app.library import Library, LibraryError, executable, local_audio_input_options, probe


class AudioImportSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "tone.wav"
        with wave.open(str(self.source), "wb") as output:
            output.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            output.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 220 * n / 8000)))
                                        for n in range(2400)))
        self.library = Library(self.root / "library")

    def tearDown(self):
        self.temporary.cleanup()

    def concat(self):
        playlist = self.root / "disguised.wav"
        playlist.write_text("ffconcat version 1.0\nfile 'tone.wav'\nduration 0.3\n", encoding="utf-8")
        return playlist

    def assert_empty_library(self):
        self.assertEqual(self.library.library()["tracks"], [])
        self.assertEqual(list(self.library.audio.iterdir()), [])

    def test_real_bytes_determine_type_despite_untrusted_filename(self):
        disguised = self.root / "uploaded.html"
        disguised.write_bytes(self.source.read_bytes())
        track = self.library.import_file(disguised, filename="A song.mp3")
        self.assertTrue(track["audioUrl"].endswith(".wav"))
        self.assertEqual((self.library.root / track["audioUrl"].lstrip("/")).read_bytes(), self.source.read_bytes())

    def test_scripts_html_images_and_malformed_audio_are_rejected(self):
        payloads = (
            b"#!/bin/sh\nprintf executable\n",
            b"<!doctype html><html><script>alert('audio')</script></html>",
            b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
            b"\x89PNG\r\n\x1a\nnot audio",
            b"RIFF\x20\x00\x00\x00WAVEtruncated",
            b"ID3\x04\x00\x00\x00\x00\x00\x00<html>not an MP3</html>",
            b"",
        )
        for index, payload in enumerate(payloads):
            with self.subTest(index=index):
                source = self.root / f"payload-{index}.mp3"
                source.write_bytes(payload)
                with self.assertRaises(LibraryError):
                    self.library.import_file(source)
                self.assert_empty_library()

    def test_disguised_concat_cannot_probe_or_import_a_referenced_local_file(self):
        playlist = self.concat()
        with self.assertRaises(LibraryError):
            probe(playlist)
        # Keep the staged playlist beside the synthetic referenced recording.
        # Without the demuxer restriction this opens tone.wav and imports it.
        with self.assertRaises(LibraryError):
            self.library.import_file(playlist, _staging_dir=self.root)
        self.assert_empty_library()
        self.assertEqual(list(self.root.glob(".import-*")), [])

    def test_hls_and_disguised_hls_cannot_open_local_segments(self):
        segment = self.root / "tone.aac"
        subprocess.run([executable("ffmpeg"), "-v", "error", "-nostdin", "-i", str(self.source),
                        "-c:a", "aac", str(segment)], check=True, capture_output=True)
        content = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n"
                   "#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:0.3,\ntone.aac\n#EXT-X-ENDLIST\n")
        for name in ("playlist.m3u8", "disguised.mp3"):
            with self.subTest(name=name):
                playlist = self.root / name
                playlist.write_text(content, encoding="utf-8")
                with self.assertRaises(LibraryError):
                    probe(playlist)
                with self.assertRaises(LibraryError):
                    self.library.import_file(playlist)
                self.assert_empty_library()

    def test_optional_metadata_probe_cannot_follow_a_concat_reference(self):
        inspected = []
        run_tool = import_sources._run_tool
        def record_probe(*args, **kwargs):
            result = run_tool(*args, **kwargs)
            inspected.append(result)
            return result
        with patch.object(import_sources, "_run_tool", side_effect=record_probe):
            result = import_sources.local_metadata(self.concat(), filename="Uploaded song.mp3")
        self.assertEqual(result, {"title": "Uploaded song", "artist": "", "album": "", "sourceUrl": ""})
        self.assertEqual(len(inspected), 1)
        self.assertNotEqual(inspected[0].returncode, 0)

    def test_decoder_rejects_playlist_even_without_a_prior_probe(self):
        with self.assertRaises(LibraryError):
            audio_prepare._window(self.concat(), 0, .1)

    def test_each_input_of_a_multi_input_preparation_has_its_own_restrictions(self):
        popen = subprocess.Popen
        with patch.object(audio_prepare.subprocess, "Popen", wraps=popen) as start:
            audio_prepare._run([
                executable("ffmpeg"), "-v", "error", "-nostdin", "-i", str(self.source),
                "-i", str(self.source), "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[out]",
                "-map", "[out]", "-f", "null", "-",
            ])
        arguments = start.call_args.args[0]
        input_indices = [index for index, value in enumerate(arguments) if value == "-i"]
        self.assertEqual(len(input_indices), 2)
        previous = 0
        for index in input_indices:
            options = arguments[previous:index]
            self.assertEqual(options[options.index("-protocol_whitelist") + 1], "file")
            formats = options[options.index("-format_whitelist") + 1].split(",")
            self.assertTrue({"wav", "mp3", "mov", "ogg", "matroska"}.issubset(formats))
            self.assertFalse({"hls", "dash", "concat", "sdp", "image2"}.intersection(formats))
            previous = index + 2

    def test_common_real_audio_containers_still_import_without_extension_trust(self):
        cases = (
            ("mp3", "libmp3lame", ".mp3"), ("flac", "flac", ".flac"),
            ("aac", "aac", ".aac"), ("m4a", "aac", ".m4a"),
            ("m4a", "alac", ".m4a"), ("ogg", "libopus", ".m4a"),
            ("webm", "libopus", ".m4a"), ("aiff", "pcm_s16be", ".m4a"),
        )
        for extension, codec, stored_extension in cases:
            with self.subTest(extension=extension, codec=codec):
                encoded = self.root / f"encoded-{codec}.{extension}"
                subprocess.run([executable("ffmpeg"), "-v", "error", "-nostdin", "-i", str(self.source),
                                "-c:a", codec, str(encoded)], check=True, capture_output=True)
                uploaded = self.root / "uploaded.bin"
                uploaded.write_bytes(encoded.read_bytes())
                track = self.library.import_file(uploaded, {"title": codec}, allow_duplicate=True)
                self.assertTrue(track["audioUrl"].endswith(stored_extension))
                self.assertGreater(track["fileDuration"], .1)
                if extension not in ("ogg", "webm", "aiff"):
                    stored = self.library.root / track["audioUrl"].lstrip("/")
                    self.assertEqual(stored.read_bytes(), encoded.read_bytes())

    def test_mov_private_options_follow_bytes_and_work_during_preparation(self):
        encoded = self.root / "encoded.m4a"
        subprocess.run([executable("ffmpeg"), "-v", "error", "-nostdin", "-i", str(self.source),
                        "-c:a", "aac", str(encoded)], check=True, capture_output=True)
        disguised = self.root / "uploaded.bin"
        disguised.write_bytes(encoded.read_bytes())
        options = local_audio_input_options(disguised)
        self.assertEqual(options[options.index("-enable_drefs") + 1], "0")
        self.assertEqual(options[options.index("-use_absolute_path") + 1], "0")
        self.assertNotIn("-enable_drefs", local_audio_input_options(self.source))
        result = audio_prepare.prepare_audio(disguised, self.root / "prepared", {"start": .05, "end": .2, "exact": True})
        self.assertAlmostEqual(probe(result["file"])[0], .15, delta=.001)

    def test_oversize_source_is_rejected_before_probe_import_or_preparation(self):
        with patch.object(library, "MAX_AUDIO_BYTES", self.source.stat().st_size - 1):
            with patch.object(library.subprocess, "run") as run:
                for action in (
                    lambda: probe(self.source),
                    lambda: self.library.import_file(self.source),
                    lambda: audio_prepare.prepare_audio(self.source, self.root / "prepared", {}),
                ):
                    with self.assertRaises(LibraryError) as caught:
                        action()
                    self.assertEqual(caught.exception.status, 413)
                run.assert_not_called()
        self.assert_empty_library()

    def test_file_growing_after_initial_size_check_is_bounded_and_cleaned_up(self):
        limit = self.source.stat().st_size + 1
        mkstemp = tempfile.mkstemp
        def grow_then_stage(*args, **kwargs):
            with self.source.open("ab") as source:
                source.write(b"x" * 100)
            return mkstemp(*args, **kwargs)
        with patch.object(library, "MAX_AUDIO_BYTES", limit), patch.object(library.tempfile, "mkstemp", side_effect=grow_then_stage):
            with self.assertRaises(LibraryError) as caught:
                self.library.import_file(self.source)
        self.assertEqual(caught.exception.status, 413)
        self.assert_empty_library()

    def test_conversion_that_exceeds_file_limit_is_rejected_and_cleaned_up(self):
        compressed = self.root / "small.ogg"
        subprocess.run([executable("ffmpeg"), "-v", "error", "-nostdin", "-i", str(self.source),
                        "-c:a", "libopus", "-b:a", "6k", str(compressed)], check=True, capture_output=True)
        original = compressed.read_bytes()
        with patch.object(library, "MAX_AUDIO_BYTES", len(original) + 1):
            with self.assertRaises(LibraryError) as caught:
                self.library.import_file(compressed)
        self.assertEqual(caught.exception.status, 413)
        self.assertEqual(compressed.read_bytes(), original)
        self.assert_empty_library()


if __name__ == "__main__":
    unittest.main()
