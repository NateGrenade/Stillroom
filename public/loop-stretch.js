/**
 * Small circular WSOLA player for decoded PCM at the AudioContext sample rate.
 * Grains retain the source sample spacing (and therefore its pitch); only their
 * analysis positions move faster/slower. Every overlap uses complementary gains.
 * Nothing here rewrites saved audio, adds loop guards, or schedules loop restarts.
 */
const MIN_RATE = 0.5;
const MAX_RATE = 2;
const clampRate = value => Number.isFinite(value) ? Math.max(MIN_RATE, Math.min(MAX_RATE, value)) : 1;
const wrap = (value, length) => ((value % length) + length) % length;

export class CircularWSOLA {
  constructor({ channels, sampleRate, offset = 0, rate = 1, loop = true } = {}) {
    if (!Array.isArray(channels) || !channels.length || !channels[0]?.length ||
        channels.some(channel => !(channel instanceof Float32Array) || channel.length !== channels[0].length)) {
      throw new TypeError('Stretch playback needs equally sized, nonempty Float32Array channels.');
    }
    if (!Number.isFinite(sampleRate) || sampleRate <= 0) throw new TypeError('Invalid stretch sample rate.');
    this.channels = channels;
    this.sampleRate = sampleRate;
    this.length = channels[0].length;
    this.rate = clampRate(rate);
    this.loop = !!loop;
    this.ended = false;
    this.stopped = false;
    this.hop = Math.max(32, Math.round(sampleRate * 0.02));
    this.search = Math.min(Math.round(sampleRate * 0.015), Math.floor(this.length / 2));
    this._tail = channels.map(() => new Float32Array(this.hop));
    this._grain = channels.map(() => new Float32Array(this.hop));
    this._weights = Float32Array.from({ length: this.hop }, (_, index) =>
      0.5 - 0.5 * Math.cos(Math.PI * (index + 0.5) / this.hop));
    this.seek(offset);
  }

  get position() { return this._position / this.sampleRate; }

  seek(offset = 0) {
    this._position = Number.isFinite(offset) ? Math.max(0, offset * this.sampleRate) : 0;
    this._position = this.loop ? wrap(this._position, this.length) : Math.min(this.length, this._position);
    this._absolutePosition = this._position;
    this._clockAbsoluteStart = this._absolutePosition;
    this._clockStart = this._position;
    this._clockFrames = 0;
    this._clockRate = this.rate;
    this._finiteCycleBase = 0;
    this._grainStart = null;
    this._previousStart = null;
    this._grainWasFirst = true;
    this._first = true;
    this._read = this.hop;
    this.stopped = false;
    this.ended = !this.loop && this._position >= this.length;
  }

  setLoop(value) {
    if (this.loop === !!value) return;
    this.loop = !!value;
    this._finiteCycleBase = Math.floor(this._absolutePosition / this.length) * this.length;
    this._refreshBoundaryContext();
    this._clockAbsoluteStart = this._absolutePosition;
    this._clockStart = this._position;
    this._clockFrames = 0;
  }

  _refreshBoundaryContext() {
    if (this._grainStart === null) return;
    const end = this._finiteCycleBase + this.length;
    const sampleIndex = index => this.loop || index < end
      ? wrap(index, this.length) : this._sourceIndex(index - this._finiteCycleBase);
    // Retain the exact WSOLA phase. Only cached contributions beyond the next
    // boundary need a different extension; valid cached audio stays untouched.
    for (let channel = 0; channel < this.channels.length; channel++) {
      const source = this.channels[channel], grain = this._grain[channel], tail = this._tail[channel];
      for (let index = this._read; index < this.hop; index++) {
        const current = this._grainStart + index;
        const previous = this._previousStart === null ? current : this._previousStart + this.hop + index;
        if (current >= end || (!this._grainWasFirst && previous >= end)) {
          const sample = source[sampleIndex(current)];
          const old = source[sampleIndex(previous)];
          grain[index] = this._grainWasFirst ? sample : old + (sample - old) * this._weights[index];
        }
      }
      for (let index = 0; index < this.hop; index++) {
        const current = this._grainStart + this.hop + index;
        if (current >= end) tail[index] = source[sampleIndex(current)];
      }
    }
  }

  stop() { this.stopped = true; this.ended = true; }

