import test from 'node:test';
import assert from 'node:assert/strict';
import { ListenTracker, RecommendationPlayer, RecommendationPanel, connectRecommendationPlayback } from '../public/recommendations.js';

import { MusicPlayer } from '../public/player.js';
import { MEDIA_EFFECTS_UNSUPPORTED, NATIVE_MEDIA_REVERB_MESSAGE } from '../public/audio-effects.js';

class Audio extends EventTarget {
  constructor() { super(); this.currentTime = 0; this.duration = 100; this.playbackRate = 1; this.defaultPlaybackRate = 1; this.preservesPitch = true; this.webkitPreservesPitch = true; this.mozPreservesPitch = true; this.paused = true; this.src = ''; this.playCalls = 0; }
  async play() { this.paused = false; this.playCalls++; this.dispatchEvent(new Event('play')); this.dispatchEvent(new Event('playing')); }
  pause() { if (!this.paused) { this.paused = true; this.dispatchEvent(new Event('pause')); } }
  removeAttribute(name) { if (name === 'src') this.src = ''; }
  getAttribute(name) { return name === 'src' ? this.src : null; }
  load() { this.currentTime = 0; }
  event(type) { this.dispatchEvent(new Event(type)); }
}
const tick = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const timeRanges = ranges => ({ length: ranges.length, start: index => ranges[index][0], end: index => ranges[index][1] });
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => { resolve=a; reject=b; }); return {promise, resolve, reject}; };
function fixture(handler = null, options = {}) {
  let now = 0, sequence = 0, nextTimer = 0;
  const requests = [], timers = new Map(), stored = new Map(), audio = new Audio();
  const session = { id: 'session-1', candidateId: 'candidate-1', status: 'ready', completedListens: 0, track: { audioUrl: '/temporary/one.wav', title: 'One', duration: 100 } };
  const deps = { audio, now: () => now, id: () => `event-${++sequence}`, schedule: fn => { timers.set(++nextTimer, fn); return nextTimer; }, cancel: id => timers.delete(id), storage: { getItem: k => stored.get(k), setItem: (k,v) => stored.set(k,v), removeItem: k => stored.delete(k) }, api: async (path, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null; requests.push({path, body, options});
    if (handler) { const handled = handler(path, body, session); if (handled) return handled; }
    if (body?.type === 'skip') return { session: {...session, status: 'limbo'} };
    if (body?.type === 'complete') return { session: {...session, status: 'limbo', completedListens: 1} };
    if (body?.type === 'up') return { session: {...session, status: 'retained', track: {...session.track, audioUrl: '/audio/kept.wav'}} };
    if (body?.type === 'down') return { session: {...session, status: 'rejected'} };
    return {session: {...session}};
  }};
  const player = new RecommendationPlayer({...deps, ...options});
  return {player, audio, requests, session, timers, stored, now: value => { now=value; }, advance(seconds, rate=1) { now += seconds * 1000; audio.currentTime += seconds * rate; audio.event('timeupdate'); }};
}

test('recommendation acquisition snapshots native WebM Opus support for each new listen', async () => {
  const f = fixture();
  let capability = '';
  f.audio.canPlayType = type => { assert.equal(type, 'audio/webm; codecs="opus"'); return capability; };
  await f.player.listen('candidate-1');
  const first = f.requests.find(request => request.path.endsWith('/listen')).body;
  assert.equal(first.allowNativeOpus, false);
  capability = 'probably';
  await f.player.listen('candidate-1');
  const requests = f.requests.filter(request => request.path.endsWith('/listen'));
  assert.equal(requests[1].body.allowNativeOpus, true);
  assert.notEqual(requests[1].body.requestId, first.requestId);
  assert.equal(first.allowNativeOpus, false, 'the previous request snapshot remains unchanged');
});

test('listen tracker credits playback, not pause/buffering time, and respects rate', () => {
  let time=0; const t = new ListenTracker(() => time);
  t.setPlaying(true,0); time=10000; t.sample(10); t.setPlaying(false,10);
  time=100000; t.sample(10); assert.equal(t.seconds,10);
  t.setPlaying(true,10,2); time=120000; t.sample(50); assert.equal(t.seconds,50);
  t.setPlaying(false,50); time=200000; t.sample(50); assert.equal(t.seconds,50);
  assert.equal(t.completed(100),false);
});

test('completion requires 95 percent and is invalidated by seeks or unexplained jumps', () => {
  let time=0; const t = new ListenTracker(() => time); t.setPlaying(true,0);
  time=95000; t.sample(95); assert.equal(t.completed(100),true);
  t.seek(99); assert.equal(t.completed(100),false);
  t.reset(); t.setPlaying(true,0); time+=1000; t.sample(99); assert.equal(t.seconds,1); assert.equal(t.seeked,true);
});

