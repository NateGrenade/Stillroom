#!/usr/bin/env python3
"""Prepare an explicitly selected musical phrase as a guarded, crossfaded loop."""
import argparse, json, math, re, shutil, subprocess, tempfile
from pathlib import Path


def run(args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr[-3500:])
    return result


def prepare(source, destination, start, end, crossfade, guard=1):
    source, destination = Path(source).expanduser().resolve(), Path(destination).expanduser().resolve()
    if not source.is_file(): raise ValueError('Input audio does not exist.')
    if destination.exists(): raise ValueError('Output already exists; choose a new filename.')
    if source == destination: raise ValueError('Input and output must differ.')
    if destination.suffix.lower() != '.mp3': raise ValueError('Output filename must end in .mp3.')
    if not all(math.isfinite(v) for v in (start,end,crossfade,guard)): raise ValueError('Times must be finite.')
    if start < 0 or crossfade <= 0 or guard <= 0 or end-start <= 2*crossfade+guard:
        raise ValueError('Choose positive crossfade/guard and a range longer than twice the crossfade plus guard.')
    ffmpeg=shutil.which('ffmpeg') or '/opt/homebrew/bin/ffmpeg'
    ffprobe=shutil.which('ffprobe') or '/opt/homebrew/bin/ffprobe'
    info=json.loads(run([ffprobe,'-v','error','-show_format','-show_streams','-of','json',str(source)]).stdout)
    duration=float(info['format']['duration'])
    if end > duration+0.002: raise ValueError('End exceeds the source duration.')
    destination.parent.mkdir(parents=True,exist_ok=True)
    sr=44100
    start,end,crossfade,guard=[round(v*sr)/sr for v in (start,end,crossfade,guard)]
    length=end-start-crossfade
    with tempfile.TemporaryDirectory(prefix='loop-',dir=destination.parent) as tmp:
        loop=Path(tmp)/'loop.wav'; padded=Path(tmp)/'ready.mp3'
        # Rotate the head/tail crossfade to the end of the cycle. Its final sample
        # then flows into the matching next sample at the beginning of the cycle.
        graph=(f'[0:a]aresample={sr},aformat=sample_fmts=fltp:channel_layouts=stereo,asplit=3[m][t][h];'
               f'[m]atrim=start={start+crossfade}:end={end-crossfade},asetpts=PTS-STARTPTS[mid];'
               f'[t]atrim=start={end-crossfade}:end={end},asetpts=PTS-STARTPTS[tail];'
               f'[h]atrim=start={start}:end={start+crossfade},asetpts=PTS-STARTPTS[head];'
               f'[tail][head]acrossfade=d={crossfade}:c1=qsin:c2=qsin[blend];'
               '[mid][blend]concat=n=2:v=0:a=1[out]')
        run([ffmpeg,'-hide_banner','-loglevel','error','-i',str(source),'-filter_complex',graph,'-map','[out]','-c:a','pcm_f32le',str(loop)])
        stats=run([ffmpeg,'-hide_banner','-i',str(loop),'-af','astats=metadata=0:reset=0','-f','null','-']).stderr
        levels=[float(v) for v in re.findall(r'Peak level dB: ([-\d.]+)',stats)]
        if not levels: raise RuntimeError('Could not verify loop peak level.')
        peak=max(levels); gain=min(1,10**((-1-peak)/20))
        # Guards keep MP3 frame-edge artifacts outside the playable loop region.
        graph=(f'[0:a]volume={gain},asplit=3[t][m][h];'
               f'[t]atrim=start={length-guard}:end={length},asetpts=PTS-STARTPTS[tail];'
               f'[h]atrim=start=0:end={guard},asetpts=PTS-STARTPTS[head];'
               '[tail][m][head]concat=n=3:v=0:a=1[out]')
        run([ffmpeg,'-hide_banner','-loglevel','error','-i',str(loop),'-filter_complex',graph,'-map','[out]','-c:a','libmp3lame','-b:a','192k',str(padded)])
        # Decode again: metadata describes the PCM timeline, excluding MP3 padding.
        decoded=Path(tmp)/'decoded.f32'
        run([ffmpeg,'-hide_banner','-loglevel','error','-i',str(padded),'-ar',str(sr),'-ac','2','-f','f32le',str(decoded)])
        decoded_frames=decoded.stat().st_size//8; expected=round((length+2*guard)*sr)
        if abs(decoded_frames-expected)>1: raise RuntimeError('Decoded loop duration did not match expected samples.')
        # Verify PCM around the guarded wrap; retain diagnostic values without
        # pretending a numerical test establishes the subjective musical seam.
        import struct
        with decoded.open('rb') as handle:
            handle.seek(round(guard*sr)*8); first=struct.unpack('<ff',handle.read(8))
            handle.seek((round((guard+length)*sr)-1)*8); last=struct.unpack('<ff',handle.read(8))
        seam=max(abs(a-b) for a,b in zip(first,last))
        shutil.copyfile(padded,destination)
    return {'file':str(destination),'duration':length,'loopStart':guard,'loopEnd':guard+length,'crossfade':crossfade,'sourceRange':[start,end],'gain':gain,'peakBeforeGainDB':peak,'decodedSeconds':decoded_frames/sr,'wrapSampleDelta':seam,'sampleRate':sr}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',required=True);p.add_argument('--output',required=True)
    p.add_argument('--start',type=float,required=True);p.add_argument('--end',type=float,required=True)
    p.add_argument('--crossfade',type=float,required=True);p.add_argument('--guard',type=float,default=1)
    a=p.parse_args()
    try: print(json.dumps(prepare(a.input,a.output,a.start,a.end,a.crossfade,a.guard),indent=2))
    except (ValueError,RuntimeError,OSError) as exc: p.exit(1,str(exc)+'\n')
if __name__=='__main__': main()