  _sourceIndex(index) {
    if (this.loop) return wrap(index, this.length);
    // Mirror just the terminal context for finite playback: a WSOLA lookahead
    // must neither leak the intro into the outro nor insert a silent pad.
    if (this.length === 1) return 0;
    const period = 2 * (this.length - 1), reflected = wrap(index, period);
    return reflected < this.length ? reflected : period - reflected;
  }

  // Both channels vote independently: averaging them would erase antiphase audio.
  _correlation(start, step, tailEnergy) {
    let dot = 0, energy = 0;
    for (let channel = 0; channel < this.channels.length; channel++) {
      const source = this.channels[channel], tail = this._tail[channel];
      let sourceIndex = this.loop ? wrap(start, this.length) : start;
      for (let index = 0; index < this.hop; index += step) {
        const sample = source[this.loop ? sourceIndex : this._sourceIndex(sourceIndex)];
        dot += tail[index] * sample;
        energy += sample * sample;
        sourceIndex += step;
        if (this.loop && sourceIndex >= this.length) sourceIndex %= this.length;
      }
    }
    return energy > 1e-14 ? dot / Math.sqrt(tailEnergy * energy) : -1;
  }

  _tailEnergy(step) {
    let energy = 0;
    for (const tail of this._tail) for (let index = 0; index < this.hop; index += step) energy += tail[index] * tail[index];
    return energy;
  }

  _chooseStart(nominal) {
    if (!this.loop) nominal = Math.max(0, Math.min(nominal, Math.max(0, this.length - this.hop)));
    const coarseStep = 8, fineStep = 4;
    const coarseEnergy = this._tailEnergy(coarseStep);
    if (coarseEnergy < 1e-14 || !this.search) return nominal;
    let winner = nominal, best = -Infinity, bestDistance = Infinity;
    const consider = (candidate, step, energy) => {
      if (!this.loop && (candidate < 0 || candidate > Math.max(0, this.length - this.hop))) return;
      const score = this._correlation(candidate, step, energy), distance = Math.abs(candidate - nominal);
      if (score > best + 1e-7 || (Math.abs(score - best) <= 1e-7 && distance < bestDistance)) {
        winner = candidate; best = score; bestDistance = distance;
      }
    };
    // The nominal candidate is always tested, including at an exact 1x rate.
    consider(nominal, coarseStep, coarseEnergy);
    for (let delta = -this.search; delta <= this.search; delta += coarseStep) consider(nominal + delta, coarseStep, coarseEnergy);
    const coarseWinner = winner;
    best = -Infinity; bestDistance = Infinity;
    const fineEnergy = this._tailEnergy(fineStep);
    for (let delta = -coarseStep + 1; delta < coarseStep; delta++) {
      const candidate = coarseWinner + delta;
      if (Math.abs(candidate - nominal) <= this.search) consider(candidate, fineStep, fineEnergy);
    }
    return winner;
  }

  _makeGrain() {
    // This clock is the integral of playbackRate, never the selected match. Using
    // the winning offset as the next origin would gradually change actual speed.
    const nominal = Math.floor(this._position);
    const start = this._first ? nominal : this._chooseStart(nominal);
    this._previousStart = this._grainStart;
    const cycleBase = this.loop ? Math.floor(this._absolutePosition / this.length) * this.length : this._finiteCycleBase;
    this._grainStart = cycleBase + start;
    this._grainWasFirst = this._first;
    for (let channel = 0; channel < this.channels.length; channel++) {
      const source = this.channels[channel], tail = this._tail[channel], grain = this._grain[channel];
      let sourceIndex = this.loop ? wrap(start, this.length) : start;
      for (let index = 0; index < this.hop; index++) {
        const sample = source[this.loop ? sourceIndex : this._sourceIndex(sourceIndex)];
        grain[index] = this._first ? sample : tail[index] + (sample - tail[index]) * this._weights[index];
        sourceIndex++;
        if (this.loop && sourceIndex === this.length) sourceIndex = 0;
      }
      for (let index = 0; index < this.hop; index++) {
        tail[index] = source[this.loop ? sourceIndex : this._sourceIndex(sourceIndex)];
        sourceIndex++;
        if (this.loop && sourceIndex === this.length) sourceIndex = 0;
      }
    }
    // Lookahead never advances the logical output clock or changes duration.
    this._first = false;
    this._read = 0;
  }

