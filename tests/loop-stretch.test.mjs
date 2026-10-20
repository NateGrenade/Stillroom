import test from 'node:test';
import assert from 'node:assert/strict';
import { CircularWSOLA } from '../public/loop-stretch.js';

const tone = (frequency, frames, sampleRate = 48000, amplitude = 0.8) =>
  Float32Array.from({ length: frames }, (_, index) => amplitude * Math.sin(2 * Math.PI * frequency * index / sampleRate));

function render(engine, frames, rate = engine.rate, quantum = 128) {
  const result = engine.channels.map(() => new Float32Array(frames));
  for (let start = 0; start < frames; start += quantum) {
    const count = Math.min(quantum, frames - start);
    engine.process(result.map(channel => channel.subarray(start, start + count)), rate);
  }
  return result;
}

function measuredFrequency(samples, sampleRate) {
  const crossings = [];
  for (let index = 1; index < samples.length; index++) {
    if (samples[index - 1] < 0 && samples[index] >= 0) {
      crossings.push(index - 1 - samples[index - 1] / (samples[index] - samples[index - 1]));
    }
  }
  assert.ok(crossings.length > 5);
  return sampleRate * (crossings.length - 1) / (crossings.at(-1) - crossings[0]);
}

function assertFinite(samples) {
  for (const sample of samples) assert.ok(Number.isFinite(sample), 'output is finite');
}

test('preserves 40/55 Hz bass and 440 Hz pitch at every supported speed, through many circular wraps', () => {
  for (const sampleRate of [44100, 48000]) for (const frequency of [40, 55, 440]) for (const rate of [0.5, 0.75, 1, 1.5, 2]) {
    const engine = new CircularWSOLA({ channels: [tone(frequency, sampleRate / 5, sampleRate)], sampleRate, rate });
    const [output] = render(engine, sampleRate * 2, rate);
    assertFinite(output);
    const measured = measuredFrequency(output.subarray(sampleRate / 5), sampleRate);
    assert.ok(Math.abs(measured - frequency) < frequency * 0.003, `${sampleRate} Hz source, ${frequency} Hz tone, ${rate}x measured ${measured}`);
    let longestSilence = 0, silence = 0;
    for (const sample of output) {
      silence = Math.abs(sample) < 1e-6 ? silence + 1 : 0;
      longestSilence = Math.max(longestSilence, silence);
      assert.ok(Math.abs(sample) <= 0.800001, 'complementary overlap gains do not increase amplitude');
    }
    assert.ok(longestSilence <= 2, `${rate}x inserted ${longestSilence} silent samples`);
    assert.equal(engine.ended, false);
  }
});

test('one-times playback is transparent, starts immediately, and wraps without an inserted render gap', () => {
  const source = Float32Array.from({ length: 10007 }, (_, index) => 0.2 + index / 20000);
  const engine = new CircularWSOLA({ channels: [source], sampleRate: 48000 });
  const [output] = render(engine, source.length * 8 + 99);
  for (let index = 0; index < output.length; index++) assert.ok(Math.abs(output[index] - source[index % source.length]) < 1e-6, `sample ${index}`);
});

test('uses one candidate for all stereo channels and does not cancel antiphase correlation', () => {
  const left = Float32Array.from({ length: 10003 }, (_, index) => 0.5 * Math.sin(index * 0.043) + 0.2 * Math.sin(index * 0.113));
  const right = Float32Array.from(left, value => -value);
  for (const rate of [0.5, 1.5, 2]) {
    const stereo = new CircularWSOLA({ channels: [left, right], sampleRate: 48000, offset: 0.203, rate });
    const mono = new CircularWSOLA({ channels: [left], sampleRate: 48000, offset: 0.203, rate });
    const [stereoLeft, stereoRight] = render(stereo, 48000, rate);
    const [monoOutput] = render(mono, 48000, rate);
    for (let index = 0; index < stereoLeft.length; index++) {
      assert.ok(Math.abs(stereoLeft[index] + stereoRight[index]) < 1e-7, 'stereo relationship survives wrap');
      assert.ok(Math.abs(stereoLeft[index] - monoOutput[index]) < 1e-6, 'antiphase stereo matches mono alignment');
    }
  }
});

