# Changes

## 1.7.0

- Export the whole library, one playlist in saved order, or a custom song selection to a versioned `.stillroomlibrary` bundle containing original audio bytes, supported metadata and saved loop boundaries. Source libraries remain unchanged; account cookies and credentials are excluded.
- Add reviewed, cancellable Mac USB transfer into the compatible Pocket Stillroom Dev app, with device selection, space checks, full readback verification and persistent receipts for uncertain outcomes. Uses existing trust and Apple's private MobileDevice interfaces; automatic Windows transfer is unavailable.
- Publish the native iOS companion source under `PocketStillroom/`: offline library import, playlists, queue, saved loops, speed/pitch, reverb, background controls, local backup/recovery and setup help. Its + import sheet discovers complete cable-delivered bundles in Documents for review.

Pocket requires separate user signing and device setup; this release contains no signed iPhone IPA. Mac Intel, native Windows and Safari have not been newly validated for this cable-export release. The existing Safari reverb limitation in 1.6.0 remains.

## 1.6.0

- Add continuous 0.5×–2× playback speed, gentle preset snapping, and a pitch-follow toggle that defaults on; preferences apply to recommendation listening too.
- Keep speed controls compact until opened, and add remembered Show/Hide recommendations without interrupting active listening.
- Add smoothly mixed, finite reverb under Advanced options, off by default. Desktop Chrome/Firefox support all playback paths; Safari/WebKit preserves native dry songs/recommendations and supports loop reverb.
- Preserve compatible compressed source audio for future imports, use lossless Opus remux when suitable, and retain a browser-compatible AAC fallback. Edited trims and loops retain verified lossless FLAC timing.
- Reduce retained pitch-preserving loop PCM to one owner and reuse the worklet across pause/seek/resume. No decoded disk cache or bulk conversion of existing songs is introduced.
- Preserve seeking, queue/repeat behavior, recommendation previews and once-only natural-listen counting across playback changes.


## 1.5.2

- Share a recognized YouTube source link without playlist, tracking, or timestamp parameters.
- Download one freshly encoded M4A from an uploaded or imported song. Include only the displayed title, artist, and album, or choose an untagged copy with a generic filename.
- Export a saved loop as one pass and import received audio through the existing Audio files flow.
- Restrict local-media decoding to supported self-contained formats and bounded work. Reject media manifests and external media references.
- Require matching local control credentials before Stop; an unauthenticated health response cannot authorize an OS process signal.

No automatic uploads or hosting are added. Updates preserve the existing local library.
