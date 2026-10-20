"""Assemble empty-library portable releases using an explicit source allowlist."""
from __future__ import annotations
import argparse, hashlib, json, os, shutil, stat, zipfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / 'packaging'
VERSION = '1.6.0'

MAC_LAUNCH = '''#!/bin/sh
# The folder stays portable; all dependencies are included beside the app.
cd -- "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)" || exit 1
unset PYTHONHOME PYTHONPATH
case "$(/usr/bin/uname -m)" in
  arm64) stillroom_arch=macos-arm64 ;;
  x86_64) stillroom_arch=macos-x64 ;;
  *) echo "This package needs an Intel or Apple Silicon Mac."; exit 1 ;;
esac
"./runtime/$stillroom_arch/python/bin/python3" -E -s -B -m app.launch {stop} "$@"
stillroom_result=$?
if [ "$stillroom_result" -ne 0 ]; then
  printf '\\nPress Return to close. '
  read -r stillroom_pause
fi
exit "$stillroom_result"
'''
WINDOWS_LAUNCH = '''@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
set "PYTHONHOME="
set "PYTHONPATH="
"%~dp0runtime\\windows-x64\\python\\python.exe" -E -s -B -m app.launch {stop} %*
if errorlevel 1 (
  echo.
  echo Stillroom could not complete that request. See the message above.
  pause
  exit /b 1
)
exit /b 0
'''

def digest(path):
    with path.open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()

def copy(source,destination):
    if source.is_dir():
        shutil.copytree(source,destination,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','.DS_Store','*.pyc'))
    else:
        destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,destination)

