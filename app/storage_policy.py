"""Storage decisions for newly imported audio; existing library bytes stay intact."""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import time
import uuid

from .library import LibraryError, MAX_AUDIO_BYTES, executable, inspect_audio, local_audio_input_options
from .runtime import stop_process_tree, subprocess_options

LOSSLESS = {"flac", "alac", "ape", "wavpack", "tta"}


def reject_quality_options(options):
    """Fail stale clients explicitly instead of interpreting removed choices."""
    if "storageProfile" in options:
        raise LibraryError("Download quality options are no longer supported. Refresh Stillroom and try again.")


def native_opus(value=True):
    if not isinstance(value, bool):
        raise LibraryError("Native Opus playback support must be true or false.")
    return value


def file_hash(path):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            size += len(chunk)
            if size > MAX_AUDIO_BYTES:
                raise LibraryError("Audio imports are limited to 512 MB.", 413)
            digest.update(chunk)
    return digest.hexdigest()


def describe(path):
    info = inspect_audio(path)
    return {key: info[key] for key in ("codec", "container", "bitrate", "bitrateEstimated", "bytes")} | {"sha256": file_hash(path)}


def provenance(source, stored, transformation, note):
    return {"transformation": transformation, "note": note,
            "source": describe(source), "stored": describe(stored)}


def _run(arguments, *, cancel_event=None):
    if cancel_event is not None and cancel_event.is_set():
        raise LibraryError("Preparation cancelled.")
    try:
        process = subprocess.Popen(arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   stdin=subprocess.DEVNULL, **subprocess_options(new_process_group=True))
    except OSError as exc:
        raise LibraryError("The audio tools could not be started.", 503) from exc
    deadline = time.monotonic() + 600
    try:
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                raise LibraryError("Preparation cancelled.")
            if time.monotonic() > deadline:
                raise LibraryError("Audio conversion took too long. Try a shorter recording.")
            try:
                process.wait(timeout=.1)
            except subprocess.TimeoutExpired:
                pass
        if process.returncode:
            raise LibraryError("The audio could not be converted to a browser-compatible format.")
    finally:
        if process.poll() is None:
            stop_process_tree(process)
            process.wait()


def prepare_original(source, destination_dir, *, allow_native_opus=True, run=None, progress=None, cancel_event=None):
    """Return a browser-playable original and exact source/stored provenance.

    WebM Opus and established compatible formats retain their bytes. Other Opus
    containers use a verified stream copy into WebM. AAC encoding is reserved
    for incompatible formats or browsers that cannot play native Opus.
    """
    allow_native_opus = native_opus(allow_native_opus)
    source = Path(source).resolve()
    info = inspect_audio(source)
    lossless = info["codec"] in LOSSLESS or info["codec"].startswith("pcm_")
    opus_fallback = info["codec"] == "opus" and not allow_native_opus
    remux = info["codec"] == "opus" and info["extension"] != ".webm" and not opus_fallback
    convert = opus_fallback or (info["extension"] is None and not remux)
    extension = ".m4a" if convert else ".webm" if remux else info["extension"]
    encode_bitrate = 256000
    if convert and not lossless:
        encode_bitrate = max(32000, min(256000, info["bitrate"] or 192000))
    if opus_fallback:
        encode_bitrate = max(32000, min(192000, info["bitrate"] or 128000))
    transformation = "transcoded" if convert else "remuxed" if remux else "preserved"
    if opus_fallback:
        note = (f"This browser does not report native Opus playback support. Converted to AAC at up to {encode_bitrate // 1000} kbps "
                "for compatibility; lossy conversion cannot restore source detail.")
    elif convert:
        note = f"Converted to AAC at up to {encode_bitrate // 1000} kbps for browser compatibility; source and saved audio details are recorded separately."
    elif remux:
        note = "Copied the original Opus audio into a WebM container without re-encoding for browser playback."
    else:
        note = "Kept the compatible source file unchanged."
    # The canonical suffix comes exclusively from the byte-inspected stream.
    destination = source
    created = False
    try:
        if convert or remux or source.suffix.lower() != extension:
            directory = Path(destination_dir).resolve()
            directory.mkdir(parents=True, exist_ok=True)
            destination = directory / f"stored-{uuid.uuid4().hex}{extension}"
            created = True
            if convert or remux:
                if progress:
                    progress("Preparing audio for playback")
                arguments = [executable("ffmpeg"), "-v", "error", "-nostdin", "-y"]
                if run is None:
                    arguments += local_audio_input_options(source)
                arguments += ["-i", str(source), "-map", "0:a:0", "-vn", "-map_metadata", "-1", "-map_chapters", "-1"]
                arguments += ["-c:a", "copy"] if remux else ["-c:a", "aac", "-b:a", f"{encode_bitrate // 1000}k", "-movflags", "+faststart"]
                arguments += ["-fs", str(MAX_AUDIO_BYTES + 1), str(destination)]
                if run is not None:
                    run(arguments)
                else:
                    _run(arguments, cancel_event=cancel_event)
            else:
                if source.stat().st_size > MAX_AUDIO_BYTES:
                    raise LibraryError("Audio imports are limited to 512 MB.", 413)
                # Staging copies are bounded even if the source grows mid-copy.
                count = 0
                with source.open("rb") as input_file, destination.open("xb") as output:
                    while chunk := input_file.read(1024 * 1024):
                        if cancel_event is not None and cancel_event.is_set():
                            raise LibraryError("Preparation cancelled.")
                        count += len(chunk)
                        if count > MAX_AUDIO_BYTES:
                            raise LibraryError("Audio imports are limited to 512 MB.", 413)
                        output.write(chunk)
            stored_info = inspect_audio(destination)
            if stored_info["extension"] != extension:
                raise LibraryError("Saved audio compatibility could not be verified.")
            if remux and (stored_info["codec"] != info["codec"] or abs(stored_info["duration"] - info["duration"]) > .05):
                raise LibraryError("The original audio timing could not be preserved during remuxing.")
        storage = provenance(source, destination, transformation, note)
        return {"file": str(destination), "duration": inspect_audio(destination)["duration"], "storage": storage}
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
