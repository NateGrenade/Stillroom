import test from 'node:test';
import assert from 'node:assert/strict';
import { MusicPlayer } from '../public/player.js';
import { NATIVE_MEDIA_REVERB_MESSAGE } from '../public/audio-effects.js';
class FakeAudio extends EventTarget {
  constructor(){super();this.currentTime=0;this.paused=true;this.src='';this.playbackRate=1;this.defaultPlaybackRate=1;this.preservesPitch=true;this.playCalls=0;}
  play(){this.paused=false;this.playCalls++;return Promise.resolve();}pause(){this.paused=true;}load(){this.currentTime=0;}removeAttribute(){this.src='';}
  getAttribute(name){return name==='src'?this.src:null;}
}
const makeBuffer=(channels,length,sampleRate)=>({numberOfChannels:channels,length,sampleRate,duration:length/sampleRate,data:Array.from({length:channels},()=>new Float32Array(length)),getChannelData(c){return this.data[c]},copyToChannel(v,c){this.data[c].set(v)}});
class FakeContext{
 constructor(){this.currentTime=0;this.sampleRate=100;this.destination={};this.sources=[];this.stretchNodes=[];this.buffers=[];this.gains=[];this.convolvers=[];this.mediaSources=[];this.resumeCalls=0;this.decoded=makeBuffer(2,1000,100);}
 resume(){this.resumeCalls++;return Promise.resolve()}close(){this.closed=true;return Promise.resolve()}decodeAudioData(){return Promise.resolve(this.decoded)}
 createBuffer(...a){if(this.failNative)throw new Error('Injected native allocation failure');const buffer=makeBuffer(...a);this.buffers.push(buffer);return buffer;}
 createGain(){const node=this.node();node.gain={value:1,setValueAtTime(value){this.value=value;},linearRampToValueAtTime(value){this.value=value;},setTargetAtTime(value){this.value=value;},cancelAndHoldAtTime(){}};this.gains.push(node);return node;}
 node(){return{connections:new Set(),connect(target){if(this.failConnect?.(target))throw new Error('Injected gain connection failure');this.connections.add(target);},disconnect(target){if(target)this.connections.delete(target);else this.connections.clear();}};}
 createConvolver(){if(this.failConvolver)throw new Error('Injected convolver failure');const node=this.node();this.convolvers.push(node);return node;}
 createMediaElementSource(audio){if(this.mediaSources.some(node=>node.audio===audio))throw new Error('Duplicate media source');const node=this.node();node.audio=audio;this.mediaSources.push(node);return node;}
 createBufferSource(){const s={playbackRate:{value:1,setValueAtTime(value){this.value=value;}},connect(){},disconnect(){},start(t,o){this.startTime=t;this.offset=o},stop(){this.stopped=true},finish(){this.onended?.()}};this.sources.push(s);return s;}
}
function setup(deps={}){
  const audio=new FakeAudio(),ctx=new FakeContext();
  const p=new MusicPlayer({audio,contextFactory:()=>ctx,fetch:async()=>({ok:true,arrayBuffer:async()=>new ArrayBuffer(10)}),prepareStretch:async()=>{},stretchNodeFactory:(context,options)=>{
    const messages=[],node={options,messages,parameters:new Map([['playbackRate',{value:options.parameterData.playbackRate}]]),port:{onmessage:null,postMessage(message,transfer=[]){
      if(message.type==='load'&&context.failTransfer)throw new DOMException('Injected transfer failure','DataCloneError');
      const data=structuredClone(message,{transfer});messages.push(data);
      if(data.type==='load'){node.pcm={channels:data.channels,sampleRate:data.sampleRate};node.loaded=data;node.transferred=transfer;node.position=data.offset;
        if(context.failWorkletLoad)queueMicrotask(()=>node.port.onmessage?.({data:{type:'error'}}));}
      if(data.serial!==undefined)node.serial=data.serial;
      if(data.type==='seek')node.position=data.offset;
      if(data.type==='play')node.position=data.offset;
      if(data.type==='release'&&!node.deferReturn)queueMicrotask(()=>node.returnPCM());
      if(data.type==='dispose')node.pcm=null;
    },close(){node.portClosed=true;}},connect(target){this.connectedTo=target;},disconnect(){this.disconnected=true;},finish(serial=this.serial){this.port.onmessage?.({data:{type:'ended',serial}});},returnPCM(){
      const pcm=this.pcm;if(!pcm)return;
      const data=structuredClone({type:'released',...pcm},{transfer:pcm.channels.map(channel=>channel.buffer)});this.pcm=null;this.port.onmessage?.({data});
    }};
    context.stretchNodes.push(node);return node;
  },...deps});
  const catalog=[{id:'a',title:'A',kind:'original',duration:10,audioUrl:'/audio/a.mp3'},{id:'b',title:'B',kind:'original',duration:20,audioUrl:'/audio/b.mp3'},{id:'loop',title:'Loop',kind:'loop',duration:8,loopStart:1,loopEnd:9,audioUrl:'/audio/loop.mp3'}];
  p.setCatalog(catalog);return {p,audio,ctx,catalog};
}
const settle=()=>new Promise(r=>setImmediate(r));
test('editing a playing original updates labels and Media Session without pausing or seeking', async () => {
  const oldNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
  const oldMetadata = Object.getOwnPropertyDescriptor(globalThis, 'MediaMetadata');
  const mediaSession = { setActionHandler() {}, setPositionState() {} };
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { mediaSession } });
  Object.defineProperty(globalThis, 'MediaMetadata', { configurable: true, value: class { constructor(value) { Object.assign(this, value); } } });
  const { p, audio, catalog } = setup();
  try {
    await p.playTrack('a', ['a', 'b']);
    audio.currentTime = 4.5;
    const generation = p.generation, source = audio.src;
    let pauses = 0, reloads = 0;
    audio.pause = () => { pauses += 1; };
    audio.load = () => { reloads += 1; };
    const updated = { ...catalog[0], title: 'New name', artist: 'New artist', album: 'New album' };
    p.setCatalog([updated, ...catalog.slice(1)]);
    assert.equal(p.track, updated);
    assert.equal(p.playing, true);
    assert.equal(p.currentTime(), 4.5);
    assert.equal(audio.src, source);
    assert.equal(p.generation, generation);
    assert.equal(pauses, 0);
    assert.equal(reloads, 0);
    assert.deepEqual(p.queue, ['a', 'b']);
    assert.equal(mediaSession.metadata.title, 'New name');
    assert.equal(mediaSession.metadata.artist, 'New artist');
    assert.equal(mediaSession.metadata.album, 'New album');
  } finally {
    p.destroy();
    if (oldNavigator) Object.defineProperty(globalThis, 'navigator', oldNavigator); else delete globalThis.navigator;
    if (oldMetadata) Object.defineProperty(globalThis, 'MediaMetadata', oldMetadata); else delete globalThis.MediaMetadata;
  }
});
test('editing a playing loop keeps its source, decoded buffer, position, and repeat mode', async () => {
  const { p, ctx, catalog } = setup();
  try {
    await p.playTrack('loop', ['loop', 'a'], { repeatOne: true });
    ctx.currentTime = 3.25;
    const source = p.voice.source, buffer = p.buffer;
    p.setCatalog(catalog.map(row => row.id === 'loop' ? { ...row, title: 'Renamed loop', artist: 'Composer' } : row));
    assert.equal(p.track.title, 'Renamed loop');
    assert.equal(p.currentTime(), 3.25);
    assert.equal(p.voice.source, source);
    assert.equal(p.buffer, buffer);
    assert.equal(ctx.sources.length, 1);
    assert.equal(source.stopped, undefined);
    assert.equal(p.repeat, 'one');
    assert.equal(p.playing, true);
  } finally { p.destroy(); }
});
test('deleting the playing song stops audio and removes it from both queues', async () => {
  const {p,audio,catalog}=setup();
  try {
    await p.playTrack('a',['a','b']); p.setRepeat('all');
    p.setCatalog(catalog.filter(track=>track.id!=='a'));
    assert.equal(p.track,null); assert.equal(p.playing,false); assert.equal(audio.paused,true); assert.equal(audio.src,'');
    assert.deepEqual(p.queue,['b']); assert.deepEqual(p.originalQueue,['b']);
    audio.dispatchEvent(new Event('error')); assert.equal(p.error,'');
    await p.toggle(); assert.equal(p.track.id,'b');
  } finally {p.destroy();}
});
test('deleting a queued song preserves current playback and skips the deleted entry', async () => {
  const {p,catalog}=setup();
  try {
    await p.playTrack('a',['a','b','loop']);
    p.setCatalog(catalog.filter(track=>track.id!=='b'));
    assert.equal(p.track.id,'a'); assert.equal(p.playing,true);
    await p.next(); assert.equal(p.track.id,'loop');
  } finally {p.destroy();}
});
test('deleting a loop while decoding prevents delayed playback', async () => {
  const {p,ctx,catalog}=setup();
  try {
    let resolve; ctx.decodeAudioData=()=>new Promise(done=>{resolve=done;});
    const loading=p.playTrack('loop',['loop','a']); await settle();
    p.setCatalog(catalog.filter(track=>track.id!=='loop'));
    resolve(ctx.decoded); await loading;
    assert.equal(p.track,null); assert.equal(p.buffer,null); assert.equal(p.loading,false); assert.equal(ctx.sources.length,0);
  } finally {p.destroy();}
});
test('deleting a playing loop stops its audio source', async () => {
  const {p,ctx,catalog}=setup();
  try {
    await p.playTrack('loop',['loop','a'],{repeatOne:true});
    const source=ctx.sources[0]; p.setCatalog(catalog.filter(track=>track.id!=='loop'));
    assert.equal(source.stopped,true); assert.equal(p.voice,null); assert.equal(p.track,null);
  } finally {p.destroy();}
});
test('no autoplay; originals pause/resume, advance in queue, and stop at its end',async()=>{const {p,audio}=setup();try{assert.equal(p.getState().playing,false);await p.playTrack('a',['a','b']);assert.equal(audio.src,'/audio/a.mp3');p.pause();assert.equal(audio.paused,true);await p.toggle();assert.equal(p.playing,true);audio.dispatchEvent(new Event('ended'));await settle();assert.equal(p.track.id,'b');audio.dispatchEvent(new Event('ended'));await settle();assert.equal(p.playing,false);}finally{p.destroy()}});
test('prepared loops trim guards and wrap continuously without another source',async()=>{const {p,ctx}=setup();try{await p.playTrack('loop',['loop'],{repeatOne:true});assert.equal(p.buffer.length,800);assert.equal(p.duration,8);assert.equal(ctx.sources.length,1);assert.equal(ctx.sources[0].loop,true);ctx.currentTime=17.25;assert.equal(p.currentTime(),1.25);assert.equal(ctx.sources.length,1);p.pause();ctx.currentTime=30;assert.equal(p.currentTime(),1.25);await p.toggle();assert.equal(ctx.sources.at(-1).offset,1.25);p.setRepeat('off');assert.equal(ctx.sources.at(-1).loop,false);ctx.sources.at(-1).finish();await settle();assert.equal(p.playing,false);}finally{p.destroy()}});
test('prepared track played once advances to original instead of looping',async()=>{const {p,ctx}=setup();try{await p.playTrack('loop',['loop','a']);assert.equal(ctx.sources.at(-1).loop,false);ctx.sources.at(-1).finish();await settle();assert.equal(p.track.id,'a');assert.equal(p.playing,true);}finally{p.destroy()}});
test('shuffle visits each song once; repeat all continues without immediate repeated track',async()=>{const {p}=setup();try{p.setShuffle(true);await p.playTrack('a',['a','b','loop']);p.setRepeat('all');const seen=[p.track.id];await p.next();seen.push(p.track.id);await p.next();seen.push(p.track.id);assert.equal(new Set(seen).size,3);const last=p.track.id;await p.next();assert.notEqual(p.track.id,last);}finally{p.destroy()}});
test('loop action and ordinary play action set predictable repeat behavior',async()=>{const {p,audio}=setup();try{await p.playTrack('a',['a','b'],{repeatOne:true});assert.equal(audio.loop,true);await p.next();assert.equal(p.track.id,'b');await p.playTrack('a',['a','b']);assert.equal(p.repeat,'off');p.setRepeat('all');await p.playTrack('b',['a','b']);assert.equal(p.repeat,'all');}finally{p.destroy()}});
test('rapid track replacement discards stale loop decoding',async()=>{const {p,ctx}=setup();try{let resolve;ctx.decodeAudioData=()=>new Promise(r=>resolve=r);const old=p.playTrack('loop',['loop','a']);await settle();await p.playTrack('a',['a']);resolve(ctx.decoded);await old;assert.equal(p.track.id,'a');assert.equal(p.buffer,null);assert.equal(ctx.sources.length,0);assert.equal(p.playing,true);}finally{p.destroy()}});
test('volume and seeking clamp invalid values; previous restarts the current song',async()=>{const {p,audio}=setup();try{await p.playTrack('a',['a','b']);p.setVolume(9);assert.equal(audio.volume,1);p.setVolume(NaN);assert.equal(p.volume,1);p.seek(99);assert.equal(audio.currentTime,10);p.seek(-1);assert.equal(audio.currentTime,0);p.seek(5);await p.previous();assert.equal(audio.currentTime,0);}finally{p.destroy()}});
test('pause while decoding prevents delayed autoplay, and later resume works',async()=>{const {p,ctx}=setup();try{let resolve;ctx.decodeAudioData=()=>new Promise(r=>resolve=r);const loading=p.playTrack('loop',['loop']);await settle();p.pause();resolve(ctx.decoded);await loading;assert.equal(p.playing,false);assert.equal(p.loading,false);assert.equal(ctx.sources.length,0);await p.toggle();assert.equal(p.playing,true);}finally{p.destroy()}});
test('a failed prepared-track fetch can be retried with Play',async()=>{const {p}=setup();try{p.fetch=async()=>({ok:false});await p.playTrack('loop',['loop']);assert.equal(p.playing,false);assert.ok(p.error);p.fetch=async()=>({ok:true,arrayBuffer:async()=>new ArrayBuffer(10)});await p.toggle();assert.equal(p.playing,true);assert.equal(p.error,'');}finally{p.destroy()}});

