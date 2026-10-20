import test from 'node:test';
import assert from 'node:assert/strict';
import {createReverbImpulse, ReverbBus, MediaAudioRoute, REVERB_SECONDS, MAX_REVERB_WET, requiresNativeMediaEffectsFallback, MEDIA_EFFECTS_UNSUPPORTED, NATIVE_MEDIA_REVERB_MESSAGE} from '../public/audio-effects.js';

const safariAgent = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1.1 Safari/605.1.15';

const settle = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; };

class Parameter {
  constructor(context, value = 1) { this.context = context; this.initial = value; this.events = []; }
  get value() { return this.at(this.context.currentTime); }
  set value(value) { this.initial = value; this.events = []; }
  at(time) {
    let previous = {time: 0, value: this.initial};
    for (const event of this.events) {
      if (event.time > time) {
        if (event.type === 'ramp' && event.time > previous.time) return previous.value + (event.value - previous.value) * Math.max(0, (time - previous.time) / (event.time - previous.time));
        return previous.value;
      }
      previous = event;
    }
    return previous.value;
  }
  cancelAndHoldAtTime(time) { const value = this.at(time); this.events = this.events.filter(event => event.time < time); this.setValueAtTime(value, time); }
  cancelScheduledValues(time) { this.events = this.events.filter(event => event.time < time); }
  setValueAtTime(value, time) { this.events.push({type: 'set', value, time}); this.events.sort((a, b) => a.time - b.time); }
  linearRampToValueAtTime(value, time) { this.events.push({type: 'ramp', value, time}); this.events.sort((a, b) => a.time - b.time); }
}

class Node {
  constructor(context, kind) { this.context = context; this.kind = kind; this.connections = new Set(); this.disconnects = []; }
  connect(target) {
    if (this.context.failConnect?.(this, target)) throw new Error('Injected connection failure');
    this.connections.add(target); return target;
  }
  disconnect(target) { this.disconnects.push(target); if (arguments.length) this.connections.delete(target); else this.connections.clear(); }
}

class Context {
  constructor(sampleRate = 48000) { this.sampleRate = sampleRate; this.currentTime = 0; this.state = 'running'; this.resumeCalls = 0; this.gains = []; this.convolvers = []; this.buffers = []; this.media = []; this.elements = new Set(); this.destination = new Node(this, 'destination'); }
  async resume() { this.resumeCalls++; if (this.resumeGate) await this.resumeGate.promise; if (this.resumeFailure) throw this.resumeFailure; this.state = 'running'; }
  createGain() { const node = new Node(this, 'gain'); node.gain = new Parameter(this); this.gains.push(node); return node; }
  createConvolver() { if (this.failConvolver) throw new Error('Injected convolver allocation failure'); const node = new Node(this, 'convolver'); node.normalize = true; this.convolvers.push(node); return node; }
  createBuffer(channels, length, sampleRate) {
    const data = Array.from({length: channels}, () => new Float32Array(length));
    const buffer = {numberOfChannels: channels, length, sampleRate, duration: length / sampleRate, getChannelData: channel => data[channel], copyToChannel: (values, channel) => data[channel].set(values)};
    this.buffers.push(buffer); return buffer;
  }
  createMediaElementSource(audio) {
    if (this.elements.has(audio)) throw new Error('Media element captured twice');
    this.elements.add(audio); const node = new Node(this, 'media'); node.mediaElement = audio; this.media.push(node); return node;
  }
}

class Audio extends EventTarget {
  constructor() { super(); this._volume = .4; this._muted = false; this.paused = true; this.currentTime = 2.75; this.src = '/unchanged-fixture.wav'; this.playCalls = 0; this.pauseCalls = 0; }
  get volume() { return this._volume; }
  set volume(value) { const changed = value !== this._volume; this._volume = value; if (changed) this.dispatchEvent(new Event('volumechange')); }
  get muted() { return this._muted; }
  set muted(value) { this._muted = value; this.dispatchEvent(new Event('volumechange')); }
  play() { this.playCalls++; this.paused = false; this.dispatchEvent(new Event('play')); return Promise.resolve(); }
  pause() { this.pauseCalls++; if (!this.paused) { this.paused = true; this.dispatchEvent(new Event('pause')); } }
}