test('native played coverage preserves media seconds across the captured mixed-rate browser trace', () => {
  let now=0, covered=0;
  const tracker=new ListenTracker(()=>now,()=>timeRanges(covered ? [[0,covered]] : []));
  const fallback=new ListenTracker(()=>now);
  tracker.reset(.5); fallback.reset(.5);
  // Chrome's media clock sometimes advances more than elapsed JS time at a
  // rate change. Capping every delta loses over 5% of this actual 2s listen.
  const trace=[['playing',8.4,0,.5],['timeupdate',266.8,.043493,.5],['ratechange',360.6,.097409,2],['timeupdate',532.9,.18599,2],['ratechange',712.2,.488478,1.5],['timeupdate',797.7,.652148,1.5],['timeupdate',1071.2,1.123766,1.5],['timeupdate',1329.6,1.51137,1.5],['timeupdate',1596.7,1.911982,1.5],['timeupdate',1678.9,2,1.5],['pause',1679,2,1.5]];
  for (const [event,at,position,rate] of trace) {
    now=at; covered=position;
    for (const t of [tracker,fallback]) {
      if (event==='timeupdate') t.sample(position);
      else t.setPlaying(event!=='pause',position,rate);
    }
  }
  assert.ok(fallback.seconds<1.9,'captured timing reproduces the old undercount');
  assert.equal(tracker.seconds,2); assert.equal(tracker.completed(2),true); assert.equal(tracker.seeked,false);
  assert.equal(tracker.snapshot(2).playbackRate,2);
});

test('native played coverage recovers a late pause position without adding paused or buffering time', () => {
  let now=0, covered=0;
  const t=new ListenTracker(()=>now,()=>timeRanges(covered ? [[0,covered]] : [])); t.reset(2);
  now=6.8; t.setPlaying(true,0,2);
  now=209.5; covered=.04781; t.setPlaying(false,covered);
  const paused=t.seconds;
  now=860.8; t.sample(covered); assert.equal(t.seconds,paused);
  // The resumed native media clock exposes audio already confirmed as played.
  now=860.9; covered=.455021; t.setPlaying(true,covered,2); assert.equal(t.seconds,covered);
  for (const [at,position] of [[1115.9,.569319],[1386.6,1.112881],[1647.8,1.643713],[1806.3,2]]) { now=at; covered=position; t.sample(position); }
  assert.equal(t.seconds,2); assert.equal(t.completed(2),true); assert.equal(t.seeked,false);
  t.setPlaying(false,2); now+=600000; t.sample(2); assert.equal(t.seconds,2);
  t.reset(.5); covered=0; t.setPlaying(true,0,.5);
  now+=1000; covered=.25; t.setPlaying(false,.25); // waiting
  now+=600000; t.sample(.25); assert.equal(t.seconds,.25); assert.equal(t.completed(2),false);
});

test('native played ranges do not bypass seek invalidation or the 95 percent completion requirement', async () => {
  const f=fixture(); let ranges=[]; Object.defineProperty(f.audio,'played',{get:()=>timeRanges(ranges)});
  await f.player.listen('candidate-1'); f.now(94000); f.audio.currentTime=94; ranges=[[0,94]]; f.audio.event('timeupdate');
  assert.equal(f.player.tracker.completed(100),false);
  assert.equal(f.requests.filter(request=>request.body?.type==='complete').length,0);
  // Even confirmed coverage above 95% cannot turn an explicit seek into a listen.
  f.audio.currentTime=100; ranges=[[0,100]]; f.audio.event('seeking'); f.audio.event('ended'); await tick();
  assert.equal(f.requests.at(-1).body.type,'skip'); assert.equal(f.requests.at(-1).body.seeked,true);
  ranges=[]; await f.player.listen('candidate-1');
  assert.equal(f.player.tracker.seconds,0); assert.equal(f.player.tracker.seeked,false);
});

test('acquisition happens only on Listen, and repeated clicks create one request', async () => {
  const d=deferred(), f=fixture((path,body) => path.endsWith('/listen') ? d.promise : null);
  assert.equal(f.requests.length,0); const first=f.player.listen('candidate-1'); await f.player.listen('candidate-1');
  assert.equal(f.requests.length,1); d.resolve({session:f.session}); await first;
  assert.equal(f.audio.src,'/temporary/one.wav'); assert.equal(f.audio.playCalls,1); assert.equal(f.stored.size,1);
});

test('natural ended reports one completion and duplicate ended events cannot count twice', async () => {
  const f=fixture(); await f.player.listen('candidate-1');
  for(let i=0;i<20;i++) f.advance(5);
  f.audio.event('ended'); f.audio.event('ended'); await tick();
  const events=f.requests.filter(r=>r.body?.type==='complete'); assert.equal(events.length,1);
  assert.equal(events[0].body.listenedSeconds,100); assert.equal(events[0].body.playbackRate,1);
  assert.equal(f.player.session.status,'limbo'); assert.equal(f.audio.src,''); assert.equal(f.stored.size,0);
});

