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

## Song sharing in 1.5.2

Choose a song’s **⋯ → Share song**. Copy a recognized YouTube source link, or download one fresh M4A copy for an uploaded or imported song. Choose whether to include the displayed title, artist, and album. A saved loop exports one pass through its region. Nothing uploads automatically; send the downloaded file yourself and share only audio you have the right to share. Recipients can use **Add music → Audio files**. Source access, loop settings, and playlists are not transferred.

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
