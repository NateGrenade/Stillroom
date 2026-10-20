import test from 'node:test';
import assert from 'node:assert/strict';
import { parseTimestamp, validateTimes, preciseTime, loopRegion, LoopPreview, ImportDialog } from '../public/import-dialog.js';

test('timestamps support full videos, long excerpts and precise fractional boundaries', () => {
  assert.equal(parseTimestamp(''), null);
  assert.equal(parseTimestamp(' 10:00 '), 600);
  assert.equal(parseTimestamp('1:20:00.25'), 4800.25);
  assert.equal(parseTimestamp('600.125'), 600.125);
  assert.deepEqual(validateTimes('10:00', '20:00'), { start: 600, end: 1200 });
  assert.deepEqual(validateTimes('', '1:30'), { start: null, end: 90 });
  assert.equal(preciseTime(1200.503), '20:00.503');
  assert.equal(preciseTime(3599.9999), '1:00:00');
});

test('invalid or reversed timestamps fail before starting acquisition', () => {
  for (const value of ['-1', '1:99', '1:60:00', '1::2', '1:2:3:4', 'NaN', 'Infinity', 'hello']) assert.throws(() => parseTimestamp(value));
  assert.throws(() => validateTimes('10:00', '9:59'));
  assert.throws(() => validateTimes('', '0'));
  assert.throws(() => validateTimes('1', '1'));
});

test('preview uses the same sample-rounded inner loop as the saved player', () => {
  const buffer = { length: 480000, sampleRate: 48000 };
  assert.deepEqual(loopRegion({ loopStart: .500009, loopEnd: 9.500001 }, buffer), { start: .5, end: 9.5 });
  assert.deepEqual(loopRegion({ loopStart: .5, loopEnd: 10.001 }, buffer), { start: .5, end: 10 });
  assert.throws(() => loopRegion({ loopStart: 4, loopEnd: 2 }, buffer));
  assert.throws(() => loopRegion({ loopStart: 0, loopEnd: 11 }, buffer));
});

function previewFixture(fetchAudio) {
  const starts = [], voices = [];
  const context = {
    currentTime: 0, destination: {}, createGain: () => ({ gain: { value: 1 }, connect() {} }), resume: async () => {}, close: async () => {},
    decodeAudioData: async () => ({ length: 480000, sampleRate: 48000 }),
    createBufferSource: () => { const voice = { connect() {}, disconnect() {}, stop() { this.stopped = true; }, start(...args) { starts.push(args); } }; voices.push(voice); return voice; },
  };
  const preview = new LoopPreview({ contextFactory: () => context, fetchAudio: fetchAudio || (async () => ({ ok: true, arrayBuffer: async () => new ArrayBuffer(1) })) });
  return { context, preview, starts, voices };
}

test('Hear the join repeats the prepared inner cycle without encoding guards', async () => {
  const { preview, starts, voices, context } = previewFixture();
  preview.setJob({ audioUrl: '/preview.wav', loopStart: .5, loopEnd: 9.5, duration: 9 });
  await preview.play(true);
  assert.equal(voices[0].loop, true);
  assert.equal(voices[0].loopStart, .5);
  assert.equal(voices[0].loopEnd, 9.5);
  assert.deepEqual(starts[0], [0, 6.5]);
  context.currentTime = 4;
  assert.equal(preview.position(), 1);
  preview.destroy();
  assert.equal(voices[0].stopped, true);
});

test('closing while preview audio downloads cannot start sound afterward', async () => {
  let resolveFetch;
  const { preview, voices } = previewFixture(() => new Promise(resolve => { resolveFetch = resolve; }));
  preview.setJob({ audioUrl: '/preview.wav', loopStart: 0, loopEnd: 9, duration: 9 });
  const loading = preview.play();
  await Promise.resolve();
  preview.destroy();
  resolveFetch({ ok: true, arrayBuffer: async () => new ArrayBuffer(1) });
  await loading;
  assert.equal(voices.length, 0);
  assert.equal(preview.playing, false);
});


function importController(checked) {
  const requests = [];
  const nodes = { '#import-youtube-url': { value: 'https://www.youtube.com/watch?v=Abcdef12345' }, '#import-youtube-account': { checked } };
  const controller = Object.create(ImportDialog.prototype);
  Object.assign(controller, { source: 'youtube', files: [], phase: 'idle', generation: 0, $: selector => nodes[selector], options: () => ({}), discard() {}, render() {}, status() {}, showPreview(job) { this.job = job; }, api: async (url, options) => { requests.push({ url, body: JSON.parse(options.body) }); return { job: { id: 'job', status: 'ready' } }; } });
  return { controller, requests };
}

test('Single video imports only use the account when that import explicitly opts in', async () => {
  for (const selected of [false, true, undefined]) {
    const { controller, requests } = importController(selected);
    await controller.submit();
    assert.equal(requests[0].url, '/api/import-jobs');
    assert.equal(requests[0].body.useYouTubeAccount, selected === true);
  }
});
