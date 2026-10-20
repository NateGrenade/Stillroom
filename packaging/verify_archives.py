"""Verify checksums, source freshness, and empty-library invariants of release ZIPs."""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path

from build_portable import VERSION


def verify(folder, version=VERSION):
    source = Path(__file__).resolve().parents[1]
    reports = []
    paths = [folder / f'Stillroom-{version}-{target}.zip' for target in ('Mac', 'Windows-x64')]
    assert all(path.is_file() for path in paths), 'Both Mac and Windows release archives must exist.'
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None, path
            names = archive.namelist()
            assert len(names) == len(set(names)), 'Archive contains repeated entries'
            roots = {name.split('/')[0] for name in names}
            assert roots == {path.stem}, roots
            root = path.stem + '/'
            relative = [name[len(root):] for name in names]
            prohibited = {'audio', 'data', 'imports', 'tests', 'skills', '.tools', 'packaging', 'sources'}
            assert not any(name.split('/')[0] in prohibited for name in relative), path
            assert not any(Path(name).suffix.lower() in {'.sqlite3', '.mp3', '.m4a', '.wav', '.flac', '.ogg', '.webm', '.part'} for name in relative), path
            checks = archive.read(root + 'Checksums.sha256').decode().splitlines()
            checked = set()
            for entry in checks:
                expected, name = entry.split('  ', 1)
                assert name not in checked, name
                checked.add(name)
                assert hashlib.sha256(archive.read(root + name)).hexdigest() == expected, name
            assert checked == set(relative) - {'Checksums.sha256'}, path
            for name in relative:
                if name.endswith(('.command', '/bin/python3', '/bin/node', '/bin/ffmpeg', '/bin/ffprobe', '/bin/yt-dlp')):
                    assert archive.getinfo(root + name).external_attr >> 16 & 0o111, name
            manifest = json.loads(archive.read(root + 'Component manifest.json'))
            assert manifest['version'] == version and manifest['empty_library'] is True, manifest
            platforms = ['windows-x64'] if path.stem.endswith('Windows-x64') else ['macos-arm64', 'macos-x64']
            assert manifest['platforms'] == platforms, manifest
            app_files = sorted(source.joinpath('app').glob('*.py'))
            public_files = sorted(p for p in source.joinpath('public').iterdir()
                                  if p.is_file() and p.suffix in ('.html', '.js', '.css', '.svg', '.ico'))
            for group, files in [('app', app_files), ('public', public_files)]:
                packaged = {name for name in relative if name.startswith(group + '/')}
                assert packaged == {group + '/' + file.name for file in files}, group
                for file in files:
                    assert archive.read(root + group + '/' + file.name) == file.read_bytes(), f'Release source differs: {file}'
            assert b'Spotify playlist' in archive.read(root + 'public/index.html')
            assert b'YouTube playlist' in archive.read(root + 'public/index.html')
            assert b'Delete from library' in archive.read(root + 'public/app.js')
            assert b'Edit info' in archive.read(root + 'public/app.js')
            assert root + 'app/youtube_playlists.py' in names
            assert root + 'app/youtube_auth.py' in names
            assert root + 'app/updates.py' in names
            assert root + 'public/settings.js' in names
            extension = 'cmd' if platforms == ['windows-x64'] else 'command'
            assert root + f'Update Stillroom.{extension}' in names
            assert b'PlaylistImportDialog' in archive.read(root + 'public/playlist-import.js')
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        reports.append({'archive': path.name, 'version': version, 'bytes': path.stat().st_size,
                        'sha256': digest, 'files': len(names), 'app_source_files': len(app_files),
                        'public_source_files': len(public_files), 'crc': 'passed', 'checksums': 'passed',
                        'source_match': True, 'spotify_ui_present': True, 'youtube_playlist_ui_present': True, 'delete_song_ui_present': True,
                        'edit_info_ui_present': True, 'private_library_files': 0, 'audio_files': 0})
    return reports


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--folder', type=Path, default=Path.home() / 'Desktop/Stillroom Releases')
    parser.add_argument('--version', default=VERSION)
    parser.add_argument('--report', type=Path, default=Path(__file__).with_name('research') / f'archive-qa-{VERSION}.json')
    args = parser.parse_args()
    reports = verify(args.folder, args.version)
    print(json.dumps(reports, indent=2))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(reports, indent=2) + '\n')
