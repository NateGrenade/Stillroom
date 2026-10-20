import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { ShareSongDialog, canonicalShareUrl, shareFilename, isM4aAudio, saveAudioBlob } from '../public/share-song.js';

class Node extends EventTarget {
  constructor() { super(); this.value = ''; this.checked = false; this.disabled = false; this.hidden = false; this.textContent = ''; this.attributes = {}; }
  setAttribute(key, value) { this.attributes[key] = value; }
  focus() { this.focused = true; }
  select() { this.selected = true; }
}
const track = { id: 'a'.repeat(32) };
const sourceUrl = 'https://www.youtube.com/watch?v=Abcdef12345';
const snapshot = { title: 'A song', artist: 'Artist', album: 'Album', sourceUrl, kind: 'original', exportDuration: 60, filename: 'A song.m4a', revision: 'c'.repeat(64) };
const audio = new Blob([new Uint8Array([0, 0, 0, 24, 102, 116, 121, 112, 77, 52, 65, 32, 0, 0, 0, 0])], { type: 'audio/mp4' });
const audioResponse = (filename = 'attachment; filename="A song.m4a"') => new Response(audio, { headers: { 'Content-Type': 'audio/mp4', 'Content-Disposition': filename } });
function deferred() { let resolve, reject; const promise = new Promise((done, fail) => { resolve = done; reject = fail; }); return { promise, resolve, reject }; }
function setup(options = {}) {
  const names = ['heading', 'status', 'options', 'source', 'source-url', 'no-source', 'copy', 'download', 'retry', 'include-info', 'info-preview', 'without-info', 'title', 'artist', 'album', 'audio-summary'];
  const nodes = Object.fromEntries(names.map(name => [`#share-song-${name}`, new Node()]));
  const closeButtons = [new Node(), new Node()];
  const dialog = new Node();
  dialog.querySelector = selector => nodes[selector];
  dialog.querySelectorAll = selector => selector === '.close-dialog' ? closeButtons : [];
  dialog.close = () => { dialog.open = false; dialog.dispatchEvent(new Event('close')); };
  const calls = [], copies = [], downloads = [], toasts = [], closed = [], missing = [];
  const instance = new ShareSongDialog({
    dialog,
    api: async (...args) => { calls.push(args); return { ...snapshot }; },
    showDialog: node => { node.open = true; },
    fetchAudio: async () => audioResponse(),
    clipboard: { writeText: async text => { copies.push(text); } },
    downloadFile: (...args) => downloads.push(args),
    toast: value => toasts.push(value),
    onClose: id => closed.push(id), onMissing: () => missing.push(true), ...options,
  });
  return { instance, nodes, dialog, closeButtons, calls, copies, downloads, toasts, closed, missing };
}

test('opening fetches a fresh snapshot and shows literal labels without copying or exporting', async () => {
  const hostile = { ...snapshot, title: '<img src=x onerror=alert(1)>', artist: 'A & B', album: '' };
  const f = setup({ api: async () => hostile });
  await f.instance.open(track);
  assert.equal(f.dialog.open, true);
  assert.equal(f.nodes['#share-song-heading'].focused, true);
  assert.equal(f.nodes['#share-song-title'].textContent, hostile.title);
  assert.equal(f.nodes['#share-song-artist'].textContent, hostile.artist);
  assert.equal(f.nodes['#share-song-album'].textContent, '(not set)');
  assert.equal(f.nodes['#share-song-source-url'].value, sourceUrl);
  assert.equal(f.nodes['#share-song-source'].hidden, false);
  assert.equal(f.nodes['#share-song-include-info'].checked, true);
  assert.deepEqual(f.copies, []);
  assert.deepEqual(f.downloads, []);
  f.dialog.close();
  assert.deepEqual(f.closed, [track.id]);
  assert.equal(f.instance.trackId, null);
});

test('loading blocks actions and duplicate opens while remaining dismissible', async () => {
  const pending = deferred(); let requests = 0;
  const f = setup({ api: async () => { ++requests; return pending.promise; } });
  const opening = f.instance.open(track);
  await f.instance.open({ id: 'b'.repeat(32) });
  await f.instance.copy(); await f.instance.download();
  assert.equal(requests, 1);
  assert.equal(f.nodes['#share-song-download'].disabled, true);
  assert.equal(f.nodes['#share-song-include-info'].disabled, true);
  assert.equal(f.nodes['#share-song-options'].attributes['aria-busy'], 'true');
  assert.equal(f.dialog.attributes['aria-busy'], undefined);
  assert.ok(f.closeButtons.every(button => !button.disabled));
  const cancel = new Event('cancel', { cancelable: true }); f.dialog.dispatchEvent(cancel);
  assert.equal(cancel.defaultPrevented, false);
  pending.resolve(snapshot); await opening;
  assert.equal(f.nodes['#share-song-download'].disabled, false);
  assert.deepEqual(f.downloads, []);
});

