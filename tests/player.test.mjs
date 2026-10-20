import test from 'node:test';
import assert from 'node:assert/strict';
import { MusicPlayer } from '../public/player.js';
class FakeAudio extends EventTarget {
  constructor(){super();this.currentTime=0;this.paused=true;this.src='';}
  play(){this.paused=false;return Promise.resolve();}pause(){this.paused=true;}load(){this.currentTime=0;}removeAttribute(){this.src='';}
}
const makeBuffer=(channels,length,sampleRate)=>({numberOfChannels:channels,length,sampleRate,duration:length/sampleRate,data:Array.from({length:channels},()=>new Float32Array(length)),getChannelData(c){return this.data[c]},copyToChannel(v,c){this.data[c].set(v)}});
class FakeContext{
 constructor(){this.currentTime=0;this.destination={};this.sources=[];this.decoded=makeBuffer(2,1000,100);}
 resume(){return Promise.resolve()}close(){return Promise.resolve()}decodeAudioData(){return Promise.resolve(this.decoded)}
 createBuffer(...a){return makeBuffer(...a)}
 createGain(){return {gain:{value:1,setValueAtTime(){},linearRampToValueAtTime(){},setTargetAtTime(){}},connect(){},disconnect(){}}}
 createBufferSource(){const s={connect(){},disconnect(){},start(t,o){this.startTime=t;this.offset=o},stop(){this.stopped=true},finish(){this.onended?.()}};this.sources.push(s);return s;}
}
function setup(){const audio=new FakeAudio(),ctx=new FakeContext();const p=new MusicPlayer({audio,contextFactory:()=>ctx,fetch:async()=>({ok:true,arrayBuffer:async()=>new ArrayBuffer(10)})});const catalog=[{id:'a',title:'A',kind:'original',duration:10,audioUrl:'/audio/a.mp3'},{id:'b',title:'B',kind:'original',duration:20,audioUrl:'/audio/b.mp3'},{id:'loop',title:'Loop',kind:'loop',duration:8,loopStart:1,loopEnd:9,audioUrl:'/audio/loop.mp3'}];p.setCatalog(catalog);return {p,audio,ctx,catalog};}
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