test('speed defaults to 1x with pitch changes on, and settings never autoplay', async () => {
  const {p,audio}=setup();
  try {
    assert.equal(p.getState().playbackRate,1);
    assert.equal(p.getState().pitchChanges,true);
    assert.equal(audio.playbackRate,1);
    assert.equal(audio.preservesPitch,false);
    await p.setPlaybackRate(.5); await p.setPitchChanges(false);
    assert.equal(p.getState().playbackRate,.5);
    assert.equal(p.getState().pitchChanges,false);
    assert.equal(p.playing,false);
    assert.equal(audio.playCalls,0);
  } finally {p.destroy();}
});

test('speed accepts the supported limits, clamps outliers, and ignores non-finite values', async () => {
  const {p}=setup();
  try {
    await p.setPlaybackRate(-1); assert.equal(p.getState().playbackRate,.5);
    await p.setPlaybackRate(9); assert.equal(p.getState().playbackRate,2);
    for(const rate of [NaN,Infinity,-Infinity]) {
      await p.setPlaybackRate(rate); assert.equal(p.getState().playbackRate,2);
    }
    await p.setPlaybackRate(1.5); assert.equal(p.getState().playbackRate,1.5);
  } finally {p.destroy();}
});

test('changing original speed or pitch keeps position, playback, and both queues', async () => {
  const {p,audio}=setup();
  try {
    await p.playTrack('a',['a','b','loop']); p.setRepeat('all');
    audio.currentTime=4.5;
    const source=audio.src, queue=[...p.queue], originalQueue=[...p.originalQueue], plays=audio.playCalls;
    await p.setPlaybackRate(1.5); await p.setPitchChanges(false);
    assert.equal(p.currentTime(),4.5); assert.equal(audio.src,source);
    assert.equal(audio.playbackRate,1.5); assert.equal(audio.preservesPitch,true);
    assert.equal(p.playing,true); assert.equal(audio.paused,false); assert.equal(audio.playCalls,plays);
    assert.deepEqual(p.queue,queue); assert.deepEqual(p.originalQueue,originalQueue); assert.equal(p.repeat,'all');
    p.pause(); await p.setPlaybackRate(.5); await p.setPitchChanges(true);
    assert.equal(p.currentTime(),4.5); assert.equal(audio.paused,true); assert.equal(p.playing,false);
    assert.equal(audio.playbackRate,.5); assert.equal(audio.preservesPitch,false);
  } finally {p.destroy();}
});

