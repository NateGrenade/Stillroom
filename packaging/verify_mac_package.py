"""Exercise a separately extracted portable Mac release without installed tools."""
from __future__ import annotations
import argparse, base64, hashlib, io, json, math, os, socket, struct, subprocess, time, wave
from pathlib import Path
from urllib.request import Request, urlopen


def run(root, architecture, youtube=False):
    manifest_path = root / 'Component manifest.json'
    assert manifest_path.is_file(), 'Use a freshly extracted portable package, never the source library.'
    manifest = json.loads(manifest_path.read_text())
    assert manifest.get('app') == 'Stillroom' and manifest.get('empty_library') is True, manifest
    expected_version = manifest['version']
    env = dict(os.environ, PATH='/usr/bin:/bin', PYTHONHOME='', PYTHONPATH='')
    python = root / 'runtime' / architecture / 'python/bin/python3'
    prefix = ['/usr/bin/arch', '-x86_64'] if architecture == 'macos-x64' else []
    py = prefix + [str(python), '-E', '-s', '-B']
    def command(args):
        result = subprocess.run(args, cwd=root, env=env, text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout.strip()
    resolved = json.loads(command(py + ['-c', 'import json; from app.runtime import platform_id,find_executable; print(json.dumps({"platform":platform_id(),"tools":{n:find_executable(n) for n in ("node","yt-dlp","ffmpeg","ffprobe")}}))']))
    assert resolved['platform'] == architecture, resolved
    assert all(str(root / 'runtime' / architecture) in p for p in resolved['tools'].values()), resolved
    versions = {name: command([path, '--version' if name in ('node', 'yt-dlp') else '-version']).splitlines()[0] for name,path in resolved['tools'].items()}
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port = sock.getsockname()[1]
    endpoint = f'http://127.0.0.1:{port}'
    start = [str(root/'Start Stillroom.command')] if architecture == 'macos-arm64' else py+['-m','app.launch']
    stop = [str(root/'Stop Stillroom.command')] if architecture == 'macos-arm64' else py+['-m','app.launch','--stop']
    def request(path, body=None, headers=None, method=None, raw=False):
        if isinstance(body,dict):
            body=json.dumps(body).encode();headers={'Content-Type':'application/json',**(headers or {})}
        with urlopen(Request(endpoint+path,data=body,headers=headers or {},method=method),timeout=30) as r:
            content=r.read();return (r.status,content) if raw else json.loads(content)
    def ready(job):
        deadline=time.monotonic()+240
        last=None
        while time.monotonic()<deadline:
            data=request('/api/import-jobs/'+job)
            if data.get('job'): data=data['job']
            if data['stage'] != last:
                print(architecture, data['stage'], data.get('message'), flush=True);last=data['stage']
            if data['status']=='ready': return data
            assert data['status'] not in ('error','cancelled'), data
            time.sleep(.2)
        raise AssertionError('Preparation timed out')
    saved=[]; report={'version':expected_version,'architecture':architecture,'tool_versions':versions,'tests':[]}
    try:
        print(command(start+['--no-open','--port',str(port)]),flush=True)
        health=request('/api/health'); assert health['root']==str(root) and health['version']==expected_version, health
        initial=request('/api/library');assert initial['tracks']==[] and initial['playlists']==[], initial
        report['tests']+=['launch with spaces and Unicode in path','all tools resolved inside package','first launch has zero tracks and playlists']
        _,html=request('/',raw=True); assert b'Stillroom' in html
        assert b'Spotify playlist' in html and b'spotify-import-dialog' in html
        assert b'YouTube playlist' in html and b'import-youtube-playlist-open' in html
        status,playlist_ui=request('/playlist-import.js',raw=True)
        assert status==200 and b'PlaylistImportDialog' in playlist_ui
        report['tests'].append('Spotify playlist review interface and script are served')
        report['tests'].append('YouTube playlist review entry point is served')
        data=io.BytesIO()
        with wave.open(data,'wb') as f:
            f.setnchannels(1);f.setsampwidth(2);f.setframerate(44100)
            f.writeframes(b''.join(struct.pack('<h',int(9000*math.sin(2*math.pi*220*i/44100))) if 22050<i<330750 else b'\0\0' for i in range(352800)))
        for name,options in [('exact',{'start':'0:01.125','end':'0:03.625','exact':True}),('loop',{'start':'0:00.6','end':'0:07.4','exact':False,'loop':True})]:
            options.update(metadataMode='manual', metadata={'title':'Portable QA '+name,'artist':'Synthetic test'})
            job=request('/api/import-jobs/file',data.getvalue(),{'Content-Type':'audio/wav','X-Filename':'Synthetic.wav','X-Track-Metadata':base64.b64encode(json.dumps(options).encode()).decode()})['job']
            prepared=ready(job['id'])
            if name=='exact':
                assert abs(prepared['duration']-2.5)<.001,prepared
                assert abs(prepared['analysis']['appliedStart']-1.125)<1/44100,prepared
            else: assert prepared['metadata']['kind']=='loop',prepared
            status,chunk=request(prepared['audioUrl'],headers={'Range':'bytes=0-63'},raw=True);assert status==206 and len(chunk)==64
            result=request('/api/import-jobs/'+job['id']+'/commit',{'metadata':{'title':'Portable QA '+name,'artist':'Synthetic test'}})
            saved.append(result['track']['id']);report['tests'].append(name+' preparation, preview byte ranges, and save')
        original = request('/api/library')['tracks']
        edited_original = next(track for track in original if track['id'] == saved[0])
        audio_before = hashlib.sha256((root / edited_original['audioUrl'].lstrip('/')).read_bytes()).hexdigest()
        correction = {'title': 'Edited <song> & title', 'artist': 'New artist — café', 'album': 'Corrected album'}
        edited = request('/api/tracks/'+saved[0], correction, method='PATCH')['track']
        assert all(edited[key] == value for key, value in correction.items()), edited
        assert all(edited[key] == value for key, value in edited_original.items() if key not in correction), edited
        assert hashlib.sha256((root / edited['audioUrl'].lstrip('/')).read_bytes()).hexdigest() == audio_before
        report['tests'].append('Edit info updates only title/artist/album and preserves audio bytes')
        if youtube:
            job=request('/api/import-jobs',{'url':'https://www.youtube.com/watch?v=e9r5hx47kxM','start':'0:10','end':'0:15','exact':True})['job']
            prepared=ready(job['id']);assert abs(prepared['duration']-5)<.001,prepared
            assert request(prepared['audioUrl'],headers={'Range':'bytes=0-63'},raw=True)[0]==206
            request('/api/import-jobs/'+job['id'],method='DELETE')
            report['tests'].append('real public YouTube acquisition, exact trim, preview, discard')
        print(command(stop+['--port',str(port)]),flush=True)
        assert not (root/'data/server-control.json').exists()
        assert not list((root/'imports').glob('.preview-*'))
        print(command(start+['--no-open','--port',str(port)]),flush=True)
        persisted=request('/api/library');assert sorted(t['id'] for t in persisted['tracks'])==sorted(saved)
        reloaded = next(track for track in persisted['tracks'] if track['id'] == saved[0])
        assert all(reloaded[key] == value for key, value in correction.items()), reloaded
        report['tests'].append('edited song information survives restart')
        report['tests']+=['authenticated shutdown and temporary preview cleanup','saved library survives restart']
        playlist=request('/api/playlists',{'name':'Deletion QA'})
        request('/api/playlists/'+playlist['id'],{'trackIds':saved},method='PATCH')
        deleted_track=next(track for track in persisted['tracks'] if track['id']==saved[0])
        removed=request('/api/tracks/'+saved[0],method='DELETE');assert removed['success'] and not removed['cleanupPending']
        assert not (root/deleted_track['audioUrl'].lstrip('/')).exists()
        remaining=request('/api/library');assert [t['id'] for t in remaining['tracks']]==saved[1:]
        assert remaining['playlists'][0]['trackIds']==saved[1:]
        report['tests'].append('song deletion removes saved audio and playlist membership')
        print(command(stop+['--port',str(port)]),flush=True)
        print(command(start+['--no-open','--port',str(port)]),flush=True)
        assert [t['id'] for t in request('/api/library')['tracks']]==saved[1:]
        report['tests'].append('song deletion persists after restart')
    finally:
        print(command(stop+['--port',str(port)]),flush=True)
    return report

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('root',type=Path);parser.add_argument('--architecture',choices=['macos-arm64','macos-x64'],required=True);parser.add_argument('--youtube',action='store_true');parser.add_argument('--report',type=Path,required=True);args=parser.parse_args()
    report=run(args.root.resolve(),args.architecture,args.youtube)
    args.report.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report),flush=True)