test('seeking or ending without real playback sends neutral skip', async () => {
  const f=fixture(); await f.player.listen('candidate-1'); f.advance(10); f.audio.currentTime=100; f.audio.event('seeking'); f.audio.event('ended'); await tick();
  assert.equal(f.requests.at(-1).body.type,'skip'); assert.equal(f.requests.at(-1).body.seeked,true);
  assert.equal(f.player.session.completedListens,0);
});

test('playing at double speed sends media seconds with playbackRate', async () => {
  const f=fixture(); await f.player.listen('candidate-1'); f.audio.playbackRate=2; f.audio.event('ratechange');
  f.advance(50,2); f.audio.event('ended'); await tick();
  assert.equal(f.requests.at(-1).body.type,'complete'); assert.equal(f.requests.at(-1).body.listenedSeconds,100); assert.equal(f.requests.at(-1).body.playbackRate,2);
});

test('half-speed recommendation listening counts a natural full listen with the chosen pitch mode', async () => {
  const f=fixture(); f.player.setPlaybackRate(.5); f.player.setPitchChanges(false);
  await f.player.listen('candidate-1');
  assert.equal(f.audio.playbackRate,.5); assert.equal(f.audio.defaultPlaybackRate,.5);
  assert.equal(f.audio.preservesPitch,true); assert.equal(f.audio.webkitPreservesPitch,true); assert.equal(f.audio.mozPreservesPitch,true);
  for(let i=0;i<40;i++) f.advance(5,.5);
  assert.equal(f.requests.filter(r=>r.body?.type==='complete').length,0);
  f.audio.event('ended'); await tick();
  const event=f.requests.at(-1).body;
  assert.equal(event.type,'complete'); assert.equal(event.listenedSeconds,100); assert.equal(event.playbackRate,.5); assert.equal(event.peakRate,.5); assert.equal(event.seeked,false);
});

test('rate and pitch changes sample the old rate without seeking, restarting, or finishing playback', async () => {
  const f=fixture(); let beforePlay=0; f.player.beforePlay=()=>beforePlay++;
  await f.player.listen('candidate-1');
  const requests=f.requests.length, generation=f.player.generation, intent=f.player.playIntent, playCalls=f.audio.playCalls, before=beforePlay;
  f.now(20000); f.audio.currentTime=20; f.player.setPlaybackRate(2);
  assert.equal(f.player.tracker.seconds,20);
  f.now(35000); f.audio.currentTime=50; f.player.setPlaybackRate(.5);
  assert.equal(f.player.tracker.seconds,50);
  f.now(55000); f.audio.currentTime=60; f.player.setPitchChanges(false);
  assert.equal(f.player.tracker.seconds,60); assert.equal(f.audio.preservesPitch,true);
  f.player.setPitchChanges(true); assert.equal(f.audio.preservesPitch,false);
  assert.equal(f.audio.currentTime,60); assert.equal(f.audio.src,'/temporary/one.wav'); assert.equal(f.audio.paused,false);
  assert.equal(f.audio.playCalls,playCalls); assert.equal(beforePlay,before); assert.equal(f.requests.length,requests);
  assert.equal(f.player.generation,generation); assert.equal(f.player.playIntent,intent); assert.equal(f.player.tracker.seeked,false);
  f.now(135000); f.audio.currentTime=100; f.audio.event('ended'); await tick();
  assert.equal(f.requests.at(-1).body.type,'complete'); assert.equal(f.requests.at(-1).body.listenedSeconds,100); assert.equal(f.requests.at(-1).body.playbackRate,2); assert.equal(f.requests.at(-1).body.seeked,false);
});

test('recommendation settings are bounded and survive metadata and kept audio source changes', async () => {
  const f=fixture();
  assert.equal(f.player.pitchChanges,true); assert.equal(f.audio.preservesPitch,false);
  f.player.setPlaybackRate(10); assert.equal(f.audio.playbackRate,2);
  f.player.setPlaybackRate(-1); assert.equal(f.audio.playbackRate,.5);
  f.player.setPlaybackRate(NaN); assert.equal(f.audio.playbackRate,1);
  f.player.setPlaybackRate(.75); f.player.setPitchChanges(false); await f.player.listen('candidate-1');
  f.advance(20,.75); f.audio.playbackRate=1; f.audio.preservesPitch=false; f.audio.event('loadedmetadata');
  assert.equal(f.audio.playbackRate,.75); assert.equal(f.audio.preservesPitch,true);
  await f.player.finish('up');
  assert.equal(f.audio.src,'/audio/kept.wav'); assert.equal(f.audio.playbackRate,.75); assert.equal(f.audio.defaultPlaybackRate,.75); assert.equal(f.audio.currentTime,15); assert.equal(f.audio.paused,false);
});

