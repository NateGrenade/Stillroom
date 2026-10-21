# Portable releases

Release packaging is an explicit step after Nathan tests the local app. Ordinary changes stop at local development; do not automatically build ZIPs, bump versions, or publish. Invoke $stillroom-package to build and verify local packages, or request `$stillroom-package and publish` to also release them on GitHub.

Every packaging run requires a **separate security review** of the app and every final archive before delivery or any upload/publication. This includes local-only packages and drafts. Check for credentials, API keys, cookies/session files, private keys, user music, library/account data, and other material vulnerabilities. Record an independent review result and the final artifact hashes under `research/`; unresolved findings block release. Functional tests, archive checks, and prior security reviews do not replace this check. Follow the maintainer's local Stillroom packaging skill and its security-review procedure.

The builder produces two empty-library archives in `~/Desktop/Stillroom Releases`:

- `Stillroom-1.7.0-Mac.zip`: Apple Silicon and Intel, macOS 11 or newer.
- `Stillroom-1.7.0-Windows-x64.zip`: 64-bit Intel/AMD, Windows 10 22H2 or Windows 11.

Extract the entire folder to a writable location and double-click `Start Stillroom.command` (Mac) or `Start Stillroom.cmd` (Windows). Each package carries Python, Node.js, yt-dlp, and FFmpeg/ffprobe. The source library is never copied. `audio/`, `data/`, and `imports/` are created only after launch. Use the original ZIP when sharing an empty copy.

Version 1.7.0 adds reviewed Mac USB export of all songs, a playlist or custom selections into the compatible Pocket Stillroom Dev app. The byte-preserving bundle carries supported metadata, saved loop boundaries and playlist order, excludes accounts/cookies, and leaves the source unchanged. The companion source is under `PocketStillroom/`; a signed iPhone app is not included. Existing USB trust is required; the private Apple interface may change. Automatic Windows cable transfer is unavailable. Check uncertain transfer receipts before retrying.

Version 1.6.0 adds continuous 0.5×–2× playback speed with an optional pitch-preserving mode, compact animated speed controls, a remembered recommendations disclosure, and added reverb under Advanced options. Desktop Chrome and Firefox support reverb for songs, prepared loops and recommendations; Safari/WebKit preserves native dry playback for songs and recommendations with a compatibility notice, while loop reverb works. Source-aware imports keep compatible compressed originals, remux compatible Opus without re-encoding and use an AAC compatibility fallback when needed. Selected pitch-preserving loops retain one PCM owner; no decoded disk cache is introduced. Existing library audio is not bulk-converted.

Version 1.5.2 adds **⋯ → Share song**. Copy a recognized YouTube source link or download one freshly encoded M4A, with an explicit option to include only the displayed title, artist, and album. Original embedded metadata, artwork, chapters, source links, playlists, and library/account information are stripped from the audio copy. A saved loop exports one pass through its saved region. The downloaded file stays local until you send it, and recipients can import it through **Add music → Audio files**. Only share audio you have the right to share.

Version 1.5.1 adds **⋯ → Edit info** for a song's title, artist, and album. Audio, loop settings, and playlist membership are preserved, and current playback continues. Version 1.5.0 introduced **Settings → App updates**, a migration launcher for older copies, and optional YouTube account access for private playlists and videos. Song deletion and reviewed YouTube playlist imports of up to 100 entries remain available.

For Spotify, choose **Add music → Spotify playlist**. Paste a public Spotify playlist link, review the proposed YouTube recordings, choose the songs to save, and select **Import selected**. Spotify supplies track information; the saved audio comes from the reviewed YouTube sources. Matching the correct recording and version is best effort. You can select another candidate, supply a YouTube link, skip a track, or explicitly reuse a matching copy already in your library.

The review covers up to 100 publicly visible playlist entries. Spotify may expose only part of a playlist; the total count and completeness can be unknown. Private playlists are unsupported. Imports continue after the browser closes while Stillroom keeps running. Stopping an import keeps tracks already saved. Restarting the app expires the review, so reload the playlist link to continue; saved tracks and playlists persist. No Spotify account, source playlist, matching results, or imported songs are pre-added to either ZIP.

