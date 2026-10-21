"""Create a silent AAC library for simulator UI checks; never read user media."""
from pathlib import Path
import hashlib
import json
import struct
import subprocess
import sys
import wave


def main():
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=False)
    wav = root / 'generated-silence.wav'
    with wave.open(str(wav), 'wb') as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(48_000)
        handle.writeframes(bytes(8 * 48_000 * 4))
    compressed = root / 'generated-silence.m4a'
    subprocess.run(['/usr/bin/afconvert', '-f', 'm4af', '-d', 'aac', '-b', '128000', str(wav), str(compressed)], check=True)
    payload = compressed.read_bytes()
    fingerprint = hashlib.sha256(payload).hexdigest()
    audio_path = 'audio/' + 'a' * 32 + '.m4a'
    common = dict(artist='Generated simulator fixture', album='Disposable silent audio',
                  fileDuration=8, tags=['generated-test'], audioFile=audio_path)
    manifest = dict(format='stillroom-library', version=1,
                    tracks=[dict(common, id='a' * 32, title='Generated AAC silence', kind='original', duration=8, loopStart=0),
                            dict(common, id='b' * 32, title='Generated saved A–B loop', kind='loop', duration=2, loopStart=1, loopEnd=3)],
                    playlists=[dict(id='c' * 32, name='Generated reverse order', trackIds=['b' * 32, 'a' * 32])],
                    files=[dict(path=audio_path, byteCount=len(payload), sha256=fingerprint)],
                    settings=dict(playbackRate=1.37, pitchChanges=False, reverb=0.2))
    encoded = json.dumps(manifest, separators=(',', ':'), ensure_ascii=False).encode()
    output = root / 'Generated Simulator Library.stillroomlibrary'
    with output.open('xb') as handle:
        handle.write(b'STLRMB01' + struct.pack('>I', len(encoded)) + encoded + payload)
    print(json.dumps(dict(bundle=str(output), tracks=2, playlists=1, compressedBytes=len(payload), audioSHA256=fingerprint)))


if __name__ == '__main__':
    main()
