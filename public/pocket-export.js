const ACTIVE = new Set(['preparing', 'transferring', 'verifying']);
const STATES = new Set([...ACTIVE, 'complete', 'cancelled', 'failed', 'uncertain']);
const TRACK_ID = /^[a-f0-9]{32}$/;
const MARKER_KEY = 'stillroom:pocket-export-job';
const $ = (root, name) => root.querySelector(`#pocket-export-${name}`);
const json = body => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
function safeText(value, limit = 1000) { return typeof value === 'string' ? value.slice(0, limit) : ''; }
function bytes(value) {
  if (!Number.isSafeInteger(value) || value < 0) return 'size unavailable';
  if (value < 1024) return `${value} B`;
  const unit = value < 1024 ** 2 ? 'KiB' : value < 1024 ** 3 ? 'MiB' : 'GiB';
  const divisor = unit === 'KiB' ? 1024 : unit === 'MiB' ? 1024 ** 2 : 1024 ** 3;
  return `${(value / divisor).toFixed(1)} ${unit}`;
}
function defaultStorage() { try { return globalThis.localStorage; } catch { return null; } }
function requestId() {
  const id = globalThis.crypto?.randomUUID?.().replaceAll('-', '');
  if (!id) throw new Error('This browser cannot create a transfer identifier. Update the browser and try again.');
  return id;
}
export function normalizePocketSelection(selection, library) {
  const tracks = new Set((library?.tracks || []).map(track => track.id));
  if (selection?.mode === 'all') return { mode: 'all' };
  if (selection?.mode === 'playlist') {
    if (!TRACK_ID.test(selection.playlistId || '') || !(library?.playlists || []).some(entry => entry.id === selection.playlistId)) throw new Error('Choose an existing playlist.');
    return { mode: 'playlist', playlistId: selection.playlistId };
  }
  if (selection?.mode === 'tracks') {
    if (!Array.isArray(selection.trackIds) || selection.trackIds.length > 10000 || !selection.trackIds.length) throw new Error('Choose at least one song.');
    if (selection.trackIds.some(id => typeof id !== 'string' || !TRACK_ID.test(id) || !tracks.has(id))) throw new Error('A chosen song is no longer in this library. Choose the songs again.');
    return { mode: 'tracks', trackIds: [...new Set(selection.trackIds)] };
  }
  throw new Error('Choose all songs, a playlist, or individual songs.');
}
export function pocketDeviceOptions(response) {
  if (response?.supported !== true || !Array.isArray(response.devices)) return [];
  return response.devices.flatMap((device, deviceIndex) => {
    if (!device || typeof device.id !== 'string' || !device.id || device.id.length > 200 || !Array.isArray(device.apps)) return [];
    return device.apps.flatMap((app, appIndex) => {
      if (!app || typeof app.bundleId !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9.-]{0,254}$/.test(app.bundleId)) return [];
      return [{ key: `${deviceIndex}:${appIndex}`, deviceId: device.id, bundleId: app.bundleId,
        label: `${safeText(device.name, 160) || 'iPhone'} — ${safeText(app.name, 160) || 'Pocket Stillroom'}${app.version ? ` (${safeText(app.version, 50)})` : ''}` }];
    });
  });
}
export function pocketJobProgress(job) {
  const terminal = { complete: 'Copy complete', cancelled: 'Transfer cancelled', failed: 'Transfer failed', uncertain: 'Copy outcome uncertain' }[job?.state];
  const phase = terminal || safeText(job?.phase, 120) || ({ preparing: 'Preparing library', transferring: 'Copying to iPhone', verifying: 'Verifying copy' }[job?.state] || 'Checking transfer');
  if (job?.state === 'complete') return { value: 100, label: phase };
  const total = job?.totalBytes, done = job?.completedBytes;
  const value = Number.isSafeInteger(total) && total > 0 && Number.isSafeInteger(done) && done >= 0 ? Math.min(100, Math.max(0, done / total * 100)) : null;
  return { value, label: terminal || (value === null ? phase : `${phase} · ${Math.floor(value)}%`) };
}
function validJob(job) { return job && typeof job.id === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(job.id) && STATES.has(job.state); }