test('speed and pitch preferences survive original queue advancement and prepared-loop loading', async () => {
  const {p,audio,ctx}=setup();
  try {
    await p.setPlaybackRate(2); await p.setPitchChanges(true);
    await p.playTrack('a',['a','b','loop']);
    audio.dispatchEvent(new Event('ended')); await settle();
    assert.equal(p.track.id,'b'); assert.equal(audio.playbackRate,2); assert.equal(audio.preservesPitch,false);
    await p.next();
    assert.equal(p.track.id,'loop'); assert.equal(ctx.sources.at(-1).playbackRate.value,2);
    assert.equal(p.getState().playbackRate,2); assert.equal(p.getState().pitchChanges,true);
  } finally {p.destroy();}
});

test('prepared loop progress uses media seconds and stays continuous when speed changes live', async () => {
  const {p,ctx}=setup();
  try {
    await p.setPlaybackRate(2); await p.playTrack('loop',['loop'],{repeatOne:true});
    ctx.currentTime=1.5; assert.equal(p.currentTime(),3);
    await p.setPlaybackRate(.5); assert.equal(p.currentTime(),3);
    ctx.currentTime=3.5; assert.equal(p.currentTime(),4);
    assert.equal(ctx.sources.at(-1).playbackRate.value,.5);
    assert.equal(p.duration,8);
    ctx.currentTime=13.5; assert.equal(p.currentTime(),1);
  } finally {p.destroy();}
});

test('prepared loop pause, resume, seek, and repeat changes retain the selected speed', async () => {
  const {p,ctx}=setup();
  try {
    await p.setPlaybackRate(2); await p.playTrack('loop',['loop'],{repeatOne:true});
    ctx.currentTime=4.25; assert.equal(p.currentTime(),.5);
    p.pause(); ctx.currentTime=20; assert.equal(p.currentTime(),.5);
    await p.setPlaybackRate(.5); assert.equal(p.playing,false); assert.equal(p.currentTime(),.5);
    await p.resume(); assert.equal(ctx.sources.at(-1).offset,.5); assert.equal(ctx.sources.at(-1).playbackRate.value,.5);
    ctx.currentTime=21; assert.equal(p.currentTime(),1);
    p.seek(6); assert.equal(p.currentTime(),6); assert.equal(ctx.sources.at(-1).offset,6); assert.equal(ctx.sources.at(-1).playbackRate.value,.5);
    p.setRepeat('off'); ctx.currentTime=22; assert.equal(p.currentTime(),6.5);
    assert.equal(ctx.sources.at(-1).loop,false);
    p.seek(8); await settle(); assert.equal(p.playing,false);
  } finally {p.destroy();}
});

test('reapplying speed and pitch values leaves a playing loop source untouched', async () => {
  const {p,ctx}=setup();
  try {
    await p.setPlaybackRate(1.5); await p.playTrack('loop',['loop'],{repeatOne:true});
    ctx.currentTime=2;
    const source=p.voice.source, count=ctx.sources.length;
    await p.setPlaybackRate(1.5); await p.setPitchChanges(true);
    assert.equal(p.voice.source,source); assert.equal(ctx.sources.length,count);
    assert.equal(source.stopped,undefined); assert.equal(p.currentTime(),3);
  } finally {p.destroy();}
});

test('preserving loop pitch processes only trimmed samples in a shared stereo worklet', async () => {
  const {p,audio,ctx}=setup();
  try {
    ctx.decoded.getChannelData(0).fill(-.8,0,100); ctx.decoded.getChannelData(0).fill(.3,100,900); ctx.decoded.getChannelData(0).fill(.9,900);
    ctx.decoded.getChannelData(1).fill(-.3,100,900);
    await p.setPlaybackRate(.5); await p.setPitchChanges(false);
    await p.playTrack('loop',['loop','a'],{repeatOne:true});
    assert.equal(p.usesStream(),false); assert.equal(p.needsStretch(),true); assert.equal(p.voice.stretch,true); assert.equal(ctx.sources.length,0);
    assert.equal(ctx.stretchNodes.length,1); assert.equal(p.duration,8); assert.equal(audio.src,''); assert.equal(audio.playCalls,0);
    const node=ctx.stretchNodes[0],options=node.loaded;
    assert.equal(options.channels.length,2); assert.equal(options.channels[0].length,800); assert.equal(options.channels[1].length,800);
    assert.ok(options.channels[0].every(value=>Math.abs(value-.3)<1e-6)); assert.ok(options.channels[1].every(value=>Math.abs(value+.3)<1e-6));
    assert.equal(options.sampleRate,100); assert.equal(options.offset,0); assert.equal(options.rate,.5); assert.equal(options.loop,true);
    assert.equal(node.options.processorOptions,undefined); assert.equal(p.buffer,null); assert.equal(p.pcm,null);
    assert.equal(ctx.buffers.length,0); assert.ok(node.transferred.every(buffer=>buffer.byteLength===0));
    assert.deepEqual(node.options.outputChannelCount,[2]); assert.equal(node.parameters.get('playbackRate').value,.5);
    ctx.currentTime=7; assert.equal(p.currentTime(),3.5);
    p.seek(6); assert.equal(node.position,6); assert.equal(p.playing,true);
    p.pause(); await p.resume(); assert.equal(node.position,6); assert.equal(p.currentTime(),6);
    assert.equal(ctx.stretchNodes.length,1); assert.equal(node.messages.filter(message=>message.type==='load').length,1);
  } finally {p.destroy();}
});