test('thumbs-up retains the track, updates its URL, and resumes at the same position', async () => {
  const f=fixture(); await f.player.listen('candidate-1'); f.advance(20); await f.player.finish('up');
  assert.equal(f.player.session.status,'retained'); assert.equal(f.audio.src,'/audio/kept.wav'); assert.equal(f.audio.currentTime,20); assert.equal(f.audio.paused,false);
  await f.player.finish('down'); assert.equal(f.player.session.status,'rejected'); assert.equal(f.audio.src,'');
});

test('switching playback while thumbs-up is saving cannot resume the old recommendation', async () => {
  const d=deferred(), f=fixture((path,body,session)=>body?.type==='up'?d.promise:null);
  await f.player.listen('candidate-1'); const saving=f.player.finish('up'); await tick(); f.player.interrupt();
  d.resolve({session:{...f.session,status:'retained',track:{...f.session.track,audioUrl:'/kept.wav'}}}); await saving;
  assert.equal(f.audio.paused,true); assert.equal(f.audio.playCalls,1); assert.equal(f.player.session.status,'retained');
  const count=f.requests.length; f.player.interrupt(); await tick(); assert.equal(f.requests.length,count);
});

test('a cancelled acquisition response is closed and never starts audio', async () => {
  const d=deferred(), f=fixture((path)=>path.endsWith('/listen')?d.promise:null);
  const listening=f.player.listen('candidate-1'); f.player.interrupt(); d.resolve({session:f.session}); await listening; await tick();
  assert.equal(f.audio.playCalls,0); assert.equal(f.requests.at(-1).body.type,'skip'); assert.equal(f.requests.at(-1).options.keepalive,true);
});

test('cancelled preparation poll cannot resurrect the session', async () => {
  const poll=deferred(); let loads=0;
  const f=fixture((path,body,session)=>path.endsWith('/listen')?Promise.resolve({session:{...session,status:'preparing'}}):!body?(loads++,poll.promise):null);
  await f.player.listen('candidate-1'); const callback=[...f.timers.values()][0]; const polling=callback(); await tick();
  await f.player.finish('skip'); poll.resolve({session:f.session}); await polling;
  assert.equal(loads,1); assert.equal(f.player.session.status,'limbo'); assert.equal(f.audio.playCalls,0);
});

test('failed feedback retries the identical event id and blocks overlapping actions', async () => {
  let attempts=0; const f=fixture((path,body,session)=>body?.type==='down'&&++attempts===1?Promise.reject(new Error('Offline')):null);
  await f.player.listen('candidate-1'); await f.player.finish('down'); assert.ok(f.player.pendingAction); assert.match(f.player.error,/not been confirmed/);
  const id=f.requests.at(-1).body.eventId; await f.player.finish('down'); assert.equal(f.requests.at(-1).body.eventId,id); assert.equal(f.player.pendingAction,null); assert.equal(f.player.session.status,'rejected');
});

test('pagehide abandons with keepalive; reload recovery never counts a completion', async () => {
  const f=fixture(); await f.player.listen('candidate-1'); f.advance(20); f.player.pagehide(); await tick();
  assert.equal(f.audio.src,''); assert.equal(f.requests.at(-1).options.keepalive,true); assert.equal(f.requests.at(-1).body.type,'skip');
  await f.player.recover(); assert.equal(f.requests.at(-1).body.type,'skip'); assert.equal(f.stored.size,0);
});

test('progress is ordered before a terminal feedback request', async () => {
  const d=deferred(), f=fixture((path,body)=>body?.type==='progress'?d.promise:null);
  await f.player.listen('candidate-1'); f.advance(5); const progress=f.player.progress(); await tick(); const ending=f.player.finish('down'); await tick();
  assert.equal(f.requests.at(-1).body.type,'progress'); d.resolve({session:f.session}); await progress; await ending;
  assert.equal(f.requests.at(-1).body.type,'down'); assert.equal(f.player.session.status,'rejected');
});


test('browser timer APIs are invoked without the player as their receiver', () => {
  let cancelled=false, scheduled=false;
  const player=new RecommendationPlayer({ audio:new Audio(), api:async()=>({}), id:()=> 'id', schedule:function () { assert.equal(this,undefined); scheduled=true; return 1; }, cancel:function () { assert.equal(this,undefined); cancelled=true; } });
  player.stopAudio(); player.scheduleProgress(0);
  assert.equal(cancelled,true); assert.equal(scheduled,true);
});


