# Stillroom

A private music library served on `127.0.0.1:8768`, with disk-backed songs and playlists. No runtime package install or internet access is required for playback. The source checkout requires Python 3.10+, FFmpeg/ffprobe for imports, and a modern browser. Portable Mac and Windows releases include their own runtimes; see `packaging/README.md`.

## Run

```sh
python3 -m app.launch --no-open
python3 -m app.launch --stop
```

The executable `Start Music.command` opens the app for normal use. The service runs independently of the terminal. There is no login item or automatic startup change.

## Library and playback

`app/library.py` owns SQLite transactions and media imports. Use `scripts/import_track.py`, never direct database edits, to add tracks. Originals use streamed browser audio. Prepared loops are decoded and trimmed to their stored `loopStart`/`loopEnd`. At 1×, or with **Pitch follows speed** on, they repeat through a single Web Audio source with sample-accurate boundaries. Keeping pitch at another speed uses a local AudioWorklet that stretches overlapping, waveform-aligned grains continuously through the repeat boundary. Neither mode rewrites saved audio or creates a disk cache. Time stretching can change the texture of music, especially at extreme speeds. Repeat-off plays through the active queue once; repeat-all wraps it; repeat-one repeats the selected track. Shuffle traverses each selected queue entry before repeating.

The compact speed readout opens a continuous **0.5× to 2×** slider. Pointer movement gently snaps near common speeds, while intermediate values and keyboard arrow adjustments remain available. Release a pointer drag to finish, or use Enter, Escape, outside click or focus movement to close the control. Expansion respects reduced-motion preferences. **Pitch follows speed** starts on: slower playback lowers pitch and faster playback raises it. Turn it off inside the expanded control to keep the original pitch while changing speed. These preferences are saved in the browser and also apply to recommendation listening. Import preparation previews stay at their original speed so you can assess trims and loop boundaries.

**Advanced options** at the right of the song bar contains the added-reverb slider. It defaults to zero/off and applies to ordinary songs, prepared loops and recommendation listening. The browser saves the chosen amount locally. Reverb uses a finite room response with smoothly blended dry/wet paths and conservative headroom; it does not rewrite or upload audio. Zero bypasses the effect. Pausing, seeking and changing songs clear old reverb history. A browser unable to initialize an optional effect reports the issue and retains dry playback where available.