test('live loop transport changes transfer PCM and preserve position, queue, repeat, and volume', async () => {
  const {p,audio,ctx}=setup();
  try {
    await p.setPlaybackRate(2); await p.playTrack('loop',['loop','a'],{repeatOne:true}); p.setVolume(.7);
    ctx.currentTime=1.25;
    const buffer=p.buffer, source=p.voice.source, queue=[...p.queue], originalQueue=[...p.originalQueue];
    p.fetch=async()=>{throw new Error('Transport switches must reuse decoded audio');};
    await p.setPitchChanges(false);
    assert.equal(p.currentTime(),2.5); assert.equal(source.stopped,true);
    assert.equal(p.voice.stretch,true); assert.equal(audio.paused,true); assert.equal(p.playing,true);
    assert.equal(ctx.stretchNodes.at(-1).loaded.offset,2.5); assert.equal(ctx.stretchNodes.at(-1).parameters.get('playbackRate').value,2);
    assert.equal(p.buffer,null);assert.equal(p.pcm,null);
    const stretchNode=ctx.stretchNodes.at(-1); ctx.currentTime=2.625; assert.equal(p.currentTime(),5.25);
    await p.setPitchChanges(true);
    assert.equal(p.usesStream(),false); assert.equal(audio.paused,true); assert.equal(p.playing,true);
    assert.equal(p.voice.stretch,false); assert.equal(stretchNode.disconnected,true); assert.ok(stretchNode.messages.some(message=>message.type==='release'));
    assert.equal(stretchNode.portClosed,true);assert.equal(p.stretchSource,null);assert.equal(p.pcm,null);
    for(let c=0;c<2;c++)assert.deepEqual(p.buffer.getChannelData(c),buffer.getChannelData(c));
    assert.equal(ctx.sources.at(-1).offset,5.25); assert.equal(ctx.sources.at(-1).playbackRate.value,2);
    assert.equal(p.currentTime(),5.25); ctx.currentTime=3.125; assert.equal(p.currentTime(),6.25);
    assert.deepEqual(p.queue,queue); assert.deepEqual(p.originalQueue,originalQueue); assert.equal(p.repeat,'one'); assert.equal(p.volume,.7);
  } finally {p.destroy();}
});

test('paused loop transport changes and returning to 1x never autoplay or reset position', async () => {
  const {p,audio,ctx}=setup();
  try {
    await p.setPlaybackRate(2); await p.playTrack('loop',['loop']);
    ctx.currentTime=1.5; p.pause();
    await p.setPitchChanges(false);
    assert.equal(p.needsStretch(),true); assert.equal(p.currentTime(),3); assert.equal(p.playing,false); assert.equal(audio.paused,true);
    assert.equal(ctx.stretchNodes.length,0);
    await p.setPlaybackRate(1);
    assert.equal(p.usesStream(),false); assert.equal(p.currentTime(),3); assert.equal(p.playing,false);
    await p.resume(); assert.equal(ctx.sources.at(-1).offset,3); assert.equal(ctx.sources.at(-1).playbackRate.value,1);
    assert.equal(p.getState().pitchChanges,false);
  } finally {p.destroy();}
});

test('a pitch-preserved loop played once advances normally and keeps playback settings', async () => {
  const {p,audio,ctx}=setup();
  try {
    await p.setPlaybackRate(1.5); await p.setPitchChanges(false); await p.playTrack('loop',['loop','a']);
    assert.equal(ctx.stretchNodes.at(-1).loaded.loop,false); ctx.stretchNodes.at(-1).finish(); await settle();
    assert.equal(p.track.id,'a'); assert.equal(p.playing,true); assert.equal(audio.src,'/audio/a.mp3');
    assert.equal(audio.playbackRate,1.5); assert.equal(audio.preservesPitch,true);
    assert.deepEqual(p.queue,['loop','a']);
  } finally {p.destroy();}
});

test('latest playback settings are used after a pending loop decode without reviving paused playback', async () => {
  const {p,ctx,audio}=setup();
  try {
    let resolve; ctx.decodeAudioData=()=>new Promise(done=>{resolve=done;});
    const loading=p.playTrack('loop',['loop']); await settle();
    await p.setPlaybackRate(2); await p.setPitchChanges(false); p.pause();
    resolve(ctx.decoded); await loading;
    assert.equal(p.playing,false); assert.equal(audio.paused,true); assert.equal(ctx.sources.length,0);
    assert.equal(p.getState().playbackRate,2); assert.equal(p.getState().pitchChanges,false);
    await p.resume();
    assert.equal(p.voice.stretch,true); assert.equal(ctx.stretchNodes.at(-1).parameters.get('playbackRate').value,2);
    assert.equal(ctx.stretchNodes.length,1); assert.equal(p.playing,true);
  } finally {p.destroy();}
});

test('stretch nodes stop and disconnect when replacing or deleting their track and on destroy', async () => {
  const {p,audio,ctx,catalog}=setup();
  try {
    await p.setPlaybackRate(2); await p.setPitchChanges(false);
    await p.playTrack('loop',['loop','a']); const first=ctx.stretchNodes.at(-1);
    await p.next(); assert.equal(first.disconnected,true); assert.equal(first.messages.at(-1).type,'dispose'); assert.equal(first.port.onmessage,null); assert.equal(first.portClosed,true);
    first.finish(); await settle(); assert.equal(p.track.id,'a');
    await p.playTrack('loop',['loop','a']); const second=ctx.stretchNodes.at(-1);
    p.setCatalog(catalog.filter(track=>track.id!=='loop'));
    assert.equal(p.track,null); assert.equal(audio.src,''); assert.equal(audio.paused,true); assert.equal(second.disconnected,true); assert.equal(second.messages.at(-1).type,'dispose');assert.equal(second.portClosed,true);
    p.setCatalog(catalog); await p.playTrack('loop',['loop']); const third=ctx.stretchNodes.at(-1);
    p.destroy(); assert.equal(third.disconnected,true); assert.equal(third.messages.at(-1).type,'dispose'); assert.equal(third.port.onmessage,null);assert.equal(third.portClosed,true);
  } finally {p.destroy();}
});

test('Media Session reports the selected speed and logical media position', async () => {
  const oldNavigator=Object.getOwnPropertyDescriptor(globalThis,'navigator');
  const positions=[], mediaSession={setActionHandler(){},setPositionState(state){positions.push(state);}};
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{mediaSession}});
  const {p,ctx}=setup();
  try {
    await p.setPlaybackRate(2); await p.playTrack('loop',['loop'],{repeatOne:true}); ctx.currentTime=1.5; p.emit();
    assert.deepEqual(positions.at(-1),{duration:8,playbackRate:2,position:3});
    await p.setPitchChanges(false); p.emit();
    assert.deepEqual(positions.at(-1),{duration:8,playbackRate:2,position:3});
  } finally {
    p.destroy();
    if(oldNavigator)Object.defineProperty(globalThis,'navigator',oldNavigator);else delete globalThis.navigator;
  }
});

