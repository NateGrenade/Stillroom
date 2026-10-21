# Desktop library transfer prototype

This command creates a music-only transfer file. It does not build an iPhone app,
modify a source library, re-encode audio, or run either desktop app's updater.
The source root must be supplied by the authorized caller. The desktop server binds
its own configured library; browser requests cannot choose a root. The caller must explicitly select its source library. Automated checks use
only disposable fixtures.

```sh
python3 -B Desktop/export_library.py \
  --root '/absolute/path/to/Stillroom' \
  --output '/absolute/path/outside/Music/sample.stillroomlibrary'
```

Choose an existing destination folder and a new filename. Existing files are
never overwritten. If Stillroom reports a busy/changing library, finish editing
or close Dev cleanly and retry. A failed or cancelled export removes its own
incomplete temporary output and keeps any existing destination.

Only registered audio and these whitelisted fields transfer: song IDs, title,
artist, album, original/loop kind, playback/file duration, saved loop bounds,
tags, added date, playlist IDs/names/order, and canonical YouTube video links.
Other source URLs are omitted so account/tracking query parameters cannot leak.
Runtime files, the SQLite database, account settings, cookies, import sessions,
storage provenance and desktop credentials are never copied. Artwork is not a
field in the current desktop library and is not invented by this prototype.

Optional `--settings /path/settings.json` accepts only `playbackRate` (0.5–2),
`pitchChanges` (boolean), and `reverb` (0–1). Omitted values within that object
use 1×, pitch-following on, and reverb off. Without the option, no settings
object is exported. It does not read browser preferences or account files.

## Version 1 format

The file begins with eight ASCII bytes `STLRMB01`, then one unsigned 32-bit
big-endian JSON byte length, UTF-8 JSON, and the original audio streams joined
in `files` order. There is no archive extraction or additional compression;
already compressed recordings retain identical bytes. The JSON looks like:

```json
{
  "format": "stillroom-library",
  "version": 1,
  "tracks": [{
    "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "title": "Example", "artist": "", "album": "", "kind": "original",
    "duration": 1, "fileDuration": 1, "loopStart": 0, "loopEnd": null,
    "tags": [], "audioFile": "audio/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.wav"
  }],
  "playlists": [{
    "id": "cccccccccccccccccccccccccccccccc", "name": "Example",
    "trackIds": ["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]
  }],
  "files": [{
    "path": "audio/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.wav",
    "byteCount": 16044,
    "sha256": "<64 lowercase hex characters>"
  }]
}
```

`sourceUrl` and `addedAt` are optional track fields. A loop's `duration` is its
playable span; `fileDuration` also includes any stored circular guards. Keep
both, and preserve `loopStart`/`loopEnd` in source-file seconds. Transferring
through the existing single-track M4A sharing menu would lose that structure.

Prototype bounds: 8 MiB JSON; 10,000 tracks/playlists; 512 MiB per audio file;
32 GiB total audio; six hours per recording. Supported transfer suffixes are
M4A, MP3, AAC, WAV, FLAC, Opus, Ogg and WebM. The importer must additionally test
actual decoding on the target device before making new tracks visible; a
supported transfer suffix is not a promise of iPhone decoder support.

The exporter checks SQLite integrity/references, metadata, regular audio files,
file signatures, sizes and stored SHA-256 values. It reads one database
snapshot from a private bounded DB/WAL copy, streams bounded chunks, verifies
each file again during copying, and rejects source database/WAL changes before
publishing. SQLite never opens the original database, so stopped WAL-mode
libraries do not gain sidecars. Temporary metadata copies are private and removed;
they are never included in the transfer. Publication uses an
exclusive atomic operation and leaves existing outputs untouched. SHA-256
detects corruption; it does not authenticate who sent a bundle.

Run the disposable-fixture tests from the project root:

```sh
python3 -B -W error::ResourceWarning -m unittest discover -s Tests -p test_transfer_export.py
```


## Selection and desktop API

The standalone stdlib module may also be installed as `app/library_bundle.py`.
Construct it from trusted application configuration:

```python
exporter = LibraryExporter(configured_library_root)
selection = {"mode": "playlist", "playlistId": stable_playlist_id}
review = exporter.preflight(new_output, selection=selection, cancelled=cancel_event.is_set)
# After the user confirms the reviewed counts/size and target:
result = exporter.export(new_output, selection=selection,
                         expected_review_token=review["reviewToken"],
                         cancelled=cancel_event.is_set, progress=on_progress)
```

Selection descriptors accept exactly:

- `{"mode":"all"}`: all tracks in existing chronological order and every complete playlist.
- `{"mode":"track","trackId":"<stable ID>"}`: one original/loop, no playlists.
- `{"mode":"playlist","playlistId":"<stable ID>"}`: that complete playlist, its original ID/name and exact saved membership order; tracks/files appear in that order.
- `{"mode":"tracks","trackIds":["<ID>","<ID>"]}`: unique tracks in the requested order, no invented/partial playlists.

Unknown IDs, empty custom selections/selected playlists, duplicate IDs and extra
fields fail closed. Saved loops transfer whole original bytes plus precise file
and playback-span boundaries. No loop is trimmed or re-encoded. Unselected audio
is not opened. Existing global SQLite integrity/reference guards still apply.

Preflight creates no output and returns `tracks`, `playlists`, `files`,
`audioBytes`, `bundleBytes`, `requiredBytes`, `availableBytes`, `selection`,
`output`, and a 64-lowercase-hex `reviewToken`. The optional export token rejects
changed source metadata/selection/settings after review. Audio is revalidated
independently even without a token. The destination must have bundle size plus a
16 MiB safety margin. A private metadata copy is separately bounded at 128 MiB DB
and 256 MiB WAL; an oversized/changing database fails safely. Source SHM files,
accounts, runtime and other source files are never copied.

Callbacks run synchronously on the caller's worker thread. Progress contains only
`phase`, `completedBytes`, `totalBytes`, `completedFiles`, `totalFiles`; byte
progress starts again per phase (`preparing`, `copying`, `verifying`, `publishing`).
`ExportCancelled` is a subtype of `ExportError`. No external callbacks or
cancellation checks run after exclusive output publication; a late cancellation
must not misreport a committed file as absent. The caller reports completion
from the returned `published: true`, rather than expecting a final callback.

CLI remains compatible. Optional `--track ID`, `--playlist ID`, or `--tracks ID ID`
are mutually exclusive; omit them to export all. `--preflight` verifies without
saving. `--expected-review-token TOKEN` guards a subsequent save.

Run all fixture checks:

```sh
python3 -B -W error::ResourceWarning -m unittest discover -s Tests -p 'test_*.py'
```

Verified: 52 tests pass (30 existing exporter checks, 22 selection/snapshot checks).
These module checks use disposable fixtures. Native codec decodability and USB
import are separate integration checks; the current prototype has also passed
an initial three-song Mac-to-iPhone cable delivery/import smoke test.