test('finite output ends on logical time, independent of grain lookahead, offset, and quantum size', () => {
  for (const rate of [0.5, 0.75, 1, 1.5, 1.7, 2]) for (const offsetFrames of [0, 1000, 9990]) {
    const source = new Float32Array(10003).fill(0.4);
    const engine = new CircularWSOLA({ channels: [source], sampleRate: 48000, offset: offsetFrames / 48000, rate, loop: false });
    const expected = Math.ceil((source.length - offsetFrames) / rate);
    const [output] = render(engine, expected + 300, rate, 127);
    assert.ok(output.subarray(0, expected).every(sample => Math.abs(sample - 0.4) < 1e-6), `${rate}x finite voice stopped early`);
    assert.ok(output.subarray(expected).every(sample => sample === 0), `${rate}x finite voice stopped late`);
    assert.equal(engine.position, source.length / 48000);
    assert.equal(engine.ended, true);
  }
});

test('fractional speeds do not accumulate an extra output frame on exact-divisible durations', () => {
  for (const [length, rate] of [[100000, 0.8], [44100, 0.9], [44100, 1.2], [48000, 1.2]]) {
    const engine = new CircularWSOLA({ channels: [new Float32Array(length).fill(0.5)], sampleRate: 48000, rate, loop: false });
    const expected = Math.ceil(length / rate);
    const [output] = render(engine, expected + 20, rate);
    assert.ok(output.subarray(0, expected).every(sample => sample === 0.5));
    assert.ok(output.subarray(expected).every(sample => sample === 0));
  }
});

test('finite lookahead extends the outro without copying intro samples or ending early', () => {
  const source = new Float32Array(4800).fill(0.6);
  source.fill(-0.8, 0, 960);
  const engine = new CircularWSOLA({ channels: [source], sampleRate: 48000, offset: 0.098, rate: 0.5, loop: false });
  const [output] = render(engine, 400);
  assert.ok(output.subarray(0, 192).every(sample => Math.abs(sample - 0.6) < 1e-6));
  assert.ok(output.subarray(192).every(sample => sample === 0));
});

test('a finite near-end seek cannot leak a nonzero intro into a silent outro', () => {
  const source = new Float32Array(1000);
  source.fill(1, 0, 100);
  const engine = new CircularWSOLA({ channels: [source], sampleRate: 1000, offset: 0.99, rate: 0.5, loop: false });
  const output = new Float32Array(40).fill(2);
  assert.equal(engine.process([output]), false);
  assert.ok(output.every(sample => sample === 0));
  assert.equal(engine.position, 1);
});

test('turning repeat off discards already cached circular intro samples', () => {
  const source = new Float32Array(1000);
  source.fill(1, 0, 100);
  const engine = new CircularWSOLA({ channels: [source], sampleRate: 1000, offset: 0.99, rate: 0.5, loop: true });
  engine.process([new Float32Array(1)]);
  engine.setLoop(false);
  assert.equal(engine.position, 0.9905);
  const output = new Float32Array(40).fill(2);
  assert.equal(engine.process([output]), false);
  assert.ok(output.every(sample => sample === 0));
  assert.equal(engine.position, 1);
});

test('repeat changes preserve cached waveform phase away from a loop boundary', () => {
  const source = tone(440, 48000, 48000);
  for (const rate of [0.5, 1, 1.5, 2]) {
    const switched = new CircularWSOLA({ channels: [source], sampleRate: 48000, rate });
    const control = new CircularWSOLA({ channels: [source], sampleRate: 48000, rate });
    const [before] = render(switched, 12347, rate);
    render(control, 12347, rate);
    switched.setLoop(false);
    const [after] = render(switched, 2000, rate);
    const [expected] = render(control, 2000, rate);
    assert.ok(Math.abs(after[0] - before.at(-1)) < 0.047, `${rate}x mode switch introduced a waveform jump`);
    assert.deepEqual(after, expected, `${rate}x changed valid cached waveform phase`);
    switched.setLoop(true);
    const [back] = render(switched, 2000, rate);
    const [controlBack] = render(control, 2000, rate);
    assert.deepEqual(back, controlBack, `${rate}x enabling repeat changed valid waveform phase`);
  }
});

test('repeat mode changes after a wrap preserve phase in the new logical cycle', () => {
  const source = tone(440, 48000, 48000);
  for (const rate of [0.5, 1.5, 2]) {
    const switched = new CircularWSOLA({ channels: [source], sampleRate: 48000, offset: 0.99, rate });
    const control = new CircularWSOLA({ channels: [source], sampleRate: 48000, offset: 0.99, rate });
    const frames = Math.ceil(600 / rate);
    render(switched, frames, rate);
    render(control, frames, rate);
    switched.setLoop(false);
    const [after] = render(switched, 2000, rate);
    const [expected] = render(control, 2000, rate);
    assert.deepEqual(after, expected, `${rate}x rewrote valid context from a preceding cycle`);
  }
});