test('closing and reopening during loading ignores stale snapshots and errors', async () => {
  const first = deferred(), second = deferred(); let requests = 0;
  const f = setup({ api: () => ++requests === 1 ? first.promise : second.promise });
  const oldOpen = f.instance.open(track);
  f.dialog.close();
  const newOpen = f.instance.open({ id: 'b'.repeat(32) });
  first.reject(Object.assign(new Error('Old song missing'), { status: 404 })); await oldOpen;
  assert.equal(f.instance.loading, true);
  assert.deepEqual(f.missing, []);
  second.resolve({ ...snapshot, title: 'New song' }); await newOpen;
  assert.equal(f.nodes['#share-song-title'].textContent, 'New song');
  assert.equal(f.instance.trackId, 'b'.repeat(32));
  f.dialog.close();
  await f.instance.open(track);
  assert.equal(requests, 3);
});

test('invalid IDs never open a dialog or request a path', async () => {
  const f = setup();
  for (const id of ['../song', '', 'https://bad.test', null, 'a'.repeat(31)]) await f.instance.open({ id });
  assert.equal(f.dialog.open, undefined);
  assert.deepEqual(f.calls, []);
});

test('uploaded songs and unsafe URLs have audio download available without a copy option', async () => {
  for (const url of ['', 'javascript:alert(1)', 'https://www.youtube.com.evil.test/watch?v=Abcdef12345']) {
    const f = setup({ api: async () => ({ ...snapshot, sourceUrl: url, kind: 'loop', exportDuration: 30 }) });
    await f.instance.open(track);
    assert.equal(f.nodes['#share-song-source'].hidden, true);
    assert.equal(f.nodes['#share-song-no-source'].hidden, false);
    assert.equal(f.nodes['#share-song-copy'].disabled, true);
    assert.equal(f.nodes['#share-song-download'].disabled, false);
    assert.match(f.nodes['#share-song-audio-summary'].textContent, /saved loop region \(0:30\).*flattened/);
    await f.instance.copy(); assert.deepEqual(f.copies, []);
  }
});

test('source URLs must already be canonical public-format YouTube video URLs', () => {
  assert.equal(canonicalShareUrl(sourceUrl), sourceUrl);
  for (const url of [null, {}, 'javascript:alert(1)', 'https://youtu.be/Abcdef12345', `${sourceUrl}&list=secret`, `${sourceUrl}#fragment`, 'https://user:pass@www.youtube.com/watch?v=Abcdef12345', 'https://www.youtube.com:444/watch?v=Abcdef12345', 'https://www.youtube.com/watch?v=short', `${sourceUrl}\n`]) assert.equal(canonicalShareUrl(url), '', String(url));
});

test('copy happens only on explicit action and only announces confirmed clipboard success', async () => {
  const f = setup(); await f.instance.open(track);
  await f.instance.copy();
  assert.deepEqual(f.copies, [sourceUrl]);
  assert.deepEqual(f.toasts, ['Source link copied.']);
  assert.equal(f.nodes['#share-song-status'].textContent, 'Source link copied.');
});

test('blocked or absent clipboard offers a selected in-dialog manual copy fallback', async () => {
  for (const clipboard of [null, { writeText: async () => { throw new Error('NotAllowedError'); } }]) {
    const f = setup({ clipboard }); await f.instance.open(track); await f.instance.copy();
    assert.equal(f.nodes['#share-song-source-url'].value, sourceUrl);
    assert.equal(f.nodes['#share-song-source-url'].focused, true);
    assert.equal(f.nodes['#share-song-source-url'].selected, true);
    assert.match(f.nodes['#share-song-status'].textContent, /copy it manually/);
    assert.deepEqual(f.toasts, []);
    assert.equal(f.instance.busy, false);
  }
});

