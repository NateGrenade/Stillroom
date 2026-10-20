"""Fetch and verify pinned portable runtimes from upstream distribution channels."""
from concurrent.futures import ThreadPoolExecutor
import hashlib, json, os, shutil, tarfile, urllib.request, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / 'cache'
VENDOR = ROOT / 'vendor' / 'runtimes'
CACHE.mkdir(exist_ok=True)
VENDOR.mkdir(parents=True, exist_ok=True)

def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent':'Stillroom-Packaging/1.2'}), timeout=180)

def sha(path):
    with path.open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()

def download(item):
    path = CACHE / item['name']
    if not path.exists() or (item.get('sha256') and sha(path) != item['sha256']):
        temporary=path.with_suffix(path.suffix+'.part')
        with get(item['url']) as remote, temporary.open('wb') as f: shutil.copyfileobj(remote,f)
        if item.get('sha256') and sha(temporary) != item['sha256']:
            temporary.unlink(); raise RuntimeError('Checksum mismatch: '+item['name'])
        temporary.replace(path)
    item['sha256']=sha(path);item['bytes']=path.stat().st_size
    print('Verified',item['name'],item['bytes'],flush=True)
    return item

if __name__=='__main__':
    python=json.loads((ROOT/'research/python-releases.json').read_text())
    ytdlp=json.loads((ROOT/'research/ytdlp-releases.json').read_text())
    node_version='v22.23.3'
    with get(f'https://nodejs.org/dist/{node_version}/SHASUMS256.txt') as f: sums=f.read().decode()
    checks={line.split()[1]:line.split()[0] for line in sums.splitlines()}
    (CACHE/'node-SHASUMS256.txt').write_text(sums)
    items=[]
    for arch,triple in [('macos-arm64','aarch64'),('macos-x64','x86_64')]:
        a=next(a for a in python['assets'] if a['name'].startswith('cpython-3.13.16+') and f'{triple}-apple-darwin-install_only_stripped.tar.gz' in a['name'])
        items.append({'component':'python','version':'3.13.16','platform':arch,'name':a['name'],'url':a['browser_download_url'],'sha256':a['digest'].split(':')[1]})
    items.append({'component':'python','version':'3.13.16','platform':'windows-x64','name':'python-3.13.16-embed-amd64.zip','url':'https://www.python.org/ftp/python/3.13.16/python-3.13.16-embed-amd64.zip'})
    for platform,upstream in [('macos-arm64','darwin-arm64.tar.gz'),('macos-x64','darwin-x64.tar.gz'),('windows-x64','win-x64.zip')]:
        name=f'node-{node_version}-{upstream}'
        items.append({'component':'node','version':node_version,'platform':platform,'name':name,'url':f'https://nodejs.org/dist/{node_version}/{name}','sha256':checks[name]})
    for platform,name in [('macos','yt-dlp_macos'),('windows-x64','yt-dlp.exe')]:
        a=next(a for a in ytdlp['assets'] if a['name']==name)
        items.append({'component':'yt-dlp','version':ytdlp['tag_name'],'platform':platform,'name':name,'url':a['browser_download_url'],'sha256':a['digest'].split(':')[1]})
    items=list(ThreadPoolExecutor(4).map(download,items))
    for item in items:
        source=CACHE/item['name']; destination=VENDOR/item['platform'];destination.mkdir(exist_ok=True)
        if item['component']=='python':
            if item['platform'].startswith('macos'):
                with tarfile.open(source) as archive: archive.extractall(destination,filter='data')
            else:
                target=destination/'python';target.mkdir(exist_ok=True)
                with zipfile.ZipFile(source) as archive: archive.extractall(target)
                (target/'python313._pth').write_text('python313.zip\n.\n../../..\n\n# App-local imports only. System Python and site-packages are not used.\n')
        elif item['component']=='node':
            temp=destination/'node-distribution';temp.mkdir(exist_ok=True)
            if source.suffix=='.zip':
                with zipfile.ZipFile(source) as archive: archive.extractall(temp)
            else:
                with tarfile.open(source) as archive: archive.extractall(temp,filter='data')
            base=next(temp.iterdir());bins=destination/'bin';bins.mkdir(exist_ok=True)
            executable=base/'node.exe' if item['platform'].startswith('windows') else base/'bin/node'
            shutil.copy2(executable,bins/executable.name)
            licenses=destination/'licenses';licenses.mkdir(exist_ok=True)
            shutil.copy2(base/'LICENSE',licenses/'Node-LICENSE.txt')
            shutil.rmtree(temp)
        else:
            bins=destination/'bin';bins.mkdir(exist_ok=True)
            target=bins/('yt-dlp.exe' if item['platform'].startswith('windows') else 'yt-dlp')
            shutil.copy2(source,target);target.chmod(0o755)
    licenses=VENDOR/'licenses';licenses.mkdir(exist_ok=True)
    for name,url in {
      'yt-dlp-LICENSE.txt':f"https://raw.githubusercontent.com/yt-dlp/yt-dlp/{ytdlp['tag_name']}/LICENSE",
      'yt-dlp-THIRD_PARTY_LICENSES.txt':f"https://raw.githubusercontent.com/yt-dlp/yt-dlp/{ytdlp['tag_name']}/THIRD_PARTY_LICENSES.txt",
      'Python-LICENSE.txt':'https://raw.githubusercontent.com/python/cpython/v3.13.16/LICENSE',
      'Python-build-standalone-LICENSE.txt':'https://raw.githubusercontent.com/astral-sh/python-build-standalone/20261013/LICENSE',
    }.items():
        with get(url) as f: (licenses/name).write_bytes(f.read())
    (ROOT/'research/runtime-manifest.json').write_text(json.dumps({'components':items},indent=2)+'\n')
    print('Runtime assets ready',flush=True)
