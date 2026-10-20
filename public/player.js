/** Audio transport: streamed originals and sample-accurate prepared loops. */
export class MusicPlayer extends EventTarget {
  constructor(deps = {}) {
    super();
    this.audio = deps.audio || new Audio();
    this.fetch = deps.fetch || window.fetch.bind(window);
    this.contextFactory = deps.contextFactory || (() => new (window.AudioContext || window.webkitAudioContext)({latencyHint: 'playback'}));
    this.context = null; this.gain = null; this.voice = null; this.buffer = null;
    this.catalog = new Map(); this.track = null; this.queue = []; this.originalQueue = [];
    this.playing = false; this.loading = false; this.error = ''; this.volume = .4;
    this.shuffle = false; this.repeat = 'off'; this.offset = 0; this.startedAt = 0;
    this.duration = 0; this.generation = 0; this.intent = 0; this.controller = null; this.autoplayRequested = false;
    this.audio.preload = 'metadata'; this.audio.volume = this.volume;
    this.audio.addEventListener('ended', () => { if (this.track?.kind !== 'loop' && !this.loading) this.advance(true); });
    this.audio.addEventListener('error', () => {
      if (this.track && this.track.kind !== 'loop' && !this.loading) { this.playing = false; this.error = 'This audio could not be played. Try another track or import it again.'; this.emit(); }
    });
    this.audio.addEventListener('timeupdate', () => { if (this.track?.kind !== 'loop') this.emit(); });
    this.audio.addEventListener('waiting', () => this.emit());
    this.timer = setInterval(() => { if (this.playing) this.emit(); }, 250);
    this.setupMediaSession();
  }
  setCatalog(tracks) {
    this.catalog = new Map(tracks.map(t => [t.id,t]));
    this.queue = this.queue.filter(id => this.catalog.has(id));
    this.originalQueue = this.originalQueue.filter(id => this.catalog.has(id));
    if (this.track && !this.catalog.has(this.track.id)) {
      ++this.generation; ++this.intent; this.controller?.abort(); this.controller = null;
      this.autoplayRequested = false; this.stopVoice(); this.buffer = null;
      this.track = null; this.playing = false; this.loading = false; this.error = '';
      this.offset = 0; this.duration = 0;
      this.audio.pause(); this.audio.removeAttribute('src'); this.audio.load();
      if (typeof navigator !== 'undefined' && navigator.mediaSession) navigator.mediaSession.metadata = null;
    }
    if (this.track && this.catalog.has(this.track.id)) {
      const previous = this.track;
      this.track = this.catalog.get(this.track.id);
      if (['title', 'artist', 'album'].some(field => previous[field] !== this.track[field])) this.setMetadata();
    }
    this.emit();
  }
  currentTime() {
    if (!this.track) return 0;
    if (this.track.kind !== 'loop') return Math.min(this.duration, Number(this.audio.currentTime) || 0);
    const t = this.offset + (this.playing && this.context ? Math.max(0,this.context.currentTime-this.startedAt) : 0);
    return this.repeat === 'one' && this.duration ? t % this.duration : Math.min(t,this.duration);
  }
  getState() { return {track:this.track,playing:this.playing,loading:this.loading,error:this.error,currentTime:this.currentTime(),duration:this.duration,volume:this.volume,shuffle:this.shuffle,repeat:this.repeat,queueIds:[...this.queue]}; }
  emit() {
    const state=this.getState();
    this.dispatchEvent(new CustomEvent('statechange',{detail:state}));
    if (typeof navigator !== 'undefined' && navigator.mediaSession) {
      navigator.mediaSession.playbackState = this.playing ? 'playing' : (this.track ? 'paused' : 'none');
      if (this.duration>0) { try {navigator.mediaSession.setPositionState({duration:this.duration,playbackRate:1,position:Math.max(0,Math.min(this.duration,state.currentTime))});} catch {} }
    }
  }
  shuffled(ids) {
    const out=[...ids];
    for(let i=out.length-1;i>0;i--){const j=Math.floor(Math.random()*(i+1));[out[i],out[j]]=[out[j],out[i]];}
    return out;
  }
  async playTrack(id,queueIds=[],options={}) {
    if(!this.catalog.has(id)) return;
    this.originalQueue=[...new Set(queueIds.filter(x=>this.catalog.has(x)))];
    if(!this.originalQueue.includes(id))this.originalQueue.unshift(id);
    this.queue=this.shuffle?[id,...this.shuffled(this.originalQueue.filter(x=>x!==id))]:[...this.originalQueue];
    if(options.repeatOne)this.repeat='one';else if(this.repeat==='one')this.repeat='off';
    return this.load(id);
  }
  stopVoice() {
    const v=this.voice;this.voice=null;
    if(v){v.source.onended=null;try{v.source.stop();}catch{}v.source.disconnect();v.envelope.disconnect();}
  }
  async load(id) {
    const track=this.catalog.get(id);if(!track)return;
    const generation=++this.generation;++this.intent;
    this.controller?.abort();const controller=new AbortController();this.controller=controller;this.autoplayRequested=true;
    this.audio.pause();this.audio.removeAttribute('src');this.audio.load();this.stopVoice();this.buffer=null;
    this.track=track;this.offset=0;this.duration=Number(track.duration)||0;this.playing=false;this.loading=true;this.error='';
    this.emit();this.setMetadata();
    try {
      if(track.kind==='loop') {
        if(!this.context){this.context=this.contextFactory();this.gain=this.context.createGain();this.gain.gain.value=this.volume;this.gain.connect(this.context.destination);}
        await this.context.resume();
        if(generation!==this.generation)return;
        const response=await this.fetch(track.audioUrl,{signal:controller.signal});if(!response.ok)throw new Error('Audio request failed');
        const bytes=await response.arrayBuffer();const decoded=await this.context.decodeAudioData(bytes);
        if(generation!==this.generation)return;
        const first=Math.round((Number(track.loopStart)||0)*decoded.sampleRate);
        const end=track.loopEnd==null?decoded.length:Math.round(Number(track.loopEnd)*decoded.sampleRate);
        if(first<0||end<=first||end>decoded.length+Math.round(decoded.sampleRate*.005))throw new Error('Invalid loop region');
        const last=Math.min(end,decoded.length);this.buffer=this.context.createBuffer(decoded.numberOfChannels,last-first,decoded.sampleRate);
        for(let c=0;c<decoded.numberOfChannels;c++)this.buffer.copyToChannel(decoded.getChannelData(c).subarray(first,last),c);
        this.duration=this.buffer.duration;
      } else {
        this.audio.src=track.audioUrl;this.audio.loop=this.repeat==='one';
      }
      if(generation!==this.generation)return;
      this.loading=false;if(this.autoplayRequested)await this.resume();else this.emit();
    }catch(error){
      if(generation!==this.generation||error.name==='AbortError')return;
      this.loading=false;this.playing=false;this.error='Couldn’t open this song. Check that its audio file is available, then try again.';this.emit();
    }
  }
  startVoice() {
    this.stopVoice();
    const source=this.context.createBufferSource(),envelope=this.context.createGain();
    source.buffer=this.buffer;source.loop=this.repeat==='one';source.loopStart=0;source.loopEnd=this.duration;
    envelope.gain.setValueAtTime(0,this.context.currentTime);envelope.gain.linearRampToValueAtTime(1,this.context.currentTime+.015);
    source.connect(envelope);envelope.connect(this.gain);this.startedAt=this.context.currentTime;
    const voice={source,envelope};this.voice=voice;
    source.onended=()=>{if(this.voice!==voice)return;this.voice=null;source.disconnect();envelope.disconnect();this.offset=this.duration;this.playing=false;this.advance(true);};
    source.start(this.startedAt,Math.max(0,Math.min(this.offset,this.duration-.001)));
  }
  async resume() {
    if(!this.track||this.loading)return;
    if(this.playing)return;
    if(this.track.kind==='loop'&&!this.buffer)return this.load(this.track.id);
    this.autoplayRequested=true;
    const intent=++this.intent,generation=this.generation;this.error='';
    try{
      if(this.track.kind==='loop'){
        if(!this.buffer)return;await this.context.resume();if(intent!==this.intent||generation!==this.generation)return;
        if(this.offset>=this.duration)this.offset=0;this.startVoice();this.playing=true;
      }else{
        if(this.audio.currentTime>=this.duration&&this.duration)this.audio.currentTime=0;
        const promise=this.audio.play();await promise;
        if(intent!==this.intent||generation!==this.generation)return;
        this.playing=true;
      }
    }catch(error){if(intent!==this.intent||generation!==this.generation)return;this.playing=false;this.error='Playback paused. Press Play to try again.';}
    this.emit();
  }
  async toggle(){if(!this.track){const ids=[...this.catalog.keys()];if(ids.length)return this.playTrack(ids[0],ids);}else if(this.loading){this.autoplayRequested=!this.autoplayRequested;this.emit();}else if(this.playing)this.pause();else return this.resume();}
  pause(){++this.intent;this.autoplayRequested=false;if(this.track?.kind==='loop'){this.offset=this.currentTime();this.stopVoice();}this.audio.pause();this.playing=false;this.emit();}
  async advance(automatic=false){
    if(!this.track)return;
    if(automatic&&this.repeat==='one'){this.seek(0);return this.resume();}
    const index=this.queue.indexOf(this.track.id);let next=index+1;
    if(next>=this.queue.length){
      if(this.repeat==='all'){
        if(this.shuffle&&this.queue.length>1){this.queue=[this.track.id,...this.shuffled(this.originalQueue.filter(x=>x!==this.track.id))];next=1;}else next=0;
      }else{this.pause();if(automatic){this.offset=this.duration;}this.emit();return;}
    }
    if(this.queue[next])return this.load(this.queue[next]);
  }
  next(){return this.advance(false);}
  previous(){
    if(!this.track)return;if(this.currentTime()>3){this.seek(0);return;}
    const index=this.queue.indexOf(this.track.id);let prev=index-1;
    if(prev<0){if(this.repeat==='all')prev=this.queue.length-1;else{this.seek(0);return;}}
    return this.load(this.queue[prev]);
  }
  seek(seconds){
    if(!this.track||this.loading)return;let t=Number(seconds);if(!Number.isFinite(t))return;
    t=Math.max(0,Math.min(t,this.duration));
    if(this.track.kind==='loop'){const wasPlaying=this.playing;this.stopVoice();this.offset=t===this.duration&&this.repeat==='one'?0:t;if(wasPlaying){if(t===this.duration&&this.repeat!=='one'){this.playing=false;this.advance(true);return;}this.startVoice();}}
    else this.audio.currentTime=t;
    this.emit();
  }
  setVolume(value){const v=Number(value);if(!Number.isFinite(v))return;this.volume=Math.max(0,Math.min(1,v));this.audio.volume=this.volume;if(this.gain)this.gain.gain.setTargetAtTime(this.volume,this.context.currentTime,.015);this.emit();}
  setShuffle(enabled){this.shuffle=!!enabled;const current=this.track?.id;this.queue=this.shuffle&&current?[current,...this.shuffled(this.originalQueue.filter(x=>x!==current))]:[...this.originalQueue];this.emit();}
  setRepeat(mode){if(!['off','one','all'].includes(mode))return;this.offset=this.currentTime();this.startedAt=this.context?.currentTime||0;this.repeat=mode;this.audio.loop=mode==='one';if(this.voice)this.voice.source.loop=mode==='one';this.emit();}
  setMetadata(){if(typeof navigator==='undefined'||!navigator.mediaSession||typeof MediaMetadata==='undefined')return;navigator.mediaSession.metadata=new MediaMetadata({title:this.track.title,artist:this.track.artist||'Unknown artist',album:this.track.album||'Stillroom'});}
  setupMediaSession(){if(typeof navigator==='undefined'||!navigator.mediaSession)return;const actions={play:()=>this.resume(),pause:()=>this.pause(),previoustrack:()=>this.previous(),nexttrack:()=>this.next(),seekto:e=>this.seek(e.seekTime),seekbackward:e=>this.seek(this.currentTime()-(e.seekOffset||10)),seekforward:e=>this.seek(this.currentTime()+(e.seekOffset||10))};for(const [name,handler]of Object.entries(actions))try{navigator.mediaSession.setActionHandler(name,handler);}catch{}}
  destroy(){++this.generation;++this.intent;this.controller?.abort();this.pause();this.stopVoice();clearInterval(this.timer);this.audio.removeAttribute('src');this.audio.load();this.context?.close();}
}