test('clipboard permission can remain pending while the dialog is dismissed safely', async () => {
  const pending = deferred(); let copies = 0;
  const f = setup({ clipboard: { writeText: () => { ++copies; return pending.promise; } } });
  await f.instance.open(track);
  const copying = f.instance.copy();
  await f.instance.copy();
  assert.equal(copies, 1);
  assert.equal(f.nodes['#share-song-copy'].disabled, true);
  assert.equal(f.instance.locked, false);
  assert.ok(f.closeButtons.every(button => !button.disabled));
  const cancel = new Event('cancel', { cancelable: true }); f.dialog.dispatchEvent(cancel);
  assert.equal(cancel.defaultPrevented, false);
  f.dialog.close();
  await f.instance.open({ id: 'b'.repeat(32) });
  pending.resolve(); await copying;
  assert.deepEqual(f.toasts, []);
  assert.equal(f.instance.trackId, 'b'.repeat(32));
  assert.equal(f.instance.busy, false);
});

test('download uses raw binary fetch, strict opt-in JSON labels, and a safe attachment filename', async () => {
  const requests = [];
  const f = setup({ fetchAudio: async (...args) => { requests.push(args); return audioResponse("attachment; filename*=UTF-8''%C3%89tude.m4a"); } });
  await f.instance.open(track); await f.instance.download();
  assert.equal(f.calls[0][0], `/api/tracks/${track.id}/share`);
  assert.equal(requests[0][0], `/api/tracks/${track.id}/export`);
  assert.equal(requests[0][1].method, 'POST');
  assert.deepEqual(JSON.parse(requests[0][1].body), { includeInfo: true, revision: snapshot.revision });
  assert.equal(requests[0][1].headers['Content-Type'], 'application/json');
  assert.equal(f.downloads.length, 1);
  assert.equal(f.downloads[0][0].type, 'audio/mp4');
  assert.equal(f.downloads[0][1], 'Étude.m4a');
  assert.match(f.nodes['#share-song-status'].textContent, /stays on this computer/);
  f.nodes['#share-song-include-info'].checked = false;
  f.nodes['#share-song-include-info'].dispatchEvent(new Event('change'));
  assert.equal(f.nodes['#share-song-info-preview'].hidden, true);
  assert.equal(f.nodes['#share-song-without-info'].hidden, false);
  await f.instance.download();
  assert.deepEqual(JSON.parse(requests[1][1].body), { includeInfo: false, revision: snapshot.revision });
  assert.equal(f.downloads[1][1], 'stillroom-audio.m4a');
});

test('a pending export disables checkbox, duplicate actions, and all close routes until it settles', async () => {
  const pending = deferred(); let requests = 0;
  const f = setup({ fetchAudio: async () => { ++requests; return pending.promise; } });
  await f.instance.open(track);
  const exporting = f.instance.download();
  await f.instance.download(); await f.instance.copy(); await f.instance.open({ id: 'b'.repeat(32) });
  assert.equal(requests, 1);
  assert.ok(f.closeButtons.every(button => button.disabled));
  assert.equal(f.nodes['#share-song-download'].disabled, true);
  assert.equal(f.nodes['#share-song-include-info'].disabled, true);
  const cancel = new Event('cancel', { cancelable: true }); f.dialog.dispatchEvent(cancel);
  assert.equal(cancel.defaultPrevented, true);
  pending.resolve(audioResponse()); await exporting;
  assert.equal(f.instance.busy, false);
  assert.ok(f.closeButtons.every(button => !button.disabled));
  assert.equal(f.downloads.length, 1);
  const app = readFileSync(new URL('../public/app.js', import.meta.url), 'utf8');
  assert.match(app, /dialog\.id === 'share-song-dialog' && shareSong\.locked/);
});

test('failed and non-audio exports cannot create a download and can be retried', async () => {
  const responses = [new Response(JSON.stringify({ error: 'Try a shorter song.' }), { status: 500, headers: { 'Content-Type': 'application/json' } }), new Response('<html>wrong response</html>', { headers: { 'Content-Type': 'text/html' } }), new Response('not audio, despite MIME', { headers: { 'Content-Type': 'audio/mp4' } }), audioResponse()];
  const f = setup({ fetchAudio: async () => responses.shift() }); await f.instance.open(track);
  await f.instance.download();
  assert.equal(f.nodes['#share-song-status'].textContent, 'Try a shorter song.');
  await f.instance.download();
  assert.match(f.nodes['#share-song-status'].textContent, /valid M4A/);
  await f.instance.download(); assert.equal(f.downloads.length, 0);
  assert.equal(f.nodes['#share-song-download'].disabled, false);
  await f.instance.download(); assert.equal(f.downloads.length, 1);
});

