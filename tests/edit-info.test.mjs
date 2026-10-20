import test from 'node:test';
import assert from 'node:assert/strict';
import { EditInfoDialog } from '../public/edit-info.js';

class Node extends EventTarget {
  constructor() { super(); this.value = ''; this.disabled = false; this.hidden = false; this.textContent = ''; this.attributes = {}; }
  setAttribute(key, value) { this.attributes[key] = value; }
  focus() { this.focused = true; }
  select() { this.selected = true; }
}
const track = { id: 'song', title: 'Original title', artist: 'Artist', album: 'Album', duration: 50, audioUrl: '/audio/song.mp3' };
function setup(api = async () => ({ track })) {
  const nodes = Object.fromEntries(['form', 'title', 'artist', 'album', 'status', 'save'].map(name => [`#edit-info-${name}`, new Node()]));
  const closeButtons = [new Node(), new Node()];
  const dialog = new Node();
  dialog.querySelector = selector => nodes[selector];
  dialog.querySelectorAll = selector => selector === 'input' ? ['title', 'artist', 'album'].map(name => nodes[`#edit-info-${name}`]) : closeButtons;
  dialog.close = () => { dialog.open = false; dialog.dispatchEvent(new Event('close')); };
  const saved = [], toasts = [], closed = [], missing = [];
  const instance = new EditInfoDialog({ dialog, api, showDialog: node => { node.open = true; }, onSave: value => saved.push(value), onMissing: () => missing.push(true), onClose: id => closed.push(id), toast: text => toasts.push(text) });
  return { instance, dialog, nodes, closeButtons, saved, toasts, closed, missing };
}

test('editing prepopulates literal metadata, focuses the title, and Cancel discards changes', () => {
  let requests = 0;
  const { instance, dialog, nodes, saved, closed } = setup(() => { requests += 1; });
  const original = { ...track, title: '<script>& "Title"', artist: 'A & B' };
  instance.open(original);
  assert.equal(nodes['#edit-info-title'].value, original.title);
  assert.equal(nodes['#edit-info-title'].focused, true);
  assert.equal(nodes['#edit-info-title'].selected, true);
  nodes['#edit-info-title'].value = 'Unsaved changes';
  dialog.close();
  assert.deepEqual(closed, ['song']);
  assert.equal(instance.trackId, null);
  assert.equal(requests, 0);
  assert.deepEqual(saved, []);
  assert.equal(original.title, '<script>& "Title"');
  instance.open(original);
  assert.equal(nodes['#edit-info-title'].value, original.title);
});

test('Save sends only editable labels and applies the confirmed server row', async () => {
  const calls = [], updated = { ...track, title: '<The "Song"> & more', artist: '', album: 'New album' };
  const { instance, nodes, dialog, saved, toasts } = setup(async (url, options) => { calls.push({ url, options }); return { track: updated }; });
  instance.open(track);
  nodes['#edit-info-title'].value = `  ${updated.title}  `;
  nodes['#edit-info-artist'].value = '   ';
  nodes['#edit-info-album'].value = ' New album ';
  await instance.save();
  assert.equal(calls[0].url, '/api/tracks/song');
  assert.equal(calls[0].options.method, 'PATCH');
  assert.deepEqual(JSON.parse(calls[0].options.body), { title: updated.title, artist: '', album: updated.album });
  assert.deepEqual(saved, [updated]);
  assert.deepEqual(toasts, ['Song info updated.']);
  assert.equal(dialog.open, false);
});

test('blank titles stay editable without sending a request', async () => {
  let requests = 0;
  const { instance, nodes, dialog } = setup(() => { requests += 1; });
  instance.open(track);
  nodes['#edit-info-title'].value = '   ';
  await instance.save();
  assert.equal(requests, 0);
  assert.equal(dialog.open, true);
  assert.match(nodes['#edit-info-status'].textContent, /Enter a song title/);
  assert.equal(nodes['#edit-info-title'].focused, true);
  assert.equal(nodes['#edit-info-save'].disabled, false);
});

test('a pending save blocks duplicate submissions, edits, and Escape until confirmed', async () => {
  let resolve, requests = 0;
  const { instance, nodes, dialog, closeButtons } = setup(() => { requests += 1; return new Promise(done => { resolve = done; }); });
  instance.open(track);
  const saving = instance.save();
  await instance.save();
  instance.open({ ...track, id: 'different' });
  assert.equal(requests, 1);
  assert.equal(instance.trackId, 'song');
  assert.equal(nodes['#edit-info-save'].disabled, true);
  assert.equal(nodes['#edit-info-title'].disabled, true);
  assert.ok(closeButtons.every(button => button.disabled));
  const cancel = new Event('cancel', { cancelable: true });
  dialog.dispatchEvent(cancel);
  assert.equal(cancel.defaultPrevented, true);
  resolve({ track });
  await saving;
  assert.equal(instance.busy, false);
  assert.ok(closeButtons.every(button => !button.disabled));
});

test('failed saves preserve entered labels and allow a retry', async () => {
  let requests = 0;
  const { instance, nodes, saved, dialog } = setup(async () => { if (++requests === 1) throw new Error('Connection interrupted.'); return { track: { ...track, title: 'Retry title' } }; });
  instance.open(track);
  nodes['#edit-info-title'].value = 'Retry title';
  await instance.save();
  assert.equal(dialog.open, true);
  assert.equal(nodes['#edit-info-title'].value, 'Retry title');
  assert.equal(nodes['#edit-info-save'].disabled, false);
  assert.equal(nodes['#edit-info-status'].textContent, 'Connection interrupted.');
  assert.deepEqual(saved, []);
  await instance.save();
  assert.equal(requests, 2);
  assert.equal(saved[0].title, 'Retry title');
});

test('a song deleted elsewhere shows a useful message and cannot be saved again', async () => {
  let requests = 0;
  const { instance, nodes, saved, missing, closeButtons } = setup(async () => { requests += 1; throw Object.assign(new Error('Song not found'), { status: 404 }); });
  instance.open(track);
  await instance.save();
  await instance.save();
  assert.equal(requests, 1);
  assert.equal(nodes['#edit-info-save'].disabled, true);
  assert.match(nodes['#edit-info-status'].textContent, /removed from your library/);
  assert.deepEqual(saved, []);
  assert.deepEqual(missing, [true]);
  assert.ok(closeButtons.every(button => !button.disabled));
  instance.open({ ...track, id: 'next' });
  assert.equal(nodes['#edit-info-save'].disabled, false);
});

test('an unexpected API result never replaces a different library row', async () => {
  const { instance, saved, nodes, dialog } = setup(async () => ({ track: { ...track, id: 'other' } }));
  instance.open(track);
  await instance.save();
  assert.deepEqual(saved, []);
  assert.equal(dialog.open, true);
  assert.match(nodes['#edit-info-status'].textContent, /could not be confirmed/);
});