test('rapid loop mode changes retain play intent while an older stretch module load is pending', async () => {
  const {p,audio,ctx}=setup();
  let resolvePlay;
  try {
    await p.setPlaybackRate(2); await p.playTrack('loop',['loop','a'],{repeatOne:true}); ctx.currentTime=1;
    p.prepareStretch=()=>new Promise(resolve=>{resolvePlay=resolve;});
    const oldTransition=p.setPitchChanges(false); await settle();
    await p.setPitchChanges(true);
    resolvePlay(); await oldTransition; await settle();
    assert.equal(p.usesStream(),false); assert.equal(p.playing,true); assert.equal(audio.paused,true);
    assert.equal(p.currentTime(),2); assert.equal(ctx.sources.at(-1).offset,2);
    assert.equal(ctx.stretchNodes.length,0);
    assert.deepEqual(p.queue,['loop','a']);
  } finally {resolvePlay?.();p.destroy();}
});

test('an explicit pause cancels a pending stretch module load', async () => {
  const {p,audio,ctx}=setup();
  let resolvePlay;
  try {
    await p.setPlaybackRate(2); await p.playTrack('loop',['loop'],{repeatOne:true}); ctx.currentTime=1;
    p.prepareStretch=()=>new Promise(resolve=>{resolvePlay=resolve;});
    const transition=p.setPitchChanges(false); await settle(); p.pause();
    resolvePlay(); await transition;
    assert.equal(p.playing,false); assert.equal(audio.paused,true); assert.equal(p.currentTime(),2);
    assert.equal(p.autoplayRequested,false);
    assert.equal(ctx.stretchNodes.length,0);
  } finally {resolvePlay?.();p.destroy();}
});

test('replacing a loop during a pending module load discards its stale result', async () => {
  const {p,audio,ctx}=setup();
  let resolvePlay;
  try {
    await p.setPlaybackRate(2); await p.playTrack('loop',['loop','a']); ctx.currentTime=1;
    p.prepareStretch=()=>new Promise(resolve=>{resolvePlay=resolve;});
    const transition=p.setPitchChanges(false); await settle();
    await p.playTrack('a',['a','b']);
    resolvePlay(); await transition;
    assert.equal(p.track.id,'a'); assert.equal(p.playing,true); assert.equal(audio.src,'/audio/a.mp3');
    assert.equal(p.currentTime(),0); assert.equal(p.voice,null); assert.equal(ctx.stretchNodes.length,0);
    assert.deepEqual(p.queue,['a','b']);
  } finally {resolvePlay?.();p.destroy();}
});

test('failed stretch module loads stay paused and retries never play with the wrong pitch mode', async () => {
  const {p,ctx}=setup({prepareStretch:async()=>{throw new Error('Module unavailable');}});
  try {
    await p.setPlaybackRate(2); await p.playTrack('loop',['loop'],{repeatOne:true}); ctx.currentTime=1;
    await p.setPitchChanges(false);
    assert.ok(p.error); assert.equal(p.playing,false); assert.equal(p.voice,null);
    const count=ctx.sources.length;
    await p.resume();
    assert.equal(p.playing,false); assert.equal(ctx.sources.length,count); assert.equal(ctx.stretchNodes.length,0);
    p.prepareStretch=async()=>{}; await p.resume();
    assert.equal(p.playing,true); assert.equal(p.voice.stretch,true); assert.equal(p.currentTime(),2); assert.equal(p.error,'');
  } finally {p.destroy();}
});

test('a playing stretch node changes speed and repeat without restarting or seeking', async () => {
  const {p,ctx}=setup();
  try {
    await p.setPlaybackRate(.5); await p.setPitchChanges(false); await p.playTrack('loop',['loop'],{repeatOne:true});
    const node=ctx.stretchNodes[0],source=p.voice.source;
    ctx.currentTime=5; assert.equal(p.currentTime(),2.5);
    await p.setPlaybackRate(2);
    assert.equal(p.currentTime(),2.5); assert.equal(p.voice.source,source); assert.equal(ctx.stretchNodes.length,1);
    assert.equal(node.parameters.get('playbackRate').value,2); assert.equal(node.disconnected,undefined);
    ctx.currentTime=6; assert.equal(p.currentTime(),4.5);
    p.setRepeat('off'); assert.equal(p.currentTime(),4.5); assert.deepEqual(node.messages.at(-1),{type:'loop',value:false});
    ctx.currentTime=6.5; assert.equal(p.currentTime(),5.5);
    p.setRepeat('one'); assert.equal(p.currentTime(),5.5); assert.deepEqual(node.messages.at(-1),{type:'loop',value:true});
    await p.setPitchChanges(false); await p.setPlaybackRate(2);
    assert.equal(p.voice.source,source); assert.equal(ctx.stretchNodes.length,1);
  } finally {p.destroy();}
});

test('a stretch processor error pauses the current voice, preserves its position, and allows retry', async () => {
  const {p,ctx}=setup();
  try {
    await p.setPlaybackRate(.5); await p.setPitchChanges(false); await p.playTrack('loop',['loop'],{repeatOne:true});
    const node=ctx.stretchNodes[0]; ctx.currentTime=4;
    assert.equal(typeof node.onprocessorerror,'function'); node.onprocessorerror(new Event('processorerror'));
    assert.equal(p.playing,false); assert.equal(p.autoplayRequested,false); assert.equal(p.voice,null); assert.equal(p.currentTime(),2);
    assert.ok(p.error); assert.equal(node.disconnected,true); assert.equal(node.port.onmessage,null);
    assert.ok(node.messages.some(message=>message.type==='dispose'));assert.equal(node.portClosed,true);
    await p.resume(); assert.equal(p.playing,true); assert.equal(p.voice.stretch,true); assert.equal(ctx.stretchNodes.length,2);
    assert.equal(ctx.stretchNodes[1].loaded.offset,2); assert.equal(p.error,'');
  } finally {p.destroy();}
});

test('a discarded stretch processor error cannot interrupt another track', async () => {
  const {p,audio,ctx}=setup();
  try {
    await p.setPlaybackRate(2); await p.setPitchChanges(false); await p.playTrack('loop',['loop','a']);
    const oldError=ctx.stretchNodes[0].onprocessorerror;
    assert.equal(typeof oldError,'function'); await p.next(); audio.currentTime=3;
    oldError(new Event('processorerror'));
    assert.equal(p.track.id,'a'); assert.equal(p.playing,true); assert.equal(p.currentTime(),3); assert.equal(p.error,'');
    assert.equal(audio.src,'/audio/a.mp3'); assert.equal(audio.paused,false);
  } finally {p.destroy();}
});

test('changing speed and seeking during a pending stretch module load uses the latest values', async () => {
  const {p,ctx}=setup(); let resolveModule;
  try {
    await p.setPlaybackRate(.5); await p.playTrack('loop',['loop'],{repeatOne:true}); ctx.currentTime=2;
    p.prepareStretch=()=>new Promise(resolve=>{resolveModule=resolve;});
    const transition=p.setPitchChanges(false); await settle();
    await p.setPlaybackRate(2); p.seek(5);
    resolveModule(); await transition;
    const node=ctx.stretchNodes.at(-1);
    assert.equal(p.playing,true); assert.equal(p.currentTime(),5); assert.equal(node.loaded.offset,5);
    assert.equal(node.parameters.get('playbackRate').value,2); assert.equal(ctx.stretchNodes.length,1);
  } finally {resolveModule?.();p.destroy();}
});