function fixture(options = {}) {
  const audio = new Audio(), context = new Context(), errors = [], playbackErrors = []; let contextCalls = 0;
  const route = new MediaAudioRoute({audio, getContext: () => { contextCalls++; return context; }, onError: error => errors.push(error), onPlaybackError: error => playbackErrors.push(error), ...options});
  return {audio, context, route, errors, playbackErrors, get contextCalls() { return contextCalls; }};
}

function sparseConvolution(input, impulse) {
  const output = new Float64Array(input.length + impulse.length - 1);
  for (let frame = 0; frame < impulse.length; frame++) if (impulse[frame]) {
    for (let index = 0; index < input.length; index++) output[index + frame] += input[index] * impulse[frame];
  }
  return output;
}

test('room impulse is deterministic stereo, finite, delayed, and bounded independently of song duration', () => {
  for (const rate of [8000, 44100, 48000, 96000]) {
    const impulse = createReverbImpulse(rate);
    assert.deepEqual(impulse, createReverbImpulse(rate));
    assert.equal(impulse.length, 2); assert.notDeepEqual(impulse[0], impulse[1]);
    for (const channel of impulse) {
      assert.equal(channel.length, Math.ceil(rate * REVERB_SECONDS));
      assert.ok(channel.every(Number.isFinite));
      const absolute = channel.reduce((sum, value) => sum + Math.abs(value), 0);
      assert.ok(absolute > .8 && absolute <= .980001, `bounded filter sum ${absolute}`);
      assert.ok(channel.subarray(0, Math.floor(rate * .016)).every(value => value === 0), 'direct sound is confined to the dry path');
      assert.ok(channel.subarray(Math.floor(rate * .3)).some(value => value !== 0), 'late room response remains audible after early reflections');
    }
    assert.ok(impulse.reduce((bytes, channel) => bytes + channel.byteLength, 0) <= 2 * 96000 * 1.4 * 4);
  }
  for (const rate of [NaN, Infinity, 0, -1]) assert.throws(() => createReverbImpulse(rate), TypeError);
});

test('finite wet filtering and convex mix cannot amplify full-scale impulses, DC, alternating signs, or tones', () => {
  const rate = 8000, channels = createReverbImpulse(rate), frames = 2048;
  const signals = [
    Float32Array.from({length: frames}, (_, index) => index === 0 ? 1 : 0),
    new Float32Array(frames).fill(1),
    Float32Array.from({length: frames}, (_, index) => index % 2 ? -1 : 1),
    Float32Array.from({length: frames}, (_, index) => Math.sin(2 * Math.PI * 40 * index / rate)),
    Float32Array.from({length: frames}, (_, index) => Math.sin(2 * Math.PI * 440 * index / rate)),
  ];
  for (const impulse of channels) for (const input of signals) {
    const wet = sparseConvolution(input, impulse);
    for (const amount of [0, .25, .7, 1]) {
      const gain = MAX_REVERB_WET * amount;
      for (let frame = 0; frame < wet.length; frame++) {
        const output = (1 - gain) * (input[frame] || 0) + gain * wet[frame];
        assert.ok(Number.isFinite(output)); assert.ok(Math.abs(output) <= 1.000001, `full-scale output at ${amount}`);
      }
    }
    assert.equal(wet.length, input.length + Math.ceil(rate * REVERB_SECONDS) - 1);
  }
});