test('pausing a still-loading main loop does not cancel the chosen recommendation', async () => {
  const pending = deferred(), f=fixture();
  const main = new MusicPlayer({audio:new Audio(),fetch:()=>pending.promise,contextFactory:()=>({resume:async()=>{},close:()=>{},createGain:()=>({gain:{value:1,setTargetAtTime(){}},connect(){}})})});
  const disconnect=connectRecommendationPlayback(main,f.player,()=>{});
  f.player.beforePlay=()=>main.pause();
  try {
    main.setCatalog([{id:'loop',kind:'loop',audioUrl:'/loop.wav',duration:100}]);
    const loading=main.playTrack('loop',['loop']); await tick(); assert.equal(main.loading,true);
    await f.player.listen('candidate-1'); assert.equal(f.audio.paused,false); assert.equal(f.player.session.status,'ready'); assert.equal(main.autoplayRequested,false);
    main.setVolume(.3); main.setPlaybackRate(.5); main.setPitchChanges(false);
    assert.equal(f.player.session.status,'ready'); assert.equal(f.audio.paused,false);
    pending.resolve({ok:false}); await loading; assert.equal(f.audio.paused,false);
    main.setCatalog([{id:'original',kind:'original',audioUrl:'/owned.wav',duration:100}]);
    await main.playTrack('original',['original']); await tick(); assert.equal(f.audio.paused,true); assert.equal(f.player.session.status,'limbo');
  } finally { disconnect(); main.destroy(); }
});

test('lowering playback speed and losing a progress report cannot invalidate actual listening', async () => {
  const f=fixture((path,body)=>body?.type==='progress'?Promise.reject(new Error('Offline')):null);
  await f.player.listen('candidate-1'); f.audio.playbackRate=2; f.audio.event('ratechange'); f.advance(25,2);
  await f.player.progress(); f.audio.playbackRate=1; f.audio.event('ratechange'); f.advance(50);
  f.audio.event('ended'); await tick();
  assert.equal(f.requests.at(-1).body.type,'complete'); assert.equal(f.requests.at(-1).body.listenedSeconds,100); assert.equal(f.requests.at(-1).body.playbackRate,2);
});


function panelFixture(api, settings = {}) {
  const nodes=new Map(), audio=new Audio(), timers=new Map(); let timer=0;
  const node=selector=>{if(!nodes.has(selector))nodes.set(selector,{value:'',textContent:'',innerHTML:'',disabled:false,hidden:false});return nodes.get(selector)};
  const listeners=new Map(), root={addEventListener(name,handler){listeners.set(name,handler)},querySelector:selector=>selector==='audio'?audio:node(selector),querySelectorAll:()=>[]};
  const speedControlFactory=options=>({options,setPlaybackSettings({playbackRate,pitchChanges}){node('#recommendation-speed').value=String(playbackRate);node('#recommendation-pitch-changes').checked=pitchChanges;},destroy(){}});
  const panel=new RecommendationPanel({root,api,pauseMain(){},getVolume:()=>.4,refreshLibrary(){},speedControlFactory,schedule:fn=>{timers.set(++timer,fn);return timer},cancel:id=>timers.delete(id),...settings});
  return {panel,node,timers,listeners};
}

test('recommendation controls reflect shared settings and route changes to the global player', () => {
  const changes=[], f=panelFixture(async()=>({}),{playbackRate:.75,pitchChanges:false,onPlaybackRateChange:value=>changes.push(['rate',value]),onPitchChangesChange:value=>changes.push(['pitch',value])});
  f.panel.renderPlayer();
  assert.equal(f.node('#recommendation-speed').value,'0.75'); assert.equal(f.node('#recommendation-pitch-changes').checked,false);
  f.panel.speedControl.options.onPlaybackRateChange(1.5);
  f.panel.speedControl.options.onPitchChangesChange(true);
  assert.deepEqual(changes,[['rate',1.5],['pitch',true]]);
  f.panel.setPlaybackSettings({playbackRate:1.5,pitchChanges:true});
  assert.equal(f.node('#recommendation-speed').value,'1.5'); assert.equal(f.node('#recommendation-pitch-changes').checked,true);
  assert.equal(f.panel.player.audio.playbackRate,1.5); assert.equal(f.panel.player.audio.preservesPitch,false);
});

test('library deletion stops retained recommendation audio and refreshes metadata without feedback', async () => {
  const requests=[], retained={id:'saved-session',candidateId:'candidate-1',status:'retained',track:{id:'saved-track',audioUrl:'/audio/saved.wav',title:'Saved'},candidate:{id:'candidate-1',rating:'up'}};
  const f=panelFixture(async (path, options={}) => {
    requests.push({path,options});
    return path.startsWith('/api/recommendation-sessions/') ? {session:{...retained,status:'limbo',track:null,message:'Deleted from your library.'}} : {candidates:[],limbo:[],refresh:{status:'idle'}};
  });
  f.panel.setLibrary([retained.track]);
  f.panel.player.session=retained; f.panel.player.active=true; f.panel.player.audio.src=retained.track.audioUrl;
  await f.panel.player.audio.play(); f.panel.setLibrary([]); await tick();
  assert.equal(f.panel.player.audio.paused,true); assert.equal(f.panel.player.audio.src,'');
  assert.equal(f.panel.player.session.status,'limbo'); assert.equal(f.panel.player.session.track,null);
  assert.equal(f.panel.player.session.candidate.rating,'up'); assert.equal(f.panel.player.active,false); assert.equal(f.panel.player.busy,false);
  assert.ok(requests.some(request=>request.path==='/api/recommendation-sessions/saved-session'));
  assert.ok(requests.some(request=>request.path==='/api/recommendations'));
  assert.ok(requests.every(request=>!request.options.method));
  assert.match(f.node('#recommendation-now-status').textContent,/Deleted/);
});