test('snapshot errors support retry; malformed snapshots never enable actions', async () => {
  let requests = 0;
  const f = setup({ api: async () => { if (++requests === 1) throw new Error('Connection interrupted.'); if (requests === 2) return { ...snapshot, title: {} }; return snapshot; } });
  await f.instance.open(track);
  assert.equal(f.nodes['#share-song-status'].textContent, 'Connection interrupted.');
  assert.equal(f.nodes['#share-song-retry'].hidden, false);
  await f.instance.load();
  assert.match(f.nodes['#share-song-status'].textContent, /could not be loaded/);
  assert.equal(f.nodes['#share-song-download'].disabled, true);
  await f.instance.load();
  assert.equal(f.nodes['#share-song-download'].disabled, false);
});

test('a changed song requires an explicit review of reloaded metadata before another download', async () => {
  let previews = 0, exports = 0; const requests = [];
  const latest = { ...snapshot, title: 'Changed title', revision: 'd'.repeat(64) };
  const f = setup({
    api: async () => ++previews === 1 ? snapshot : latest,
    fetchAudio: async (_path, options) => {
      requests.push(JSON.parse(options.body));
      return ++exports === 1 ? new Response('{"error":"This song changed."}', { status: 409, headers: { 'Content-Type': 'application/json' } }) : audioResponse();
    },
  });
  await f.instance.open(track); await f.instance.download();
  assert.equal(f.nodes['#share-song-options'].hidden, true);
  assert.equal(f.nodes['#share-song-download'].disabled, true);
  assert.equal(f.nodes['#share-song-retry'].hidden, false);
  assert.equal(f.nodes['#share-song-retry'].textContent, 'Reload options');
  assert.equal(f.nodes['#share-song-retry'].focused, true);
  assert.equal(f.nodes['#share-song-status'].textContent, 'This song changed.');
  assert.equal(exports, 1);
  f.nodes['#share-song-heading'].focused = false;
  await f.instance.load();
  assert.equal(f.nodes['#share-song-title'].textContent, latest.title);
  assert.equal(f.nodes['#share-song-heading'].focused, true);
  assert.equal(f.nodes['#share-song-download'].disabled, false);
  assert.equal(exports, 1);
  await f.instance.download();
  assert.deepEqual(requests, [{ includeInfo: true, revision: snapshot.revision }, { includeInfo: true, revision: latest.revision }]);
  assert.equal(f.downloads.length, 1);
});

test('other conflicts preserve the backend explanation and offer explicit reload without retrying', async () => {
  for (const message of ['Another audio share is being prepared. Try again shortly.', 'An app update is being installed.', "This song's library audio is unavailable."]) {
    let exports = 0;
    const f = setup({ fetchAudio: async () => { ++exports; return new Response(JSON.stringify({ error: message }), { status: 409, headers: { 'Content-Type': 'application/json' } }); } });
    await f.instance.open(track); await f.instance.download();
    assert.equal(f.nodes['#share-song-status'].textContent, message);
    assert.equal(f.nodes['#share-song-retry'].textContent, 'Reload options');
    assert.equal(f.nodes['#share-song-retry'].hidden, false);
    assert.equal(exports, 1);
    assert.deepEqual(f.downloads, []);
  }
});

test('snapshots without a valid revision cannot enable export', async () => {
  for (const revision of ['', undefined, {}, 'invalid', 'a'.repeat(63)]) {
    const f = setup({ api: async () => ({ ...snapshot, revision }) });
    await f.instance.open(track); await f.instance.download();
    assert.equal(f.nodes['#share-song-download'].disabled, true);
    assert.deepEqual(f.downloads, []);
  }
});

test('oversized audio is rejected before reading a declared large body or saving a large blob', async () => {
  let reads = 0;
  const f = setup({ fetchAudio: async () => ({ ok: true, headers: new Headers({ 'Content-Type': 'audio/mp4', 'Content-Length': String(512 * 1024 * 1024 + 1) }), blob: async () => { ++reads; return audio; } }) });
  await f.instance.open(track); await f.instance.download();
  assert.equal(reads, 0);
  assert.deepEqual(f.downloads, []);
  assert.match(f.nodes['#share-song-status'].textContent, /too large/);
  assert.equal(await isM4aAudio({ size: 512 * 1024 * 1024 + 1 }, 'audio/mp4'), false);
});