/** One explicit, locally reviewed cable transfer. Closing is separate from cancellation. */
export class PocketExportDialog {
  constructor({ dialog, api, showDialog, getLibrary, onClose = () => {}, onChange = () => {}, storage = defaultStorage(), schedule = globalThis.setTimeout.bind(globalThis), clearSchedule = globalThis.clearTimeout.bind(globalThis), createRequestId = requestId }) {
    Object.assign(this, { dialog, api, showDialog, getLibrary, onClose, onChange, storage, scheduleTimer: schedule, clearTimer: clearSchedule, createRequestId });
    this.$ = name => $(dialog, name);
    this.document = dialog.ownerDocument || globalThis.document;
    this.selection = { mode: 'all' }; this.selectedTracks = new Set();
    this.review = null; this.job = null; this.devices = null; this.deviceOptions = [];
    this.pending = false; this.loadingDevices = false; this.reviewing = false; this.stopping = false; this.unknownCreation = false;
    this.requestId = null; this.timer = null; this.generation = 0; this.deviceGeneration = 0; this.reviewGeneration = 0;
    this.errorMessage = ''; this.polling = false; this.reconciling = false; this.restoreMarker();
    this.$('mode').addEventListener('change', () => this.selectionChanged());
    this.$('playlist').addEventListener('change', () => this.selectionChanged());
    this.$('device').addEventListener('change', () => this.render());
    this.$('track-search').addEventListener('input', () => this.renderTracks());
    this.$('tracks').addEventListener('change', event => {
      const input = event.target.closest?.('input[data-pocket-track]');
      if (!input || this.locked) return;
      if (input.checked) this.selectedTracks.add(input.dataset.pocketTrack); else this.selectedTracks.delete(input.dataset.pocketTrack);
      this.selectionChanged(false);
    });
    this.$('select-visible').addEventListener('click', () => { if (this.locked) return; this.filteredTracks().forEach(track => this.selectedTracks.add(track.id)); this.selectionChanged(); });
    this.$('clear-tracks').addEventListener('click', () => { if (this.locked) return; this.selectedTracks.clear(); this.selectionChanged(); });
    this.$('refresh-devices').addEventListener('click', () => this.refreshDevices());
    this.$('review').addEventListener('click', () => this.reviewSelection());
    this.$('start').addEventListener('click', () => this.start());
    this.$('cancel').addEventListener('click', () => this.cancel());
    this.$('check').addEventListener('click', () => this.check());
    this.$('new').addEventListener('click', () => this.newExport());
    dialog.addEventListener('close', () => this.onClose());
    // Native Escape closes the dialog without cancelling a cable copy. The
    // persistent job notice remains visible and reopening resumes its status.
    dialog.addEventListener('cancel', () => {});
  }
  get active() { return !!this.job && ACTIVE.has(this.job.state); }
  get locked() { return this.active || this.pending || this.stopping || this.unknownCreation; }
  restoreMarker() {
    try {
      const value = JSON.parse(this.storage?.getItem(MARKER_KEY) || 'null');
      if (value && /^[a-f0-9]{32}$/.test(value.requestId || '')) { this.requestId = value.requestId; this.unknownCreation = true; }
    } catch { /* An unavailable preference store does not stop a new explicit export. */ }
  }
  persist() {
    try {
      if (this.requestId) this.storage?.setItem(MARKER_KEY, JSON.stringify({ requestId: this.requestId, jobId: this.job?.id || null }));
      else this.storage?.removeItem(MARKER_KEY);
    } catch { /* Keep the current transfer in memory even without storage. */ }
  }
  async request(path, options = {}) {
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    let timer;
    try {
      return await Promise.race([
        this.api(path, { ...options, ...(controller ? { signal: controller.signal } : {}) }),
        new Promise((resolve, reject) => {
          timer = this.scheduleTimer(() => { controller?.abort(); reject(new Error('The request timed out. Its server action may still be running.')); }, 12000);
        }),
      ]);
    } finally { this.clearTimer(timer); }
  }
  async resume() {
    if (this.unknownCreation) { this.render(); await this.recoverCreation(); return; }
    // Reconnect even when browser storage was unavailable. This only reads
    // existing jobs; it does not discover devices or start a transfer.
    try {
      const response = await this.request('/api/pocket-export/jobs', { cache: 'no-store' });
      const existing = Array.isArray(response?.jobs) && response.jobs.find(job => validJob(job) && (ACTIVE.has(job.state) || job.state === 'uncertain'));
      if (existing) this.accept(existing);
    } catch { /* No saved request means there is no unresolved action to claim. */ }
  }
  async open(selection = null) {
    if (!this.locked && (!this.job || selection)) {
      if (this.job) { ++this.generation; this.clearTimer(this.timer); this.job = null; this.requestId = null; this.persist(); }
      ++this.reviewGeneration; this.reviewing = false;
      this.selection = normalizePocketSelection(selection || { mode: 'all' }, this.getLibrary());
      this.selectedTracks = new Set(this.selection.trackIds || []);
      this.review = null; this.errorMessage = ''; this.$('device').value = '';
      this.renderSelection();
    }
    this.showDialog(this.dialog); this.$('title').focus(); this.render();
    if (this.unknownCreation) await this.recoverCreation();
    else if (this.job) await this.poll();
    else await this.refreshDevices();
  }
  renderSelection() {
    this.$('mode').value = this.selection.mode;
    const select = this.$('playlist'), previous = this.selection.playlistId || select.value;
    const placeholder = this.document.createElement('option'); placeholder.value = ''; placeholder.textContent = 'Choose a playlist';
    select.replaceChildren(placeholder);
    (this.getLibrary()?.playlists || []).forEach(entry => { const option = this.document.createElement('option'); option.value = entry.id; option.textContent = safeText(entry.name, 4096); select.appendChild(option); });
    select.value = previous;
    this.renderTracks();
  }
  filteredTracks() {
    const query = this.$('track-search').value.trim().toLocaleLowerCase();
    return (this.getLibrary()?.tracks || []).filter(track => !query || `${track.title || ''} ${track.artist || ''}`.toLocaleLowerCase().includes(query));
  }
  renderTracks() {
    const rows = this.filteredTracks().map(track => {
      const label = this.document.createElement('label'); label.className = 'pocket-track-choice';
      const input = this.document.createElement('input'); input.type = 'checkbox'; input.dataset.pocketTrack = track.id; input.checked = this.selectedTracks.has(track.id); input.disabled = this.locked;
      const text = this.document.createElement('span'); text.textContent = `${safeText(track.title, 4096) || 'Untitled track'} · ${safeText(track.artist, 4096) || 'Unknown artist'}${track.kind === 'loop' ? ' · saved loop' : ''}`;
      label.append(input, text); return label;
    });
    if (!rows.length) { const empty = this.document.createElement('p'); empty.className = 'pocket-empty-selection'; empty.textContent = 'No songs match your search.'; rows.push(empty); }
    this.$('tracks').replaceChildren(...rows);
  }
  currentSelection() {
    return normalizePocketSelection({ mode: this.$('mode').value, playlistId: this.$('playlist').value, trackIds: [...this.selectedTracks] }, this.getLibrary());
  }
  selectionChanged(renderTracks = true) {
    if (this.locked) return;
    ++this.reviewGeneration; this.reviewing = false; this.review = null; this.errorMessage = '';
    try { this.selection = this.currentSelection(); } catch { this.selection = { mode: this.$('mode').value }; }
    if (renderTracks) this.renderTracks(); this.render();
  }
  async refreshDevices() {
    if (this.loadingDevices || this.active || this.pending || this.unknownCreation) return;
    const token = ++this.deviceGeneration; this.loadingDevices = true; this.render();
    try {
      const response = await this.request('/api/pocket-export/devices', { cache: 'no-store' });
      if (token !== this.deviceGeneration) return;
      const previous = this.deviceOptions.find(entry => entry.key === this.$('device').value);
      this.devices = response; this.deviceOptions = pocketDeviceOptions(response);
      const placeholder = this.document.createElement('option'); placeholder.value = ''; placeholder.textContent = 'Choose an iPhone and installed app';
      this.$('device').replaceChildren(placeholder);
      this.deviceOptions.forEach(entry => { const option = this.document.createElement('option'); option.value = entry.key; option.textContent = entry.label; this.$('device').appendChild(option); });
      this.$('device').value = this.deviceOptions.find(entry => entry.deviceId === previous?.deviceId && entry.bundleId === previous?.bundleId)?.key || '';
      this.errorMessage = '';
    } catch (error) { if (token === this.deviceGeneration) { this.devices = null; this.deviceOptions = []; this.errorMessage = error.message; } }
    finally { if (token === this.deviceGeneration) { this.loadingDevices = false; this.render(); } }
  }
  async reviewSelection() {
    if (this.locked || this.reviewing) return;
    let selection;
    try { selection = this.currentSelection(); } catch (error) { this.errorMessage = error.message; this.render(); return; }
    const token = ++this.reviewGeneration;
    this.reviewing = true; this.review = null; this.errorMessage = ''; this.render();
    try {
      const response = await this.request('/api/pocket-export/review', json({ selection }));
      if (token !== this.reviewGeneration) return;
      if (!response || typeof response.reviewToken !== 'string' || !response.reviewToken || !Number.isSafeInteger(response.trackCount) || response.trackCount < 1) throw new Error('The selection could not be reviewed. Choose songs and try again.');
      this.selection = selection; this.review = response;
    } catch (error) { if (token === this.reviewGeneration) this.errorMessage = error.message; }
    finally { if (token === this.reviewGeneration) { this.reviewing = false; this.render(); } }
  }
  async start() {
    if (this.locked || this.loadingDevices || this.reviewing || !this.review || this.job) return;
    const device = this.deviceOptions.find(entry => entry.key === this.$('device').value);
    if (!device || this.devices?.supported !== true) { this.errorMessage = 'Choose the exact iPhone and installed Pocket Stillroom app first.'; this.render(); return; }
    let selection;
    try {
      selection = this.currentSelection();
      if (JSON.stringify(selection) !== JSON.stringify(this.selection)) throw new Error('The selection changed. Review it again before copying.');
      this.requestId = this.createRequestId();
      if (!/^[a-f0-9]{32}$/.test(this.requestId)) throw new Error('The transfer identifier could not be created.');
    } catch (error) { this.errorMessage = error.message; this.render(); return; }
    this.pending = true; this.unknownCreation = true; this.errorMessage = ''; this.persist(); this.render();
    const token = ++this.generation;
    try {
      const response = await this.request('/api/pocket-export/jobs', json({ selection, reviewToken: this.review.reviewToken, deviceId: device.deviceId, bundleId: device.bundleId, requestId: this.requestId }));
      if (token !== this.generation) return;
      this.accept(response?.job);
    } catch (error) {
      if (token !== this.generation) return;
      if (error.status >= 400 && error.status < 500) { this.unknownCreation = false; this.requestId = null; this.review = null; this.persist(); this.errorMessage = `${error.message} Review the selection again.`; }
      else { this.errorMessage = 'The copy request was sent, but its outcome is not confirmed. Checking the tracked transfer before another request.'; await this.recoverCreation(); }
    } finally { if (token === this.generation) { this.pending = false; this.render(); this.schedule(); } }
  }
  accept(job) {
    if (!validJob(job)) throw new Error('Stillroom returned an invalid transfer status. Check the tracked transfer before trying again.');
    if (this.job?.id === job.id && ['complete', 'cancelled'].includes(this.job.state) && job.state !== this.job.state) return;
    if (this.job?.id === job.id && !ACTIVE.has(this.job.state) && ACTIVE.has(job.state)) return;
    if (this.requestId && job.requestId && job.requestId !== this.requestId) throw new Error('The transfer status belongs to another request.');
    this.job = job; this.requestId = job.requestId || this.requestId; this.unknownCreation = false; this.errorMessage = '';
    this.persist(); this.render(); this.schedule();
  }
  schedule() {
    this.clearTimer(this.timer); this.timer = null;
    if (!this.active && !this.unknownCreation) return;
    this.timer = this.scheduleTimer(() => this.unknownCreation ? this.recoverCreation() : this.poll(), this.unknownCreation ? 2500 : 1200);
  }
  async recoverCreation() {
    if (!this.requestId || this.reconciling) return;
    this.reconciling = true;
    const token = this.generation, request = this.requestId;
    try {
      const response = await this.request('/api/pocket-export/jobs', { cache: 'no-store' });
      if (token !== this.generation || request !== this.requestId) return;
      const found = Array.isArray(response?.jobs) && response.jobs.find(job => job.requestId === request);
      if (found) this.accept(found);
      else { this.unknownCreation = true; this.errorMessage = 'No matching transfer is currently tracked. Keep this window open and check the iPhone before starting another copy; a server restart can lose transfer status.'; }
    } catch (error) { if (token === this.generation) this.errorMessage = `Transfer status could not be confirmed. ${error.message}`; }
    finally { this.reconciling = false; if (token === this.generation) { this.render(); this.schedule(); } }
  }
  async check() {
    if (this.unknownCreation) return this.recoverCreation();
    if (this.job?.state !== 'uncertain') return this.poll();
    if (this.pending) return;
    const id = this.job.id, token = ++this.generation; this.clearTimer(this.timer); this.pending = true; this.errorMessage = ''; this.render();
    try {
      const response = await this.request(`/api/pocket-export/jobs/${encodeURIComponent(id)}/check`, json({}));
      if (token === this.generation && id === this.job?.id) this.accept(response?.job);
    } catch (error) { if (token === this.generation) this.errorMessage = `The copy still could not be confirmed. ${error.message}`; }
    finally { if (token === this.generation) { this.pending = false; this.render(); this.schedule(); } }
  }
  async poll() {
    if (!this.job || this.pending || this.stopping) return;
    if (this.polling) { this.schedule(); return; }
    this.polling = true; this.clearTimer(this.timer);
    const token = this.generation, id = this.job.id;
    try {
      const response = await this.request(`/api/pocket-export/jobs/${encodeURIComponent(id)}`, { cache: 'no-store' });
      if (token !== this.generation || id !== this.job?.id) return;
      this.accept(response?.job);
    } catch (error) {
      if (token !== this.generation) return;
      this.errorMessage = error.status === 404 ? 'This transfer is no longer tracked. Its copy may still be on the iPhone. Check before starting another export.' : `Connection lost; the transfer may still be running. ${error.message}`;
      if (error.status === 404) { this.job = { ...this.job, state: 'uncertain', message: this.errorMessage }; }
    } finally { this.polling = false; if (token === this.generation) { this.render(); this.schedule(); } }
  }
  async cancel() {
    if (!this.active || this.pending || this.stopping || this.job.canCancel === false) return;
    const id = this.job.id, token = ++this.generation;
    this.stopping = true; this.errorMessage = ''; this.clearTimer(this.timer); this.render();
    try {
      const response = await this.request(`/api/pocket-export/jobs/${encodeURIComponent(id)}`, { method: 'DELETE' });
      if (token === this.generation) this.accept(response?.job);
    } catch (error) {
      if (token === this.generation) this.errorMessage = `Cancellation is not confirmed. Check transfer status; it may still finish. ${error.message}`;
    } finally { if (token === this.generation) { this.stopping = false; this.render(); this.schedule(); } }
  }
  newExport() {
    if (this.locked) return;
    ++this.generation; ++this.reviewGeneration; this.clearTimer(this.timer);
    this.job = null; this.review = null; this.requestId = null; this.errorMessage = ''; this.$('device').value = ''; this.persist(); this.renderSelection(); this.render(); this.refreshDevices();
  }
  render() {
    const mode = this.$('mode').value || this.selection.mode;
    this.$('playlist-wrap').hidden = mode !== 'playlist'; this.$('tracks-wrap').hidden = mode !== 'tracks';
    this.$('selected-count').textContent = `${this.selectedTracks.size} selected`;
    const editingLocked = this.locked || !!this.job;
    this.$('selection-fields').disabled = editingLocked;
    this.$('review').disabled = editingLocked || this.reviewing || this.devices?.supported === false;
    this.$('review').textContent = this.reviewing ? 'Reviewing…' : 'Review selection';
    this.$('refresh-devices').disabled = this.loadingDevices || editingLocked;
    this.$('device').disabled = this.loadingDevices || editingLocked || !this.deviceOptions.length;
    this.$('device-help').textContent = this.loadingDevices ? 'Checking connected devices…' : safeText(this.devices?.message) || (this.devices?.supported === false ? 'Cable transfer is not available on this computer. Use the written transfer guide for a manual path.' : this.deviceOptions.length ? 'Choose the exact iPhone and app. Nothing copies until you press Copy to iPhone.' : 'Connect the iPhone by USB, unlock it, and approve Trust on the phone. Install Pocket Stillroom first if its app is not listed, then refresh.');
    this.$('review-summary').textContent = this.review ? `${this.review.trackCount} songs · ${this.review.playlistCount ?? 0} playlists · ${bytes(this.review.audioBytes)} audio · ${bytes(this.review.bundleBytes)} bundle` : 'Review the selection to check its song count and size before copying.';
    this.$('start').disabled = this.locked || !!this.job || this.loadingDevices || this.reviewing || !this.review || this.devices?.supported !== true || !this.deviceOptions.some(entry => entry.key === this.$('device').value);
    this.$('start').textContent = this.pending ? 'Starting transfer…' : 'Copy to iPhone';
    this.$('status').textContent = safeText(this.errorMessage); this.$('status').hidden = !this.errorMessage;
    const visible = !!this.job || this.unknownCreation || this.pending;
    this.$('job').hidden = !visible;
    const progress = pocketJobProgress(this.job);
    this.$('phase').textContent = this.stopping ? 'Cancelling… waiting for confirmation' : this.unknownCreation ? 'Checking whether the transfer started…' : progress.label;
    if (progress.value === null) this.$('progress').removeAttribute('value'); else this.$('progress').value = progress.value;
    const terminalMessage = this.job?.state === 'complete' ? 'The library bundle was copied to the iPhone. Open Pocket Stillroom and import the copied file; copying does not automatically import songs.' : this.job?.state === 'uncertain' ? 'The copy may be on the iPhone. Check it before sending another copy.' : '';
    this.$('job-message').textContent = safeText(this.job?.message) || terminalMessage;
    this.$('next-step').textContent = this.job?.state === 'complete' ? terminalMessage : '';
    this.$('cancel').hidden = !this.active; this.$('cancel').disabled = this.pending || this.stopping || this.job?.canCancel === false;
    this.$('check').hidden = !this.job && !this.unknownCreation; this.$('check').disabled = this.pending || this.stopping;
    this.$('new').hidden = !this.job || this.active || this.unknownCreation;
    this.dialog.setAttribute('aria-busy', String(this.pending || this.reviewing));
    this.onChange({ job: this.job, active: this.active, pending: this.pending, unknownCreation: this.unknownCreation, message: this.$('phase').textContent });
  }
}