test('library deletion leaves a temporary recommendation preview playing', async () => {
  const f=fixture(); f.player.setLibrary([{id:'unrelated'}]); await f.player.listen('candidate-1');
  const count=f.requests.length; f.player.setLibrary([]); await tick();
  assert.equal(f.player.session.status,'ready'); assert.equal(f.audio.src,'/temporary/one.wav'); assert.equal(f.audio.paused,false);
  assert.equal(f.requests.length,count);
});

test('deleting another library song preserves retained recommendation playback', async () => {
  const f=fixture((path,body,session)=>path.endsWith('/listen')?Promise.resolve({session:{...session,status:'retained',track:{...session.track,id:'kept',audioUrl:'/audio/kept.wav'}}}):null);
  f.player.setLibrary([{id:'kept'},{id:'unrelated'}]); await f.player.listen('candidate-1');
  f.player.setLibrary([{id:'kept'}]);
  assert.equal(f.player.session.status,'retained'); assert.equal(f.audio.src,'/audio/kept.wav'); assert.equal(f.audio.paused,false);
});

test('a retained listen response arriving after library deletion never starts audio', async () => {
  const d=deferred(), f=fixture(path=>path.endsWith('/listen')?d.promise:null);
  f.player.setLibrary([{id:'deleted'}]); const listening=f.player.listen('candidate-1');
  f.player.setLibrary([]);
  d.resolve({session:{...f.session,status:'retained',track:{...f.session.track,id:'deleted',audioUrl:'/audio/deleted.wav'}}});
  await listening; await tick();
  assert.equal(f.audio.playCalls,0); assert.equal(f.audio.src,''); assert.equal(f.player.session.status,'limbo');
  assert.equal(f.player.busy,false); assert.equal(f.stored.size,0);
  assert.equal(f.requests.filter(request=>request.body?.type).length,0);
});

test('a delayed keep response cannot replay a track deleted while its response was pending', async () => {
  const d=deferred(), f=fixture((path,body)=>body?.type==='up'?d.promise:null);
  f.player.setLibrary([{id:'deleted'}]); await f.player.listen('candidate-1');
  const saving=f.player.finish('up'); await tick(); f.player.setLibrary([]);
  d.resolve({session:{...f.session,status:'retained',track:{...f.session.track,id:'deleted',audioUrl:'/audio/deleted.wav'}}});
  await saving; await tick();
  assert.equal(f.audio.playCalls,1); assert.equal(f.audio.paused,true); assert.equal(f.audio.src,'');
  assert.equal(f.player.session.status,'limbo'); assert.equal(f.player.busy,false); assert.equal(f.player.pendingAction,null);
  assert.deepEqual(f.requests.filter(request=>request.body?.type).map(request=>request.body.type),['up']);
});

test('a newly retained recommendation can play before the next library refresh', async () => {
  const f=fixture((path,body,session)=>body?.type==='up'?Promise.resolve({session:{...session,status:'retained',track:{...session.track,id:'new-track',audioUrl:'/audio/new.wav'}}}):null);
  f.player.setLibrary([]); await f.player.listen('candidate-1'); await f.player.finish('up');
  assert.equal(f.player.session.status,'retained'); assert.equal(f.audio.paused,false); assert.equal(f.audio.src,'/audio/new.wav');
  f.player.setLibrary([{id:'new-track'}]); assert.equal(f.audio.paused,false);
});

test('server working refresh status keeps polling and uses the chosen library seed', async () => {
  const requests=[];
  const f=panelFixture(async (path,options)=>{requests.push({path,options});return requests.length===1?{candidates:[],limbo:[],refresh:{status:'working',message:'Finding metadata'}}:{candidates:[{id:'new',title:'A song',artist:'Artist',reasons:['Related to a library song']}],limbo:[],refresh:{status:'complete'}};});
  f.node('#recommendation-seed').value='owned-song'; await f.panel.load(true);
  assert.equal(JSON.parse(requests[0].options.body).seedId,'owned-song'); assert.equal(f.node('#recommendation-refresh').disabled,true); assert.equal(f.timers.size,1);
  await [...f.timers.values()][0]();
  assert.equal(f.node('#recommendation-refresh').disabled,false); assert.match(f.node('#recommendation-list').innerHTML,/A song/); assert.match(f.node('#recommendation-list').innerHTML,/No audio stored/);
});

