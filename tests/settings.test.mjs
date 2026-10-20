import test from 'node:test';
import assert from 'node:assert/strict';
import { SettingsDialog, releaseLink, updateMessage } from '../public/settings.js';

const account = (extra = {}) => ({ mode: 'off', browser: 'firefox', hasCookies: false, browsers: [{ id: 'firefox', label: 'Firefox' }, { id: 'chrome', label: 'Chrome' }], ...extra });
function fixture(api = async () => ({}), options = {}) {
  const nodes = new Map();
  const createNode = () => ({
    value: '', textContent: '', hidden: false, disabled: false, files: [], children: [], listeners: {}, classList: { toggle() {} },
    addEventListener(type, handler) { this.listeners[type] = handler; },
    setAttribute(name, value) { this[name] = value; }, removeAttribute(name) { delete this[name]; },
    replaceChildren() { this.children = []; }, append(child) { this.children.push(child); },
    ownerDocument: { createElement() { return createNode(); } },
  });
  const dialog = { open: false, querySelector(selector) { if (!nodes.has(selector)) nodes.set(selector, createNode()); return nodes.get(selector); }, addEventListener() {} };
  dialog.querySelector('#settings-youtube-mode').value = 'off';
  const data = new Map(), storage = { getItem: key => data.get(key), setItem: (key, value) => data.set(key, value) };
  const messages = [], updates = [];
  const instance = new SettingsDialog({ dialog, api, showDialog: () => { dialog.open = true; }, toast: message => messages.push(message), onUpdate: value => updates.push(value), storage, ...options });
  return { instance, nodes, messages, updates, data, node: selector => dialog.querySelector(selector) };
}

test('Release links only point to the configured Stillroom GitHub releases', () => {
  assert.equal(releaseLink('https://github.com/NateGrenade/Stillroom/releases/tag/v1.5.0'), 'https://github.com/NateGrenade/Stillroom/releases/tag/v1.5.0');
  for (const value of ['javascript:alert(1)', 'https://github.com/other/repo/releases/latest', 'https://github.com/NateGrenade/Stillroom/releases.evil', 'https://github.com.evil.test/NateGrenade/Stillroom/releases', 'https://name:secret@github.com/NateGrenade/Stillroom/releases', null]) assert.equal(releaseLink(value), '');
});

test('Settings load the backend-supported browsers without exposing or inventing cookie contents', async () => {
  const calls = [];
  const f = fixture(async path => { calls.push(path); return { youtube: account({ mode: 'browser' }), updates: { state: 'current', currentVersion: '1.5.0' } }; });
  try {
    await f.instance.open();
    assert.deepEqual(calls, ['/api/settings']);
    assert.deepEqual(f.node('#settings-youtube-browser').children.map(option => option.value), ['firefox', 'chrome']);
    assert.equal(f.node('#settings-youtube-mode').value, 'browser');
    assert.equal(f.node('#settings-current-version').textContent, 'Version 1.5.0');
    assert.equal(f.node('#settings-youtube-cookie-field').hidden, true);
    assert.equal(f.node('#settings-youtube-browser-field').hidden, false);
  } finally { f.instance.destroy(); }
});

test('Browser configuration sends only the account method and browser name', async () => {
  const calls = [], f = fixture(async (path, options) => { calls.push({ path, options }); return { youtube: account({ mode: 'browser', browser: 'chrome' }) }; });
  try {
    f.instance.acceptAccount(account());
    f.node('#settings-youtube-mode').value = 'browser'; f.node('#settings-youtube-browser').value = 'chrome';
    await f.instance.saveAccount();
    assert.equal(calls[0].path, '/api/settings/youtube');
    assert.deepEqual(JSON.parse(calls[0].options.body), { mode: 'browser', browser: 'chrome' });
    assert.match(f.node('#settings-status').textContent, /Enable it separately for each YouTube import/);
  } finally { f.instance.destroy(); }
});