## Optional YouTube account access

In **Settings → YouTube account**, choose a signed-in browser and save. The downloader uses the most recently accessed browser profile; open and use the intended account profile before importing. Alternatively, select a Netscape-format YouTube cookie file from your own account. The app never asks for a Google password. Account settings and uploaded cookies remain locally under `data/.youtube-auth/`; use the original release ZIP when sharing an empty app so you do not share those files.

For a single video, enable **Use connected YouTube account for this import**. For a private YouTube playlist your account can access, enable **Use connected YouTube account when loading this playlist**, then load and review the playlist normally. Account use defaults off for each new import. Each playlist review retains its original account method for downloads and retries. **Disconnect** disables future account use and removes the uploaded cookie copy without signing out the browser.

macOS may ask to unlock browser cookies in Keychain, and Safari may require Full Disk Access. Sessions can expire, and YouTube may still reject a download with a bot or access check. Deleted or inaccessible videos stay unavailable. Recommendations remain anonymous, and Spotify playlists remain public-only without account sign-in.

## Update an existing copy

### Version 1.5.0 and newer

While the app page is open, Stillroom checks the public [NateGrenade/Stillroom Releases](https://github.com/NateGrenade/Stillroom/releases) feed once a day. Open **Settings → App updates** for **Check for updates**, **Download update**, and then **Install and restart**. Installing always requires that final click. The page waits for the updated service and refreshes when it reconnects.

If a release ZIP was sent directly to you, use **Settings → Use an update ZIP from a file**, choose the original ZIP, and install after verification finishes. Online downloads verify their release checksum; all packages verify the component manifest, per-file checksums, version, platform, and permitted app files before installation. Source checkouts can view available releases but cannot download, stage, or install an update over their source tree.

### One-time upgrade from 1.4.0 or earlier

1. Extract the new release ZIP into a separate folder.
2. Open **Update Stillroom.command** (Mac) or **Update Stillroom.cmd** (Windows) in that new folder.
3. At the prompt, drag the old Stillroom folder into the Mac terminal and press Return, or paste its folder path on Windows and press Enter.
4. The helper verifies the new package, stops the old app, replaces its app files, and starts the updated copy. Keep using **Start Stillroom** in the old folder; its folder name may still show the earlier version. Settings shows the installed version.
5. Once the update succeeds, you may remove the separate newly extracted delivery folder. Your library stays in the original folder.

Both update routes preserve `audio/`, `data/`, `imports/`, and other personal files, including the library database, recommendation history, and optional YouTube account settings. Browser preferences survive when using the same browser and origin (the same address and port). Finish or cancel active imports before installation; unsaved previews and playlist reviews expire at restart. The in-app installer rejects active imports; the standalone launcher stops the selected app, so finish or cancel imports before using it.

The updater keeps a copy of replaced app files under `data/.updates/` until the new version starts successfully, then removes that rollback copy. A replacement failure restores the journaled files; if the new version fails its startup check, the helper restores the previous version and attempts to start it. Failed updates retain recovery files and report an error. A forced interruption may require recovery using those files; do not delete them or either package folder until resolved. In-app helper diagnostics are recorded in `data/.updates/updater.log`. These app-file rollback measures are not a backup of your music library; keep a separate backup.

## Reproduce

Run from the Music source directory with Python 3.13 or newer:

```sh
python3 packaging/download_runtimes.py
python3 packaging/build_portable.py
```

The downloader uses pinned upstream release metadata under `research/`, verifies publisher hashes where supplied, and records all downloaded hashes in `runtime-manifest.json`. It does not use system Python or Node in the packaged app. Node 22 is deliberately selected for macOS 11 support and yt-dlp's JavaScript runtime requirement.

The FFmpeg vendor tree must also be prepared before building. `vendor/ffmpeg/<platform>/licenses/` contains exact source archives, the Mac build script, Windows upstream build recipes, and validation details. `research/audio-manifest.json` records original download URLs, hashes, versions, and dependency checks. These sources and notices are included in the releases.

`build_portable.py` copies only explicitly allowed app/public source, vendor runtimes, and licenses. It refuses package stages containing a library database, known audio extensions, or private top-level folders. Each archive includes a component manifest, per-file SHA-256 checksums, and user instructions. Earlier versioned ZIPs are retained. Adjacent `SHA256SUMS.txt` verifies all release ZIPs in the output directory.

The launchers use local authenticated shutdown for clean preview cleanup. Each computer has an independent library. The portable package is unsigned and is not an installer; no login item or system-wide runtime is added.

## Publish a future update

Building creates local files only. It does not publish a GitHub Release. The update feed has no GitHub sign-in flow, so the repository and release assets must be publicly downloadable.

1. Set both `app/__init__.py` (`__version__`) and `packaging/build_portable.py` (`VERSION`) to the same new stable version and run the build and verification steps above. Use a version greater than the last published release; downgrades and same-version installs are rejected. Preserve previously published files and assets.
2. Create a stable release in `NateGrenade/Stillroom` with the tag `v<version>` (for example, `v1.5.0`). Do not mark it as a draft or prerelease; the updater reads GitHub's latest stable release endpoint.
3. Attach the generated assets with their exact names: `Stillroom-<version>-Mac.zip`, `Stillroom-<version>-Windows-x64.zip`, and `SHA256SUMS.txt`. For 1.7.0 these are `Stillroom-1.7.0-Mac.zip` and `Stillroom-1.7.0-Windows-x64.zip`. Keep each ZIP's contents and internal checksums unchanged.
4. Upload the builder's adjacent `SHA256SUMS.txt`, which contains the SHA-256 digest and exact filename for each release ZIP. GitHub's asset SHA-256 digest is used when available; this file supplies the fallback. Do not rename an asset without rebuilding the corresponding metadata.
5. Publish the release with both platform ZIPs and checksums attached. From an older disposable packaged copy, choose **Check for updates** and verify download, installation, restart, and library persistence before distributing the update broadly.

Publishing repository source alone does not create an app update. Existing 1.5.0+ copies discover a newer published release at the next daily check or when the user checks manually. An older 1.4.0 copy first needs the one-time launcher upgrade above.

## Verify

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/*.test.mjs
python3 packaging/verify_archives.py
```

`verify_archives.py` requires both current-version ZIPs and verifies archive integrity, every file checksum, executable permissions, the version and platform manifests, the playlist, account-settings, and update UI, and an exact match with current Python and browser sources. It also checks that no library or audio files were included. Its report is `research/archive-qa-1.7.0.json`.

`verify_mac_package.py` exercises a disposable **extracted copy** with `PATH=/usr/bin:/bin`. It checks bundled tools, empty initial state, the Spotify review interface, uploads, exact trimming, loop preparation, byte-range previews, saving, shutdown, and persistence after restart. Its optional `--youtube` flag verifies live public-video acquisition. Never point this script at a library you wish to keep: it intentionally adds synthetic test tracks. Use a separate fresh extraction for each architecture. Live Spotify playlist checks, including the supplied Casual Christmas playlist, must also use a disposable library and must never import into the source collection.

`verify_update_package.py --old <previous-Mac.zip> --new <candidate-Mac.zip> --output <report.json>` creates a disposable library and tests the standalone migration helper. Add `--local-upload` to test the old running app's actual ZIP-upload and install APIs, including package verification, restart, staging cleanup, and preservation of music, playlists, and settings files. Use `--online` only after publication to test the live GitHub download; the two modes are mutually exclusive. Browser preferences also need a browser check at the same address and port.

Earlier releases checked Mac Intel execution through Rosetta on an Apple Silicon Mac. The current host cannot execute the Intel runtime (Bad CPU type); that architecture is included but not newly runtime-validated for 1.7.0. Windows PE architecture, DLL dependencies, launcher paths, and mocked process behavior are checked here; actual Windows execution still requires a Windows host. Current validation reports are under `research/`.