test('a page closing during metadata refresh does not restart refresh polling', async () => {
  const d=deferred(), f=panelFixture(()=>d.promise); const loading=f.panel.load(); f.panel.pagehide();
  d.resolve({refresh:{status:'working'},candidates:[],limbo:[]}); await loading; assert.equal(f.timers.size,0);
});


test('thumbs-up after a completed listen reacquires and keeps without autoplaying', async () => {
  let keeping=false, libraryUpdates=0;
  const f=fixture((path,body,session)=>{
    if(body?.type==='up') { keeping=true; return Promise.resolve({session:{...session,status:'preparing',track:null}}); }
    if(!body&&keeping) return Promise.resolve({session:{...session,status:'retained',track:{...session.track,audioUrl:'/kept.wav'}}});
    return null;
  });
  f.player.onLibraryChange=()=>libraryUpdates++;
  await f.player.listen('candidate-1'); await f.player.finish('skip'); assert.equal(f.player.session.status,'limbo');
  await f.player.finish('up'); assert.equal(f.player.session.status,'preparing'); assert.equal(f.player.retainOnly,true); assert.equal(f.stored.size,0);
  await [...f.timers.values()].at(-1)(); assert.equal(f.player.session.status,'retained'); assert.equal(f.audio.playCalls,1); assert.equal(f.audio.paused,true); assert.equal(libraryUpdates,1);
});

test('closing after an acknowledged post-listen keep does not cancel that acquisition', async () => {
  const f=fixture((path,body,session)=>body?.type==='up'?Promise.resolve({session:{...session,status:'preparing',track:null}}):null);
  await f.player.listen('candidate-1'); await f.player.finish('skip'); await f.player.finish('up');
  const requests=f.requests.length; f.player.pagehide(); await tick(); assert.equal(f.requests.length,requests); assert.equal(f.stored.size,0);
});


test('retrying an interrupted keep retrieves the retained song without starting playback', async () => {
  const f=fixture((path,body,session)=>path.endsWith('/listen')?Promise.resolve({session:{...session,status:'retained',track:{...session.track,audioUrl:'/kept.wav'}}}):null);
  await f.player.listen('candidate-1',{retainOnly:true}); assert.equal(f.player.session.status,'retained'); assert.equal(f.audio.playCalls,0); assert.equal(f.player.active,false); assert.equal(f.stored.size,0);
});


test('recommendation provenance links use the existing YouTube URL allowlist', () => {
  const f=panelFixture(async()=>({}));
  const card=f.panel.card({id:'one',title:'A <song>',artist:'Artist',sourceUrl:'https://youtu.be/abcdefghijk'});
  assert.match(card,/href="https:\/\/www.youtube.com\/watch\?v=abcdefghijk"/); assert.match(card,/rel="noopener noreferrer"/); assert.match(card,/A &lt;song&gt;/);
  assert.doesNotMatch(f.panel.card({id:'two',title:'Unsafe',sourceUrl:'javascript:alert(1)'}),/href=/);
});


test('choosing Listen pauses other playback before acquisition can race main-track advancement', async () => {
  const d=deferred(), f=fixture(path=>path.endsWith('/listen')?d.promise:null); let mainPaused=false;
  f.player.beforePlay=()=>{mainPaused=true}; const loading=f.player.listen('candidate-1');
  assert.equal(mainPaused,true); assert.equal(f.audio.playCalls,0);
  d.resolve({session:f.session}); await loading; assert.equal(f.audio.playCalls,1);
});

test('recommendation route prepares initial and retained playback without restarting for effect changes', async () => {
  const calls=[], route={setReverb:async value=>{calls.push(['reverb',value]);return true;},setVolume:value=>calls.push(['volume',value]),prepare:async()=>{calls.push(['prepare']);},destroy:()=>calls.push(['destroy'])};
  const f=fixture(null,{audioRoute:route});
  await f.player.listen('candidate-1');
  assert.equal(calls.filter(([name])=>name==='prepare').length,1);
  const playCalls=f.audio.playCalls, position=f.audio.currentTime;
  f.player.setVolume(.3); await f.player.setReverb(.65);
  assert.equal(f.audio.playCalls,playCalls); assert.equal(f.audio.currentTime,position);
  assert.equal(f.player.reverb,.65); assert.equal(f.player.effectsError,'');
  assert.deepEqual(calls.filter(([name])=>name==='volume'),[['volume',.3]]);
  await f.player.finish('up');
  assert.equal(calls.filter(([name])=>name==='prepare').length,2);
  assert.equal(f.audio.paused,false); assert.equal(f.player.reverb,.65);
  f.player.destroy(); assert.deepEqual(calls.at(-1),['destroy']);
});

