"""Bounded, deterministic snippet correction and guarded loop preparation.

Only short windows around the requested edges are decoded into memory. Musical
phrase selection is inherently uncertain; the result describes what changed and
never equates waveform similarity with a verified musical seam.
"""
from __future__ import annotations

from array import array
from contextvars import ContextVar
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

from .library import LibraryError, executable, local_audio_input_options, probe
from .runtime import stop_process_tree, subprocess_options
from .storage_policy import prepare_original, provenance, reject_quality_options, native_opus

SAMPLE_RATE = 44100
ANALYSIS_RATE = 8000
SEARCH_SECONDS = 2.0
MAX_PREPARED_BYTES = 512 * 1024 * 1024
MAX_WORK_BYTES = 1536 * 1024 * 1024
MAX_LOOP_SECONDS = 30 * 60
_WORK_SCOPE = ContextVar("audio_prepare_work_scope", default=None)
_CANCEL_EVENT = ContextVar("audio_prepare_cancel", default=None)


def parse_time(value, field="Timestamp"):
    """Accept seconds, MM:SS, or HH:MM:SS; return None for an omitted edge."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise LibraryError(f"{field} must be seconds, MM:SS, or HH:MM:SS.")
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        value = value.strip()
        if not re.fullmatch(r"\d+(?:\.\d+)?|\d+:\d{1,2}(?:\.\d+)?|\d+:\d{1,2}:\d{1,2}(?:\.\d+)?", value):
            raise LibraryError(f"{field} must be seconds, MM:SS, or HH:MM:SS.")
        parts = [float(part) for part in value.split(":")]
        if len(parts) > 1 and any(part >= 60 for part in parts[1:]):
            raise LibraryError(f"The minutes/seconds after a colon in {field.lower()} must be below 60.")
        result = sum(part * 60 ** index for index, part in enumerate(reversed(parts)))
    else:
        raise LibraryError(f"{field} must be seconds, MM:SS, or HH:MM:SS.")
    if not math.isfinite(result) or result < 0:
        raise LibraryError(f"{field} must be a finite, nonnegative time.")
    return result


# Public alias used by the job manager before downloading a recording.
parse_timestamp = parse_time


def _check_work_limits(additional_bytes=0):
    scope = _WORK_SCOPE.get()
    if scope is None:
        return
    root, source = scope
    total = 0
    source_counted = False
    if root.exists():
        for path in root.rglob("*"):
            try:
                if not path.is_file():
                    continue
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            total += size
            if path == source:
                source_counted = True
            elif size > MAX_PREPARED_BYTES:
                raise LibraryError("The prepared audio exceeded the 512 MB file limit. Choose a shorter selection.", 413)
    if not source_counted:
        try:
            total += source.stat().st_size
        except FileNotFoundError:
            pass
    if total+additional_bytes > MAX_WORK_BYTES:
        raise LibraryError("Audio preparation exceeded its 1.5 GB workspace limit. Choose a shorter selection.", 413)


def _run(arguments, *, binary=False, timeout=900):
    cancel_event = _CANCEL_EVENT.get()
    if cancel_event is not None and cancel_event.is_set():
        raise LibraryError("Preparation cancelled.")
    _check_work_limits()
    # Apply restrictions per input, including the multi-input loop filters.
    # Preparation only reads local staged media; generated filter graphs never
    # need a playlist demuxer or an input that opens other resources.
    guarded_arguments = []
    for index, argument in enumerate(arguments):
        if argument == "-i" and index + 1 < len(arguments):
            guarded_arguments.extend(local_audio_input_options(arguments[index + 1]))
        guarded_arguments.append(argument)
    try:
        process = subprocess.Popen(guarded_arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   stdin=subprocess.DEVNULL, text=not binary,
                                   encoding="utf-8" if not binary else None,
                                   errors="replace" if not binary else None,
                                   **subprocess_options(new_process_group=True))
    except OSError as exc:
        raise LibraryError("The audio tools could not be started. Check that ffmpeg is installed.", 503) from exc
    deadline = time.monotonic()+timeout
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise LibraryError("Preparation cancelled.")
            _check_work_limits()
            if time.monotonic() > deadline:
                raise LibraryError("Audio preparation took too long. Try a shorter selection.")
            try:
                output, _ = process.communicate(timeout=.2)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            raise LibraryError("The audio could not be decoded or prepared. Try another recording or a shorter selection.")
        _check_work_limits()
        return output
    finally:
        if process.poll() is None:
            stop_process_tree(process)
            process.communicate()


def _window(source, start, end, rate=ANALYSIS_RATE, channels=1):
    """Decode a small, explicitly bounded region, never an entire long video."""
    if end <= start:
        return array("f")
    raw = _run([executable("ffmpeg"), "-v", "error", "-nostdin", "-ss", f"{start:.9f}",
                "-i", str(source), "-t", f"{end-start:.9f}", "-map", "0:a:0", "-vn",
                "-ar", str(rate), "-ac", str(channels), "-f", "f32le", "pipe:1"], binary=True, timeout=90)
    samples = array("f")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def _snap_boundary(source, requested, duration, is_start, lower, upper):
    """Move only to a nearby sustained silence/music transition of the right type."""
    begin, finish = max(0, lower - .15), min(duration, upper + .15)
    samples = _window(source, begin, finish)
    block = ANALYSIS_RATE // 100  # 10 ms, with 80 ms of silence required.
    rms = [math.sqrt(sum(v*v for v in samples[i:i+block]) / block)
           for i in range(0, len(samples)-block+1, block)]
    if not rms or max(rms) < 1e-5:
        return requested, False
    threshold = max(1e-4, max(rms) * .025)
    active = [level > threshold for level in rms]
    candidates = []
    for index in range(8, len(active)-8):
        if is_start:
            matches = not any(active[index-8:index]) and all(active[index:index+3])
            candidate = begin + index / 100 - .01
        else:
            matches = all(active[index-3:index]) and not any(active[index:index+8])
            candidate = begin + index / 100 + .01
        if matches and lower <= candidate <= upper:
            candidates.append(candidate)
    if not candidates:
        return requested, False
    return min(candidates, key=lambda candidate: (abs(candidate-requested), candidate)), True


def _descriptor(values):
    if not values:
        return None
    energy = sum(value*value for value in values)
    if energy / len(values) < 1e-10:
        return None
    # A small frequency fingerprint plus time envelope supports candidate choice.
    bands = []
    for frequency in (110, 220, 440, 880, 1760, 3000):
        coefficient = 2 * math.cos(2 * math.pi * frequency / ANALYSIS_RATE)
        previous = previous2 = 0.0
        for value in values:
            current = value + coefficient * previous - previous2
            previous2, previous = previous, current
        bands.append(math.sqrt(max(0, previous*previous + previous2*previous2 - coefficient*previous*previous2)))
    norm = math.sqrt(sum(value*value for value in bands)) or 1
    return {"samples": values, "energy": energy, "rms": math.sqrt(energy/len(values)),
            "bands": [value/norm for value in bands]}


def _similarity(head, tail):
    if head is None or tail is None:
        return 0.0
    correlation = sum(a*b for a, b in zip(head["samples"], tail["samples"])) / math.sqrt(head["energy"]*tail["energy"])
    spectrum = sum(a*b for a, b in zip(head["bands"], tail["bands"]))
    level = min(head["rms"], tail["rms"]) / max(head["rms"], tail["rms"])
    return .45 * max(0, correlation) + .35 * max(0, spectrum) + .2 * level


def _loop_edges(source, start, end, start_limits, end_limits, crossfade):
    """Search at most 2 seconds per side, preferring the original selection."""
    count = round(crossfade * ANALYSIS_RATE)
    left_origin = start_limits[0]
    right_origin = max(0, end_limits[0] - crossfade)
    left = _window(source, left_origin, start_limits[1] + crossfade)
    right = _window(source, right_origin, end_limits[1])

    def candidates(bounds, original, values, origin, tail):
        times = {original}
        # At most 34 candidates per edge; no whole-track beat or PCM allocation.
        steps = max(1, math.ceil((bounds[1]-bounds[0]) / .125))
        times.update(bounds[0] + (bounds[1]-bounds[0])*index/steps for index in range(steps+1))
        result = []
        for stamp in sorted(times):
            offset = round((stamp-origin-(crossfade if tail else 0))*ANALYSIS_RATE)
            region = values[offset:offset+count] if offset >= 0 else []
            if len(region) == count:
                result.append((stamp, _descriptor(region)))
        return result

    heads = candidates(start_limits, start, left, left_origin, False)
    tails = candidates(end_limits, end, right, right_origin, True)
    baseline = 0.0
    best_score, best_similarity, best_edges = -1.0, 0.0, (start, end)
    for left_time, head in heads:
        for right_time, tail in tails:
            if right_time-left_time < max(.25, 4*crossfade):
                continue
            similarity = _similarity(head, tail)
            if left_time == start and right_time == end:
                baseline = similarity
            movement = abs(left_time-start) + abs(right_time-end)
            score = similarity - .14 * movement / SEARCH_SECONDS
            if score > best_score:
                best_score, best_similarity, best_edges = score, similarity, (left_time, right_time)
    # Do not change the phrase for a negligible numerical improvement.
    if best_score < baseline + .07:
        return start, end, baseline
    return *best_edges, best_similarity


def _trim(source, destination, start, end, allow_padded_end=False):
    # Seek near the region, then count samples. This avoids decoding hours before
    # a late snippet while keeping the boundary independent of compressed frames.
    anchor = max(0, math.floor(start)-1)
    first = round((start-anchor)*SAMPLE_RATE)
    length = round((end-start)*SAMPLE_RATE)
    graph = (f"aresample={SAMPLE_RATE},atrim=start_sample={first}:end_sample={first+length},"
             "asetpts=PTS-STARTPTS")
    _run([executable("ffmpeg"), "-v", "error", "-nostdin", "-ss", str(anchor), "-i", str(source),
          "-map", "0:a:0", "-vn", "-af", graph, "-ac", "2", "-c:a", "flac", "-sample_fmt", "s32", str(destination)])
    actual, _ = probe(destination)
    if abs(actual-length/SAMPLE_RATE) > 2/SAMPLE_RATE:
        # MP3/WebM format duration may include codec padding absent from the
        # decoded audio. An omitted end means the actual final audio sample.
        if allow_padded_end and 0 < length/SAMPLE_RATE-actual <= 1 and actual >= .05:
            return round(actual*SAMPLE_RATE)
        raise LibraryError("The source ended before the selected timestamps. Choose an earlier end time.")
    return length


def _guarded_loop(selected, destination, frames, crossfade_frames, temporary):
    cycle = selected
    cycle_frames = frames-crossfade_frames
    if crossfade_frames:
        head, tail, blend = (temporary / name for name in ("head.flac", "tail.flac", "blend.flac"))
        _trim(selected, head, 0, crossfade_frames/SAMPLE_RATE)
        _trim(selected, tail, (frames-crossfade_frames)/SAMPLE_RATE, frames/SAMPLE_RATE)
        _run([executable("ffmpeg"), "-v", "error", "-nostdin", "-i", str(tail), "-i", str(head),
              "-filter_complex", f"[0:a][1:a]acrossfade=ns={crossfade_frames}:c1=tri:c2=tri[out]",
              "-map", "[out]", "-c:a", "flac", "-sample_fmt", "s32", str(blend)])
        cycle = temporary / "cycle.flac"
        graph = (f"[0:a]atrim=start_sample={crossfade_frames}:end_sample={frames-crossfade_frames},"
                 "asetpts=PTS-STARTPTS[mid];[mid][1:a]concat=n=2:v=0:a=1[out]")
        _run([executable("ffmpeg"), "-v", "error", "-nostdin", "-i", str(selected), "-i", str(blend),
              "-filter_complex", graph, "-map", "[out]", "-c:a", "flac", "-sample_fmt", "s32", str(cycle)])
    guard_frames = min(SAMPLE_RATE, cycle_frames//4)
    # Extract tiny guards separately. A split/concat graph that asks for the
    # source tail first can buffer the entire middle of a long recording.
    guard_tail, guard_head = temporary / "guard-tail.flac", temporary / "guard-head.flac"
    _trim(cycle, guard_tail, (cycle_frames-guard_frames)/SAMPLE_RATE, cycle_frames/SAMPLE_RATE)
    _trim(cycle, guard_head, 0, guard_frames/SAMPLE_RATE)
    _run([executable("ffmpeg"), "-v", "error", "-nostdin", "-i", str(guard_tail),
          "-i", str(cycle), "-i", str(guard_head), "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1[out]",
          "-map", "[out]", "-c:a", "flac", "-sample_fmt", "s32", str(destination)])
    return guard_frames/SAMPLE_RATE, (guard_frames+cycle_frames)/SAMPLE_RATE


def _prepare_audio(source: Path, destination_dir: Path, options: dict, progress=None):
    """Prepare a whole track, corrected excerpt, or best-effort loop for import.

    ``progress``, when supplied, receives a status string. All generated files
    live under destination_dir; callers retain ownership of source/staging files.
    """
    if not isinstance(options, dict):
        raise LibraryError("Audio preparation options must be an object.")
    reject_quality_options(options)
    allow_native_opus = native_opus(options.get("allowNativeOpus", True))
    exact, looping = options.get("exact", False), options.get("loop", False)
    if not isinstance(exact, bool) or not isinstance(looping, bool):
        raise LibraryError("Exact timestamps and loop must be true or false.")
    requested_start = parse_time(options.get("start"), "Start time")
    requested_end = parse_time(options.get("end"), "End time")
    source = Path(source).expanduser().resolve()
    if not source.is_file():
        raise LibraryError("Choose an existing audio file.")
    if progress:
        progress("Inspecting audio")
    source_duration, _ = probe(source)
    start = 0.0 if requested_start is None else requested_start
    end = source_duration if requested_end is None else requested_end
    if start >= source_duration or end > source_duration + 1/SAMPLE_RATE or end <= start:
        raise LibraryError("Choose timestamps with 0 ≤ start < end ≤ the video duration.")
    end = min(end, source_duration)
    if end-start < .05:
        raise LibraryError("Choose a selection at least 0.05 seconds long.")
    if looping and end-start < .25:
        raise LibraryError("Choose at least a quarter second of audio to prepare a loop.")
    if looping and end-start > MAX_LOOP_SECONDS:
        raise LibraryError("Choose a loop selection of 30 minutes or less, or turn off loop preparation.")
    notes = []
    analysis = {"sourceDuration": source_duration, "requestedStart": requested_start, "requestedEnd": requested_end,
                "exact": exact, "loop": looping, "crossfade": 0.0, "notes": notes,
                "searchRadius": 0.0 if exact else SEARCH_SECONDS}
    if requested_start is None and requested_end is None and not looping:
        analysis.update(appliedStart=0.0, appliedEnd=source_duration, boundaryConfidence="unchanged")
        stored = prepare_original(source, destination_dir, allow_native_opus=allow_native_opus, run=_run, progress=progress,
                                  cancel_event=_CANCEL_EVENT.get())
        _check_work_limits()
        notes.append(stored["storage"]["note"])
        return {**stored, "metadata": {"kind": "original", "loopStart": 0, "loopEnd": None}, "analysis": analysis}

    start_limits = (max(0, start-SEARCH_SECONDS), min(end-.25, start+SEARCH_SECONDS))
    end_limits = (max(start+.25, end-SEARCH_SECONDS), min(source_duration, end+SEARCH_SECONDS))
    if not exact:
        if progress:
            progress("Looking for nearby musical boundaries")
        adjusted = 0
        if requested_start is not None or looping:
            start, found = _snap_boundary(source, start, source_duration, True, *start_limits)
            adjusted += int(found)
        if requested_end is not None or looping:
            end, found = _snap_boundary(source, end, source_duration, False, *end_limits)
            adjusted += int(found)
        if end-start < max(.05, .25 if looping else 0):
            start = requested_start or 0.0
            end = source_duration if requested_end is None else requested_end
            adjusted = 0
        analysis["boundaryConfidence"] = "detected" if adjusted else "limited"
        if adjusted:
            notes.append("Nearby sustained silence/music transitions were used to refine the selection.")
        else:
            notes.append("No clear nearby silence/music transition was found." if looping else
                         "No clear nearby silence/music transition was found; the requested selection was retained.")
    else:
        analysis["boundaryConfidence"] = "exact"
        notes.append("Exact mode preserves the requested source boundaries to the nearest audio sample.")

    crossfade = 0.0
    if looping:
        if exact:
            analysis["loopConfidence"] = "exact"
            notes.append("Exact mode applies no crossfade or boundary correction; an audible repeat seam may remain.")
        else:
            crossfade = min(.08, (end-start)/12)
            if progress:
                progress("Comparing loop boundaries")
            before_loop_edges = (start, end)
            start, end, similarity = _loop_edges(source, start, end, start_limits, end_limits, crossfade)
            if (start, end) != before_loop_edges:
                notes.append("The source boundaries were adjusted within the search window to improve the loop match.")
            analysis["boundarySimilarity"] = round(similarity, 4)
            analysis["loopConfidence"] = "matched" if similarity >= .8 else "limited"
            notes.append("Nearby edges were compared using waveform, frequency, and level similarity; a short crossfade smooths the join.")
        notes.append("Preview several repeats: automatic preparation cannot guarantee a seamless musical phrase.")

    if looping and end-start > MAX_LOOP_SECONDS:
        raise LibraryError("The adjusted loop exceeds 30 minutes. Choose a shorter selection or use exact timestamps.")
    start, end = round(start*SAMPLE_RATE)/SAMPLE_RATE, round(end*SAMPLE_RATE)/SAMPLE_RATE
    destination_dir = Path(destination_dir).expanduser().resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"prepared-{uuid.uuid4().hex}.flac"
    try:
        with tempfile.TemporaryDirectory(prefix="prepare-", dir=destination_dir) as temp:
            temporary = Path(temp)
            selected = temporary / "selected.flac" if looping else destination
            if progress:
                progress("Preparing audio")
            frames = _trim(source, selected, start, end,
                           allow_padded_end=requested_end is None or (not exact and end >= source_duration-1/SAMPLE_RATE))
            decoded_end = start+frames/SAMPLE_RATE
            if end-decoded_end > 2/SAMPLE_RATE:
                end = decoded_end
                notes.append("The actual final audio sample was used, excluding compressed-file padding.")
            if looping:
                crossfade_frames = round(crossfade*SAMPLE_RATE)
                loop_start, loop_end = _guarded_loop(selected, destination, frames, crossfade_frames, temporary)
                metadata = {"kind": "loop", "loopStart": loop_start, "loopEnd": loop_end}
                duration = loop_end-loop_start
                expected = duration+2*loop_start
                analysis["crossfade"] = crossfade_frames/SAMPLE_RATE
            else:
                metadata = {"kind": "original", "loopStart": 0, "loopEnd": None}
                duration = frames/SAMPLE_RATE
                expected = duration
            actual, _ = probe(destination)
            if abs(actual-expected) > 2/SAMPLE_RATE:
                raise LibraryError("Prepared audio timing could not be verified. Try exact timestamps.")
            if progress:
                progress("Verifying prepared audio")
            _run([executable("ffmpeg"), "-v", "error", "-nostdin", "-i", str(destination), "-map", "0:a:0", "-f", "null", "-"])
            analysis.update(appliedStart=start, appliedEnd=end, sampleRate=SAMPLE_RATE, fileDuration=actual)
        note = "Saved the prepared selection as lossless FLAC to preserve its verified sample boundaries."
        notes.append(note)
        storage = provenance(source, destination, "edited_lossless", note)
        return {"file": str(destination), "metadata": metadata, "analysis": analysis, "duration": duration, "storage": storage}
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def prepare_audio(source: Path, destination_dir: Path, options: dict, progress=None, cancel_event=None):
    """Prepare audio, optionally stopping the worker's own subprocesses on cancel."""
    token = _CANCEL_EVENT.set(cancel_event)
    work_token = _WORK_SCOPE.set((Path(destination_dir).expanduser().resolve(), Path(source).expanduser().resolve()))
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise LibraryError("Preparation cancelled.")
        return _prepare_audio(source, destination_dir, options, progress)
    finally:
        _WORK_SCOPE.reset(work_token)
        _CANCEL_EVENT.reset(token)