test('loop mode can stop at the next cycle after many wraps and live rates use an independent fractional clock', () => {
  const source = tone(55, 44100, 44100);
  const engine = new CircularWSOLA({ channels: [source], sampleRate: 44100, rate: 1.3 });
  render(engine, 100003, 1.3, 127);
  const firstPosition = (100003 * 1.3) % source.length;
  assert.ok(Math.abs(engine.position * 44100 - firstPosition) < 1e-6);
  render(engine, 12345, 0.65, 61);
  const position = (firstPosition + 12345 * 0.65) % source.length;
  assert.ok(Math.abs(engine.position * 44100 - position) < 1e-6);
  engine.setLoop(false);
  const expected = Math.ceil((source.length - position) / 0.65);
  assert.equal(engine.process([new Float32Array(expected - 1)], 0.65), true);
  assert.equal(engine.process([new Float32Array(1)], 0.65), false);
  assert.equal(engine.position, 1);
});

test('rate changes keep pitch and finite samples, and stop silences the next quantum', () => {
  const engine = new CircularWSOLA({ channels: [tone(220, 9600)], sampleRate: 48000 });
  for (const rate of [1, 0.5, 2, 0.75, 1.5]) {
    const [output] = render(engine, 48000, rate);
    assertFinite(output);
    assert.ok(Math.abs(measuredFrequency(output.subarray(4800), 48000) - 220) < 0.5);
  }
  engine.stop();
  const output = new Float32Array(128).fill(1);
  assert.equal(engine.process([output]), false);
  assert.ok(output.every(sample => sample === 0));
  assert.equal(engine.stopped, true);
});

test('tiny circular clips and nonzero seek offsets remain finite', () => {
  for (const length of [1, 2, 17, 127]) for (const loop of [true, false]) {
    const engine = new CircularWSOLA({ channels: [new Float32Array(length).fill(0.25)], sampleRate: 48000, offset: length * 5 / 48000, loop });
    const [output] = render(engine, 10000, 0.5);
    assertFinite(output);
    assert.ok(output.every(sample => sample === (loop ? 0.25 : 0)));
  }
});

async function workletProcessor() {
  const previousProcessor = globalThis.AudioWorkletProcessor, previousRegister = globalThis.registerProcessor;
  let Processor, processorName;
  globalThis.AudioWorkletProcessor = class {
    constructor() { this.messages = []; this.transferred = []; this.port = { postMessage: (message, transfer = []) => {
      this.messages.push(structuredClone(message, { transfer })); this.transferred.push(transfer);
    } }; }
  };
  globalThis.registerProcessor = (name, processor) => { processorName = name; Processor = processor; };
  try {
    await import(`../public/loop-stretch.js?worklet-test=${Math.random()}`);
    assert.equal(processorName, 'stillroom-loop-stretch');
    return Processor;
  } finally {
    if (previousProcessor === undefined) delete globalThis.AudioWorkletProcessor; else globalThis.AudioWorkletProcessor = previousProcessor;
    if (previousRegister === undefined) delete globalThis.registerProcessor; else globalThis.registerProcessor = previousRegister;
  }
}

test('worklet stays reusable when idle, paused, or ended; only disposal terminates it', async () => {
  const Processor = await workletProcessor();
  assert.deepEqual(Processor.parameterDescriptors, [{ name: 'playbackRate', defaultValue: 1, minValue: 0.5, maxValue: 2, automationRate: 'k-rate' }]);
  const processor = new Processor();
  const send = data => processor.port.onmessage({ data });
  const output = [new Float32Array(128).fill(2)];
  assert.equal(processor.process([], [output], { playbackRate: [1] }), true);
  assert.ok(output[0].every(sample => sample === 0));
  send({ type: 'load', channels: [new Float32Array(193).fill(0.5)], sampleRate: 48000, loop: false });
  assert.equal(processor.engine.position, 0);
  send({ type: 'play', serial: 1, loop: false });
  processor.process([], [output], { playbackRate: [1] });
  send({ type: 'pause', serial: 2 });
  const position = processor.engine.position;
  assert.equal(processor.process([], [output], { playbackRate: [1] }), true);
  assert.equal(processor.engine.position, position);
  assert.ok(output[0].every(sample => sample === 0));
  send({ type: 'play', serial: 3, loop: false });
  assert.equal(processor.process([], [output], { playbackRate: [1] }), true);
  assert.equal(processor.process([], [output], { playbackRate: [1] }), true);
  assert.deepEqual(processor.messages, [{ type: 'ended', serial: 3 }]);
  send({ type: 'seek', offset: 0, serial: 4 });
  send({ type: 'play', serial: 5, loop: false });
  processor.process([], [output], { playbackRate: [1] });
  assert.equal(output[0][0], 0.5);
  send({ type: 'dispose' });
  assert.equal(processor.engine, null);
  assert.equal(processor.process([], [output], { playbackRate: [1] }), false);
  assert.ok(output[0].every(sample => sample === 0));
  assert.deepEqual(processor.messages, [{ type: 'ended', serial: 3 }]);
});

