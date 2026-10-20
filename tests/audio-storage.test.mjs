import test from 'node:test';
import assert from 'node:assert/strict';
import { storageBytes, storageDescription, supportsNativeOpus } from '../public/audio-storage.js';
import { ImportDialog } from '../public/import-dialog.js';

test('storage facts report exact byte units and source bitrate without inventing unavailable values', () => {
  assert.equal(storageBytes(0), '0 B'); assert.equal(storageBytes(2048), '2.0 KiB');
  assert.equal(storageBytes(2097152), '2.00 MiB'); assert.equal(storageBytes(null), 'Size unavailable');
  assert.equal(storageDescription({ codec: 'opus', bitrate: 130800, bytes: 2097152 }), 'OPUS · 130.8 kb/s · 2.00 MiB');
  assert.equal(storageDescription({ codec: 'flac', bitrate: null, bytes: 1024 }), 'FLAC · 1.0 KiB');
  assert.equal(storageDescription(null), 'Details unavailable');
});

test('native Opus capability is explicit and unavailable browser support chooses compatibility fallback', () => {
  for (const result of ['probably', 'maybe']) assert.equal(supportsNativeOpus(() => ({ canPlayType: type => { assert.equal(type, 'audio/webm; codecs="opus"'); return result; } })), true);
  assert.equal(supportsNativeOpus(() => ({ canPlayType: () => '' })), false);
  assert.equal(supportsNativeOpus(() => { throw new Error('No native audio'); }), false);
  assert.equal(storageDescription({ codec: 'opus', bitrate: 130800, bitrateEstimated: true, bytes: 1024 }), 'OPUS · ≈130.8 kb/s · 1.0 KiB');
});

function singleImport() {
  const nodes = { '#import-start': { value: '' }, '#import-end': { value: '' }, '#import-exact': { checked: false }, '#import-loop': { checked: false } };
  const requests = [], controller = Object.create(ImportDialog.prototype);
  Object.assign(controller, { source: 'file', metadataMode: 'auto', files: [{ name: 'recording.opus' }], phase: 'idle', generation: 0,
    $: selector => nodes[selector], discard() {}, render() {}, status() {}, showPreview(job) { this.job = job; },
    api: async (url, options) => { requests.push({ url, options }); return { job: { id: 'preview-1', status: 'ready' } }; },
  });
  return { controller, requests };
}

test('new upload preparation sends browser capability with source-preserving defaults', async () => {
  const { controller, requests } = singleImport();
  await controller.submit();
  assert.equal(requests[0].url, '/api/import-jobs/file');
  const options = JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(requests[0].options.headers['X-Track-Metadata']), ch => ch.charCodeAt(0))));
  assert.equal(options.allowNativeOpus, false);
  assert.equal(Object.hasOwn(options, 'storageProfile'), false);
  assert.deepEqual(options.metadata, {});
});

test('changing preparation options discards the old preview and blocks its later commit', () => {
  const { controller } = singleImport(), discarded = [];
  controller.job = { id: 'old', status: 'ready' }; controller.phase = 'ready';
  controller.discard = job => discarded.push(job.id); controller.pausePreview = () => {};
  controller.invalidate();
  assert.deepEqual(discarded, ['old']); assert.equal(controller.job, null); assert.equal(controller.phase, 'idle');
});