Desktop Chrome and Firefox support reverb on all three playback paths. Safari/WebKit keeps ordinary songs and recommendation listening on native, dry playback and displays a compatibility notice when reverb is requested; prepared loops still support reverb. The locally tested Safari 18.1.1 media capture ignored volume, mute and pitch-following controls. Its [reported rate/pitch bug](https://bugs.webkit.org/show_bug.cgi?id=240405) has an upstream fix, but newer Safari volume/mute routing has not been verified here, so the guard remains conservative for WebKit. This avoids taking away working native controls or decoding whole ordinary songs as a workaround. Native dry pitch/volume behavior is preserved; Safari fallback checks verify those properties and advancing playback without recapturing the signal.

The recommendation section has a **Show/Hide recommendations** toggle. Its remembered choice affects the section's display without interrupting audio or changing recommendation history.

A selected loop retains one full decoded PCM copy, either in the native Web Audio buffer or transferred to the pitch-preserving worklet. Pauses, seeks and resumes reuse that worklet. Decoding initially overlaps the full recording and selected region in memory; switching back to native playback can also briefly overlap copies. Browser allocation and garbage collection determine process memory after release. No decoded tracks are cached on disk or preloaded for adjacent songs.

Audio metadata lives in `data/library.sqlite3`; copied audio lives in `audio/`. Static UI files are under `public/`. Browser storage holds interface preferences, resumable playlist review state, and update-check timing. Optional YouTube account settings and uploaded cookies are kept locally under `data/.youtube-auth/`. Source audio is not deleted. Duplicate imports are rejected unless explicitly allowed. The service validates loopback host/origin and serves only static UI and registered audio filenames.

To delete a song, open its **⋯ → Delete from library** and confirm **Delete song**. This removes its saved audio and membership in every playlist, and stops it if it is playing. Original files uploaded from elsewhere are preserved. **Remove from playlist** still only removes that playlist entry. Deleting a song does not count as a negative recommendation rating.

To correct a saved song's title, artist, or album, choose **⋯ → Edit info**, edit the fields, and **Save changes**. The audio, loop settings, source information, and playlist membership are preserved. Editing the currently playing song updates its displayed information without restarting playback.

## Add and prepare tracks

```sh
python3 scripts/import_track.py --help
python3 scripts/prepare_loop.py --help
python3 scripts/import_track.py --list
```

The loop helper requires deliberately chosen musical boundaries. It crossfades the tail with the head and encodes circular guards; import the helper's emitted bounds. The built-in `/diagnostics.html?track=TRACK_ID` page checks browser decoding and a short rendered repeat boundary. Numerical validation does not replace listening to the musical seam.

The discoverable `$music-library` skill is maintained in `skills/music-library`; `~/.codex/skills/music-library` links to it.

## Import from the app

Open **Add music** and choose **Audio files**, **YouTube link**, **YouTube playlist**, or **Spotify playlist**. A single file or video is prepared as a temporary preview, then saved only when you click **Add to library**. Existing multiple-file uploads remain available. Playlists use the separate review and batch import flows below.

Compatible compressed recordings keep their original bytes. Opus in Ogg or Matroska is copied into an audio-only WebM container without re-encoding; native WebM is retained unchanged. Browsers that do not report WebM Opus support receive an AAC compatibility copy instead. Other incompatible whole recordings also use an AAC compatibility copy. The preview shows the source and saved codecs, bitrates, sizes, and any conversion note; a bitrate marked approximately is estimated from the source file. Trims and prepared loops remain lossless FLAC to retain their verified sample boundaries. There is no quality-profile selector or optional compact re-encoding. Existing library songs are untouched. Playlist reviews keep their browser-capability choice for imports and retries.

- **Auto** reads embedded tags or YouTube video details. You can edit the title, creator/artist, and album before saving. When YouTube supplies no artist, the channel is shown with an explanatory note. **Enter myself** uses your own details.
- Expand **Trim or make a loop** to enter seconds, `m:ss`, or `h:mm:ss`, including fractions. Blank start/end mean the beginning/end. With both blank, the original whole recording is retained. Time offsets embedded in a YouTube link are ignored; use the timestamp fields.
- **Exact timestamps** preserves source boundaries to the nearest audio sample. With Exact off, a bounded search within two seconds of each requested edge looks for sustained silence/music transitions. Uncertain boundaries stay in place. The preview reports the applied range.
- **Make it loop** supports selections up to 30 minutes and compares nearby edges by waveform, frequency and level, then uses a short crossfade with circular guards. **Hear the join** plays through the repeat point. Exact loops preserve the whole selection without crossfading. These rules cannot reliably identify a musical phrase in every recording; listen to the preview before saving.
- Automatically named excerpts include their range so separate excerpts can coexist. Existing duplicate protection still applies. Cancel closes the preview and stops active work. Temporary previews expire after an hour without use; restarting the app requires preparing them again.

YouTube acquisition uses yt-dlp, Node.js, and FFmpeg. Portable releases select their bundled tools first; the source checkout uses `.tools/audio/bin/yt-dlp` or installed tools. It accepts one video up to six hours and 512 MB. Imports are anonymous by default; optional YouTube account access is described below. Generated audio is limited to 512 MB, and preparation stops if temporary work exceeds 1.5 GB; use a shorter excerpt when needed. Restricted/unavailable videos show a useful error. Downloader updates may occasionally be needed as YouTube changes. [yt-dlp documentation](https://github.com/yt-dlp/yt-dlp#usage-and-options).

The job API is `POST /api/import-jobs` for YouTube or `POST /api/import-jobs/file` for raw audio uploads, then `GET /api/import-jobs/ID`, `GET /api/import-jobs/ID/audio`, and `POST /api/import-jobs/ID/commit`. Preparation accepts boolean `allowNativeOpus`; browser clients report native WebM Opus capability. Legacy quality-profile requests are rejected before acquisition; refresh an older open app page before importing. `DELETE /api/import-jobs/ID` cancels/discards the preview. Commit retries return the same saved track during the session, without re-encoding it. New tracks retain source and saved storage provenance separately; historical tracks have no invented provenance. Existing provenance and schema fields are preserved without rewriting saved audio.

## Share a song

Open a song’s **⋯ → Share song** menu. A recognized YouTube source offers **Copy source link**: the copied address contains only the video ID, without playlist, tracking, account, or timestamp parameters. It points to the source recording rather than your saved edit or loop. The recipient still needs access to that source; Stillroom does not check its availability or make a private video public. Other source addresses are not shared automatically.

**Download M4A copy** works for both uploaded and YouTube-imported songs. It creates one re-encoded M4A copy; loops contain one pass through the saved loop region. Review the displayed title, artist, and album, or uncheck **Include song title, artist, and album** for a generic filename without those tags. The export strips original embedded metadata, artwork, chapters, source links, playlists, and library/account information. The audio itself may contain personal material, so choose the song deliberately and share only files you have the right to share.

Sharing accepts saved audio up to six hours and 512 MB, limits the exported copy to 512 MB, and prepares one export at a time. Long encodes may time out; a shorter selection can help.

The downloaded file stays on your computer until you send it yourself. There is no upload, public hosting, new account, or paid service. A recipient can play the M4A in an audio player or use **Add music → Audio files** in Stillroom, prepare it, review the details, and save. Playback loop settings and playlist memberships are not transferred; the recipient can select **Make it loop** if desired. Duplicate protection still applies when importing into a library that already has the same title/artist.

## Import a YouTube playlist

Choose **Add music → YouTube playlist**, paste a playlist link (a video link containing `list=` also works), and choose **Load playlist**. Public and unlisted playlists work without account access. For a private playlist your YouTube account can access, configure the optional account method below and check **Use connected YouTube account when loading this playlist** before loading it.

Review the videos, deselect any you do not want, name your local playlist, and choose **Import selected**. Audio comes from those exact videos, in playlist order; no song matching is needed. The review is limited to the first 100 entries and shows coverage and unavailable entries. Deleted videos and videos your chosen account cannot access remain unavailable. Generated mixes are unsupported. YouTube can still block individual downloads with a bot or access check.

The dialog can also switch between YouTube and Spotify. Closing it during import leaves the batch running; **Stop import** keeps songs already saved. Failed songs can be selected again to retry. Restarting Stillroom expires the review; saved songs and playlists remain.

## Optional YouTube account access

Open **Settings → YouTube account**. Choose **Use a signed-in browser**, select the browser signed in to the intended YouTube account, and choose **Save account setting**. The browser choices are limited to those supported on this computer. The downloader uses the most recently accessed browser profile; open and use the intended profile before importing. An exported cookie file gives you a more explicit account choice when you use multiple profiles. macOS may request access to the browser's cookies through Keychain; Safari may require Full Disk Access. If cookie access fails, close the selected browser and retry.

Alternatively, choose **Use an exported cookie file**, select a Netscape-format YouTube cookie file from your own account, and save. Stillroom never asks for or stores a Google password. Cookie files grant access to the account: keep them private and do not share a used app folder containing them. Uploaded cookies and the selected method stay in the local app's `data/.youtube-auth/` directory; they are not sent to the update repository. Account cookies are used only for the YouTube requests you opt into.

Saving the method does not authorize every import. Enable **Use connected YouTube account** on each single-video import or before loading each YouTube playlist. A playlist keeps the account method selected when it was loaded for its later downloads and retries; changing Settings does not silently change that review's account choice. The checkbox resets for another playlist. Replacing or deleting an uploaded cookie file can require reloading a review that used the old file.

**Off** disables account use for new imports. **Disconnect** also removes the uploaded cookie copy, without signing you out of your browser. Sessions can expire; browser permissions, account access, and YouTube bot checks can still prevent an import. Recommendations and Spotify playlist matching/downloads remain anonymous. Spotify account sign-in and private Spotify playlists are not supported.

## App updates

Portable releases from 1.5.0 onward check the public [NateGrenade/Stillroom Releases](https://github.com/NateGrenade/Stillroom/releases) feed once a day while the app page is open. A badge appears when an update is available. Open **Settings → App updates**, choose **Download update**, then **Install and restart**. Checks and downloads do not install anything automatically. **Check for updates** can be used at any time; **Use an update ZIP from a file** supports an original release ZIP shared directly with you.

Updates replace app files in the existing folder and keep `audio/`, `data/`, `imports/`, and other personal files. This preserves songs, playlists, recommendation history, and saved account settings. Browser preferences remain when you use the same browser and address/port. Finish or cancel active imports before installing; unsaved previews and playlist reviews expire when the service restarts.

For a copy older than 1.5.0, extract the new package separately and run **Update Stillroom.command** on Mac or **Update Stillroom.cmd** on Windows from the new folder. Select the old Stillroom folder at its prompt. The helper updates that old folder in place, so no manual library merge is required. Continue using the Start launcher in the old folder, even if its folder name still contains an earlier version. After success, the separately extracted delivery folder can be removed. See [the portable release instructions](../packaging/README.md#update-an-existing-copy) for recovery and release publishing details.

Only verified portable packages can be updated in place. Source checkouts can check the release feed and open the download page, but do not replace themselves. Online updates require a published stable GitHub Release with the matching platform ZIP and a verifiable checksum; creating a build does not publish it.

## Import a Spotify playlist

Choose **Add music → Spotify playlist**, paste a public Spotify playlist link, and select **Find matches**. Stillroom reads the publicly visible song information, then searches YouTube for candidate recordings. Review the choices, select the songs you want, enter a name for the local playlist, and select **Import selected**. Audio is acquired from the YouTube source you choose; Spotify provides the track list and metadata.

You can choose another suggested recording, paste a YouTube link, or skip a row. Recording and version matching is best effort, particularly for slowed, extended, live, and remastered releases. Listen to or inspect the candidate before selecting it. When a matching original already exists in the library, explicitly select that existing copy to reuse it in the new playlist.

The review shows up to 100 publicly visible entries. Spotify can expose only a subset of a playlist; the full count and completeness may be unknown. Read the coverage notice before importing. Private playlists are unsupported, and the app does not log in to Spotify or extract account cookies.

Matching and selected imports continue when the browser closes while the Stillroom service remains running. Reopen **Spotify playlist** to see progress. **Stop import** stops further work and retains tracks already saved. Restarting the app expires the review, so load the playlist link again to continue. Saved tracks and local playlists survive a restart. The shipped portable ZIPs contain an empty library with no pre-added Spotify playlist, reviewed matches, or music.

## Recommendations

On **Your library**, choose **Find recommendations**. **Based on** can use the whole library or a particular song. Finding suggestions stores only metadata. Audio is fetched through the existing public YouTube import integration only when you choose **Listen**; nothing is bought, uploaded, or signed in to.

- **Thumbs up** keeps the song in the library and records positive feedback. **Thumbs down** rejects that recording and reduces the ranking of songs with shared artist, tags, or recording-version features. Only an audio copy created by recommendations can be removed; an already-owned library track is preserved.
- An unrated song goes to **limbo** after its listen, a skip, navigation away, or interrupted playback. Its temporary audio is removed and its metadata and completed-listen count remain. Skips, seeks, incomplete listens, and pauses do not teach the ranking model.
- A completed listen requires a natural ending, no seek or skip, and at least 95% of the recording's duration actually played. Three completed, unrated listens keep the song automatically. Paused/buffering time does not count. Each listen is counted once even if a request is retried.
- Limbo songs become eligible for suggestions after 24 hours; **Listen again** permits an earlier replay and fetches the audio again. An abandoned temporary session expires after an hour without activity. Closing/restarting Stillroom preserves metadata and feedback and cleans up temporary playback files. Kept tracks remain available offline.

Ranking is deterministic for a saved catalog, library, and feedback snapshot: the best library match contributes up to 40 artist, 25 tag, 10 album, 10 title-word, and 15 duration points. Explicit feedback adds/subtracts shared-feature points, bounded to +30/-60 overall, with final scores clamped to 0–100. Ties use normalized recording identity then source URL. The reasons appear beside each suggestion. Original and loop copies of the same recording do not add extra artist weight.

Discovery checks up to three artists with the most distinct library recordings (alphabetical ties), or the selected song's artist, and up to five metadata results per seed. It requires artist attribution in the source title/channel and excludes owned recordings, rejected aliases, compilations, and recordings over 15 minutes. Use a different **Based on** song to explore another part of the collection. These are transparent metadata heuristics, not audio fingerprints or a cross-artist similarity service; missing tags/albums are not invented. YouTube search results can change on a later refresh, while stored results keep their stable ranking until library or feedback changes.

Recommendation metadata, sessions, idempotency records, and exact audio ownership are persisted in SQLite. Downloads remain in the existing app-owned preview directory until kept or discarded. Retention records the new library track and deletion ownership together; cleanup never uses a title or URL as authority to delete an existing file.

The API is `GET /api/recommendations`, `POST /api/recommendations/refresh` with an optional `seedId`, `POST /api/recommendations/ID/listen` with a unique `requestId`, `GET /api/recommendation-sessions/ID`, and `POST /api/recommendation-sessions/ID/events`. Events use a unique `eventId` and `type` (`progress`, `complete`, `skip`, `up`, or `down`). Playback reports include `position`, cumulative `listenedSeconds`, `seeked`, `playbackRate`, and the highest `peakRate` observed during the listen. Retries reuse the same request/event ID and payload.

## Verify

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/*.test.mjs
```

Tests use isolated temporary libraries. `tests/skill-evaluation-result.json` records an independent skill exercise. Live playlist checks must use a disposable library; the supplied Casual Christmas playlist is test input only and must not be imported into the source collection. The public interface can be checked in the browser without changing existing songs.
