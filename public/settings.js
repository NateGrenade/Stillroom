/** Local account preferences and reviewable app updates. No account secrets enter page state. */
const UPDATE_BUSY = new Set(['checking', 'downloading', 'staging', 'installing']);
const CHECK_KEY = 'stillroom:last-update-check';
const DAY = 24 * 60 * 60 * 1000;
const jsonOptions = (method, body = {}) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

export function releaseLink(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && url.hostname === 'github.com' && !url.username && !url.password && !url.port && /^\/NateGrenade\/Stillroom\/releases(?:\/|$)/i.test(url.pathname) ? url.href : '';
  } catch { return ''; }
}

export function updateMessage(status = {}) {
  if (status.message) return status.message;
  return ({ idle: 'Check for a newer version of Stillroom.', checking: 'Checking for updates…', available: `Stillroom ${status.latestVersion || ''} is available.`, downloading: 'Downloading the update…', staging: 'Checking the update package…', ready: 'The update is ready to install.', installing: 'Installing the update and restarting Stillroom…', current: 'You have the latest version.', error: 'The update could not be completed. Try again.' })[status.state] || 'Check for a newer version of Stillroom.';
}

export class SettingsDialog {
  constructor({ dialog, api, showDialog, toast = () => {}, onUpdate = () => {}, beforeInstall = () => {}, storage, reload = () => globalThis.location.reload(), sleep = ms => new Promise(resolve => setTimeout(resolve, ms)), now = () => Date.now() }) {
    this.dialog = dialog; this.api = api; this.showDialog = showDialog; this.toast = toast; this.onUpdate = onUpdate; this.beforeInstall = beforeInstall;
    this.reload = reload; this.sleep = sleep; this.now = now; this.$ = selector => dialog.querySelector(selector);
    try { this.storage = storage === undefined ? globalThis.localStorage : storage; } catch { this.storage = null; }
    this.youtube = { mode: 'off', browser: '', hasCookies: false, browsers: [] };
    this.updates = { state: 'idle' }; this.loading = false; this.saving = false; this.updateRequest = false; this.reconnecting = false;
    this.timer = null; this.autoTimer = null; this.lastNotified = ''; this.disposed = false;
    this.bind(); this.render();
  }
  bind() {
    this.$('#settings-youtube-mode').addEventListener('change', () => this.renderAccountFields());
    this.$('#settings-youtube-form').addEventListener('submit', event => { event.preventDefault(); this.saveAccount(); });
    this.$('#settings-youtube-clear').addEventListener('click', () => this.clearAccount());
    this.$('#settings-update-check').addEventListener('click', () => this.checkUpdates());
    this.$('#settings-update-download').addEventListener('click', () => this.updateAction('download'));
    this.$('#settings-update-install').addEventListener('click', () => this.install());
    this.$('#settings-update-file').addEventListener('change', event => this.uploadPackage(event.target.files?.[0]));
    this.dialog.addEventListener('close', () => { if (!UPDATE_BUSY.has(this.updates.state)) clearTimeout(this.timer); });
  }
  message(text = '', success = false) {
    const node = this.$('#settings-status'); node.textContent = text; node.hidden = !text; node.classList.toggle('is-success', success);
  }
  acceptUpdates(result) {
    if (!result || typeof result !== 'object') throw new Error('The update status could not be read.');
    this.updates = result.updates || result;
    this.onUpdate(this.updates); this.render(); this.schedule();
    if (this.updates.state === 'available' && this.updates.latestVersion !== this.lastNotified) {
      this.lastNotified = this.updates.latestVersion; this.toast(`Stillroom ${this.updates.latestVersion} is available. Open Settings to update.`);
    }
  }
  acceptAccount(result) {
    this.youtube = result.youtube || result;
    const browser = this.$('#settings-youtube-browser'); browser.replaceChildren();
    for (const item of this.youtube.browsers || []) {
      const option = browser.ownerDocument.createElement('option'); option.value = item.id; option.textContent = item.label; browser.append(option);
    }
    this.$('#settings-youtube-mode').value = this.youtube.mode || 'off'; browser.value = this.youtube.browser || '';
    this.$('#settings-youtube-cookies').value = ''; this.render();
  }
  async open() {
    this.showDialog(this.dialog); if (this.loading || this.reconnecting) return;
    this.loading = true; this.message(''); this.render();
    try {
      const result = await this.api('/api/settings'); this.acceptAccount(result.youtube); if (result.updates) this.acceptUpdates(result.updates);
      else this.acceptUpdates(await this.api('/api/updates'));
    } catch (error) { this.message(error.message); }
    finally { this.loading = false; this.render(); }
  }
  renderAccountFields() {
    const mode = this.$('#settings-youtube-mode').value;
    this.$('#settings-youtube-browser-field').hidden = mode !== 'browser';
    this.$('#settings-youtube-cookie-field').hidden = mode !== 'cookies';
    this.$('#settings-youtube-cookie-status').textContent = this.youtube.hasCookies ? 'A cookie file is stored locally. Choose a file to replace it.' : 'Choose a Netscape-format YouTube cookie file exported from your own account.';
  }
  render() {
    const accountBusy = this.loading || this.saving || this.reconnecting;
    for (const id of ['mode', 'browser', 'cookies', 'save', 'clear']) this.$(`#settings-youtube-${id}`).disabled = accountBusy;
    this.$('#settings-youtube-form').setAttribute('aria-busy', String(this.saving));
    this.$('#settings-youtube-save').textContent = this.saving ? 'Saving…' : 'Save account setting';
    this.renderAccountFields();
    const state = this.updates.state, busy = this.loading || this.updateRequest || this.reconnecting || UPDATE_BUSY.has(state);
    this.$('#settings-current-version').textContent = this.updates.currentVersion ? `Version ${this.updates.currentVersion}` : '';
    const status = this.$('#settings-update-status'); status.textContent = updateMessage(this.updates); status.classList.toggle('is-working', UPDATE_BUSY.has(state) || this.reconnecting);
    const check = this.$('#settings-update-check'); check.disabled = busy; check.textContent = state === 'checking' ? 'Checking…' : 'Check for updates';
    const download = this.$('#settings-update-download'); download.hidden = state !== 'available'; download.disabled = busy || !this.updates.canInstall;
    const install = this.$('#settings-update-install'); install.hidden = state !== 'ready' || !this.updates.canInstall; install.disabled = busy;
    install.textContent = this.reconnecting ? 'Restarting…' : 'Install and restart';
    this.$('#settings-update-file').disabled = busy || !this.updates.canInstall;
    const capability = this.$('#settings-update-capability');
    if (capability) { capability.hidden = this.updates.canInstall !== false; capability.textContent = 'This is a source copy of Stillroom. Download the Mac app from Releases to use in-app updates.'; }
    const link = this.$('#settings-release-link'), href = releaseLink(this.updates.releaseUrl);
    link.hidden = !href; if (href) link.href = href; else link.removeAttribute('href');
  }
  async saveAccount() {
    if (this.loading || this.saving || this.reconnecting) return;
    const mode = this.$('#settings-youtube-mode').value, browser = this.$('#settings-youtube-browser').value;
    const file = this.$('#settings-youtube-cookies').files?.[0];
    this.saving = true; this.message(''); this.render();
    try {
      if (mode === 'cookies' && file) await this.api('/api/settings/youtube/cookies', { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file });
      const result = await this.api('/api/settings/youtube', jsonOptions('POST', { mode, browser }));
      this.acceptAccount(result); this.message(mode === 'off' ? 'YouTube account use is off.' : 'Account method saved. Enable it separately for each YouTube import.', true);
    } catch (error) { this.message(error.message); }
    finally { this.saving = false; this.render(); }
  }
  async clearAccount() {
    if (this.loading || this.saving || this.reconnecting) return;
    this.saving = true; this.message(''); this.render();
    try {
      await this.api('/api/settings/youtube', jsonOptions('POST', { mode: 'off', browser: this.youtube.browser }));
      this.acceptAccount(await this.api('/api/settings/youtube/cookies', { method: 'DELETE' }));
      this.message('Account use is off and the stored cookie file has been removed. Your browser sign-in is unchanged.', true);
    } catch (error) { this.message(error.message); }
    finally { this.saving = false; this.render(); }
  }
  async checkUpdates({ quiet = false } = {}) {
    if (this.updateRequest || this.reconnecting || UPDATE_BUSY.has(this.updates.state)) return;
    if (quiet) {
      let last = 0; try { last = Number(this.storage?.getItem(CHECK_KEY)) || 0; } catch {}
      if (last > 0 && this.now() - last < DAY) return;
    }
    this.updateRequest = true; if (!quiet) this.message(''); this.render();
    try {
      this.acceptUpdates(await this.api('/api/updates/check', jsonOptions('POST', { auto: quiet })));
      try { this.storage?.setItem(CHECK_KEY, String(this.now())); } catch {}
    } catch (error) { if (!quiet) this.message(error.message); }
    finally { this.updateRequest = false; this.render(); }
  }
  startAutoChecks() {
    if (this.autoTimer) return;
    this.checkUpdates({ quiet: true }); this.autoTimer = setInterval(() => this.checkUpdates({ quiet: true }), 60 * 60 * 1000);
  }
  async updateAction(action) {
    if (this.updateRequest || this.reconnecting || UPDATE_BUSY.has(this.updates.state)) return;
    this.updateRequest = true; this.message(''); this.render();
    try { this.acceptUpdates(await this.api(`/api/updates/${action}`, jsonOptions('POST'))); }
    catch (error) { this.message(error.message); }
    finally { this.updateRequest = false; this.render(); }
  }
  async uploadPackage(file) {
    if (!file || this.updateRequest || this.reconnecting || UPDATE_BUSY.has(this.updates.state)) return;
    if (!/\.zip$/i.test(file.name)) { this.message('Choose a Stillroom update ZIP file.'); this.$('#settings-update-file').value = ''; return; }
    this.updateRequest = true; this.message('Checking the selected update package…'); this.render();
    try {
      this.acceptUpdates(await this.api('/api/updates/package', { method: 'POST', headers: { 'Content-Type': 'application/octet-stream', 'X-Filename': encodeURIComponent(file.name) }, body: file }));
      this.message('');
    } catch (error) { this.message(error.message); }
    finally { this.updateRequest = false; this.$('#settings-update-file').value = ''; this.render(); }
  }
  schedule() {
    clearTimeout(this.timer);
    if (this.disposed || this.reconnecting || (!this.dialog.open && !UPDATE_BUSY.has(this.updates.state)) || this.updates.state === 'installing') return;
    this.timer = setTimeout(() => this.poll(), UPDATE_BUSY.has(this.updates.state) ? 1000 : 10000);
  }
  async poll() {
    if (this.disposed || this.reconnecting) return;
    try { this.acceptUpdates(await this.api('/api/updates')); }
    catch (error) { if (this.dialog.open) this.message(error.message); this.schedule(); }
  }
  async install() {
    if (this.updateRequest || this.reconnecting || this.updates.state !== 'ready' || !this.updates.canInstall) return;
    const expectedVersion = this.updates.latestVersion;
    this.updateRequest = true; this.message(''); this.render();
    try {
      await this.beforeInstall();
      try {
        const result = await this.api('/api/updates/install', jsonOptions('POST'));
        this.acceptUpdates(result);
        if (this.updates.state !== 'installing') return;
      } catch (error) {
        if (error.status) throw error;
        // The server can exit after accepting installation but before its response reaches this page.
        this.updates = { ...this.updates, state: 'installing', message: 'Waiting for Stillroom to restart…' };
      }
      this.reconnecting = true; clearTimeout(this.timer); this.render();
      await this.waitForRestart(expectedVersion);
    } catch (error) { this.message(error.message); }
    finally { this.updateRequest = false; this.reconnecting = false; this.render(); }
  }
  async waitForRestart(expectedVersion) {
    for (let attempt = 0; attempt < 90 && !this.disposed; attempt++) {
      await this.sleep(1500);
      try {
        const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 3000);
        let health;
        try { health = await this.api('/api/health', { cache: 'no-store', signal: controller.signal }); }
        finally { clearTimeout(timeout); }
        if (health?.app === 'stillroom' && expectedVersion && health.version === expectedVersion) { this.reload(); return true; }
        if (health?.app === 'stillroom') {
          const statusController = new AbortController(), statusTimeout = setTimeout(() => statusController.abort(), 3000);
          let status;
          try { status = await this.api('/api/updates', { cache: 'no-store', signal: statusController.signal }); }
          finally { clearTimeout(statusTimeout); }
          if (status?.state === 'error') {
            this.updates = status; this.onUpdate(this.updates); return false;
          }
        }
      } catch { /* The local service is expected to disconnect while installing. */ }
    }
    if (this.disposed) return false;
    this.updates = { ...this.updates, state: 'error', message: 'Stillroom has not reconnected yet. Open Start Stillroom in the app folder, then refresh this page. Your library is preserved.' };
    this.onUpdate(this.updates); return false;
  }
  destroy() { this.disposed = true; clearTimeout(this.timer); clearInterval(this.autoTimer); }
}