test('missing songs during either snapshot or export disable all further sharing actions', async () => {
  for (const phase of ['snapshot', 'export']) {
    const f = setup(phase === 'snapshot' ? { api: async () => { throw Object.assign(new Error('Gone'), { status: 404 }); } } : { fetchAudio: async () => new Response('{"error":"Gone"}', { status: 404, headers: { 'Content-Type': 'application/json' } }) });
    await f.instance.open(track);
    if (phase === 'export') await f.instance.download();
    await f.instance.copy(); await f.instance.download();
    assert.equal(f.nodes['#share-song-download'].disabled, true);
    assert.equal(f.nodes['#share-song-copy'].disabled, true);
    assert.equal(f.nodes['#share-song-retry'].hidden, true);
    assert.match(f.nodes['#share-song-status'].textContent, /removed from your library/);
    assert.deepEqual(f.missing, [true]);
    assert.deepEqual(f.downloads, []);
    assert.ok(f.closeButtons.every(button => !button.disabled));
  }
});

test('even a forced close suppresses delayed export completion', async () => {
  const pending = deferred(); const f = setup({ fetchAudio: () => pending.promise }); await f.instance.open(track);
  const exporting = f.instance.download(); f.dialog.close();
  await f.instance.open({ id: 'b'.repeat(32) });
  pending.resolve(audioResponse()); await exporting;
  assert.deepEqual(f.downloads, []);
  assert.equal(f.instance.trackId, 'b'.repeat(32));
  assert.equal(f.instance.busy, false);
});

test('download names reject paths, malformed encoding, other extensions and platform-special names', () => {
  assert.equal(shareFilename('attachment; filename="A song.m4a"'), 'A song.m4a');
  assert.equal(shareFilename('attachment; filename=Simple.m4a'), 'Simple.m4a');
  assert.equal(shareFilename("attachment; filename=Fallback.m4a; filename*=UTF-8''%E9%9B%A8.m4a"), '雨.m4a');
  for (const header of ['', null, 'attachment; filename="../../secret.m4a"', 'attachment; filename="C:\\secret.m4a"', 'attachment; filename="bad\nname.m4a"', 'attachment; filename="page.html"', 'attachment; filename="CON.m4a"', 'attachment; filename=".hidden.m4a"', 'attachment; filename="song.m4a "', "attachment; filename*=UTF-8''bad%ZZ.m4a", "attachment; filename*=UTF-8''%2Fprivate%2Fsong.m4a", 'attachment; filename="<script>.m4a"']) assert.equal(shareFilename(header), 'stillroom-audio.m4a', String(header));
  assert.equal(shareFilename('attachment; filename="Artist - Song.m4a"', false), 'stillroom-audio.m4a');
});

test('audio validation checks MIME and MP4 container signature before saving', async () => {
  assert.equal(await isM4aAudio(audio, 'audio/mp4; codecs=mp4a.40.2'), true);
  assert.equal(await isM4aAudio(audio, 'video/mp4'), false);
  assert.equal(await isM4aAudio(new Blob([]), 'audio/mp4'), false);
  assert.equal(await isM4aAudio(new Blob(['<html>not audio</html>']), 'audio/mp4'), false);
});

test('binary downloads use an attached hidden anchor and clean up object URLs after a delay', () => {
  const events = [], anchor = { click() { events.push(['click', this.href, this.download, this.hidden]); }, remove() { events.push(['remove']); } };
  let release;
  saveAudioBlob(audio, 'Song.m4a', {
    document: { createElement: tag => { assert.equal(tag, 'a'); return anchor; }, body: { appendChild: node => events.push(['append', node === anchor]) } },
    urlApi: { createObjectURL: blob => { assert.equal(blob, audio); return 'blob:local'; }, revokeObjectURL: url => events.push(['revoke', url]) },
    schedule: (callback, delay) => { release = callback; assert.equal(delay, 60000); },
  });
  assert.deepEqual(events, [['append', true], ['click', 'blob:local', 'Song.m4a', true], ['remove']]);
  release(); assert.deepEqual(events.at(-1), ['revoke', 'blob:local']);
});

test('dialog provides a visible readonly fallback, honest flattened loop guidance, and exact import labels', () => {
  const html = readFileSync(new URL('../public/index.html', import.meta.url), 'utf8');
  assert.match(html, /id="share-song-source-url"[^>]*readonly/);
  assert.match(html, /Add music → Audio files/);
  assert.match(html, /data-import-source="file"[^>]*>.*Audio files/);
  assert.match(html, /does not include your saved edits or loop/);
  assert.match(html, /Only share audio you have the rights to share/);
});