  /** Fill a render quantum; false means this voice has reached EOF or was stopped. */
  process(outputChannels, rate = this.rate) {
    this.rate = clampRate(rate);
    if (this.rate !== this._clockRate) {
      this._clockAbsoluteStart = this._absolutePosition;
      this._clockStart = this._position;
      this._clockFrames = 0;
      this._clockRate = this.rate;
    }
    if (!outputChannels.length) return !this.ended;
    const frames = outputChannels[0].length;
    for (const output of outputChannels) {
      if (output.length !== frames) throw new TypeError('Stretch output channel lengths must match.');
      output.fill(0);
    }
    if (this.ended) return false;
    let written = 0;
    while (written < frames && !this.ended) {
      if (this._read === this.hop) this._makeGrain();
      let count = Math.min(frames - written, this.hop - this._read);
      // Count logical output frames from the last rate/mode change. Repeated
      // floating-point additions otherwise add one silent frame at some rates.
      const endFrame = this.loop ? Infinity : Math.ceil((this.length - this._clockStart) / this.rate);
      count = Math.min(count, endFrame - this._clockFrames);
      for (let channel = 0; channel < outputChannels.length; channel++) {
        const grain = this._grain[Math.min(channel, this._grain.length - 1)];
        for (let index = 0; index < count; index++) outputChannels[channel][written + index] = grain[this._read + index];
      }
      this._read += count;
      written += count;
      this._clockFrames += count;
      this._absolutePosition = this._clockAbsoluteStart + this._clockFrames * this.rate;
      this._position = this._clockStart + this._clockFrames * this.rate;
      if (this.loop) this._position = wrap(this._absolutePosition, this.length);
      else if (this._clockFrames >= endFrame) { this._position = this.length; this.ended = true; }
    }
    return !this.ended;
  }
}

// Keeping registration guarded makes the exact DSP engine importable by Node tests.
if (typeof registerProcessor === 'function' && typeof AudioWorkletProcessor === 'function') {
  class StillroomLoopStretchProcessor extends AudioWorkletProcessor {
    static get parameterDescriptors() {
      return [{ name: 'playbackRate', defaultValue: 1, minValue: MIN_RATE, maxValue: MAX_RATE, automationRate: 'k-rate' }];
    }

    constructor() {
      super();
      this.engine = null;
      this.active = false;
      this.disposed = false;
      this.serial = 0;
      this.port.onmessage = ({ data }) => {
        if (this.disposed) return;
        try {
          if (data?.type === 'load') this.engine = new CircularWSOLA(data);
          else if (data?.type === 'loop') this.engine?.setLoop(data.value);
          else if (data?.type === 'play') {
            this.serial = data.serial;
            this.engine?.setLoop(data.loop);
            // Align with the transport clock on resume, just as a new native
            // source would, without allocating another PCM or scratch buffer.
            if (Number.isFinite(data.offset)) this.engine?.seek(data.offset);
            this.active = !!this.engine;
          } else if (data?.type === 'pause') { this.serial = data.serial; this.active = false; }
          else if (data?.type === 'seek') { this.serial = data.serial; this.engine?.seek(data.offset); }
          else if (data?.type === 'release') {
            this.active = false;
            if (!this.engine) throw new Error('Loop PCM is unavailable');
            const { channels, sampleRate } = this.engine;
            this.engine = null;
            this.port.postMessage({ type: 'released', channels, sampleRate }, channels.map(channel => channel.buffer));
          } else if (data?.type === 'dispose') {
            this.active = false; this.engine = null; this.disposed = true;
          }
        } catch {
          // Port callback exceptions do not reliably raise node.processorerror.
          // Explicitly settle the owner/reclaim path before terminating it.
          this.active = false; this.engine = null; this.disposed = true;
          this.port.postMessage({ type: 'error' });
        }
      };
    }

    process(inputs, outputs, parameters) {
      const channels = outputs[0] || [];
      if (this.active && this.engine) {
        if (!this.engine.process(channels, parameters.playbackRate?.[0] ?? this.engine.rate)) {
          this.active = false;
          this.port.postMessage({ type: 'ended', serial: this.serial });
        }
      } else {
        for (const channel of channels) channel.fill(0);
      }
      // Returning false ends a Chromium worklet permanently. A paused or ended
      // owner must remain reusable without another full PCM allocation.
      return !this.disposed;
    }
  }
  registerProcessor('stillroom-loop-stretch', StillroomLoopStretchProcessor);
}
