/** Audio transport: streamed originals and sample-accurate prepared loops. */
import { MediaAudioRoute, ReverbBus, MEDIA_EFFECTS_UNSUPPORTED, NATIVE_MEDIA_REVERB_MESSAGE } from './audio-effects.js';

export class MusicPlayer extends EventTarget {
  constructor(deps = {}) {
    super();
    this.audio = deps.audio || new Audio();
    this.fetch = deps.fetch || window.fetch.bind(window);
    this.contextFactory = deps.contextFactory || (() => new (window.AudioContext || window.webkitAudioContext)({latencyHint: 'playback'}));
    this.stretchModule = null;
    this.prepareStretch = deps.prepareStretch || (() => {
      if(!this.stretchModule)this.stretchModule=this.context.audioWorklet.addModule(new URL('./loop-stretch.js',import.meta.url)).catch(error=>{this.stretchModule=null;throw error;});
      return this.stretchModule;
    });
    this.stretchNodeFactory = deps.stretchNodeFactory || ((context,options) => new AudioWorkletNode(context,'stillroom-loop-stretch',options));
    this.playbackRate = 1; this.pitchChanges = true;
    this.reverb = 0; this.effectsError = ''; this.reverbRevision = 0; this.loopEffects = null;
    this.context = null; this.gain = null; this.voice = null; this.buffer = null;
    // Exactly one retained PCM owner: native AudioBuffer, pending standalone
    // channels, or a reusable worklet. Worklet messages transfer, never clone it.
    this.pcm = null; this.stretchSource = null;
    this.catalog = new Map(); this.track = null; this.queue = []; this.originalQueue = [];
    this.playing = false; this.loading = false; this.error = ''; this.volume = .4;
    this.shuffle = false; this.repeat = 'off'; this.offset = 0; this.startedAt = 0;
    this.duration = 0; this.generation = 0; this.intent = 0; this.controller = null; this.autoplayRequested = false;
    this.audio.preload = 'metadata'; this.audio.volume = this.volume;
    this.streamRoute = deps.mediaRoute || this.createMediaRoute(this.audio,error=>this.reportEffectsError(error),()=>{
      ++this.intent;this.playing=false;this.autoplayRequested=false;this.error='Playback paused. Press Play to try again.';this.emit();
    });
    this.applyStreamSettings();
    this.audio.addEventListener('ended', () => { if (this.track && this.usesStream() && !this.loading) this.advance(true); });
    this.audio.addEventListener('error', () => {
      if (this.track && this.usesStream() && !this.loading) { this.playing = false; this.error = 'This audio could not be played. Try another track or import it again.'; this.emit(); }
    });
    this.audio.addEventListener('timeupdate', () => { if (this.usesStream()) this.emit(); });
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
      this.autoplayRequested = false; this.stopVoice(); this.releaseLoopData();
      this.track = null; this.playing = false; this.loading = false; this.error = '';
      this.offset = 0; this.duration = 0;
      this.audio.pause(); this.streamRoute.flush(); this.audio.removeAttribute('src'); this.audio.load();
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
    if (this.usesStream()) return Math.min(this.duration, Number(this.audio.currentTime) || 0);
    const t = this.offset + (this.playing && this.context ? Math.max(0,this.context.currentTime-this.startedAt) * this.playbackRate : 0);
    return this.repeat === 'one' && this.duration ? t % this.duration : Math.min(t,this.duration);
  }
  usesStream() { return this.track?.kind !== 'loop'; }
  needsStretch() { return !this.pitchChanges&&this.playbackRate!==1; }
  getState() { return {track:this.track,playing:this.playing,loading:this.loading,error:this.error,effectsError:this.effectsError,currentTime:this.currentTime(),duration:this.duration,volume:this.volume,shuffle:this.shuffle,repeat:this.repeat,playbackRate:this.playbackRate,pitchChanges:this.pitchChanges,reverb:this.reverb,queueIds:[...this.queue]}; }
  emit() {
    const state=this.getState();
    this.dispatchEvent(new CustomEvent('statechange',{detail:state}));
    if (typeof navigator !== 'undefined' && navigator.mediaSession) {
      navigator.mediaSession.playbackState = this.playing ? 'playing' : (this.track ? 'paused' : 'none');
      if (this.duration>0) { try {navigator.mediaSession.setPositionState({duration:this.duration,playbackRate:this.playbackRate,position:Math.max(0,Math.min(this.duration,state.currentTime))});} catch {} }
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
    if(v){v.source.onended=null;try{v.source.stop();}catch{}v.source.disconnect();v.envelope.disconnect();this.flushLoopEffects();}
  }
  ensureContext() {
    if(!this.context){
      const context=this.contextFactory();let gain;
      try{gain=context.createGain();gain.gain.value=this.volume;gain.connect(context.destination);this.context=context;this.gain=gain;}
      catch(error){try{gain?.disconnect();}catch{}try{context.close()?.catch(()=>{});}catch{}throw error;}
    }
    return this.context;
  }
  createMediaRoute(audio,onError=()=>{},onPlaybackError=()=>{}) { return new MediaAudioRoute({audio,getContext:()=>this.ensureContext(),reverb:this.reverb,volume:audio.volume,onError,onPlaybackError}); }
  reportEffectsError(error) {
    if(error?.code===MEDIA_EFFECTS_UNSUPPORTED&&!this.usesStream())return;
    this.effectsError=error?.code===MEDIA_EFFECTS_UNSUPPORTED?NATIVE_MEDIA_REVERB_MESSAGE:'Reverb is unavailable. Playback continues without it.';this.emit();
  }
  loopInput() {
    if(!this.loopEffects&&this.reverb){
      let bus;
      try{bus=new ReverbBus(this.context,this.gain);bus.setReverb(this.reverb);this.loopEffects=bus;this.effectsError='';}
      catch(error){bus?.destroy();this.reportEffectsError(error);}
    }
    return this.loopEffects?.input||this.gain;
  }
  flushLoopEffects() { try{this.loopEffects?.flush();}catch(error){this.loopEffects?.bypass();this.reportEffectsError(error);} }
  connectEnvelope(envelope,target,previous=null) {
    if(previous===target)return previous;
    try{envelope.connect(target);if(previous)envelope.disconnect(previous);return target;}
    catch(error){try{envelope.disconnect(target);}catch{}if(!previous){envelope.connect(this.gain);previous=this.gain;}this.reportEffectsError(error);return previous;}
  }
  async setReverb(value) {
    const next=Number(value);if(!Number.isFinite(next))return;
    const revision=++this.reverbRevision;this.reverb=Math.max(0,Math.min(1,next));this.effectsError='';
    if(this.loopEffects){try{this.loopEffects.setReverb(this.reverb);}catch(error){this.loopEffects.bypass();this.reportEffectsError(error);}}
    if(this.track?.kind==='loop'&&this.context){
      const target=this.loopInput();
      if(this.voice)this.voice.target=this.connectEnvelope(this.voice.envelope,target,this.voice.target);
    }
    await this.streamRoute.setReverb(this.reverb);
    if(revision!==this.reverbRevision)return;
    if(this.usesStream()&&this.playing&&this.reverb&&this.streamRoute.error?.code===MEDIA_EFFECTS_UNSUPPORTED)this.reportEffectsError(this.streamRoute.error);
    if(this.usesStream()&&this.playing&&this.audio.paused){this.playing=false;this.autoplayRequested=false;this.error='Playback paused. Press Play to try again.';}
    this.emit();
  }
  releaseLoopData() {
    this.stretchSource?.dispose(); this.stretchSource=null; this.buffer=null; this.pcm=null;
  }
  hasLoopData() { return !!(this.buffer||this.pcm||this.stretchSource); }
  async load(id,options={}) {
    const track=this.catalog.get(id);if(!track)return;
    const generation=++this.generation;++this.intent;
    this.controller?.abort();const controller=new AbortController();this.controller=controller;this.autoplayRequested=true;
    this.audio.pause();this.streamRoute.flush();this.audio.removeAttribute('src');this.audio.load();this.stopVoice();this.releaseLoopData();
    this.track=track;this.offset=options.offset||0;this.duration=Number(track.duration)||0;this.playing=false;this.loading=true;this.error='';
    if(track.kind==='loop'&&this.effectsError===NATIVE_MEDIA_REVERB_MESSAGE)this.effectsError='';
    this.emit();this.setMetadata();
    try {
      if(track.kind==='loop') {
        this.ensureContext();
        await this.context.resume();
        if(generation!==this.generation)return;
        const response=await this.fetch(track.audioUrl,{signal:controller.signal});if(!response.ok)throw new Error('Audio request failed');
        const bytes=await response.arrayBuffer();let decoded=await this.context.decodeAudioData(bytes);
        if(generation!==this.generation)return;
        const first=Math.round((Number(track.loopStart)||0)*decoded.sampleRate);
        const end=track.loopEnd==null?decoded.length:Math.round(Number(track.loopEnd)*decoded.sampleRate);
        if(first<0||end<=first||end>decoded.length+Math.round(decoded.sampleRate*.005))throw new Error('Invalid loop region');
        const last=Math.min(end,decoded.length);
        this.duration=(last-first)/decoded.sampleRate;
        if(this.needsStretch()) {
          this.pcm={channels:Array.from({length:decoded.numberOfChannels},(_,c)=>decoded.getChannelData(c).slice(first,last)),sampleRate:decoded.sampleRate};
        } else {
          this.buffer=first===0&&last===decoded.length?decoded:this.context.createBuffer(decoded.numberOfChannels,last-first,decoded.sampleRate);
          if(this.buffer!==decoded)for(let c=0;c<decoded.numberOfChannels;c++)this.buffer.copyToChannel(decoded.getChannelData(c).subarray(first,last),c);
        }
        decoded=null;
      } else {
        this.audio.src=track.audioUrl;this.audio.loop=this.repeat==='one';
        this.applyStreamSettings();
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
    const stretch=this.needsStretch(),source=stretch?(this.stretchSource||this.createStretchSource()):this.context.createBufferSource(),envelope=this.context.createGain();
    if(!stretch){source.buffer=this.buffer;source.loop=this.repeat==='one';source.loopStart=0;source.loopEnd=this.duration;}
    source.playbackRate.value=this.playbackRate;
    envelope.gain.setValueAtTime(0,this.context.currentTime);envelope.gain.linearRampToValueAtTime(1,this.context.currentTime+.015);
    source.connect(envelope);const target=this.connectEnvelope(envelope,this.loopInput());this.startedAt=this.context.currentTime;
    const voice={source,envelope,stretch,target};this.voice=voice;
    source.onended=()=>{if(this.voice!==voice)return;this.voice=null;source.disconnect();envelope.disconnect();this.flushLoopEffects();this.offset=this.duration;this.playing=false;this.advance(true);};
    source.start(this.startedAt,Math.max(0,Math.min(this.offset,this.duration-.001)));
  }
  createStretchSource(){
    // AudioBuffer-owned channel stores cannot reliably be transferred. Copy to
    // standalone arrays once, then relinquish the AudioBuffer after the handoff.
    const pcm=this.pcm||{channels:Array.from({length:this.buffer.numberOfChannels},(_,c)=>this.buffer.getChannelData(c).slice()),sampleRate:this.buffer.sampleRate};
    const node=this.stretchNodeFactory(this.context,{numberOfInputs:0,numberOfOutputs:1,outputChannelCount:[pcm.channels.length],parameterData:{playbackRate:this.playbackRate}});
    let serial=0,disposed=false,resolveReturn;
    const source={node,playbackRate:node.parameters.get('playbackRate'),onended:null,returning:null,
      connect:target=>node.connect(target),disconnect:()=>node.disconnect(),
      start:()=>node.port.postMessage({type:'play',offset:this.offset,serial:++serial,loop:this.repeat==='one'}),
      stop:()=>node.port.postMessage({type:'pause',serial:++serial}),
      seek:offset=>node.port.postMessage({type:'seek',offset,serial:++serial}),
      reclaim:()=>{
        if(!source.returning)source.returning=new Promise(resolve=>{resolveReturn=resolve;node.port.postMessage({type:'release'});});
        return source.returning;
      },
      dispose:()=>{
        if(disposed)return;disposed=true;
        node.port.postMessage({type:'dispose'});node.port.onmessage=null;node.onprocessorerror=null;
        source.onended=null;node.disconnect();node.port.close();resolveReturn?.(null);
      }};
    Object.defineProperty(source,'loop',{set:value=>node.port.postMessage({type:'loop',value}),get:()=>this.repeat==='one'});
    node.port.onmessage=event=>{
      if(event.data?.type==='ended'&&event.data.serial===serial)source.onended?.();
      else if(event.data?.type==='error')node.onprocessorerror?.();
      else if(event.data?.type==='released'){
        const returned={channels:event.data.channels,sampleRate:event.data.sampleRate};
        resolveReturn?.(returned);source.dispose();
      }
    };
    node.onprocessorerror=()=>{
      if(this.stretchSource!==source)return;
      this.offset=this.currentTime();++this.intent;this.stopVoice();source.dispose();this.stretchSource=null;this.playing=false;this.autoplayRequested=false;
      this.error='Pitch-preserving playback stopped. Press Play to try again, or enable pitch changes.';this.emit();
    };
    try {
      node.port.postMessage({type:'load',channels:pcm.channels,sampleRate:pcm.sampleRate,offset:this.offset,rate:this.playbackRate,loop:this.repeat==='one'},pcm.channels.map(channel=>channel.buffer));
    } catch(error) { source.dispose();throw error; }
    this.buffer=null;this.pcm=null;this.stretchSource=source;
    return source;
  }
  async reclaimStretch() {
    const source=this.stretchSource;if(!source)return;
    const generation=this.generation,pcm=await source.reclaim();
    // Playback intent can change while the channels are in flight. Keep returned
    // PCM for the same track, but never restore a discarded track's allocation.
    if(generation===this.generation&&this.stretchSource===source){this.stretchSource=null;this.pcm=pcm;}
  }
  makeNativeBuffer() {
    if(this.buffer)return;
    const pcm=this.pcm;if(!pcm)throw new Error('Loop PCM is unavailable');
    const buffer=this.context.createBuffer(pcm.channels.length,pcm.channels[0].length,pcm.sampleRate);
    for(let c=0;c<pcm.channels.length;c++)buffer.copyToChannel(pcm.channels[c],c);
    this.buffer=buffer;this.pcm=null;
  }
  async resume() {
    if(!this.track||this.loading)return;
    if(this.playing)return;
    if(this.track.kind==='loop'&&!this.hasLoopData())return this.load(this.track.id,{offset:this.offset});
    this.autoplayRequested=true;
    const intent=++this.intent,generation=this.generation;this.error='';
    try{
      if(!this.usesStream()){
        await this.context.resume();if(intent!==this.intent||generation!==this.generation)return;
        if(this.needsStretch()){
          await this.prepareStretch();if(intent!==this.intent||generation!==this.generation)return;
          if(this.stretchSource?.returning)await this.reclaimStretch();
        }else if(this.stretchSource)await this.reclaimStretch();
        if(intent!==this.intent||generation!==this.generation)return;
        if(!this.needsStretch())this.makeNativeBuffer();
        if(this.offset>=this.duration){this.offset=0;this.stretchSource?.seek(0);}this.startVoice();this.playing=true;
      }else{
        this.applyStreamSettings();
        await this.streamRoute.prepare();if(intent!==this.intent||generation!==this.generation)return;
        if(this.reverb&&this.streamRoute.error?.code===MEDIA_EFFECTS_UNSUPPORTED)this.reportEffectsError(this.streamRoute.error);
        if(this.audio.currentTime>=this.duration&&this.duration)this.audio.currentTime=0;
        const promise=this.audio.play();await promise;
        if(intent!==this.intent||generation!==this.generation)return;
        this.playing=true;
      }
    }catch(error){if(intent!==this.intent||generation!==this.generation)return;this.playing=false;this.error='Playback paused. Press Play to try again.';}
    this.emit();
  }
  async toggle(){if(!this.track){const ids=[...this.catalog.keys()];if(ids.length)return this.playTrack(ids[0],ids);}else if(this.loading){this.autoplayRequested=!this.autoplayRequested;this.emit();}else if(this.playing)this.pause();else return this.resume();}
  pause(){++this.intent;this.autoplayRequested=false;if(this.track?.kind==='loop'){this.offset=this.currentTime();this.stopVoice();}this.audio.pause();this.streamRoute.flush();this.playing=false;this.emit();}
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
    if(!this.usesStream()){const wasPlaying=this.playing;this.stopVoice();this.offset=t===this.duration&&this.repeat==='one'?0:t;this.stretchSource?.seek(this.offset);if(wasPlaying){if(t===this.duration&&this.repeat!=='one'){this.playing=false;this.advance(true);return;}this.startVoice();}}
    else {this.streamRoute.flush();this.audio.currentTime=t;}
    this.emit();
  }
  setVolume(value){const v=Number(value);if(!Number.isFinite(v))return;this.volume=Math.max(0,Math.min(1,v));this.streamRoute.setVolume(this.volume);if(this.gain)this.gain.gain.setTargetAtTime(this.volume,this.context.currentTime,.015);this.emit();}
  applyStreamSettings() {
    this.audio.defaultPlaybackRate=this.playbackRate;this.audio.playbackRate=this.playbackRate;
    for(const property of ['preservesPitch','webkitPreservesPitch','mozPreservesPitch']) {
      if(property==='preservesPitch'||property in this.audio)this.audio[property]=!this.pitchChanges;
    }
  }
  setPlaybackRate(value){const rate=Number(value);if(!Number.isFinite(rate))return;return this.setPlaybackSettings(Math.max(.5,Math.min(2,rate)),this.pitchChanges);}
  setPitchChanges(enabled){return this.setPlaybackSettings(this.playbackRate,!!enabled);}
  setPlaybackSettings(rate,pitchChanges){
    if(rate===this.playbackRate&&pitchChanges===this.pitchChanges)return;
    const previousStretch=this.needsStretch();
    const position=this.currentTime(),wasPlaying=this.playing||this.autoplayRequested;
    this.offset=position;this.startedAt=this.context?.currentTime||0;
    this.playbackRate=rate;this.pitchChanges=pitchChanges;
    if(this.track?.kind==='loop'&&this.hasLoopData()&&!this.loading&&this.needsStretch()!==previousStretch){
      ++this.intent;this.stopVoice();this.audio.pause();this.playing=false;this.autoplayRequested=wasPlaying;
      this.emit();if(wasPlaying)return this.resume();
    }else{
      this.applyStreamSettings();
      if(this.voice)this.voice.source.playbackRate.value=rate;
      this.emit();
    }
  }
  setShuffle(enabled){this.shuffle=!!enabled;const current=this.track?.id;this.queue=this.shuffle&&current?[current,...this.shuffled(this.originalQueue.filter(x=>x!==current))]:[...this.originalQueue];this.emit();}
  setRepeat(mode){if(!['off','one','all'].includes(mode))return;this.offset=this.currentTime();this.startedAt=this.context?.currentTime||0;this.repeat=mode;this.audio.loop=mode==='one';if(this.voice)this.voice.source.loop=mode==='one';else if(this.stretchSource)this.stretchSource.loop=mode==='one';this.emit();}
  setMetadata(){if(typeof navigator==='undefined'||!navigator.mediaSession||typeof MediaMetadata==='undefined')return;navigator.mediaSession.metadata=new MediaMetadata({title:this.track.title,artist:this.track.artist||'Unknown artist',album:this.track.album||'Stillroom'});}
  setupMediaSession(){if(typeof navigator==='undefined'||!navigator.mediaSession)return;const actions={play:()=>this.resume(),pause:()=>this.pause(),previoustrack:()=>this.previous(),nexttrack:()=>this.next(),seekto:e=>this.seek(e.seekTime),seekbackward:e=>this.seek(this.currentTime()-(e.seekOffset||10)),seekforward:e=>this.seek(this.currentTime()+(e.seekOffset||10))};for(const [name,handler]of Object.entries(actions))try{navigator.mediaSession.setActionHandler(name,handler);}catch{}}
  destroy(){++this.generation;++this.intent;++this.reverbRevision;this.controller?.abort();this.pause();this.stopVoice();this.releaseLoopData();this.streamRoute.destroy();this.loopEffects?.destroy();this.loopEffects=null;clearInterval(this.timer);this.audio.removeAttribute('src');this.audio.load();this.context?.close();}
}