test('destroying the player during a pending stretch module load cannot create delayed audio', async () => {
  const {p,ctx}=setup(); let resolveModule;
  try {
    await p.setPlaybackRate(2); await p.playTrack('loop',['loop']);
    p.prepareStretch=()=>new Promise(resolve=>{resolveModule=resolve;});
    const transition=p.setPitchChanges(false); await settle(); p.destroy();
    resolveModule(); await transition;
    assert.equal(p.playing,false); assert.equal(p.voice,null); assert.equal(ctx.stretchNodes.length,0);
  } finally {resolveModule?.();p.destroy();}
});

test('repeated stretch seeks and resumes transfer PCM only once and ignore stale ended messages', async () => {
  const {p,ctx}=setup();
  try {
    await p.setPlaybackRate(.5); await p.setPitchChanges(false); await p.playTrack('loop',['loop','a'],{repeatOne:true});
    const source=p.stretchSource,node=source.node,channels=node.pcm.channels,serial=node.serial;
    p.fetch=async()=>{throw new Error('Seek/resume must retain the PCM owner');};
    for(let index=0;index<12;index++){
      p.seek(index%8);ctx.currentTime+=.375;const position=p.currentTime();p.pause();ctx.currentTime+=2;await p.resume();
      assert.equal(p.stretchSource,source);assert.equal(node.pcm.channels,channels);
      assert.equal(node.position,position);assert.equal(p.currentTime(),position);
      assert.equal(p.buffer,null);assert.equal(p.pcm,null);assert.equal(p.playing,true);
    }
    node.finish(serial);await settle();
    assert.equal(p.track.id,'loop');assert.equal(p.playing,true);
    assert.equal(ctx.stretchNodes.length,1);assert.equal(ctx.buffers.length,0);
    assert.equal(node.messages.filter(message=>message.type==='load').length,1);
    assert.equal(node.messages.filter(message=>message.type==='release').length,0);
  } finally {p.destroy();}
});

test('pausing during a PCM return retains bit-exact audio without delayed autoplay or a native allocation', async () => {
  const {p,ctx}=setup();
  try {
    for(let c=0;c<2;c++)ctx.decoded.getChannelData(c).set(Float32Array.from({length:1000},(_,index)=>(c?-.5:.5)*Math.sin(index*.039)));
    const expected=ctx.decoded.data.map(channel=>channel.slice(100,900));
    await p.setPlaybackRate(2);await p.setPitchChanges(false);await p.playTrack('loop',['loop'],{repeatOne:true});
    const node=p.stretchSource.node;node.deferReturn=true;ctx.currentTime=1.5;
    const transition=p.setPitchChanges(true);await settle();
    assert.equal(node.messages.filter(message=>message.type==='release').length,1);
    p.pause();p.seek(5);node.returnPCM();await transition;
    assert.equal(p.playing,false);assert.equal(p.currentTime(),5);assert.equal(p.autoplayRequested,false);
    assert.equal(p.buffer,null);assert.deepEqual(p.pcm.channels,expected);assert.equal(ctx.buffers.length,0);
    assert.equal(p.stretchSource,null);assert.equal(node.portClosed,true);
    p.fetch=async()=>{throw new Error('Returned PCM must be reused');};
    await p.resume();assert.equal(p.playing,true);assert.equal(p.voice.stretch,false);assert.equal(ctx.sources.at(-1).offset,5);
    assert.deepEqual(p.buffer.data,expected);assert.equal(p.pcm,null);
  } finally {p.destroy();}
});

test('a mode reversal during PCM return uses one return and transfers the sole copy to the latest worklet', async () => {
  const {p,ctx}=setup();
  try {
    await p.setPlaybackRate(1.5);await p.setPitchChanges(false);await p.playTrack('loop',['loop'],{repeatOne:true});
    const old=p.stretchSource.node;old.deferReturn=true;ctx.currentTime=2;
    const native=p.setPitchChanges(true);await settle();
    const stretch=p.setPitchChanges(false);await settle();p.seek(6);
    old.returnPCM();await Promise.all([native,stretch]);
    assert.equal(old.messages.filter(message=>message.type==='release').length,1);
    assert.equal(ctx.stretchNodes.length,2);assert.equal(ctx.buffers.length,0);assert.equal(ctx.sources.length,0);
    assert.equal(p.playing,true);assert.equal(p.voice.stretch,true);assert.equal(p.currentTime(),6);
    assert.equal(p.stretchSource.node.loaded.offset,6);assert.equal(p.buffer,null);assert.equal(p.pcm,null);
    assert.ok(old.loaded.channels.every(channel=>channel.byteLength===0));assert.equal(old.portClosed,true);
  } finally {p.destroy();}
});

for(const action of ['replace','delete','destroy'])test(`${action} during a PCM return settles the transition and releases the discarded owner`,async()=>{
  const {p,ctx,catalog}=setup();
  try {
    await p.setPlaybackRate(2);await p.setPitchChanges(false);await p.playTrack('loop',['loop','a']);
    const node=p.stretchSource.node;node.deferReturn=true;
    const transition=p.setPitchChanges(true);await settle();
    if(action==='replace')await p.playTrack('a',['a']);
    else if(action==='delete')p.setCatalog(catalog.filter(track=>track.id!=='loop'));
    else p.destroy();
    await transition;
    assert.equal(p.buffer,null);assert.equal(p.pcm,null);assert.equal(p.stretchSource,null);
    assert.equal(node.portClosed,true);assert.equal(node.port.onmessage,null);assert.equal(node.pcm,null);
    assert.equal(ctx.sources.length,0);
    assert.equal(p.playing,action==='replace');assert.equal(p.track?.id,action==='replace'?'a':action==='delete'?undefined:'loop');
  } finally {p.destroy();}
});

test('a processor failure during PCM return cancels the transition and retries decoding at the preserved position',async()=>{
  const {p,ctx}=setup();let fetches=0;
  try {
    const fetch=p.fetch;p.fetch=(...args)=>{fetches++;return fetch(...args);};
    await p.setPlaybackRate(2);await p.setPitchChanges(false);await p.playTrack('loop',['loop']);
    const node=p.stretchSource.node;node.deferReturn=true;ctx.currentTime=1.25;
    const transition=p.setPitchChanges(true);await settle();node.onprocessorerror(new Event('processorerror'));await transition;
    assert.equal(p.playing,false);assert.equal(p.currentTime(),2.5);assert.equal(p.autoplayRequested,false);assert.ok(p.error);
    assert.equal(p.hasLoopData(),false);assert.equal(node.portClosed,true);
    await p.resume();assert.equal(fetches,2);assert.equal(p.playing,true);assert.equal(p.currentTime(),2.5);
    assert.equal(p.voice.stretch,false);assert.equal(ctx.sources.at(-1).offset,2.5);
  } finally {p.destroy();}
});