test('dry and wet gains stay complementary and continuous through repeated slider changes', () => {
  const context = new Context(), bus = new ReverbBus(context, context.destination);
  try {
    assert.equal(bus.dry.gain.value, 1); assert.equal(bus.wet.gain.value, 0); assert.equal(context.buffers.length, 0);
    bus.setReverb(1);
    for (const time of [0, .005, .017, .035, .1]) {
      assert.ok(Math.abs(bus.dry.gain.at(time) + bus.wet.gain.at(time) - 1) < 1e-12);
      assert.ok(bus.wet.gain.at(time) >= 0 && bus.wet.gain.at(time) <= MAX_REVERB_WET);
    }
    context.currentTime = .012; const before = bus.wet.gain.value;
    bus.setReverb(.2); assert.equal(bus.wet.gain.value, before, 'new slider target does not jump immediately');
    for (const time of [.012, .02, .035, .047]) assert.ok(Math.abs(bus.dry.gain.at(time) + bus.wet.gain.at(time) - 1) < 1e-12);
    assert.ok(Math.abs(bus.wet.gain.at(.047) - MAX_REVERB_WET * .2) < 1e-12);
    bus.dry.gain.cancelAndHoldAtTime = undefined; bus.wet.gain.cancelAndHoldAtTime = undefined;
    context.currentTime = .06; bus.setReverb(.8);
    assert.ok(Math.abs(bus.dry.gain.at(.095) + bus.wet.gain.at(.095) - 1) < 1e-12, 'older AudioParam fallback stays complementary');
  } finally { bus.destroy(); }
});

test('mix automation shares one clock origin even when the rendering clock advances between API reads', () => {
  const context = new Context(), bus = new ReverbBus(context, context.destination);
  try {
    bus.setReverb(1);
    let clock = .015, reads = 0;
    Object.defineProperty(context, 'currentTime', {configurable: true, get() { const time = clock; clock += .004; reads++; return time; }});
    bus.setReverb(.2);
    assert.ok(reads >= 2, 'the simulated audio clock advances during the parameter update');
    const dryStart = bus.dry.gain.events.filter(event => event.type === 'set').at(-1);
    const wetStart = bus.wet.gain.events.filter(event => event.type === 'set').at(-1);
    const dryEnd = bus.dry.gain.events.filter(event => event.type === 'ramp').at(-1);
    const wetEnd = bus.wet.gain.events.filter(event => event.type === 'ramp').at(-1);
    assert.equal(dryStart.time, wetStart.time, 'both anchors use the same rendering instant');
    assert.equal(dryEnd.time, wetEnd.time, 'both ramps finish together');
    assert.ok(Math.abs(dryStart.value + wetStart.value - 1) < 1e-12);
    assert.ok(Math.abs(dryEnd.value + wetEnd.value - 1) < 1e-12);
    for (let step = 0; step <= 20; step++) {
      const time = dryStart.time + (dryEnd.time - dryStart.time) * step / 20;
      assert.ok(Math.abs(bus.dry.gain.at(time) + bus.wet.gain.at(time) - 1) < 1e-12, `convex gain sum at ${time}`);
    }
    clock = .1; bus.bypass();
    const dryBypass = bus.dry.gain.events.filter(event => event.type === 'set').at(-1);
    const wetBypass = bus.wet.gain.events.filter(event => event.type === 'set').at(-1);
    assert.equal(dryBypass.time, wetBypass.time); assert.equal(dryBypass.value, 1); assert.equal(wetBypass.value, 0);
  } finally { bus.destroy(); }
});

test('flush replaces convolution history while reusing one fixed IR and no feedback path', () => {
  const context = new Context(), bus = new ReverbBus(context, context.destination);
  try {
    bus.setReverb(.7); const impulse = bus.impulse, previous = bus.convolver;
    assert.equal(previous.normalize, false);
    bus.flush(); const fresh = bus.convolver;
    assert.notEqual(fresh, previous); assert.equal(fresh.buffer, impulse); assert.equal(context.buffers.length, 1);
    assert.equal(previous.connections.size, 0); assert.equal(bus.input.connections.has(previous), false);
    assert.equal(bus.input.connections.has(fresh), true); assert.equal(fresh.connections.has(bus.wet), true);
    assert.equal(bus.output.connections.has(bus.input), false); assert.equal(bus.wet.connections.has(bus.input), false);
    bus.setReverb(0); assert.equal(bus.convolver, fresh, 'the existing branch survives its smooth fade to dry');
    assert.equal(fresh.connections.has(bus.wet), true);
    context.currentTime = .1; bus.setReverb(1);
    assert.equal(bus.impulse, impulse); assert.equal(context.buffers.length, 1); assert.notEqual(bus.convolver, fresh);
    assert.equal(fresh.connections.size, 0, 're-enabling after wet silence discards old history');
  } finally { bus.destroy(); }
});