def readme(windows):
    launch='Start Stillroom.cmd' if windows else 'Start Stillroom.command'
    stop='Stop Stillroom.cmd' if windows else 'Stop Stillroom.command'
    update='Update Stillroom.cmd' if windows else 'Update Stillroom.command'
    requirements='Windows 10 (22H2) or Windows 11, 64-bit Intel/AMD.' if windows else 'macOS 11 Big Sur or newer, on Apple Silicon or Intel.'
    extraction='Right-click the ZIP, choose Extract All, then open the extracted folder.' if windows else 'Double-click the ZIP to extract it, then open the Stillroom folder.'
    warning=('If Windows asks before running the downloaded launcher, review the file and confirm only if you trust this package. No administrator account is required.' if windows else 'This personal package is not Apple-notarized. If macOS blocks a launcher or bundled tool, use the system Privacy & Security review flow only if you trust this package. Do not disable Gatekeeper. No administrator installation or Terminal commands are needed for normal use.')
    return f'''STILLROOM {VERSION} — PORTABLE EDITION

START HERE
1. {extraction}
2. Move the entire extracted folder somewhere writable, such as your Desktop.
3. Double-click {launch}.
4. Stillroom opens in your default web browser. Use Add music for audio files,
   a YouTube link, a YouTube playlist, or a public Spotify playlist. The library starts empty.

REQUIREMENTS
{requirements}
A current browser (Safari, Chrome, Edge, or Firefox).
Python, Node.js, FFmpeg, ffprobe, and yt-dlp are included. No separate install.
Internet is needed for YouTube and Spotify playlist imports. Saved music and
file uploads work offline.
{warning}

YOUR LIBRARY
Music is stored in audio/ and the library database in data/ inside this folder.
Those folders are created on first launch. Keep the whole folder together.
Closing the browser leaves Stillroom running quietly. Double-click {stop}
before moving the folder, backing it up, or removing it.
The two computers have separate libraries; there is no automatic syncing.
Keep the original ZIP to send someone an empty copy. Zipping the folder after
adding music will include that music, so use the original ZIP for a clean copy.

UPDATING AN EXISTING COPY
For version 1.5 or newer: open Settings, download the offered update, then choose
Install and restart. Stillroom checks GitHub Releases once a day while open.
You can also select the original release ZIP under Use an update ZIP from a file.

For older versions: extract this ZIP separately, double-click {update}
in the NEW folder, then drag your OLD Stillroom folder into the prompt and
press Return. On Windows, paste the old folder path and press Enter.
The helper updates the OLD folder in place, stops/restarts the app, and leaves
your audio/, data/, imports/, and other personal files there. Keep using the
Start launcher in the OLD folder; its folder name may still show an old version.
The app reports its current version in Settings. After success, the newly
extracted delivery folder can be removed. No music needs to be merged manually.

Updates verify the package before changing the app and keep a rollback copy
until the new version starts successfully. An interrupted or failed update
reports recovery instructions; keep both folders until it has been resolved.
Finish or cancel active imports first. Keep a separate backup of your music.

DELETE A SONG
Open a song's three-dot menu, choose Delete from library, then Delete song.
This removes its saved audio and entries in every playlist. Original files you
uploaded from elsewhere are preserved. Remove from playlist keeps the song.

EDIT SONG INFORMATION
Open a song's three-dot menu and choose Edit info. Change its title, artist,
or album, then Save changes. Your saved audio, loop settings, and playlists stay
the same. You can edit information while the song is playing.

SHARE A SONG
Open a song's three-dot menu and choose Share song. Copy source link copies a
recognized YouTube video address without playlist, tracking, or account details.
Download M4A copy makes one newly encoded audio file; a saved loop contains one
pass through its loop region. Review the displayed title, artist, and album, or
uncheck Include song title, artist, and album for a generic file without those tags.
Original embedded metadata, artwork, chapters, source links, playlists, and
library/account information are stripped. The audio itself may contain personal
material, so choose it deliberately and share only files you have the right to share.
The downloaded file stays on your computer until you send it. A recipient can
play it or add it through Add music, Audio files. Loop repeat settings and playlist
membership are not transferred. Exports are limited to six hours and 512 MB;
one export is prepared at a time, and long encodes may time out.

PLAYBACK AND RECOMMENDATIONS
Click the current speed readout for a continuous 0.5x to 2x slider. Pointer
movement gently snaps near common speeds; arrow keys allow precise adjustments.
Pitch follows speed starts on. Turn it off inside the speed control to preserve
pitch. Speed and pitch preferences also apply to recommendation listening.
Advanced options (the three-dot button on the song bar) contains Added reverb,
which starts off. Desktop Chrome and Firefox support reverb on songs, loops and
recommendations. Safari/WebKit keeps songs and recommendations on native dry
playback with a compatibility notice; prepared loops still support reverb.
Time stretching and reverb can change the texture of music, especially at extremes.
Show/Hide recommendations collapses the section without stopping its audio.
These interface choices are remembered in the same browser and local address.

ADDING AUDIO
Choose Audio files or YouTube link. Auto fills available track information;
Enter myself lets you supply title, creator, and album. Review before saving.
Expand Trim or make a loop for timestamps. Blank times use the whole recording.
Exact keeps your boundaries to the nearest sample. With Exact off, correction
looks within two seconds of your times. Make it loop supports up to 30 minutes.
Listen to the preview and Hear the join: a perfect musical seam is not guaranteed.
YouTube imports support one video, up to 6 hours and 512 MB. Account access is
off by default. To use your private videos or playlists, open Settings, choose
Use a signed-in browser, select the browser with your YouTube account, and
Save account setting. The most recently used browser profile is selected;
use an exported cookie file when you need to choose an account precisely. Then enable Use connected YouTube
account for the individual import. It also applies to the playlist downloads.
You never enter your Google password into Stillroom.

Close the selected browser if access fails. macOS may prompt for Keychain access;
Safari may require Full Disk Access. A Netscape-format YouTube cookie file is
an alternative in Settings. It grants account access: keep it private. Stillroom
stores the imported YouTube/Google cookies locally under data/.youtube-auth;
Disconnect removes the uploaded copy. Account sessions can expire and YouTube
can still refuse downloads. Recommendations always use public access.
See https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies

BRING IN A YOUTUBE PLAYLIST
Open Add music, choose YouTube playlist, paste a playlist link, and
choose Load playlist. A video link containing a playlist also works. Review
the videos, choose a local playlist name, and click Import selected. The exact
videos are imported in order; unavailable/deleted entries are skipped. Review is
limited to the first 100 entries. YouTube can still block individual downloads.
Private playlists need the optional account connection above and per-import
checkbox. Generated mixes are unsupported.

BRING IN A SPOTIFY PLAYLIST
Open Add music, choose Spotify playlist, paste a public playlist link, and
choose Find matches. Spotify supplies the song list; audio comes from YouTube.
Review the YouTube choices, select the tracks you want, give the local playlist
a name, and choose Import selected. You can choose a different suggested video,
paste a YouTube link, or skip a track. Matching the correct recording or version
is best effort, so review slowed, live, remastered, and similar versions closely.
If a matching track is already in your library, explicitly choose that existing
copy to reuse it. Stillroom does not silently substitute it for your selection.

Up to 100 publicly visible entries can be reviewed at once. Spotify may expose
only part of a playlist, and the full playlist count or completeness may be
unknown. Read the coverage notice. Private playlists are not supported.
Imports continue when you close the browser while Stillroom remains running.
Reopen the playlist importer to see progress. Stop import keeps tracks already saved.
Restarting Stillroom ends the current review; load the Spotify link again to
continue. Saved tracks and local playlists remain in your library.

IF IT DOES NOT OPEN
Extract the ZIP first; do not launch the app from inside the archive.
Keep runtime/, app/, and public/ beside the launcher.
If another copy of Stillroom is running, use that copy's Stop launcher first.
The normal address is http://127.0.0.1:8768/ and is accessible only on this computer.
For a startup error, read data/server.log. YouTube changes can require a newer
Stillroom package; ordinary saved playback remains local.

BUNDLED COMPONENTS
See Third-party licenses/ and Component manifest.json for upstream licenses,
source locations and hashes. The app's Python and browser source are included.
No music, accounts, playlists, server tokens, or existing database are included.
'''