test('a failed transferable handoff retains bit-exact PCM and retries without fetching again',async()=>{
  const {p,ctx}=setup();
  try {
    for(let c=0;c<2;c++)ctx.decoded.data[c].set(Float32Array.from({length:1000},(_,index)=>(c?-.6:.6)*Math.sin(index*.031)));
    const expected=ctx.decoded.data.map(channel=>channel.slice(100,900));
    ctx.failTransfer=true;await p.setPlaybackRate(.5);await p.setPitchChanges(false);await p.playTrack('loop',['loop']);
    assert.equal(p.playing,false);assert.ok(p.error);assert.equal(p.stretchSource,null);assert.deepEqual(p.pcm.channels,expected);
    assert.equal(ctx.stretchNodes[0].portClosed,true);assert.equal(ctx.stretchNodes[0].port.onmessage,null);
    p.fetch=async()=>{throw new Error('Retry must reuse the retained PCM');};ctx.failTransfer=false;await p.resume();
    assert.equal(p.playing,true);assert.equal(p.pcm,null);assert.equal(p.buffer,null);assert.deepEqual(p.stretchSource.node.pcm.channels,expected);
  } finally {p.destroy();}
});

test('a failed native allocation after PCM return retains bit-exact audio for retry',async()=>{
  const {p,ctx}=setup();
  try {
    for(let c=0;c<2;c++)ctx.decoded.data[c].set(Float32Array.from({length:1000},(_,index)=>(c?-.6:.6)*Math.sin(index*.031)));
    const expected=ctx.decoded.data.map(channel=>channel.slice(100,900));
    await p.setPlaybackRate(2);await p.setPitchChanges(false);await p.playTrack('loop',['loop']);
    ctx.currentTime=1.25;ctx.failNative=true;await p.setPitchChanges(true);
    assert.equal(p.playing,false);assert.ok(p.error);assert.equal(p.stretchSource,null);assert.equal(p.buffer,null);
    assert.deepEqual(p.pcm.channels,expected);assert.equal(p.currentTime(),2.5);
    p.fetch=async()=>{throw new Error('Retry must reuse returned PCM');};ctx.failNative=false;await p.resume();
    assert.equal(p.playing,true);assert.equal(p.pcm,null);assert.deepEqual(p.buffer.data,expected);assert.equal(p.currentTime(),2.5);
  } finally {p.destroy();}
});

test('an asynchronous worklet load error explicitly pauses and releases transferred PCM for retry',async()=>{
  const {p,ctx}=setup();let fetches=0;
  try {
    const fetch=p.fetch;p.fetch=(...args)=>{fetches++;return fetch(...args);};
    ctx.failWorkletLoad=true;await p.setPlaybackRate(.5);await p.setPitchChanges(false);await p.playTrack('loop',['loop']);
    assert.equal(p.playing,false);assert.equal(p.autoplayRequested,false);assert.ok(p.error);
    assert.equal(p.hasLoopData(),false);assert.equal(p.voice,null);assert.equal(ctx.stretchNodes[0].portClosed,true);
    ctx.failWorkletLoad=false;await p.resume();assert.equal(p.playing,true);assert.equal(fetches,2);assert.equal(p.error,'');
  } finally {p.destroy();}
});

test('an explicit worklet command error settles a pending PCM return and preserves retry position',async()=>{
  const {p,ctx}=setup();
  try {
    await p.setPlaybackRate(2);await p.setPitchChanges(false);await p.playTrack('loop',['loop']);
    const node=p.stretchSource.node;node.deferReturn=true;ctx.currentTime=1.25;
    const transition=p.setPitchChanges(true);await settle();node.port.onmessage({data:{type:'error'}});await transition;
    assert.equal(p.playing,false);assert.equal(p.currentTime(),2.5);assert.equal(p.autoplayRequested,false);assert.ok(p.error);
    assert.equal(p.hasLoopData(),false);assert.equal(node.portClosed,true);assert.equal(node.port.onmessage,null);
    await p.resume();assert.equal(p.playing,true);assert.equal(p.currentTime(),2.5);assert.equal(p.voice.stretch,false);
  } finally {p.destroy();}
});

test('reverb preferences default off, clamp safely, and do not create audio or autoplay',async()=>{
  const {p,ctx,audio}=setup();
  try {
    assert.equal(p.getState().reverb,0);assert.equal(p.getState().effectsError,'');
    await p.setReverb(.6);assert.equal(p.reverb,.6);assert.equal(p.context,null);
    await p.setReverb(9);assert.equal(p.reverb,1);await p.setReverb(NaN);assert.equal(p.reverb,1);
    await p.setReverb(-1);assert.equal(p.reverb,0);
    assert.equal(ctx.mediaSources.length,0);assert.equal(ctx.buffers.length,0);assert.equal(audio.playCalls,0);assert.equal(p.playing,false);
  } finally {p.destroy();}
});

test('WebKit keeps original streams native, preserves controls, and displays the optional reverb limitation',async()=>{
  const oldNavigator=Object.getOwnPropertyDescriptor(globalThis,'navigator');
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{userAgent:'Mozilla/5.0 AppleWebKit/605.1.15 Version/18.1.1 Safari/605.1.15'}});
  const {p,ctx,audio}=setup();
  try {
    await p.setReverb(.8);assert.equal(p.effectsError,'');assert.equal(p.context,null);
    await p.playTrack('a',['a','loop']);audio.currentTime=3.25;
    await p.setPlaybackRate(1.37);await p.setPitchChanges(false);p.setVolume(.7);audio.muted=true;
    const plays=audio.playCalls;await p.setReverb(.6);
    assert.equal(p.effectsError,NATIVE_MEDIA_REVERB_MESSAGE);assert.equal(p.error,'');assert.equal(p.playing,true);
    assert.equal(p.currentTime(),3.25);assert.equal(audio.playCalls,plays);assert.equal(audio.volume,.7);assert.equal(audio.muted,true);
    assert.equal(audio.playbackRate,1.37);assert.equal(audio.preservesPitch,true);assert.equal(p.reverb,.6);
    assert.equal(p.context,null);assert.equal(p.streamRoute.source,null);assert.equal(ctx.mediaSources.length,0);
    p.pause();await p.resume();assert.equal(p.playing,true);assert.equal(p.effectsError,NATIVE_MEDIA_REVERB_MESSAGE);
    await p.setReverb(0);assert.equal(p.effectsError,'');assert.equal(p.context,null);
  } finally {p.destroy();if(oldNavigator)Object.defineProperty(globalThis,'navigator',oldNavigator);else delete globalThis.navigator;}
});

for(const pitchChanges of [true,false])test(`WebKit native-stream fallback retains loop reverb and clears only its stream warning, pitch changes ${pitchChanges}`,async()=>{
  const oldNavigator=Object.getOwnPropertyDescriptor(globalThis,'navigator');
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{userAgent:'Mozilla/5.0 AppleWebKit/605.1.15 Version/27.0 Safari/605.1.15'}});
  const {p,ctx}=setup();
  try {
    await p.setPlaybackRate(1.37);await p.setPitchChanges(pitchChanges);await p.setReverb(.8);
    await p.playTrack('a',['a','loop']);assert.equal(p.effectsError,NATIVE_MEDIA_REVERB_MESSAGE);assert.equal(p.context,null);
    await p.next();assert.equal(p.track.id,'loop');assert.equal(p.playing,true);assert.ok(p.loopEffects?.convolver);
    assert.equal(p.reverb,.8);assert.equal(p.effectsError,'');assert.equal(ctx.mediaSources.length,0);
    const source=p.voice.source,bus=p.loopEffects;await p.setReverb(.6);assert.equal(p.voice.source,source);assert.equal(p.effectsError,'');
    await p.playTrack('a',['a','loop']);assert.equal(p.playing,true);assert.equal(p.reverb,.6);
    assert.equal(p.effectsError,NATIVE_MEDIA_REVERB_MESSAGE);assert.equal(p.error,'');assert.equal(ctx.mediaSources.length,0);
    assert.equal(p.streamRoute.source,null);assert.equal(p.streamRoute.reverb,.6);
    await p.next();assert.equal(p.track.id,'loop');assert.equal(p.loopEffects,bus);assert.equal(p.effectsError,'');assert.equal(p.playing,true);
  } finally {p.destroy();if(oldNavigator)Object.defineProperty(globalThis,'navigator',oldNavigator);else delete globalThis.navigator;}
});