test('worklet returns bit-exact PCM with transfer ownership and clears its engine', async () => {
  const Processor = await workletProcessor(), processor = new Processor();
  const channels = [tone(440, 1007), tone(55, 1007)], expected = channels.map(channel => channel.slice());
  const send = data => processor.port.onmessage({ data });
  send({ type: 'load', channels, sampleRate: 48000, loop: true });
  send({ type: 'play', serial: 1, loop: true });
  processor.process([], [[new Float32Array(128), new Float32Array(128)]], { playbackRate: [1.5] });
  send({ type: 'release' });
  assert.equal(processor.engine, null); assert.equal(processor.active, false);
  assert.ok(channels.every(channel => channel.byteLength === 0));
  assert.ok(processor.transferred[0].every(buffer => buffer.byteLength === 0));
  assert.equal(processor.messages[0].type, 'released');
  assert.equal(processor.messages[0].sampleRate, 48000);
  assert.deepEqual(processor.messages[0].channels, expected);
  assert.equal(processor.process([], [[new Float32Array(128)]], { playbackRate: [1] }), true);
});

test('worklet command failures report an explicit error and terminate instead of leaving an owner waiting', async () => {
  const Processor = await workletProcessor();
  for (const failure of ['invalid load', 'missing PCM', 'return transfer']) {
    const processor = new Processor(), send = data => processor.port.onmessage({ data });
    if (failure === 'invalid load') send({ type: 'load', channels: [], sampleRate: 48000 });
    else {
      if (failure === 'return transfer') {
        send({ type: 'load', channels: [tone(440, 1007)], sampleRate: 48000 });
        const post = processor.port.postMessage;
        processor.port.postMessage = (message, transfer) => {
          if (message.type === 'released') throw new DOMException('Injected return failure', 'DataCloneError');
          post(message, transfer);
        };
      }
      send({ type: 'release' });
    }
    assert.deepEqual(processor.messages, [{ type: 'error' }], failure);
    assert.equal(processor.engine, null); assert.equal(processor.active, false);
    const output = [new Float32Array(128).fill(1)];
    assert.equal(processor.process([], [output], { playbackRate: [1] }), false);
    assert.ok(output[0].every(sample => sample === 0));
    send({ type: 'release' });assert.equal(processor.messages.length, 1);
  }
});

test('seeking reuses PCM and scratch arrays and produces the same finite or circular output as a fresh engine', () => {
  for (const loop of [true, false]) for (const offset of [0, 0.09, 0.0999, 0.2]) {
    const channels = [tone(440, 4800), tone(55, 4800)];
    const engine = new CircularWSOLA({ channels, sampleRate: 48000, rate: 1.5, loop });
    render(engine, 1234);
    const grain = engine._grain, tail = engine._tail, weights = engine._weights;
    engine.seek(offset);
    assert.equal(engine.channels, channels); assert.equal(engine._grain, grain); assert.equal(engine._tail, tail); assert.equal(engine._weights, weights);
    const fresh = new CircularWSOLA({ channels, sampleRate: 48000, rate: 1.5, loop, offset });
    assert.deepEqual(render(engine, 5000), render(fresh, 5000));
  }
});

test('rejects invalid PCM and keeps invalid rates bounded', () => {
  for (const channels of [[], [new Float32Array()], [new Float32Array(10), new Float32Array(11)], [[1, 2]]]) {
    assert.throws(() => new CircularWSOLA({ channels, sampleRate: 48000 }), TypeError);
  }
  assert.throws(() => new CircularWSOLA({ channels: [new Float32Array(10)], sampleRate: NaN }), TypeError);
  const engine = new CircularWSOLA({ channels: [new Float32Array(10)], sampleRate: 48000, rate: 9 });
  assert.equal(engine.rate, 2);
  engine.process([new Float32Array(128)], NaN);
  assert.equal(engine.rate, 1);
});
