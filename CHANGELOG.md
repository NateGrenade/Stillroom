# Changes

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