test('Cookie upload stays in a raw local request and does not enter settings JSON', async () => {
  const calls = [], file = { name: 'youtube-cookies.txt' };
  const f = fixture(async (path, options) => { calls.push({ path, options }); return account({ mode: 'cookies', hasCookies: true }); });
  try {
    f.instance.acceptAccount(account()); f.node('#settings-youtube-mode').value = 'cookies'; f.node('#settings-youtube-cookies').files = [file];
    await f.instance.saveAccount();
    assert.equal(calls[0].path, '/api/settings/youtube/cookies');
    assert.equal(calls[0].options.body, file);
    assert.equal(calls[0].options.headers['Content-Type'], 'application/octet-stream');
    assert.deepEqual(JSON.parse(calls[1].options.body), { mode: 'cookies', browser: 'firefox' });
    assert.equal(f.node('#settings-youtube-cookies').value, '');
  } finally { f.instance.destroy(); }
});

test('Off does not accidentally upload a previously selected cookie file', async () => {
  const calls = [], f = fixture(async (path, options) => { calls.push(path); return account(); });
  try {
    f.instance.acceptAccount(account()); f.node('#settings-youtube-cookies').files = [{ name: 'cookies.txt' }];
    await f.instance.saveAccount(); assert.deepEqual(calls, ['/api/settings/youtube']);
  } finally { f.instance.destroy(); }
});

test('Clear account disables browser use before removing stored cookies', async () => {
  const calls = [], f = fixture(async (path, options) => { calls.push({ path, options }); return account(); });
  try {
    f.instance.acceptAccount(account({ mode: 'browser', hasCookies: true }));
    await f.instance.clearAccount();
    assert.deepEqual(JSON.parse(calls[0].options.body), { mode: 'off', browser: 'firefox' });
    assert.equal(calls[1].path, '/api/settings/youtube/cookies'); assert.equal(calls[1].options.method, 'DELETE');
    assert.equal(f.instance.youtube.mode, 'off');
  } finally { f.instance.destroy(); }
});

test('Background update checks run once a day, while a manual check always checks', async () => {
  const calls = [], bodies = [], f = fixture(async (path, options) => { calls.push(path); bodies.push(JSON.parse(options.body)); return { state: 'current', currentVersion: '1.5.0' }; }, { now: () => 100000000 });
  try {
    await f.instance.checkUpdates({ quiet: true }); await f.instance.checkUpdates({ quiet: true });
    assert.deepEqual(calls, ['/api/updates/check']);
    await f.instance.checkUpdates(); assert.equal(calls.length, 2);
    assert.deepEqual(bodies, [{ auto: true }, { auto: false }]);
    assert.equal(calls.some(path => path.includes('install')), false);
  } finally { f.instance.destroy(); }
});

test('Ready updates only offer installation when the backend allows this running app to install', () => {
  const f = fixture();
  try {
    f.instance.acceptUpdates({ state: 'ready', currentVersion: '1.4.0', latestVersion: '1.5.0', canInstall: false });
    assert.equal(f.node('#settings-update-install').hidden, true);
    f.instance.acceptUpdates({ state: 'ready', currentVersion: '1.4.0', latestVersion: '1.5.0', canInstall: true });
    assert.equal(f.node('#settings-update-install').hidden, false);
    f.instance.acceptUpdates({ state: 'staging', message: 'Checking package…' });
    assert.equal(f.node('#settings-update-check').disabled, true);
    assert.equal(f.node('#settings-update-file').disabled, true);
  } finally { f.instance.destroy(); }
});

test('A local update file is reviewed without automatically installing it', async () => {
  const calls = [], file = { name: 'Stillroom-1.5.0-Mac.zip' };
  const f = fixture(async (path, options) => { calls.push({ path, options }); return { state: 'ready', latestVersion: '1.5.0', canInstall: true }; });
  try {
    await f.instance.uploadPackage(file);
    assert.equal(calls.length, 1); assert.equal(calls[0].path, '/api/updates/package'); assert.equal(calls[0].options.body, file);
    assert.equal(f.node('#settings-update-install').hidden, false);
  } finally { f.instance.destroy(); }
});