def assemble(target,output):
    windows=target=='Windows-x64';platforms=['windows-x64'] if windows else ['macos-arm64','macos-x64']
    folder=f'Stillroom-{VERSION}-{target}'
    stage=PACKAGING/'build'/folder
    if stage.exists(): shutil.rmtree(stage)
    stage.mkdir(parents=True)
    # Never copy the app root wholesale: it contains the private music library.
    for file in sorted((ROOT/'app').glob('*.py')): copy(file,stage/'app'/file.name)
    for file in sorted((ROOT/'public').iterdir()):
        if file.is_file() and file.suffix in ('.html','.js','.css','.svg','.ico'):
            copy(file,stage/'public'/file.name)
    vendors=PACKAGING/'vendor'
    for platform in platforms:
        runtime=stage/'runtime'/platform
        copy(vendors/'runtimes'/platform/'python',runtime/'python')
        copy(vendors/'runtimes'/platform/'bin',runtime/'bin')
        copy(vendors/'ffmpeg'/platform/'bin',runtime/'bin')
        copy(vendors/'runtimes'/platform/'licenses',stage/'Third-party licenses'/platform/'Node')
        copy(vendors/'ffmpeg'/platform/'licenses',stage/'Third-party licenses'/platform/'FFmpeg')
        if not windows: copy(vendors/'runtimes/macos/bin/yt-dlp',runtime/'bin/yt-dlp')
    copy(vendors/'runtimes/licenses',stage/'Third-party licenses/Common')
    for action in ('Start','Stop','Update'):
        extension='cmd' if windows else 'command';path=stage/f'{action} Stillroom.{extension}'
        template=WINDOWS_LAUNCH if windows else MAC_LAUNCH
        if action == 'Update': template = template.replace('app.launch', 'app.updates')
        path.write_text(template.replace('{stop}','--stop' if action=='Stop' else ''),newline='\r\n' if windows else '\n')
        path.chmod(0o755)
    (stage/'Read Me.txt').write_text(readme(windows))
    runtime_manifest=json.loads((PACKAGING/'research/runtime-manifest.json').read_text())
    audio_manifest=json.loads((PACKAGING/'research/audio-manifest.json').read_text())
    manifest={'app':'Stillroom','version':VERSION,'platforms':platforms,'empty_library':True,
              'runtime_components':[item for item in runtime_manifest['components'] if item['platform'] in platforms or (not windows and item['platform']=='macos')],
              'ffmpeg':{p:audio_manifest['platforms'][p] for p in platforms}}
    for platform,component in manifest['ffmpeg'].items():
        component['bin_directory']=f'runtime/{platform}/bin'
        component['licenses_directory']=f'Third-party licenses/{platform}/FFmpeg'
        if 'build_configuration_path' in component: component['build_configuration_path']=f'Third-party licenses/{platform}/FFmpeg/BUILD-CONFIGURATION.txt'
    (stage/'Component manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    # Build paths are diagnostic only and need not expose the developer's folder.
    for path in (stage/'Third-party licenses').rglob('*'):
        if path.is_file() and path.suffix in ('.txt','.json'):
            text=path.read_text(errors='strict')
            if str(ROOT) in text: path.write_text(text.replace(str(ROOT),'<build-root>'))
    all_files=sorted(p for p in stage.rglob('*') if p.is_file())
    (stage/'Checksums.sha256').write_text(''.join(f'{digest(p)}  {p.relative_to(stage).as_posix()}\n' for p in all_files))
    forbidden={'audio','data','imports','tests','skills','.tools','packaging','sources'}
    assert not any(p.name in forbidden for p in stage.iterdir())
    assert not list(stage.rglob('*.sqlite3'))
    assert not any(p.suffix.lower() in ('.mp3','.m4a','.wav','.flac','.ogg','.webm') for p in stage.rglob('*') if p.is_file())
    output.mkdir(parents=True,exist_ok=True)
    archive=output/(folder+'.zip');temp=archive.with_suffix('.zip.part')
    with zipfile.ZipFile(temp,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as z:
        for p in sorted(stage.rglob('*')):
            if p.is_file(): z.write(p,folder+'/'+p.relative_to(stage).as_posix())
    temp.replace(archive)
    print(json.dumps({'zip':str(archive),'bytes':archive.stat().st_size,'sha256':digest(archive),'files':len(all_files)+1}),flush=True)
    return archive

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=Path.home()/'Desktop/Stillroom Releases');args=parser.parse_args()
    archives=list(ThreadPoolExecutor(2).map(lambda target:assemble(target,args.output),['Mac','Windows-x64']))
    # Keep earlier release ZIPs and their checksums available when shipping updates.
    released=sorted(args.output.glob('Stillroom-*.zip'))
    (args.output/'SHA256SUMS.txt').write_text(''.join(f'{digest(p)}  {p.name}\n' for p in released))
