/** Finite, peak-bounded room ambience. No feedback or song-sized allocations. */
export const REVERB_SECONDS = 1.4;
export const MAX_REVERB_WET = 0.35;
export const MEDIA_EFFECTS_UNSUPPORTED = 'MEDIA_EFFECTS_UNSUPPORTED';
export const NATIVE_MEDIA_REVERB_MESSAGE = 'Added reverb for songs is unavailable in this browser. Try desktop Chrome or Firefox. Loops still support reverb.';
const mediaSources = new WeakMap();
const amount = value => Math.max(0, Math.min(1, Number(value) || 0));
const disconnect = node => { try { node?.disconnect(); } catch {} };

export function requiresNativeMediaEffectsFallback(userAgent = globalThis.navigator?.userAgent || '') {
  const agent = String(userAgent);
  if (!/AppleWebKit\//i.test(agent)) return false;
  // iOS browser branding does not establish a different audio engine. Keep its
  // native media path, including desktop-style iPad Safari user agents.
  if (/(?:iPhone|iPad|iPod)\b|(?:CriOS|FxiOS|EdgiOS|OPiOS)\//i.test(agent)) return true;
  return !/(?:Chrome|Chromium|HeadlessChrome|Edg|OPR|Opera|Vivaldi)\//i.test(agent);
}

export function createReverbImpulse(sampleRate) {
  if (!Number.isFinite(sampleRate) || sampleRate <= 0) throw new TypeError('Invalid reverb sample rate');
  const length = Math.ceil(sampleRate * REVERB_SECONDS);
  return [0, 1].map(channel => {
    const samples = new Float32Array(length);
    const delays = [.017, .029, .043, .061, .083, .113, .157, .211];
    const gains = [.23, .16, .11, .08, .06, .045, .03, .02];
    for (let index = 0; index < delays.length; index++) {
      const delay = delays[index] + (channel ? .0023 * (index % 3 + 1) : 0);
      samples[Math.min(length - 1, Math.round(delay * sampleRate))] += gains[index];
    }
    const late = [];
    for (let index = 0; index < 96; index++) {
      const progress = (index + .5) / 96;
      late.push({frame: Math.min(length - 1, Math.round((.23 + progress * 1.16 + Math.sin(index * 12.9898 + channel * 7.1) * .002) * sampleRate)), gain: Math.exp(-progress * 4)});
    }
    const total = late.reduce((sum, tap) => sum + tap.gain, 0);
    for (let index = 0; index < late.length; index++) {
      const tap = late[index];
      samples[tap.frame] += tap.gain / total * .245 * (Math.sin(index * 7.13 + channel * 1.9) >= 0 ? 1 : -1);
    }
    // The safety margin also covers Float32 coefficient rounding. A stereo IR
    // uses one bounded filter per channel, without true-stereo cross summation.
    const absolute = samples.reduce((sum, sample) => sum + Math.abs(sample), 0);
    if (absolute > .98) for (let index = 0; index < length; index++) samples[index] *= .98 / absolute;
    return samples;
  });
}

function ramp(parameter, value, now, current, immediate = false) {
  if (parameter.cancelAndHoldAtTime) parameter.cancelAndHoldAtTime(now);
  else parameter.cancelScheduledValues?.(now);
  // Anchor explicitly even after cancelAndHold: a completed old linear ramp
  // must not become the new ramp's origin and retroactively jump at this time.
  parameter.setValueAtTime(current, now);
  if (immediate) parameter.setValueAtTime(value, now);
  else parameter.linearRampToValueAtTime(value, now + .035);
}

export class ReverbBus {
  constructor(context, destination) {
    this.context = context; this.destination = destination; this.amount = 0; this.impulse = null; this.convolver = null;
    this.input = context.createGain(); this.dry = context.createGain(); this.wet = context.createGain(); this.output = context.createGain();
    this.dry.gain.value = 1; this.wet.gain.value = 0;
    this.input.connect(this.dry); this.dry.connect(this.output); this.wet.connect(this.output); this.output.connect(destination);
  }
  makeConvolver() {
    if (!this.impulse) {
      const channels = createReverbImpulse(this.context.sampleRate);
      const buffer = this.context.createBuffer(2, channels[0].length, this.context.sampleRate);
      for (let channel = 0; channel < 2; channel++) buffer.copyToChannel(channels[channel], channel);
      this.impulse = buffer;
    }
    const node = this.context.createConvolver(); node.normalize = false; node.buffer = this.impulse;
    try { node.connect(this.wet); this.input.connect(node); }
    catch (error) { disconnect(node); try { this.input.disconnect(node); } catch {} throw error; }
    return node;
  }
  setReverb(value) {
    const next = amount(value);
    const hadConvolver = !!this.convolver;
    if (next > 0 && !this.convolver) this.convolver = this.makeConvolver();
    if (this.amount === 0 && next > 0 && hadConvolver && this.context.currentTime >= this.zeroAt) this.flush();
    this.amount = next;
    const wet = MAX_REVERB_WET * next;
    const now = this.context.currentTime, currentWet = Math.max(0, Math.min(MAX_REVERB_WET, this.wet.gain.value));
    // One clock and one blend value keep both trajectories complementary even
    // if a render quantum elapses between the two AudioParam API calls.
    ramp(this.dry.gain, 1 - wet, now, 1 - currentWet); ramp(this.wet.gain, wet, now, currentWet);
    // Keep the finite wet branch until its gain reaches zero. A quick off/on
    // reverses the ramp without chopping the still-audible tail; after silence,
    // re-enabling can safely clear history before bringing the wet gain back.
    if (!next) this.zeroAt = this.context.currentTime + .035;
  }
  flush() {
    if (!this.convolver) return;
    // A fresh convolver clears its native history; assigning the same buffer to
    // an existing node is not a reliable flush in all browsers.
    const previous = this.convolver, fresh = this.makeConvolver();
    try { this.input.disconnect(previous); } catch {}
    disconnect(previous); this.convolver = fresh;
  }
  bypass() {
    if (this.convolver) { try { this.input.disconnect(this.convolver); } catch {} disconnect(this.convolver); this.convolver = null; }
    this.amount = 0;
    const now = this.context.currentTime, currentWet = Math.max(0, Math.min(MAX_REVERB_WET, this.wet.gain.value));
    try { ramp(this.dry.gain, 1, now, 1 - currentWet, true); ramp(this.wet.gain, 0, now, currentWet, true); }
    catch { this.dry.gain.value = 1; this.wet.gain.value = 0; }
  }
  destroy() { for (const node of [this.input, this.dry, this.wet, this.convolver, this.output]) disconnect(node); this.convolver = null; this.impulse = null; }
}

/**
 * Optional media routing shared by the main and recommendation players.
 * setReverb never starts media. Await prepare before every audio.play call:
 * capture is irreversible, so even a later dry bypass needs its context alive.
 */
export class MediaAudioRoute {
  constructor({audio, getContext, reverb = 0, volume = audio?.volume ?? 1, onError = () => {}, onPlaybackError = () => {}, userAgent = globalThis.navigator?.userAgent || ''}) {
    this.audio = audio; this.getContext = getContext; this.reverb = amount(reverb); this.onError = onError; this.onPlaybackError = onPlaybackError;
    this.nativeEffectsFallback = requiresNativeMediaEffectsFallback(userAgent);
    const retained = mediaSources.get(audio);
    retained?.owner?.destroy();
    this.context = retained?.context || null; this.source = retained?.source || null; this.bus = null; this.pending = null; this.destroyed = false; this.error = null; this.connected = false; this.direct = !!retained;
    this.setVolume(volume);
    this.listeners = new Map(['pause', 'ended', 'seeking', 'emptied'].map(name => [name, () => this.flush()]));
    this.playRevision = 0;
    for (const name of ['pause', 'emptied']) this.listeners.set(name, () => { ++this.playRevision; this.flush(); });
    this.listeners.set('play', () => {
      const revision = ++this.playRevision;
      this.prepare().catch(error => {
        if (this.destroyed || revision !== this.playRevision) return;
        this.audio.pause(); try { this.onPlaybackError(error); } catch {}
      });
    });
    this.listeners.set('volumechange', () => { if (audio.muted || audio.volume === 0) this.flush(); });
    for (const [name, listener] of this.listeners) audio.addEventListener(name, listener);
  }
  setVolume(value) { const number = Number(value); if (Number.isFinite(number)) this.audio.volume = Math.max(0, Math.min(1, number)); }
  async setReverb(value) {
    if (this.destroyed) return false;
    this.reverb = amount(value);
    if (!this.reverb && this.error?.code === MEDIA_EFFECTS_UNSUPPORTED) this.error = null;
    try {
      if (this.bus) this.bus.setReverb(this.reverb);
      // Persisted preferences on a paused element must not create/resume audio.
      if (this.reverb && !this.audio.paused) return await this.prepare();
      return true;
    } catch (error) {
      if (this.resumeError === error) { this.audio.pause(); try { this.onPlaybackError(error); } catch {} }
      else this.fallback(error);
      return false;
    }
  }
  fallback(error) {
    this.error = error; this.reverb = 0; this.bus?.bypass();
    try { this.onError(error); } catch {}
  }
  async prepare() {
    if (this.destroyed) throw new Error('Audio route is closed');
    if (this.reverb && this.nativeEffectsFallback && !this.source) {
      // Capturing is irreversible. Safari 18 was measured ignoring volume,
      // mute and pitch settings here; WebKit PR 61463 fixes rate/pitch, but its
      // volume/mute behavior is still unverified. Keep all WebKit streams
      // native until the complete route is verified, without limiting loops.
      // https://github.com/WebKit/WebKit/pull/61463
      if (this.error?.code !== MEDIA_EFFECTS_UNSUPPORTED) {
        const error = new Error(NATIVE_MEDIA_REVERB_MESSAGE);
        error.name = 'NotSupportedError'; error.code = MEDIA_EFFECTS_UNSUPPORTED; this.error = error;
        try { this.onError(error); } catch {}
      }
      return false;
    }
    if (!this.reverb && !this.source) return true;
    if (this.pending) return this.pending;
    this.pending = this.prepareRoute();
    try { return await this.pending; } finally { this.pending = null; }
  }
  async prepareRoute() {
    const retained = mediaSources.get(this.audio);
    let context;
    try {
      context = retained?.context || this.context || this.getContext();
      await context.resume();
    } catch (error) {
      if (this.destroyed) return true;
      if (this.source || retained) { this.resumeError = error; throw error; }
      this.fallback(error); return false;
    }
    if (this.destroyed || (!this.reverb && !this.source)) return true;
    this.resumeError = null;
    this.context = context;
    try {
      if (!this.bus) this.bus = new ReverbBus(context, context.destination);
      this.bus.setReverb(this.reverb);
    } catch (error) { this.fallback(error); if (!this.source && !retained) { this.bus?.destroy(); this.bus = null; return false; } }
    if (this.source && this.connected) return true;
    try {
      // Establish the dry destination before taking away the native media path.
      const source = retained?.source || context.createMediaElementSource(this.audio);
      mediaSources.set(this.audio, {source, context, owner: this}); this.source = source;
      // Connect the new route first, so a failed reassociation leaves the known
      // dry destination usable. Only then remove that specific fallback edge.
      source.connect(this.bus.input); if (this.direct) source.disconnect(context.destination);
      this.direct = false;
      this.connected = true; this.error = null; return true;
    } catch (error) {
      // Once captured, a known-good direct dry route is the fallback. Retain it
      // and the original context rather than claiming native output is restored.
      if (this.source) {
        try { this.source.connect(context.destination); try { this.source.disconnect(this.bus?.input); } catch {} this.connected = false; this.direct = true; }
        catch { this.audio.pause(); this.fallback(error); throw error; }
      }
      this.fallback(error); return false;
    }
  }
  flush() {
    if (!this.bus || this.lastFlushTime === this.context.currentTime) return;
    this.lastFlushTime = this.context.currentTime;
    try { this.bus.flush(); } catch (error) { this.fallback(error); }
  }
  destroy() {
    if (this.destroyed) return; this.destroyed = true;
    for (const [name, listener] of this.listeners) this.audio.removeEventListener(name, listener);
    // Keep captured media usable as dry audio until its owner closes the shared
    // context. There can only ever be one source associated with this element.
    if (this.source) { disconnect(this.source); try { this.source.connect(this.context.destination); } catch {} }
    this.bus?.destroy(); this.bus = null;
  }
}
