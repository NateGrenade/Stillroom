# Stillroom

A local music library for Mac and Windows, with song playback, prepared loops, playlists, and YouTube imports.

## Download

Download the Mac or Windows ZIP from [Releases](https://github.com/NateGrenade/Stillroom/releases), extract the entire folder, then open **Start Stillroom**. Each release starts with an empty library. The app and required tools are included.

## Updates

Starting with version 1.5, Stillroom checks this repository's public Releases once a day while open. Open **Settings**, download the update, then choose **Install and restart**. Songs, playlists, account settings, and other personal files stay in the existing app folder.

For an older copy, extract the new release separately and open its **Update Stillroom** launcher. Choose the existing Stillroom folder when prompted. After a successful update, keep using the Start launcher in that existing folder.

Updates are per computer; libraries are not synced between people or computers.

## Private YouTube playlists

In **Settings → YouTube account**, optionally select a browser signed in to YouTube, or use an exported Netscape-format cookie file. Enable account access separately when loading a YouTube playlist or preparing a video. Stillroom does not ask for a Google password. Sessions are local and may expire; YouTube can still reject downloads. Spotify playlist imports use publicly visible lists only.

See the included **Read Me.txt** for details, limits, and troubleshooting. Release ZIPs contain the app source and bundled component licenses, but never a user's songs, library, or account cookies.

## Playback and imports in 1.6.0

Click the current speed readout for a continuous **0.5×–2×** slider with gentle preset snapping. **Pitch follows speed** starts on; turn it off to preserve the original pitch. Advanced options contains added reverb, off by default. Collapse recommendations to keep the page compact without stopping their audio. These choices are remembered in your browser.

Desktop Chrome and Firefox support reverb for songs, loops and recommendations. Safari/WebKit keeps ordinary songs and recommendations on native dry playback with a compatibility notice; prepared loops still support reverb. Time stretching can alter musical texture at extreme speeds.

Future imports retain compatible compressed source audio, remux compatible Opus without re-encoding, and use an AAC compatibility copy when required. Existing library audio is not bulk-converted. Pitch-preserving loops retain one PCM owner and reuse it through pauses and seeks, with no decoded disk cache.

## Song sharing in 1.5.2

Choose a song’s **⋯ → Share song**. Copy a recognized YouTube source link, or download one fresh M4A copy for an uploaded or imported song. Choose whether to include the displayed title, artist, and album. A saved loop exports one pass through its region. Nothing uploads automatically; send the downloaded file yourself and share only audio you have the right to share. Recipients can use **Add music → Audio files**. Source access, loop settings, and playlists are not transferred.

## Export music to Pocket Stillroom

The native iOS companion source and build instructions are in [PocketStillroom](PocketStillroom/README.md). These desktop releases do not include a signed iPhone app; signing and device setup are separate.

The Mac desktop app can prepare a `.stillroomlibrary` bundle and copy it by USB into the intended Pocket Stillroom app's **Documents** folder. The bundle carries original compressed audio without re-encoding, song labels, saved loop boundaries and playlist ordering. Source audio and the library database are read-only during export; accounts, cookies and runtime files are excluded. Copying a bundle does not import its songs automatically.

On the Mac:

1. Connect the iPhone by USB and unlock it. Review any **Trust This Computer** prompt personally; the exporter uses existing trust and never creates pairing or signing credentials.
2. Open **Export to iPhone**. Under **Export scope**, choose **All songs in this library**, **One playlist, in its saved order**, or **Choose individual songs**.
3. Choose **Review selection**, check the count and bundle size, and select the exact **iPhone and installed app**. Use **Refresh devices** if needed.
4. Choose **Copy to iPhone** and keep Stillroom running and the cable connected until the result is confirmed. Closing the dialog leaves a started transfer running; **Cancel transfer** requests cancellation and waits for confirmation.

On the iPhone, in the cable-enabled Pocket Stillroom build:

1. Tap **+** (**Import library**).
2. Under **On this iPhone**, choose the copied bundle. Use the refresh button if it has not appeared.
3. Review the library and choose **Import into offline library**. Check the resulting songs and playlist before relying on offline playback.

The desktop checks room for the copied bundle **plus 32 MiB**. Importing then needs additional space for managed offline audio; Pocket Stillroom checks that separately. A successful copy therefore does not prove the import will fit. Existing conflicting IDs are refused rather than overwritten.

If the copy result or cancellation is unclear, use **Check transfer** before **New export** or any retry. A copy may already exist. Local `data/.pocket-exports.json` stores transfer receipts/status for this check, not account credentials; keep it private and do not delete it to force a retry. Interrupted hidden staging copies may remain when cleanup cannot be confirmed.

This Mac path uses the installed private MobileDevice framework and needs no Xcode at runtime. That private interface can change; a controlled unsupported/error result is possible after an OS change. **Direct cable export on Windows is currently unsupported.** A separate manual fallback is [Apple Devices → Files → Pocket Stillroom → Add File → Add](https://support.apple.com/en-us/120402); it is not this automatic feature. Mac users can also use [Finder file sharing](https://support.apple.com/en-us/119585). The iPhone's **Browse Files** alternative remains available for Files/iCloud Drive transfers.

## Source and development

The repository contains the app, browser interface, tests, and packaging scripts. Personal libraries, credentials, generated media, vendor binaries, and private review reports are excluded.

Run with Python 3.10 or newer:

```sh
python3 -m app.launch --no-open
python3 -m app.launch --stop
```

Open `http://127.0.0.1:8768`. Imports and sharing need FFmpeg/ffprobe; YouTube imports also need yt-dlp and Node.js. Portable releases bundle these tools.

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/*.test.mjs
```

See [usage](docs/USAGE.md), [packaging](packaging/README.md), and [changes](CHANGELOG.md). Stillroom is intended for a trusted local computer; loopback networking is not isolation from malicious local OS accounts or processes.