test('a captured recommendation route can recover on Play after a suspended-context failure', async () => {
  let attempts=0;
  const f=fixture(null,{audioRoute:{setReverb:async()=>true,setVolume(){},prepare:async()=>{if(++attempts===1)throw new Error('context suspended');},destroy(){}}});
  await f.player.listen('candidate-1');
  assert.equal(f.audio.paused,true); assert.match(f.player.error,/Press Play/);
  await f.player.resume();
  assert.equal(f.audio.paused,false); assert.equal(attempts,2); assert.equal(f.player.error,'');
  assert.equal(f.requests.filter(request=>request.path.endsWith('/listen')).length,1);
  assert.equal(f.requests.filter(request=>request.body?.type==='complete').length,0);
});

test('interrupting a recommendation while its audio route prepares cannot start or credit it later', async () => {
  const d=deferred();
  const f=fixture(null,{audioRoute:{setReverb:async()=>true,setVolume(){},prepare:()=>d.promise,destroy(){}}});
  const listening=f.player.listen('candidate-1');await tick();
  assert.equal(f.player.session.status,'ready'); assert.equal(f.player.busy,true);
  f.player.interrupt();d.resolve();await listening;await tick();
  assert.equal(f.audio.playCalls,0); assert.equal(f.audio.paused,true);
  assert.equal(f.requests.filter(request=>request.body?.type==='skip').length,1);
  assert.equal(f.requests.filter(request=>request.body?.type==='complete').length,0);
});

test('effect changes leave recommendation natural-listen counting and seek exclusion intact', async () => {
  const route={setReverb:async()=>true,setVolume(){},prepare:async()=>{},destroy(){}},f=fixture(null,{audioRoute:route});
  await f.player.listen('candidate-1');f.advance(50);await f.player.setReverb(1);
  f.player.setPitchChanges(false);f.advance(50);f.audio.event('ended');f.audio.event('ended');await tick();
  assert.equal(f.requests.filter(request=>request.body?.type==='complete').length,1);
  await f.player.listen('candidate-1');await f.player.setReverb(0);f.audio.currentTime=99;f.audio.event('seeking');f.audio.event('ended');await tick();
  assert.equal(f.requests.filter(request=>request.body?.type==='complete').length,1);
  assert.equal(f.requests.at(-1).body.type,'skip');
});

test('a stale effect failure cannot override a newer disabled-effect choice', async () => {
  const d=deferred();let calls=0;
  const f=fixture(null,{audioRoute:{setReverb:()=>++calls===2?d.promise:Promise.resolve(true),setVolume(){},prepare:async()=>{},destroy(){}}});
  const old=f.player.setReverb(.8);await f.player.setReverb(0);d.resolve(false);await old;
  assert.equal(f.player.reverb,0);assert.equal(f.player.effectsError,'');
});

test('unsupported recommendation reverb keeps native listening, retry and completion counting intact', async () => {
  const error={code:MEDIA_EFFECTS_UNSUPPORTED},route={error,setReverb:async value=>value===0,prepare:async()=>false,setVolume(){},destroy(){}};
  const f=fixture(null,{audioRoute:route,reverb:1,playbackRate:1.37,pitchChanges:false});
  await f.player.listen('candidate-1');
  assert.equal(f.player.effectsError,NATIVE_MEDIA_REVERB_MESSAGE);
  assert.equal(f.player.error,'');assert.equal(f.audio.paused,false);
  assert.equal(f.audio.playbackRate,1.37);assert.equal(f.audio.preservesPitch,true);
  f.audio.pause();await f.player.resume();
  assert.equal(f.audio.paused,false);assert.equal(f.player.effectsError,NATIVE_MEDIA_REVERB_MESSAGE);
  assert.equal(f.requests.filter(request=>request.path.endsWith('/listen')).length,1);
  await f.player.setReverb(.5);
  assert.equal(f.player.effectsError,NATIVE_MEDIA_REVERB_MESSAGE);
  f.advance(100/1.37,1.37);f.audio.event('ended');f.audio.event('ended');await tick();
  assert.equal(f.requests.filter(request=>request.body?.type==='complete').length,1);
  await f.player.setReverb(0);assert.equal(f.player.effectsError,'');
  f.player.destroy();
});

test('recommendation panel retains the specific native-reverb compatibility notice', () => {
  let reportEffect;
  const f=panelFixture(async()=>({}),{reverb:1,createMediaRoute:(audio,onError)=>{
    reportEffect=onError;
    return {setReverb:async()=>true,prepare:async()=>false,setVolume(){},destroy(){}};
  }});
  reportEffect({code:MEDIA_EFFECTS_UNSUPPORTED});
  assert.equal(f.panel.player.effectsError,NATIVE_MEDIA_REVERB_MESSAGE);
  assert.equal(f.node('#recommendation-now-status').textContent,NATIVE_MEDIA_REVERB_MESSAGE);
  f.panel.player.destroy();
});