test('stream reverb captures once and preserves playback, native volume, position, settings, and queues',async()=>{
  const {p,ctx,audio}=setup();
  try {
    await p.playTrack('a',['a','b']);audio.currentTime=3.25;
    await p.setPlaybackRate(1.25);await p.setPitchChanges(false);p.setVolume(.7);p.setRepeat('all');
    const plays=audio.playCalls,queue=[...p.queue],original=[...p.originalQueue];
    await p.setReverb(.8);
    assert.equal(ctx.mediaSources.length,1);assert.equal(p.streamRoute.source,ctx.mediaSources[0]);
    assert.equal(p.playing,true);assert.equal(audio.playCalls,plays);assert.equal(p.currentTime(),3.25);
    assert.equal(audio.volume,.7);assert.equal(p.streamRoute.bus.output.gain.value,1);
    assert.equal(audio.playbackRate,1.25);assert.equal(audio.preservesPitch,true);assert.equal(p.getState().reverb,.8);
    await p.setReverb(0);p.pause();const resumes=ctx.resumeCalls;await p.resume();
    assert.equal(ctx.mediaSources.length,1);assert.ok(ctx.resumeCalls>resumes);assert.equal(p.playing,true);
    assert.equal(p.streamRoute.bus.dry.gain.value,1);assert.equal(p.streamRoute.bus.wet.gain.value,0);
    assert.deepEqual(p.queue,queue);assert.deepEqual(p.originalQueue,original);assert.equal(p.repeat,'all');
  } finally {p.destroy();}
});

for(const pitchChanges of [true,false])test(`loop reverb preserves PCM ownership, source, and controls with pitch changes ${pitchChanges}`,async()=>{
  const {p,ctx}=setup();
  try {
    await p.setPlaybackRate(1.5);await p.setPitchChanges(pitchChanges);await p.setReverb(1);
    await p.playTrack('loop',['loop','a'],{repeatOne:true});
    const source=p.voice.source,buffer=p.buffer,owner=p.stretchSource,impulse=p.loopEffects.impulse,bufferCount=ctx.buffers.length;
    ctx.currentTime=1;const position=p.currentTime();await p.setReverb(.3);
    assert.equal(p.voice.source,source);assert.equal(p.currentTime(),position);assert.equal(p.buffer,buffer);assert.equal(p.stretchSource,owner);
    assert.equal(p.loopEffects.impulse,impulse);assert.equal(ctx.buffers.length,bufferCount);assert.equal(impulse.length,140);
    const beforeSeek=p.loopEffects.convolver;p.seek(4);assert.notEqual(p.loopEffects.convolver,beforeSeek);
    p.pause();const frozen=p.currentTime();await p.setReverb(.9);assert.equal(p.playing,false);assert.equal(p.currentTime(),frozen);
    await p.resume();assert.equal(p.currentTime(),frozen);assert.equal(p.voice.source.playbackRate.value,1.5);
    assert.equal(p.buffer,buffer);assert.equal(p.stretchSource,owner);assert.equal(ctx.buffers.length,bufferCount);
    if(!pitchChanges){assert.equal(ctx.stretchNodes.length,1);assert.equal(p.buffer,null);assert.equal(p.pcm,null);assert.equal(ctx.stretchNodes[0].messages.filter(message=>message.type==='load').length,1);}
  } finally {p.destroy();}
});

test('failed loop reverb rewiring retains the working dry voice and can be retried without restart',async()=>{
  const {p,ctx}=setup();
  try {
    await p.playTrack('loop',['loop'],{repeatOne:true});ctx.currentTime=1.5;
    const voice=p.voice,source=voice.source,position=p.currentTime();
    voice.envelope.failConnect=target=>target!==p.gain;
    await p.setReverb(.8);
    assert.equal(p.voice,voice);assert.equal(p.voice.source,source);assert.equal(p.playing,true);assert.equal(p.currentTime(),position);
    assert.equal(voice.target,p.gain);assert.equal(voice.envelope.connections.has(p.gain),true);assert.ok(p.effectsError);assert.equal(p.error,'');
    voice.envelope.failConnect=null;await p.setReverb(.9);
    assert.equal(p.voice.source,source);assert.equal(voice.target,p.loopEffects.input);assert.equal(voice.envelope.connections.has(p.gain),false);
    assert.equal(p.effectsError,'');
  } finally {p.destroy();}
});

test('an optional stream effect initialization failure does not poison later loop context creation',async()=>{
  const broken=new FakeContext();broken.createGain=()=>{throw new Error('Injected initial gain failure');};
  const {p,ctx,audio}=setup();let factories=0;
  p.contextFactory=()=>++factories===1?broken:ctx;
  try {
    await p.playTrack('a',['a','loop']);const plays=audio.playCalls;await p.setReverb(.7);
    assert.equal(p.playing,true);assert.equal(audio.paused,false);assert.equal(audio.playCalls,plays);
    assert.equal(p.context,null);assert.equal(p.gain,null);assert.equal(broken.closed,true);assert.ok(p.effectsError);assert.equal(p.error,'');
    await p.next();assert.equal(p.track.id,'loop');assert.equal(p.playing,true);assert.equal(p.context,ctx);assert.ok(p.gain);assert.equal(p.error,'');
  } finally {p.destroy();}
});

test('reverb allocation failure keeps loops playing through the existing dry path',async()=>{
  const {p,ctx}=setup();
  try {
    await p.playTrack('loop',['loop'],{repeatOne:true});const source=p.voice.source;ctx.currentTime=2;ctx.failConvolver=true;
    await p.setReverb(1);
    assert.equal(p.voice.source,source);assert.equal(p.voice.target,p.gain);assert.equal(p.playing,true);assert.equal(p.currentTime(),2);
    assert.ok(p.effectsError);assert.equal(p.error,'');assert.equal(p.loopEffects,null);
    ctx.failConvolver=false;await p.setReverb(.5);assert.equal(p.voice.source,source);assert.equal(p.voice.target,p.loopEffects.input);assert.equal(p.effectsError,'');
  } finally {p.destroy();}
});

test('pause while stream reverb context is resuming cancels delayed playback and retains a safe route',async()=>{
  const {p,ctx,audio}=setup();let release;
  try {
    await p.setReverb(.6);ctx.resume=()=>new Promise(resolve=>{release=resolve;});
    const loading=p.playTrack('a',['a']);await settle();p.pause();release();await loading;
    assert.equal(p.playing,false);assert.equal(audio.playCalls,0);assert.equal(p.autoplayRequested,false);
    ctx.resume=async()=>{};await p.resume();assert.equal(p.playing,true);assert.equal(ctx.mediaSources.length,1);
  } finally {release?.();p.destroy();}
});
