import test from 'node:test';
import assert from 'node:assert/strict';
import { ListenTracker, RecommendationPlayer, RecommendationPanel, connectRecommendationPlayback } from '../public/recommendations.js';

import { MusicPlayer } from '../public/player.js';

class Audio extends EventTarget {
  constructor() { super(); this.currentTime = 0; this.duration = 100; this.playbackRate = 1; this.paused = true; this.src = ''; this.playCalls = 0; }
  async play() { this.paused = false; this.playCalls++; this.dispatchEvent(new Event('play')); this.dispatchEvent(new Event('playing')); }
  pause() { if (!this.paused) { this.paused = true; this.dispatchEvent(new Event('pause')); } }
  removeAttribute(name) { if (name === 'src') this.src = ''; }
  getAttribute(name) { return name === 'src' ? this.src : null; }
  load() { this.currentTime = 0; }
  event(type) { this.dispatchEvent(new Event(type)); }
}
const tick = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => { resolve=a; reject=b; }); return {promise, resolve, reject}; };
function fixture(handler = null) {
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
  const player = new RecommendationPlayer(deps);
  return {player, audio, requests, session, timers, stored, now: value => { now=value; }, advance(seconds, rate=1) { now += seconds * 1000; audio.currentTime += seconds * rate; audio.event('timeupdate'); }};
}

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
    main.setVolume(.3); assert.equal(f.player.session.status,'ready');
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


function panelFixture(api) {
  const nodes=new Map(), audio=new Audio(), timers=new Map(); let timer=0;
  const node=selector=>{if(!nodes.has(selector))nodes.set(selector,{value:'',textContent:'',innerHTML:'',disabled:false,hidden:false});return nodes.get(selector)};
  const root={addEventListener(){},querySelector:selector=>selector==='audio'?audio:node(selector),querySelectorAll:()=>[]};
  const panel=new RecommendationPanel({root,api,pauseMain(){},getVolume:()=>.4,refreshLibrary(){},schedule:fn=>{timers.set(++timer,fn);return timer},cancel:id=>timers.delete(id)});
  return {panel,node,timers};
}

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