test('default and paused reverb preferences leave native media, position, and volume untouched', async () => {
  const f = fixture();
  try {
    await f.route.prepare(); await f.route.setReverb(.6); await f.route.setReverb(0);
    assert.equal(f.contextCalls, 0); assert.equal(f.context.resumeCalls, 0); assert.equal(f.context.media.length, 0);
    assert.equal(f.context.buffers.length, 0); assert.equal(f.audio.volume, .4); assert.equal(f.audio.currentTime, 2.75);
    assert.equal(f.audio.paused, true); assert.equal(f.audio.playCalls, 0); assert.equal(f.audio.src, '/unchanged-fixture.wav');
  } finally { f.route.destroy(); }
});

test('unverified WebKit media routes are guarded without misclassifying desktop Chromium or Firefox', () => {
  for (const agent of [
    safariAgent, safariAgent.replace('18.1.1', '27.0'),
    'Mozilla/5.0 AppleWebKit/605.1.15 Safari/605.1.15',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 Version/18.1 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (iPad; CPU OS 18_1 like Mac OS X) AppleWebKit/605.1.15 CriOS/131.0 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (iPhone) AppleWebKit/605.1.15 FxiOS/132.0 Mobile/15E148 Safari/605.1.15',
    'Mozilla/5.0 (iPhone) AppleWebKit/605.1.15 EdgiOS/131.0 Mobile/15E148 Safari/605.1.15',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/605.1.15 CriOS/131.0 Safari/605.1.15',
  ]) assert.equal(requiresNativeMediaEffectsFallback(agent), true, agent);
  for (const agent of [
    '', 'unknown browser', 'Mozilla/5.0 Gecko/20100101 Firefox/157.0',
    'Mozilla/5.0 (Macintosh) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/155.0 Safari/537.36 Edg/155.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chromium/155.0 Safari/537.36',
    'Mozilla/5.0 AppleWebKit/537.36 Chrome/155.0 Safari/537.36 OPR/125.0',
    'Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 Chrome/155.0 Mobile Safari/537.36',
  ]) assert.equal(requiresNativeMediaEffectsFallback(agent), false, agent);
});

test('guarded paused preferences remain lazy and the first prepare reports optional incompatibility before context or capture', async () => {
  const f = fixture({userAgent: safariAgent});
  try {
    await f.route.prepare(); await f.route.setReverb(.8);
    assert.equal(f.route.reverb, .8); assert.equal(f.errors.length, 0); assert.equal(f.contextCalls, 0);
    assert.equal(await f.route.prepare(), false);
    assert.equal(f.route.error.code, MEDIA_EFFECTS_UNSUPPORTED); assert.equal(f.route.error.name, 'NotSupportedError');
    assert.equal(f.route.error.message, NATIVE_MEDIA_REVERB_MESSAGE); assert.equal(f.errors.length, 1);
    await f.route.prepare(); await f.route.prepare();
    assert.equal(f.errors.length, 1, 'repeated playback preparation does not repeat the warning callback');
    assert.equal(f.contextCalls, 0); assert.equal(f.context.resumeCalls, 0); assert.equal(f.context.media.length, 0);
    assert.equal(f.context.buffers.length, 0); assert.equal(f.route.source, null); assert.equal(f.route.bus, null);
    assert.equal(f.route.reverb, .8, 'the loop/global preference is not discarded');
    assert.equal(f.audio.paused, true); assert.equal(f.audio.playCalls, 0); assert.equal(f.audio.pauseCalls, 0);
    await f.route.setReverb(0); assert.equal(f.route.error, null); assert.equal(await f.route.prepare(), true);
  } finally { f.route.destroy(); }
});

