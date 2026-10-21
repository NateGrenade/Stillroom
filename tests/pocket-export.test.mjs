import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { normalizePocketSelection, pocketDeviceOptions, pocketJobProgress, PocketExportDialog } from '../public/pocket-export.js';

const a = 'a'.repeat(32), b = 'b'.repeat(32), c = 'c'.repeat(32), p = 'd'.repeat(32), request = 'f'.repeat(32);
const library = { tracks: [{ id: a, title: 'First', artist: 'A' }, { id: b, title: 'Second', artist: 'B', kind: 'loop' }, { id: c, title: 'Third', artist: 'C' }], playlists: [{ id: p, name: 'Saved order', trackIds: [b, a] }] };
const devices = { supported: true, platform: 'macos', devices: [{ id: 'phone-a', name: 'My iPhone', apps: [{ bundleId: 'com.stillroom.pocket.dev', name: 'Pocket Stillroom Dev', version: '0.2' }] }] };
const review = { reviewToken: 'review-token', trackCount: 2, playlistCount: 1, audioBytes: 4096, bundleBytes: 5000 };
const job = { id: 'job-1', requestId: request, state: 'preparing', phase: 'Preparing library', completedBytes: 0, totalBytes: 5000, message: 'Preparing selected songs', canCancel: true };
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
class Node extends EventTarget {
  constructor(tag = '') { super(); this.tagName = tag; this.value = ''; this.textContent = ''; this.checked = false; this.disabled = false; this.hidden = false; this.dataset = {}; this.attributes = {}; this.children = []; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  removeAttribute(key) { delete this.attributes[key]; }
  appendChild(node) { this.children.push(node); return node; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; if (this.tagName === 'select') this.value = nodes[0]?.value || ''; }
  focus() { this.focused = true; }
}
function setup(options = {}) {
  const names = ['title', 'mode', 'playlist', 'playlist-wrap', 'tracks-wrap', 'track-search', 'tracks', 'selected-count', 'select-visible', 'clear-tracks', 'device', 'refresh-devices', 'review', 'review-summary', 'device-help', 'start', 'cancel', 'check', 'new', 'status', 'selection-fields', 'job', 'phase', 'progress', 'job-message', 'next-step'];
  const nodes = Object.fromEntries(names.map(name => [name, new Node(['mode', 'playlist', 'device'].includes(name) ? 'select' : '')]));
  nodes.mode.value = 'all';
  const dialog = new Node('dialog'); dialog.ownerDocument = { createElement: tag => new Node(tag) };
  dialog.querySelector = selector => nodes[selector.replace('#pocket-export-', '')];
  dialog.close = () => { dialog.open = false; dialog.dispatchEvent(new Event('close')); };
  const calls = [], changes = [], closed = [], timers = [], marker = new Map(); let serverJob = { ...job };
  const storage = { getItem: key => marker.get(key) || null, setItem: (key, value) => marker.set(key, value), removeItem: key => marker.delete(key) };
  const handler = options.api || (async (path, requestOptions = {}) => {
    if (path.endsWith('/devices')) return devices;
    if (path.endsWith('/review')) return review;
    if (path === '/api/pocket-export/jobs') return requestOptions.method === 'POST' ? { job: serverJob } : { jobs: [serverJob] };
    if (path.endsWith('/check')) return { job: { ...serverJob, state: 'complete', phase: 'Copy complete' } };
    if (requestOptions.method === 'DELETE') return { job: { ...serverJob, state: 'cancelled', phase: 'Transfer cancelled' } };
    return { job: serverJob };
  });
  const instance = new PocketExportDialog({ dialog, api: async (...args) => { calls.push(args); return handler(...args); }, getLibrary: () => library,
    showDialog: node => { node.open = true; }, onClose: () => closed.push(true), onChange: value => changes.push(value),
    storage: options.storage || storage, createRequestId: () => request,
    ...(options.defaultTimers ? {} : { schedule: (callback, delay) => { const timer = { callback, delay }; timers.push(timer); return timer; }, clearSchedule: timer => { if (timer) timer.cancelled = true; } }) });
  return { instance, nodes, dialog, calls, changes, closed, timers, marker, setJob: value => { serverJob = value; } };
}
async function ready(f, selection = { mode: 'playlist', playlistId: p }) {
  await f.instance.open(selection); await f.instance.reviewSelection(); f.nodes.device.value = '0:0';
}

test('default browser timers retain the global receiver instead of the dialog instance', async () => {
  const previousSet = globalThis.setTimeout, previousClear = globalThis.clearTimeout;
  let scheduled = 0, cleared = 0;
  globalThis.setTimeout = function () { assert.equal(this, globalThis); ++scheduled; return 1; };
  globalThis.clearTimeout = function () { assert.equal(this, globalThis); ++cleared; };
  try {
    const f = setup({ defaultTimers: true }); await f.instance.open();
    assert.equal(f.nodes.status.textContent, ''); assert.equal(f.instance.deviceOptions.length, 1);
    assert.equal(scheduled, 1); assert.equal(cleared, 1);
  } finally { globalThis.setTimeout = previousSet; globalThis.clearTimeout = previousClear; }
});

test('selection modes are explicit, preserve custom order, and reject missing or invalid IDs', () => {
  assert.deepEqual(normalizePocketSelection({ mode: 'all', trackIds: [a] }, library), { mode: 'all' });
  assert.deepEqual(normalizePocketSelection({ mode: 'playlist', playlistId: p }, library), { mode: 'playlist', playlistId: p });
  assert.deepEqual(normalizePocketSelection({ mode: 'tracks', trackIds: [b, a, b] }, library), { mode: 'tracks', trackIds: [b, a] });
  for (const selection of [{ mode: 'tracks', trackIds: [] }, { mode: 'tracks', trackIds: ['../audio'] }, { mode: 'tracks', trackIds: ['e'.repeat(32)] }, { mode: 'playlist', playlistId: a }, { mode: 'anything' }]) assert.throws(() => normalizePocketSelection(selection, library));
});
test('device options retain exact phone/app identities and literal untrusted labels', () => {
  const hostile = { ...devices, devices: [{ ...devices.devices[0], name: '<img src=x>', apps: [...devices.devices[0].apps, { bundleId: '../bad', name: 'Bad' }] }] };
  const options = pocketDeviceOptions(hostile);
  assert.equal(options.length, 1); assert.equal(options[0].deviceId, 'phone-a'); assert.equal(options[0].bundleId, 'com.stillroom.pocket.dev');
  assert.match(options[0].label, /<img src=x>/);
  assert.deepEqual(pocketDeviceOptions({ ...devices, supported: false }), []);
});
test('phase progress never invents a denominator or exceeds safe bounds', () => {
  assert.equal(pocketJobProgress({ state: 'transferring', totalBytes: 100, completedBytes: 50 }).value, 50);
  assert.equal(pocketJobProgress({ state: 'transferring', totalBytes: 100, completedBytes: 999 }).value, 100);
  for (const pair of [[0, 1], [null, 2], [100, -1], [100, NaN], [Infinity, 1]]) assert.equal(pocketJobProgress({ totalBytes: pair[0], completedBytes: pair[1] }).value, null);
  assert.equal(pocketJobProgress({ state: 'complete' }).value, 100);
  assert.match(pocketJobProgress({ state: 'uncertain' }).label, /uncertain/);
});
test('terminal outcomes override a retained transferring phase in progress and persistent notices', () => {
  for (const [state, label] of [['cancelled', 'Transfer cancelled'], ['failed', 'Transfer failed'], ['uncertain', 'Copy outcome uncertain'], ['complete', 'Copy complete']]) {
    const progress = pocketJobProgress({ state, phase: 'transferring', completedBytes: 50, totalBytes: 100 });
    assert.equal(progress.label, label); assert.equal(progress.value, state === 'complete' ? 100 : 50);
  }
});
test('opening a single song only discovers devices, never auto-reviews or starts a copy', async () => {
  const f = setup(); await f.instance.open({ mode: 'tracks', trackIds: [a] });
  assert.deepEqual(f.calls.map(call => call[0]), ['/api/pocket-export/devices']);
  assert.equal(f.nodes.mode.value, 'tracks'); assert.equal(f.nodes.device.value, ''); assert.equal(f.nodes.start.disabled, true);
  assert.equal(f.nodes.title.focused, true); assert.equal(f.nodes['selected-count'].textContent, '1 selected');
  assert.equal(f.instance.job, null);
});
test('review sends a canonical playlist selection and copying still requires explicit device/app choice', async () => {
  const f = setup(); await f.instance.open({ mode: 'playlist', playlistId: p }); await f.instance.reviewSelection();
  assert.deepEqual(JSON.parse(f.calls.find(call => call[0].endsWith('/review'))[1].body), { selection: { mode: 'playlist', playlistId: p } });
  await f.instance.start(); assert.equal(f.calls.some(call => call[1]?.method === 'POST' && call[0].endsWith('/jobs')), false);
  assert.match(f.nodes.status.textContent, /exact iPhone/); assert.match(f.nodes['review-summary'].textContent, /2 songs · 1 playlists/);
});
test('an explicit copy snapshots review/device/app/request ID and closing leaves the job tracked', async () => {
  const f = setup(); await ready(f); await f.instance.start();
  const create = f.calls.find(call => call[0] === '/api/pocket-export/jobs' && call[1]?.method === 'POST');
  assert.deepEqual(JSON.parse(create[1].body), { selection: { mode: 'playlist', playlistId: p }, reviewToken: 'review-token', deviceId: 'phone-a', bundleId: 'com.stillroom.pocket.dev', requestId: request });
  f.dialog.close(); assert.equal(f.instance.active, true); assert.equal(f.closed.length, 1);
  assert.equal(f.calls.some(call => call[1]?.method === 'DELETE'), false); assert.equal(f.changes.at(-1).active, true);
  assert.equal(f.marker.size, 1); assert.equal(f.timers.at(-1).delay, 1200);
});
test('refreshing a reordered device list preserves the actual identity rather than its old index', async () => {
  let list = devices;
  const f = setup({ api: async path => path.endsWith('/devices') ? list : review });
  await f.instance.open(); f.nodes.device.value = '0:0';
  list = { ...devices, devices: [{ id: 'other-phone', name: 'Other', apps: devices.devices[0].apps }, ...devices.devices] };
  await f.instance.refreshDevices(); assert.equal(f.nodes.device.value, '1:0');
  list = { ...devices, devices: [] }; await f.instance.refreshDevices(); assert.equal(f.nodes.device.value, '');
});
test('editing selection invalidates an in-flight review without leaving the reviewer stuck', async () => {
  const pending = deferred();
  const f = setup({ api: async path => path.endsWith('/devices') ? devices : pending.promise });
  await f.instance.open(); const reviewing = f.instance.reviewSelection();
  f.nodes.mode.value = 'tracks'; f.instance.selectedTracks.add(b); f.instance.selectionChanged();
  pending.resolve(review); await reviewing;
  assert.equal(f.instance.review, null); assert.equal(f.instance.reviewing, false); assert.equal(f.nodes.review.disabled, false);
});
test('cancel is explicit, waits for authoritative confirmation, and never claims success early', async () => {
  const cancelling = deferred();
  const f = setup({ api: async (path, options = {}) => options.method === 'DELETE' ? cancelling.promise : path.endsWith('/devices') ? devices : path.endsWith('/review') ? review : { job } });
  await ready(f); await f.instance.start(); const stopping = f.instance.cancel();
  assert.equal(f.instance.job.state, 'preparing'); assert.equal(f.instance.stopping, true); assert.match(f.nodes.phase.textContent, /Cancelling/);
  assert.equal(f.calls.at(-1)[0], '/api/pocket-export/jobs/job-1'); assert.equal(f.calls.at(-1)[1].method, 'DELETE'); assert.equal(f.calls.at(-1)[1].body, undefined);
  cancelling.resolve({ job: { ...job, state: 'cancelled' } }); await stopping;
  assert.equal(f.instance.job.state, 'cancelled'); assert.equal(f.instance.active, false);
});
test('failed cancellation preserves a possibly running job and a later check can report completion', async () => {
  const f = setup({ api: async (path, options = {}) => { if (options.method === 'DELETE') throw new Error('Disconnected'); return path.endsWith('/devices') ? devices : path.endsWith('/review') ? review : { job: options.method === 'POST' ? job : { ...job, state: 'complete' } }; } });
  await ready(f); await f.instance.start(); await f.instance.cancel();
  assert.equal(f.instance.job.state, 'preparing'); assert.match(f.nodes.status.textContent, /not confirmed/);
  await f.instance.poll(); assert.equal(f.instance.job.state, 'complete'); assert.match(f.nodes['next-step'].textContent, /does not automatically import/);
});
test('lost creation responses reconcile by request ID before allowing another POST', async () => {
  let posted = 0;
  const f = setup({ api: async (path, options = {}) => {
    if (path.endsWith('/devices')) return devices; if (path.endsWith('/review')) return review;
    if (options.method === 'POST') { posted += 1; throw new Error('Response lost'); }
    return { jobs: [job] };
  } });
  await ready(f); await f.instance.start(); await f.instance.start();
  assert.equal(posted, 1); assert.equal(f.instance.job.id, job.id); assert.equal(f.instance.unknownCreation, false);
});
test('unresolved creation stays visible and blocked instead of retrying after a server restart', async () => {
  let posted = 0;
  const f = setup({ api: async (path, options = {}) => { if (path.endsWith('/devices')) return devices; if (path.endsWith('/review')) return review; if (options.method === 'POST') { ++posted; throw new Error('Disconnected'); } return { jobs: [] }; } });
  await ready(f); await f.instance.start(); f.dialog.close(); await f.instance.open(); await f.instance.start();
  assert.equal(posted, 1); assert.equal(f.instance.unknownCreation, true); assert.equal(f.nodes.start.disabled, true); assert.equal(f.changes.at(-1).unknownCreation, true);
  assert.match(f.nodes.status.textContent, /No matching transfer/);
});
test('a known review conflict clears authorization and does not treat a rejected job as running', async () => {
  const f = setup({ api: async (path, options = {}) => { if (path.endsWith('/devices')) return devices; if (path.endsWith('/review')) return review; throw Object.assign(new Error('Review expired'), { status: 409 }); } });
  await ready(f); await f.instance.start();
  assert.equal(f.instance.review, null); assert.equal(f.instance.unknownCreation, false); assert.equal(f.instance.requestId, null);
  assert.match(f.nodes.status.textContent, /Review expired/); assert.equal(f.calls.some(call => call[0] === '/api/pocket-export/jobs' && !call[1]), false);
});
test('a stale status response cannot undo a newer confirmed cancellation', async () => {
  const status = deferred();
  const f = setup({ api: async (path, options = {}) => path.endsWith('/devices') ? devices : path.endsWith('/review') ? review : options.method === 'POST' ? { job } : options.method === 'DELETE' ? { job: { ...job, state: 'cancelled' } } : status.promise });
  await ready(f); await f.instance.start(); const polling = f.instance.poll(); await f.instance.cancel();
  status.resolve({ job: { ...job, state: 'transferring' } }); await polling;
  assert.equal(f.instance.job.state, 'cancelled');
});
test('checking an uncertain device copy uses the read-only check action without re-uploading', async () => {
  const f = setup(); f.setJob({ ...job, state: 'uncertain' }); await ready(f); await f.instance.start(); await f.instance.check();
  const checks = f.calls.filter(call => call[0].endsWith('/check'));
  assert.equal(checks.length, 1); assert.equal(checks[0][1].method, 'POST'); assert.deepEqual(JSON.parse(checks[0][1].body), {});
  assert.equal(f.calls.filter(call => call[0] === '/api/pocket-export/jobs' && call[1]?.method === 'POST').length, 1);
  assert.equal(f.instance.job.state, 'complete');
});
test('a persisted request resumes by discovery without creating or discovering a new device copy', async () => {
  const storage = { getItem: () => JSON.stringify({ requestId: request, jobId: job.id }), setItem() {}, removeItem() {} };
  const f = setup({ storage }); await f.instance.resume();
  assert.deepEqual(f.calls.map(call => call[0]), ['/api/pocket-export/jobs']); assert.equal(f.instance.job.id, job.id);
});
test('modal, entry points and labels preserve accessibility and distinguish copy from import', () => {
  const html = readFileSync(new URL('../public/index.html', import.meta.url), 'utf8');
  const app = readFileSync(new URL('../public/app.js', import.meta.url), 'utf8');
  const module = readFileSync(new URL('../public/pocket-export.js', import.meta.url), 'utf8');
  assert.match(html, /id="pocket-export-dialog"[^>]*aria-labelledby="pocket-export-title"/);
  assert.match(html, /id="pocket-export-device"/); assert.match(html, /id="pocket-export-notice"[^>]*aria-live="polite"/);
  assert.match(html, /copied without re-encoding/); assert.match(html, /Accounts, cookies, listening history and runtime files are excluded/);
  assert.match(app, /data-menu-action="pocket-export"/); assert.match(app, /id="export-pocket"|\$\('#export-pocket'\)/);
  assert.doesNotMatch(module, /innerHTML|player\.pause|MusicPlayer/);
});

test('request timeouts reconcile a possibly started copy instead of blindly resubmitting', async () => {
  const never = deferred(); let posts = 0;
  const f = setup({ api: async (path, options = {}) => {
    if (path.endsWith('/devices')) return devices; if (path.endsWith('/review')) return review;
    if (options.method === 'POST') { ++posts; return never.promise; }
    return { jobs: [job] };
  } });
  await ready(f); const starting = f.instance.start();
  const timeout = f.timers.findLast(timer => timer.delay === 12000 && !timer.cancelled); assert.ok(timeout);
  timeout.callback(); await starting;
  assert.equal(posts, 1); assert.equal(f.instance.job.id, job.id); assert.equal(f.instance.pending, false);
});
test('without browser storage, startup can reconnect an existing server job without copying', async () => {
  const f = setup({ storage: null }); await f.instance.resume();
  assert.equal(f.instance.job.id, job.id); assert.equal(f.calls.length, 1); assert.equal(f.calls[0][0], '/api/pocket-export/jobs');
  assert.notEqual(f.calls[0][1].method, 'POST');
});
test('unsupported cable platforms stay readable and cannot enable copying', async () => {
  const f = setup({ api: async path => path.endsWith('/devices') ? { supported: false, platform: 'windows', message: 'Use Apple Devices and the manual transfer guide.', devices: [] } : review });
  await f.instance.open(); await f.instance.reviewSelection(); await f.instance.start();
  assert.equal(f.nodes.device.disabled, true); assert.equal(f.nodes.start.disabled, true);
  assert.match(f.nodes['device-help'].textContent, /Apple Devices/);
  assert.equal(f.calls.filter(call => call[0] === '/api/pocket-export/jobs').length, 0);
});
test('closing and reopening for another selection discards an old pending review', async () => {
  const pending = deferred();
  const f = setup({ api: async path => path.endsWith('/devices') ? devices : pending.promise });
  await f.instance.open({ mode: 'tracks', trackIds: [a] }); const old = f.instance.reviewSelection(); f.dialog.close();
  await f.instance.open({ mode: 'tracks', trackIds: [b] }); pending.resolve(review); await old;
  assert.deepEqual(f.instance.selection, { mode: 'tracks', trackIds: [b] }); assert.equal(f.instance.review, null); assert.equal(f.instance.reviewing, false);
});

test('a new explicit song export replaces a completed view and requires a new device choice', async () => {
  const f = setup(); await ready(f); await f.instance.start(); f.setJob({ ...job, state: 'complete' }); await f.instance.poll();
  f.dialog.close(); await f.instance.open({ mode: 'tracks', trackIds: [c] });
  assert.equal(f.instance.job, null); assert.equal(f.instance.review, null); assert.equal(f.nodes.device.value, '');
  assert.deepEqual(f.instance.selection, { mode: 'tracks', trackIds: [c] });
});
test('a stale uncertain read cannot undo an explicitly verified complete copy', async () => {
  const pending = deferred();
  const f = setup({ api: async (path, options = {}) => {
    if (path.endsWith('/devices')) return devices; if (path.endsWith('/review')) return review;
    if (path.endsWith('/check')) return { job: { ...job, state: 'complete' } };
    if (options.method === 'POST') return { job: { ...job, state: 'uncertain' } };
    return pending.promise;
  } });
  await ready(f); await f.instance.start(); const oldRead = f.instance.poll(); await f.instance.check();
  pending.resolve({ job: { ...job, state: 'uncertain' } }); await oldRead;
  assert.equal(f.instance.job.state, 'complete');
});