test('Installation waits through disconnections and the old process until the expected version returns', async () => {
  const calls = [], health = [{ app: 'stillroom', version: '1.4.0' }, new TypeError('Failed to fetch'), { app: 'stillroom', version: '1.5.0' }];
  let reloads = 0, paused = 0;
  const f = fixture(async (path, options) => {
    calls.push(path);
    if (path.endsWith('/install') || path === '/api/updates') return { state: 'installing', latestVersion: '1.5.0', canInstall: true };
    const result = health.shift(); if (result instanceof Error) throw result; return result;
  }, { reload: () => { reloads++; }, beforeInstall: () => { paused++; }, sleep: async () => {} });
  try {
    f.instance.acceptUpdates({ state: 'ready', latestVersion: '1.5.0', canInstall: true });
    await f.instance.install();
    assert.equal(paused, 1); assert.equal(reloads, 1);
    assert.deepEqual(calls, ['/api/updates/install', '/api/health', '/api/updates', '/api/health', '/api/health']);
    assert.equal(f.node('#settings-status').textContent, '');
  } finally { f.instance.destroy(); }
});

test('A dropped install response still reconnects, while a rejected install does not', async () => {
  for (const rejected of [false, true]) {
    let reloads = 0, healthCalls = 0;
    const f = fixture(async path => {
      if (path.endsWith('/install')) throw Object.assign(new Error(rejected ? 'Import is still running.' : 'Connection closed.'), rejected ? { status: 409 } : {});
      healthCalls++; return { app: 'stillroom', version: '1.5.0' };
    }, { reload: () => { reloads++; }, sleep: async () => {} });
    try {
      f.instance.acceptUpdates({ state: 'ready', latestVersion: '1.5.0', canInstall: true }); await f.instance.install();
      assert.equal(reloads, rejected ? 0 : 1); assert.equal(healthCalls, rejected ? 0 : 1);
      if (rejected) assert.match(f.node('#settings-status').textContent, /Import is still running/);
    } finally { f.instance.destroy(); }
  }
});

test('A failed restart offers recovery after a bounded wait without announcing success', async () => {
  let reloads = 0;
  const f = fixture(async () => { throw new Error('Offline'); }, { sleep: async () => {}, reload: () => { reloads++; } });
  try {
    const result = await f.instance.waitForRestart('1.5.0');
    assert.equal(result, false); assert.equal(reloads, 0); assert.equal(f.instance.updates.state, 'error');
    assert.match(f.instance.updates.message, /Open Start Stillroom/);
  } finally { f.instance.destroy(); }
});

test('Update messages preserve useful backend explanations', () => {
  assert.equal(updateMessage({ state: 'error', message: 'No release has been published yet.' }), 'No release has been published yet.');
  assert.match(updateMessage({ state: 'available', latestVersion: '1.5.0' }), /1.5.0/);
});


test('A rolled-back app reports its actual failure as soon as its old service returns', async () => {
  const calls = []; let reloads = 0;
  const f = fixture(async path => {
    calls.push(path);
    if (path === '/api/health') return { app: 'stillroom', version: '1.4.0' };
    return { state: 'error', currentVersion: '1.4.0', latestVersion: '1.5.0', canInstall: true, message: 'The new app could not start, so the previous version was restored.' };
  }, { sleep: async () => {}, reload: () => { reloads++; } });
  try {
    assert.equal(await f.instance.waitForRestart('1.5.0'), false);
    assert.deepEqual(calls, ['/api/health', '/api/updates']); assert.equal(reloads, 0);
    assert.equal(f.instance.updates.state, 'error'); assert.match(f.instance.updates.message, /previous version was restored/);
  } finally { f.instance.destroy(); }
});