test('guarded active streams and native controls retain position, speed, pitch, volume, mute and playback', async () => {
  const f = fixture({userAgent: safariAgent});
  try {
    f.audio.playbackRate = 1.37; f.audio.preservesPitch = false; f.audio.muted = true; await f.audio.play();
    const before = {src: f.audio.src, currentTime: f.audio.currentTime, playCalls: f.audio.playCalls, pauseCalls: f.audio.pauseCalls};
    assert.equal(await f.route.setReverb(.7), false); await settle();
    assert.equal(f.audio.paused, false); assert.equal(f.audio.playbackRate, 1.37); assert.equal(f.audio.preservesPitch, false);
    assert.equal(f.audio.volume, .4); assert.equal(f.audio.muted, true);
    assert.deepEqual({src: f.audio.src, currentTime: f.audio.currentTime, playCalls: f.audio.playCalls, pauseCalls: f.audio.pauseCalls}, before);
    f.route.setVolume(.2); f.audio.muted = false; f.audio.pause(); await f.audio.play(); await settle();
    assert.equal(f.audio.volume, .2); assert.equal(f.audio.paused, false); assert.equal(f.audio.currentTime, before.currentTime);
    assert.equal(f.errors.length, 1); assert.equal(f.playbackErrors.length, 0); assert.equal(f.contextCalls, 0);
    assert.equal(f.context.media.length, 0); assert.equal(f.route.bus, null);
    await f.route.setReverb(0); await f.route.setReverb(.6);
    assert.equal(f.errors.length, 2, 'an explicit off/on choice can report its new unsupported attempt once');
  } finally { f.route.destroy(); }
});

test('a newly guarded replacement keeps an already captured element connected to its retained context', async () => {
  const first = fixture({reverb: .6, userAgent: 'Chromium/155.0'}); let replacement;
  try {
    await first.route.prepare(); const source = first.route.source;
    replacement = new MediaAudioRoute({audio: first.audio, getContext: () => { throw new Error('Do not replace a captured context'); }, reverb: .4, userAgent: safariAgent});
    await replacement.prepare();
    assert.equal(replacement.source, source); assert.equal(first.context.media.length, 1);
    assert.equal(source.connections.has(replacement.bus.input), true); assert.equal(first.context.resumeCalls, 2);
  } finally { replacement?.destroy(); first.route.destroy(); }
});

test('captured media uses one source and resumes its context even after the effect is disabled', async () => {
  const f = fixture();
  try {
    await f.route.setReverb(.8); await f.route.prepare(); const source = f.route.source;
    assert.equal(f.context.media.length, 1); assert.equal(f.audio.playCalls, 0);
    assert.equal(f.audio.volume, .4); assert.equal(f.route.bus.output.gain.value, 1, 'element volume is not applied a second time');
    await f.route.setReverb(0); f.context.state = 'suspended'; const resumes = f.context.resumeCalls;
    await f.route.prepare(); assert.equal(f.context.resumeCalls, resumes + 1); assert.equal(f.context.state, 'running');
    assert.equal(f.route.source, source); assert.equal(f.context.media.length, 1);
    f.context.currentTime = .2; assert.equal(f.route.bus.dry.gain.value, 1); assert.equal(f.route.bus.wet.gain.value, 0);
  } finally { f.route.destroy(); }
});

test('a rapid off and on reverses the wet ramp without dropping its still-audible history', () => {
  const context = new Context(), bus = new ReverbBus(context, context.destination);
  try {
    bus.setReverb(1); context.currentTime = .1; const convolver = bus.convolver;
    bus.setReverb(0); context.currentTime = .112;
    const beforeWet = bus.wet.gain.value, beforeDry = bus.dry.gain.value;
    assert.ok(beforeWet > 0); bus.setReverb(.8);
    assert.equal(bus.convolver, convolver); assert.equal(bus.wet.gain.value, beforeWet); assert.equal(bus.dry.gain.value, beforeDry);
    assert.equal(context.buffers.length, 1); assert.equal(context.convolvers.length, 1);
    for (const time of [.112, .12, .135, .147]) assert.ok(Math.abs(bus.dry.gain.at(time) + bus.wet.gain.at(time) - 1) < 1e-12);
  } finally { bus.destroy(); }
});

test('failed activation before capture preserves native output and a later explicit retry succeeds', async () => {
  const f = fixture();
  try {
    f.audio.paused = false; f.context.resumeFailure = new Error('Initial activation refused');
    assert.equal(await f.route.setReverb(1), false); assert.equal(f.audio.paused, false);
    assert.equal(f.context.media.length, 0); assert.equal(f.context.buffers.length, 0); assert.equal(f.errors.length, 1);
    assert.equal(f.playbackErrors.length, 0); assert.equal(f.route.reverb, 0);
    f.context.resumeFailure = null; await f.route.setReverb(.5);
    assert.equal(f.context.media.length, 1); assert.equal(f.route.connected, true); assert.equal(f.audio.playCalls, 0);
  } finally { f.route.destroy(); }
});

