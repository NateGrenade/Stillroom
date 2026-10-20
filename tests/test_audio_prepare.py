"""Preparation tests use synthesized audio and never touch the saved library."""
from array import array
import math
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.audio_prepare import ANALYSIS_RATE, SAMPLE_RATE, parse_timestamp, prepare_audio
from app.library import LibraryError, executable, probe


def make_wave(path, duration=8, start=0, end=None, stereo=False):
    rate = SAMPLE_RATE
    end = duration if end is None else end
    data = array("h")
    for index in range(round(duration*rate)):
        time = index/rate
        amplitude = .4 if start <= time < end else 0
        data.append(round(amplitude*32767*math.sin(2*math.pi*220*time)))
        if stereo:
            data.append(round(amplitude*32767*math.sin(2*math.pi*330*time)))
    if sys.byteorder != "little":
        data.byteswap()
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2 if stereo else 1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(data.tobytes())


def decode(path):
    raw = subprocess.run([executable("ffmpeg"), "-v", "error", "-i", str(path),
                          "-ar", str(SAMPLE_RATE), "-ac", "2", "-f", "f32le", "pipe:1"],
                         capture_output=True, check=True).stdout
    samples = array("f")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


class TimestampTests(unittest.TestCase):
    def test_supported_formats(self):
        for source, expected in ((None, None), ("  ", None), (1.25, 1.25), ("100", 100),
                                 ("10:00.5", 600.5), ("1:02:03.25", 3723.25), (0, 0)):
            self.assertEqual(parse_timestamp(source), expected)

    def test_invalid_values(self):
        for value in (True, [], {}, -1, float("nan"), float("inf"), "-1", "1:60", "1:60:00", "1::2", "1.2:03", "one", "1e6"):
            with self.subTest(value=value), self.assertRaises(LibraryError):
                parse_timestamp(value)


class AudioPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.wav"
        make_wave(self.source, stereo=True)
        self.output = self.root / "output"

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, **options):
        return prepare_audio(self.source, self.output, options)

    def test_no_timestamps_preserves_entire_original(self):
        original = self.source.read_bytes()
        result = self.prepare()
        self.assertEqual(result["file"], str(self.source.resolve()))
        self.assertEqual(result["duration"], 8)
        self.assertEqual(result["analysis"]["appliedStart"], 0)
        self.assertEqual(result["analysis"]["appliedEnd"], 8)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse(self.output.exists())

    def test_upload_without_audio_extension_gets_playable_suffix(self):
        staged = self.root / "uploaded.audio"
        staged.write_bytes(self.source.read_bytes())
        result = prepare_audio(staged, self.output, {})
        self.assertEqual(Path(result["file"]).suffix, ".wav")
        self.assertEqual(Path(result["file"]).read_bytes(), self.source.read_bytes())

    def test_webm_whole_recording_is_preserved_for_preview(self):
        staged = self.root / "download.webm"
        subprocess.run([executable("ffmpeg"), "-v", "error", "-i", str(self.source),
                        "-c:a", "libopus", str(staged)], check=True)
        result = prepare_audio(staged, self.output, {})
        self.assertEqual(Path(result["file"]).suffix, ".webm")
        self.assertEqual(Path(result["file"]).read_bytes(), staged.read_bytes())
        self.assertEqual(result["storage"]["transformation"], "preserved")
        self.assertAlmostEqual(probe(result["file"])[0], 8, delta=.05)
        self.assertGreater(len(decode(result["file"])), 7.9*SAMPLE_RATE*2)

    def test_compressed_whole_loop_uses_decoded_end_without_padding(self):
        encoded = self.root / "source.mp3"
        subprocess.run([executable("ffmpeg"), "-v", "error", "-i", str(self.source),
                        "-c:a", "libmp3lame", str(encoded)], check=True)
        from app import audio_prepare
        original_probe = audio_prepare.probe
        def padded_probe(path):
            duration, extension = original_probe(path)
            return (duration+.03, extension) if Path(path).resolve() == encoded.resolve() else (duration, extension)
        with patch.object(audio_prepare, "probe", side_effect=padded_probe):
            result = prepare_audio(encoded, self.output, {"exact": True, "loop": True})
        self.assertAlmostEqual(result["duration"], 8, delta=2/SAMPLE_RATE)
        self.assertAlmostEqual(result["analysis"]["appliedEnd"], 8, delta=2/SAMPLE_RATE)
        samples = decode(result["file"])
        metadata = result["metadata"]
        first, last = (round(metadata[key]*SAMPLE_RATE)*2 for key in ("loopStart", "loopEnd"))
        expected = decode(encoded)
        self.assertEqual(last-first, len(expected))
        # FLAC stores the decoded floating-point MP3 at 24-bit precision.
        self.assertLess(max(abs(a-b) for a, b in zip(samples[first:last], expected)), 2**-22)
        with patch.object(audio_prepare, "probe", side_effect=padded_probe), self.assertRaises(LibraryError):
            prepare_audio(encoded, self.output, {"end": probe(encoded)[0]+.03, "exact": True})

    def test_exact_trim_preserves_samples_and_duration(self):
        start, end = 2.123, 6.789
        result = self.prepare(start=start, end=end, exact=True)
        samples = decode(result["file"])
        original = decode(self.source)
        first = round(start*SAMPLE_RATE)*2
        frames = round(end*SAMPLE_RATE)-round(start*SAMPLE_RATE)
        self.assertEqual(len(samples), frames*2)
        self.assertEqual(samples, original[first:first+frames*2])
        self.assertAlmostEqual(result["duration"], frames/SAMPLE_RATE, places=8)
        self.assertEqual(result["analysis"]["boundaryConfidence"], "exact")

    def test_smart_edges_find_nearby_onset_and_close(self):
        make_wave(self.source, duration=9, start=2.35, end=6.45, stereo=True)
        result = self.prepare(start="0:02.7", end="0:06.0")
        analysis = result["analysis"]
        self.assertAlmostEqual(analysis["appliedStart"], 2.34, delta=.03)
        self.assertAlmostEqual(analysis["appliedEnd"], 6.46, delta=.03)
        self.assertEqual(analysis["boundaryConfidence"], "detected")
        exact = self.prepare(start=2.7, end=6.0, exact=True)
        self.assertAlmostEqual(exact["analysis"]["appliedStart"], 2.7)
        self.assertAlmostEqual(exact["analysis"]["appliedEnd"], 6.0)

    def test_smart_edges_never_jump_to_distant_silence(self):
        make_wave(self.source, duration=12, start=.5, end=11.5)
        result = self.prepare(start=4, end=8)
        self.assertEqual(result["analysis"]["appliedStart"], 4)
        self.assertEqual(result["analysis"]["appliedEnd"], 8)
        self.assertEqual(result["analysis"]["boundaryConfidence"], "limited")

    def test_unspecified_start_is_not_trimmed_by_smart_end(self):
        make_wave(self.source, duration=9, start=1, end=6.45)
        result = self.prepare(end=6)
        self.assertEqual(result["analysis"]["appliedStart"], 0)
        self.assertAlmostEqual(result["analysis"]["appliedEnd"], 6.46, delta=.03)

    def test_exact_loop_keeps_full_interval_with_stereo_guards(self):
        result = self.prepare(start=1.2, end=6.3, exact=True, loop=True)
        metadata = result["metadata"]
        samples = decode(result["file"])
        original = decode(self.source)
        first = round(metadata["loopStart"]*SAMPLE_RATE)*2
        last = round(metadata["loopEnd"]*SAMPLE_RATE)*2
        selected = original[round(1.2*SAMPLE_RATE)*2:round(6.3*SAMPLE_RATE)*2]
        self.assertEqual(samples[first:last], selected)
        self.assertEqual(samples[:first], selected[-first:])
        self.assertEqual(samples[last:], selected[:first])
        self.assertEqual(result["analysis"]["crossfade"], 0)
        self.assertAlmostEqual(result["duration"], 5.1)

    def test_automatic_loop_wraps_with_valid_stereo_bounds(self):
        result = self.prepare(start=1, end=7, loop=True)
        metadata = result["metadata"]
        samples = decode(result["file"])
        first = round(metadata["loopStart"]*SAMPLE_RATE)*2
        last = round(metadata["loopEnd"]*SAMPLE_RATE)*2
        self.assertGreater(result["analysis"]["crossfade"], 0)
        self.assertLess(first, last)
        self.assertLess(last, len(samples))
        self.assertEqual(samples[:first], samples[last-first:last])
        self.assertEqual(samples[last:], samples[first:first+len(samples)-last])
        self.assertLess(max(abs(samples[last-2+channel]-samples[first+channel]) for channel in (0, 1)), .04)
        self.assertLess(max(abs(value) for value in samples), .41)
        self.assertIn("Preview several repeats", " ".join(result["analysis"]["notes"]))
        self.assertLessEqual(abs(result["analysis"]["appliedStart"]-1), 2)
        self.assertLessEqual(abs(result["analysis"]["appliedEnd"]-7), 2)

    def test_silent_loop_is_safe_and_reports_low_confidence(self):
        make_wave(self.source, duration=1, start=2)
        result = self.prepare(loop=True)
        self.assertEqual(result["analysis"]["loopConfidence"], "limited")
        self.assertTrue(all(value == 0 for value in decode(result["file"])))

    def test_long_recording_analysis_only_decodes_bounded_windows(self):
        # A sparse one-hour WAV exercises late seeking without allocating PCM.
        long_source = self.root / "long.wav"
        with wave.open(str(long_source), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframesraw(b"\0" * 8000 * 2)
            for _ in range(3599):
                audio.writeframesraw(b"\0" * 8000 * 2)
        from app import audio_prepare
        actual_window = audio_prepare._window
        lengths = []
        def bounded(source, start, end, *args, **kwargs):
            lengths.append(end-start)
            return actual_window(source, start, end, *args, **kwargs)
        with patch.object(audio_prepare, "_window", side_effect=bounded):
            result = prepare_audio(long_source, self.output, {"start": 3500, "end": 3501, "loop": True})
        self.assertTrue(lengths)
        self.assertLess(max(lengths), 4.5)
        self.assertAlmostEqual(result["analysis"]["appliedStart"], 3500)

    def test_invalid_ranges_and_options_leave_no_output(self):
        for options in ({"start": 8}, {"end": 9}, {"start": 5, "end": 4}, {"start": 0, "end": .01},
                        {"start": 0, "end": .1, "loop": True}, {"exact": "true"}, {"loop": 1}):
            with self.subTest(options=options), self.assertRaises(LibraryError):
                self.prepare(**options)
        self.assertFalse(self.output.exists())

    def test_cancellation_stops_running_conversion_and_cleans_staging(self):
        from app import audio_prepare
        cancellation = threading.Event()
        actual_run = audio_prepare._run
        def slow_conversion(command, **kwargs):
            return actual_run([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        timer = threading.Timer(.15, cancellation.set)
        started = time.monotonic()
        timer.start()
        try:
            with patch.object(audio_prepare, "_run", side_effect=slow_conversion):
                with self.assertRaisesRegex(LibraryError, "cancelled"):
                    prepare_audio(self.source, self.output, {"start": 1, "end": 2, "exact": True}, cancel_event=cancellation)
        finally:
            timer.cancel()
        self.assertLess(time.monotonic()-started, 3)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertIsNone(audio_prepare._CANCEL_EVENT.get())

    def test_loop_duration_limit_rejects_before_preparation(self):
        from app import audio_prepare
        with patch.object(audio_prepare, "MAX_LOOP_SECONDS", 5):
            with self.assertRaisesRegex(LibraryError, "30 minutes"):
                self.prepare(loop=True)
        self.assertFalse(self.output.exists())

    def test_file_limit_interrupts_growing_output_and_cleans_up(self):
        from app import audio_prepare
        actual_run = audio_prepare._run
        marker = self.root / "should-not-finish"
        script = ("import pathlib,sys,time; "
                  "output=open(sys.argv[1],'wb'); "
                  "[(output.write(b'x'*65536),output.flush(),time.sleep(.01)) for _ in range(1000)]; "
                  "pathlib.Path(sys.argv[2]).write_text('finished')")
        def growing_conversion(command, **kwargs):
            return actual_run([sys.executable, "-c", script, command[-1], str(marker)], **kwargs)
        started = time.monotonic()
        with patch.object(audio_prepare, "MAX_PREPARED_BYTES", 128*1024), patch.object(audio_prepare, "_run", side_effect=growing_conversion):
            with self.assertRaisesRegex(LibraryError, "shorter selection"):
                self.prepare(start=1, end=2, exact=True)
        self.assertLess(time.monotonic()-started, 3)
        self.assertFalse(marker.exists())
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertIsNone(audio_prepare._WORK_SCOPE.get())

    def test_workspace_limit_counts_source_and_real_generated_audio(self):
        from app import audio_prepare
        # Each individual file is permitted; their aggregate exceeds the cap.
        with patch.object(audio_prepare, "MAX_WORK_BYTES", self.source.stat().st_size+1024):
            with self.assertRaisesRegex(LibraryError, "workspace limit"):
                self.prepare(start=1, end=2, exact=True)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_failed_preparation_removes_partial_output(self):
        from app import audio_prepare
        real_run = audio_prepare._run
        def fail_verify(command, **kwargs):
            if command[-1] == "-":
                raise LibraryError("Test decode failure")
            return real_run(command, **kwargs)
        with patch.object(audio_prepare, "_run", side_effect=fail_verify):
            with self.assertRaises(LibraryError):
                self.prepare(start=1, end=2, exact=True)
        self.assertEqual(list(self.output.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