test('turning the effect off while context activation is pending prevents capture and never autoplays', async () => {
  const f = fixture();
  try {
    f.audio.paused = false; f.context.resumeGate = deferred();
    const enabling = f.route.setReverb(1); await settle();
    await f.route.setReverb(0); f.context.resumeGate.resolve(); await enabling;
    assert.equal(f.context.media.length, 0); assert.equal(f.context.buffers.length, 0); assert.equal(f.audio.playCalls, 0);
    assert.equal(f.audio.currentTime, 2.75); assert.equal(f.route.bus, null); assert.equal(f.route.pending, null);
  } finally { f.route.destroy(); }
});

test('native play events activate routing and later resume an already captured dry route', async () => {
  const f = fixture({reverb: .5});
  try {
    await f.audio.play(); await settle();
    assert.equal(f.context.media.length, 1); assert.equal(f.route.connected, true); assert.equal(f.audio.playCalls, 1);
    f.audio.pause(); await f.route.setReverb(0); f.context.state = 'suspended'; const resumes = f.context.resumeCalls;
    await f.audio.play(); await settle();
    assert.equal(f.context.resumeCalls, resumes + 1); assert.equal(f.context.media.length, 1); assert.equal(f.audio.playCalls, 2);
    assert.equal(f.playbackErrors.length, 0); assert.equal(f.audio.paused, false);
  } finally { f.route.destroy(); }
});

test('pre-capture effect allocation failure preserves native playback and supports a later retry', async () => {
  const f = fixture();
  try {
    f.audio.paused = false; f.context.failConvolver = true;
    assert.equal(await f.route.setReverb(1), false); assert.equal(f.context.media.length, 0); assert.equal(f.route.bus, null);
    assert.equal(f.audio.paused, false); assert.equal(f.audio.volume, .4); assert.equal(f.errors.length, 1);
    f.context.failConvolver = false; await f.route.setReverb(.5);
    assert.equal(f.context.media.length, 1); assert.equal(f.route.connected, true); assert.equal(f.audio.playCalls, 0);
  } finally { f.route.destroy(); }
});

test('post-capture effect failure leaves a connected unity dry path and can restore reverb', async () => {
  const f = fixture({reverb: 1});
  try {
    await f.route.prepare(); const source = f.route.source;
    f.context.failConvolver = true; f.context.currentTime = .2; f.route.flush();
    assert.equal(f.route.reverb, 0); assert.equal(f.route.bus.convolver, null);
    assert.equal(source.connections.has(f.route.bus.input), true); assert.equal(f.route.bus.output.connections.has(f.context.destination), true);
    assert.equal(f.route.bus.dry.gain.value, 1); assert.equal(f.route.bus.wet.gain.value, 0); assert.equal(f.errors.length, 1);
    f.context.failConvolver = false; await f.route.setReverb(.5); await f.route.prepare();
    assert.equal(f.route.source, source); assert.equal(f.context.media.length, 1); assert.ok(f.route.bus.convolver);
  } finally { f.route.destroy(); }
});

test('a failed media-to-bus connection keeps direct dry audio and retries without another capture', async () => {
  const f = fixture({reverb: 1}); let failures = 1;
  try {
    f.context.failConnect = (source, target) => source.kind === 'media' && target !== f.context.destination && failures-- > 0;
    assert.equal(await f.route.prepare(), false); const source = f.route.source;
    assert.equal(source.connections.has(f.context.destination), true); assert.equal(f.route.connected, false); assert.equal(f.context.media.length, 1);
    await f.route.prepare(); assert.equal(f.route.source, source); assert.equal(f.context.media.length, 1);
    assert.equal(f.route.connected, true); assert.equal(source.connections.has(f.context.destination), false);
    assert.equal(source.connections.has(f.route.bus.input), true); assert.equal(f.audio.volume, .4);
  } finally { f.route.destroy(); }
});

test('captured context resume failure rejects preparation and native play pauses with an explicit callback', async () => {
  const f = fixture({reverb: .5});
  try {
    await f.route.prepare(); await f.route.setReverb(0); f.context.resumeFailure = new Error('Device activation failed');
    await assert.rejects(f.route.prepare(), /Device activation failed/);
    await f.audio.play(); await settle();
    assert.equal(f.audio.paused, true); assert.equal(f.playbackErrors.length, 1); assert.equal(f.context.media.length, 1);
    assert.equal(f.route.source.connections.has(f.route.bus.input), true, 'dry graph remains connected for retry');
    f.context.resumeFailure = null; await f.route.prepare();
    assert.equal(f.context.media.length, 1); assert.equal(f.route.connected, true);
  } finally { f.route.destroy(); }
});

test('muting, seeking, pause, and source reset clear old wet history without changing volume or media time', async () => {
  const f = fixture({reverb: .7});
  try {
    await f.route.prepare(); const impulse = f.route.bus.impulse;
    for (const event of ['seeking', 'pause', 'emptied', 'ended']) {
      const previous = f.route.bus.convolver; f.context.currentTime += .1; f.audio.dispatchEvent(new Event(event));
      assert.notEqual(f.route.bus.convolver, previous); assert.equal(previous.connections.size, 0); assert.equal(f.route.bus.impulse, impulse);
    }
    let previous = f.route.bus.convolver; f.context.currentTime += .1; f.audio.muted = true;
    assert.notEqual(f.route.bus.convolver, previous); previous = f.route.bus.convolver;
    f.context.currentTime += .1; f.route.setVolume(0); assert.notEqual(f.route.bus.convolver, previous);
    f.route.setVolume(.65); assert.equal(f.audio.volume, .65); assert.equal(f.audio.currentTime, 2.75);
    assert.equal(f.context.buffers.length, 1); assert.equal(f.context.media.length, 1); assert.equal(f.audio.playCalls, 0);
  } finally { f.route.destroy(); }
});

test('destroy while activation is pending prevents capture or delayed error callbacks', async () => {
  const f = fixture({reverb: .7});
  f.context.resumeGate = deferred(); const preparing = f.route.prepare(); await settle(); f.route.destroy();
  f.context.resumeGate.resolve(); await preparing;
  assert.equal(f.context.media.length, 0); assert.equal(f.context.buffers.length, 0); assert.equal(f.route.bus, null);
  await f.audio.play(); await settle(); assert.equal(f.context.media.length, 0); assert.equal(f.playbackErrors.length, 0);
  await assert.rejects(f.route.prepare(), /closed/);
});

test('a rejected activation after destroy cannot report an error from the discarded route', async () => {
  const f = fixture({reverb: .7});
  f.context.resumeGate = deferred(); const preparing = f.route.prepare(); await settle();
  f.route.destroy(); f.context.resumeGate.reject(new Error('Late activation failure'));
  await preparing;
  assert.equal(f.context.media.length, 0); assert.equal(f.route.bus, null);
  assert.equal(f.errors.length, 0); assert.equal(f.playbackErrors.length, 0);
  assert.equal(f.audio.pauseCalls, 0); assert.equal(f.audio.playCalls, 0);
});

test('a replacement route reuses the retained source/context and leaves one working graph', async () => {
  const first = fixture({reverb: .6}); let replacement;
  try {
    await first.route.prepare(); const source = first.route.source, oldBus = first.route.bus;
    replacement = new MediaAudioRoute({audio: first.audio, getContext: () => { throw new Error('A captured element must retain its context'); }, reverb: .4});
    assert.equal(first.route.destroyed, true); assert.equal(oldBus.input.connections.size, 0); assert.equal(source.connections.has(first.context.destination), true);
    await replacement.prepare(); assert.equal(replacement.source, source); assert.equal(first.context.media.length, 1);
    assert.equal(source.connections.has(first.context.destination), false); assert.equal(source.connections.has(replacement.bus.input), true);
    replacement.destroy(); assert.equal(source.connections.has(first.context.destination), true); assert.equal(replacement.bus, null);
  } finally { replacement?.destroy(); first.route.destroy(); }
});
