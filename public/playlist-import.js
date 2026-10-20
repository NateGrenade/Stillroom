import { supportsNativeOpus } from './audio-storage.js';

/** Import YouTube playlists directly, or review YouTube matches for Spotify tracks. */
const FINISHED_ROWS = new Set(['saved', 'existing']);
const ACTIVE_SESSIONS = new Set(['reading', 'matching', 'importing']);
const REVIEW_SESSIONS = new Set(['review', 'complete', 'cancelled']);
const STORAGE_KEY = 'stillroom:spotify-import'; // Keep existing reviews resumable.
const isUnavailable = row => row.track?.available === false;
const isRecommended = candidate => ['strong', 'direct'].includes(candidate?.confidence);
const escapeText = value => String(value ?? '').replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
const jsonOptions = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const time = value => Number.isFinite(Number(value)) && value !== null ? `${Math.floor(Number(value) / 60)}:${String(Math.floor(Number(value) % 60)).padStart(2, '0')}` : 'Length unavailable';

export function youtubeUrl(value) {
  const text = String(value ?? '').trim();
  if (!text || text.length > 2048 || /[\s\\\x00-\x1f\x7f]/.test(text)) return '';
  try {
    const parsed = new URL(text);
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password || parsed.port) return '';
    let id;
    if (['youtu.be', 'www.youtu.be'].includes(parsed.hostname)) id = parsed.pathname.match(/^\/([\w-]{11})\/?$/)?.[1];
    else if (['youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'].includes(parsed.hostname)) {
      if (/^\/watch\/?$/.test(parsed.pathname)) id = parsed.searchParams.getAll('v').length === 1 ? parsed.searchParams.get('v') : '';
      else id = parsed.pathname.match(/^\/(?:shorts|live|embed)\/([\w-]{11})\/?$/)?.[1];
    }
    return /^[\w-]{11}$/.test(id || '') ? `https://www.youtube.com/watch?v=${id}` : '';
  } catch { return ''; }
}

export function youtubePlaylistUrl(value) {
  const text = String(value ?? '').trim();
  if (!text || text.length > 2048 || /[\s\\\x00-\x1f\x7f]/.test(text)) return '';
  try {
    const parsed = new URL(text);
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password || parsed.port) return '';
    if (!['youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'].includes(parsed.hostname) || !/^\/(?:playlist|watch)\/?$/.test(parsed.pathname)) return '';
    const id = parsed.searchParams.getAll('list').length === 1 ? parsed.searchParams.get('list') : '';
    return /^[\w-]{10,150}$/.test(id || '') ? `https://www.youtube.com/playlist?list=${id}` : '';
  } catch { return ''; }
}

export function spotifyUrl(value) {
  try {
    const parsed = new URL(value);
    return parsed.protocol === 'https:' && parsed.hostname === 'open.spotify.com' && !parsed.username && !parsed.password && !parsed.port ? parsed.href : '';
  } catch { return ''; }
}

/** Preserve user choices across incremental matching, refreshes, retries, and cancellation. */
export function reconcileChoices(rows, previous = {}) {
  const next = {};
  for (const row of rows || []) {
    const old = previous[row.id];
    const top = row.candidates?.[0];
    const topUrl = youtubeUrl(top?.url);
    if (old) next[row.id] = { ...old, selected: Boolean(old.selected) && !FINISHED_ROWS.has(row.status) && !isUnavailable(row) };
    else if (row.candidates?.length || !['pending', 'matching'].includes(row.status)) {
      next[row.id] = { url: topUrl, selected: !FINISHED_ROWS.has(row.status) && !isUnavailable(row) && isRecommended(top) && Boolean(topUrl), custom: '', touched: false };
    }
  }
  return next;
}

export function selectedTracks(rows, choices) {
  return (rows || []).flatMap(row => {
    const choice = choices[row.id];
    if (FINISHED_ROWS.has(row.status) || isUnavailable(row) || !choice?.selected) return [];
    if (choice.existingTrackId) return row.existingTracks?.some(track => track.id === choice.existingTrackId) ? [{ rowId: row.id, existingTrackId: choice.existingTrackId }] : [];
    const url = youtubeUrl(choice?.url);
    return url ? [{ rowId: row.id, url }] : [];
  });
}

export function sessionSummary(session) {
  const rows = session?.rows || [];
  const saved = rows.filter(row => row.status === 'saved').length;
  const existing = rows.filter(row => row.status === 'existing').length;
  const failed = rows.filter(row => row.status === 'error').length;
  const matched = rows.filter(row => row.candidates?.length).length;
  const source = session?.source === 'youtube' ? 'YouTube' : 'Spotify';
  if (session?.status === 'reading') return `Reading the ${source} playlist…`;
  if (session?.status === 'matching') {
    const checked = rows.filter(row => !['pending', 'matching'].includes(row.status)).length;
    return session?.source === 'youtube' ? `Checking playlist videos: ${checked} of ${rows.length} checked.` : `Finding YouTube matches: ${checked} of ${rows.length} songs checked.`;
  }
  if (session?.status === 'importing') return `${saved} saved · ${existing} already in your library${failed ? ` · ${failed} failed` : ''}. Importing selected tracks…`;
  if (['complete', 'cancelled'].includes(session?.status) || saved || existing) return `${saved} saved · ${existing} already in your library${failed ? ` · ${failed} failed — select these rows to retry` : ''}.`;
  if (session?.source === 'youtube') return `${rows.filter(row => !isUnavailable(row) && row.candidates?.length).length} of ${rows.length} playlist videos are available to select. Nothing downloads until you choose Import.`;
  return `${matched} of ${rows.length} songs have possible matches. Nothing downloads until you choose Import.`;
}

export function playlistCoverage(session) {
  if (!session) return '';
  if (session.status === 'reading') return 'Checking playlist availability…';
  const count = session.rows?.length || 0;
  if (session.source === 'youtube') {
    const unavailable = (session.rows || []).filter(isUnavailable).length;
    const availability = `${count - unavailable} available${unavailable ? ` · ${unavailable} unavailable` : ''}`;
    if (session.complete) return `Complete video list · ${count} entries · ${availability}`;
    if (Number.isFinite(session.total) && session.total > count) return `Partial video list · ${count} of ${session.total} entries shown · ${availability}`;
    if (Number.isFinite(session.total)) return `${count} playlist entries shown · ${availability} · completeness unverified`;
    return `${count} playlist entries shown · ${availability} · full playlist count unavailable`;
  }
  if (session.complete) return `Complete track list · ${count} songs available`;
  if (Number.isFinite(session.total) && session.total > count) return `Partial track list · ${count} of ${session.total} songs available. Spotify has not exposed the full playlist.`;
  return `${count} public songs · full playlist count unavailable. The full playlist count could not be verified.`;
}

function candidateMarkup(candidate, index, row, choice, disabled) {
  const url = youtubeUrl(candidate.url);
  const checked = choice?.url === url && !choice?.custom && !choice?.existingTrackId;
  return `<div class="spotify-candidate"><label><input type="radio" name="spotify-match-${escapeText(row.id)}" data-spotify-action="candidate" data-candidate-index="${index}"${checked ? ' checked' : ''}${disabled || !url ? ' disabled' : ''}><span><b>${escapeText(candidate.title || 'Untitled video')}</b><span class="spotify-candidate-meta">${escapeText(candidate.channel || 'Unknown channel')} · ${escapeText(time(candidate.duration))}</span><span class="spotify-candidate-reasons">${escapeText((candidate.reasons || []).join(' · '))}</span></span></label><div class="spotify-candidate-side"><span class="spotify-match-badge${isRecommended(candidate) ? ' is-strong' : ''}">${candidate.confidence === 'direct' ? 'Playlist video' : candidate.confidence === 'strong' ? 'Good match' : 'Check match'}</span>${url ? `<a href="${escapeText(url)}" target="_blank" rel="noopener noreferrer" aria-label="Open ${escapeText(candidate.title || 'video')} on YouTube">Listen ↗</a>` : ''}</div></div>`;
}

export function rowMarkup(row, index, choice, disabled, sourceType = 'spotify') {
  const track = row.track || {};
  const candidates = (row.candidates || []).slice(0, 5);
  const existingTracks = row.existingTracks || [];
  const existing = existingTracks.find(track => track.id === choice?.existingTrackId);
  const done = FINISHED_ROWS.has(row.status), unavailable = isUnavailable(row), direct = sourceType === 'youtube';
  disabled ||= unavailable;
  const chosen = candidates.find(candidate => youtubeUrl(candidate.url) === youtubeUrl(choice?.url));
  const source = direct ? youtubeUrl(track.youtubeUrl) : spotifyUrl(track.spotifyUrl);
  const statuses = { pending: direct ? 'Waiting to load' : 'Waiting to search', matching: direct ? 'Checking video…' : 'Finding matches…', unmatched: direct ? 'Video unavailable' : 'No match found', error: 'Needs attention', importing: 'Importing…', saved: 'Saved', existing: 'Already in library', skipped: 'Skipped' };
  const status = unavailable ? 'Unavailable' : statuses[row.status] || (existing ? 'Using your library copy' : chosen?.confidence === 'direct' && !choice?.custom ? 'Ready to import' : chosen?.confidence === 'strong' && !choice?.custom ? 'Good match found' : direct ? 'Review the video' : 'Review the match');
  const customInvalid = Boolean(choice?.custom) && !youtubeUrl(choice.custom);
  return `<div class="spotify-track-top"><label class="spotify-track-select"><input type="checkbox" data-spotify-action="select" aria-label="Import ${escapeText(track.title || 'track')}"${choice?.selected && !done && !unavailable ? ' checked' : ''}${disabled || done ? ' disabled' : ''}><span class="spotify-track-number">${index + 1}</span><span class="spotify-track-copy"><b>${escapeText(track.title || 'Untitled track')}</b><span>${escapeText(track.artist || track.artists?.join(', ') || 'Unknown artist')} · ${escapeText(time(track.duration))}</span></span></label><div class="spotify-track-side"><span class="spotify-row-status${done ? ' is-done' : row.status === 'error' ? ' is-error' : ''}">${escapeText(status)}</span>${source ? `<a href="${escapeText(source)}" target="_blank" rel="noopener noreferrer">${direct ? 'YouTube' : 'Spotify'} ↗</a>` : ''}</div></div>
    ${row.message ? `<p class="spotify-row-message">${escapeText(row.message)}</p>` : ''}
    ${unavailable ? '' : `${existingTracks.length ? `<label class="spotify-existing-label">A copy is already in your library<select data-spotify-action="existing"${disabled || done ? ' disabled' : ''}><option value="">Choose a YouTube recording</option>${existingTracks.map(track => `<option value="library:${escapeText(track.id)}"${existing?.id === track.id ? ' selected' : ''}>Use existing copy · ${escapeText(time(track.duration))}</option>`).join('')}</select></label><p class="field-hint spotify-existing-hint">Check the length before reusing a copy; it may be a trimmed version.</p>` : ''}
    <details class="spotify-match-details"${!candidates.length && !done ? ' open' : ''}><summary><span>${existing ? `Your library copy · ${escapeText(time(existing.duration))}` : choice?.custom ? 'Your YouTube link' : chosen ? escapeText(chosen.title) : candidates.length ? direct ? 'Playlist video — review source' : `${candidates.length} possible match${candidates.length === 1 ? '' : 'es'} — choose a video` : done ? 'Source details' : 'Paste a YouTube link to add this song'}</span><span class="spotify-details-action">${done ? 'Details' : 'Review'} ▾</span></summary><div class="spotify-candidates">${candidates.map((candidate, candidateIndex) => candidateMarkup(candidate, candidateIndex, row, choice, disabled || done)).join('')}</div><label class="spotify-custom-label">Use a different YouTube video<input type="url" data-spotify-action="custom" value="${escapeText(choice?.custom || '')}" placeholder="https://www.youtube.com/watch?v=…" autocomplete="off" spellcheck="false"${disabled || done ? ' disabled' : ''} aria-invalid="${customInvalid}" aria-describedby="spotify-custom-hint-${escapeText(row.id)}"></label><p id="spotify-custom-hint-${escapeText(row.id)}" class="field-hint spotify-custom-hint${customInvalid ? ' is-error' : ''}">${customInvalid ? 'Paste a valid YouTube video link.' : 'Pasting a link selects this song using your chosen video.'}</p></details>`}`;
}

export class PlaylistImportDialog {
  constructor({ dialog, api, refreshLibrary, toast, showDialog, onPlaylist, storage }) {
    this.dialog = dialog; this.api = api; this.refreshLibrary = refreshLibrary; this.toast = toast; this.showDialog = showDialog; this.onPlaylist = onPlaylist; this.storage = storage;
    this.$ = selector => dialog.querySelector(selector);
    this.source = 'spotify'; this.session = null; this.choices = {}; this.pending = false; this.stopping = false; this.timer = null; this.generation = 0; this.titleTouched = false; this.lastSaved = ''; this.restored = null;
    try { this.storage = storage === undefined ? globalThis.localStorage : storage; this.restored = JSON.parse(this.storage?.getItem(STORAGE_KEY) || 'null'); } catch { /* Private browsing can disable storage. */ }
    if (this.restored?.source === 'youtube') this.source = 'youtube';
    this.bind(); this.render();
  }
  get active() { return ACTIVE_SESSIONS.has(this.session?.status); }
  get importing() { return this.session?.status === 'importing'; }
  get editable() { return !this.pending && !this.importing; }
  persist() {
    try {
      if (this.session) this.storage?.setItem(STORAGE_KEY, JSON.stringify({ id: this.session.id, source: this.source, choices: this.choices, title: this.$('#spotify-playlist-name').value, titleTouched: this.titleTouched }));
      else this.storage?.removeItem(STORAGE_KEY);
    } catch { /* The current page still retains the import. */ }
  }
  error(message = '') { const node = this.$('#spotify-error'); node.textContent = message; node.hidden = !message; }
  async open(source) {
    this.showDialog(this.dialog);
    if (this.pending) return;
    if (!this.session && this.restored?.id) {
      const restore = this.restored; this.restored = null; this.pending = true; this.render();
      try {
        const result = await this.api(`/api/playlist-imports/${encodeURIComponent(restore.id)}`);
        this.choices = restore.choices || {}; this.titleTouched = Boolean(restore.titleTouched); this.$('#spotify-playlist-name').value = restore.title || '';
        this.accept(result.session);
        if (!this.dialog.open && ['reading', 'matching'].includes(this.session.status)) { this.pending = false; await this.stop(); }
        else this.schedule();
      } catch (error) {
        if (error.status === 404) {
          this.persist(); this.error('The previous import is no longer active. Any tracks already saved are still in your library. Paste the playlist link to start again.');
        } else {
          this.restored = restore;
          this.error(`${error.message} The previous import could not be checked. Reopen this dialog to reconnect.`);
        }
      } finally { this.pending = false; this.render(); }
    } else if (this.session) await this.poll();
    if (['youtube', 'spotify'].includes(source) && source !== this.source && !this.restored?.id && !this.selectSource(source)) {
      this.error(`Your ${this.source === 'youtube' ? 'YouTube' : 'Spotify'} playlist import is still in progress. Finish or stop it before changing sources.`);
    }
    if (!this.session && !this.restored?.id && this.$('#playlist-youtube-account')) this.$('#playlist-youtube-account').checked = false;
    this.$('#spotify-playlist-url').focus();
  }
  selectSource(source) {
    if (!['youtube', 'spotify'].includes(source)) return false;
    if (source === this.source) return true;
    if (this.pending || this.active || this.restored?.id) return false;
    ++this.generation; clearTimeout(this.timer);
    if (this.$('#playlist-youtube-account')) this.$('#playlist-youtube-account').checked = false;
    this.source = source; this.session = null; this.choices = {}; this.titleTouched = false; this.lastSaved = ''; this.stopping = false;
    this.$('#spotify-playlist-url').value = ''; this.$('#spotify-playlist-name').value = ''; this.$('#spotify-rows').replaceChildren();
    this.persist(); this.error(''); this.render(); return true;
  }
  bind() {
    this.$('#playlist-import-source').addEventListener('change', event => {
      this.selectSource(event.target.value); this.render(); this.$('#spotify-playlist-url').focus();
    });
    this.$('#spotify-find-form').addEventListener('submit', event => { event.preventDefault(); this.find(); });
    this.$('#spotify-import-selected').addEventListener('click', () => this.importSelected());
    this.$('#spotify-stop').addEventListener('click', () => this.stop());
    this.$('#spotify-playlist-name').addEventListener('input', () => { this.titleTouched = true; this.persist(); });
    this.$('#spotify-select-good').addEventListener('click', () => {
      if (!this.editable) return;
      for (const row of this.session?.rows || []) {
        const top = row.candidates?.[0], url = youtubeUrl(top?.url);
        if (!FINISHED_ROWS.has(row.status) && !isUnavailable(row) && isRecommended(top) && url) this.choices[row.id] = { url, selected: true, custom: '', touched: true };
      }
      this.persist(); this.render();
    });
    this.$('#spotify-clear').addEventListener('click', () => {
      if (!this.editable) return;
      for (const row of this.session?.rows || []) this.choices[row.id] = { ...(this.choices[row.id] || { url: '', custom: '' }), selected: false, touched: true };
      this.persist(); this.render();
    });
    this.$('#spotify-view-playlist').addEventListener('click', async () => {
      if (!this.session?.playlistId) return;
      await this.refreshLibrary(); this.onPlaylist?.(this.session.playlistId); this.dialog.close();
    });
    this.$('#spotify-rows').addEventListener('change', event => this.changeChoice(event));
    this.$('#spotify-rows').addEventListener('input', event => { if (event.target.dataset.spotifyAction === 'custom') this.changeChoice(event); });
    this.dialog.addEventListener('close', () => {
      if (['reading', 'matching'].includes(this.session?.status)) this.stop();
      if (!this.importing) clearTimeout(this.timer);
      if (this.$('#playlist-youtube-account')) this.$('#playlist-youtube-account').checked = false;
    });
  }
  changeChoice(event) {
    if (!this.editable) return;
    const action = event.target.dataset.spotifyAction;
    const element = event.target.closest('[data-spotify-row]');
    const row = this.session?.rows.find(entry => entry.id === element?.dataset.spotifyRow);
    if (!row || FINISHED_ROWS.has(row.status) || isUnavailable(row)) return;
    const current = this.choices[row.id] || { url: '', selected: false, custom: '' };
    const choice = { ...current, touched: true };
    if (action === 'select') {
      const existing = row.existingTracks?.some(track => track.id === choice.existingTrackId);
      if (!existing && !youtubeUrl(choice.url) && event.target.checked) {
        const details = element.querySelector('details'); details.open = true;
        const input = element.querySelector('input[type=radio]:not(:disabled), input[type=url]'); input?.focus();
        event.target.checked = false; this.error('Choose a YouTube video or paste a video link for this song first.'); return;
      }
      choice.selected = event.target.checked;
    } else if (action === 'candidate') {
      choice.url = youtubeUrl(row.candidates[Number(event.target.dataset.candidateIndex)]?.url); choice.custom = ''; choice.existingTrackId = ''; choice.selected = Boolean(choice.url);
    } else if (action === 'custom') {
      choice.custom = event.target.value; choice.url = youtubeUrl(choice.custom); choice.existingTrackId = ''; choice.selected = Boolean(choice.url);
    } else if (action === 'existing') {
      const id = event.target.value.startsWith('library:') ? event.target.value.slice(8) : '';
      choice.existingTrackId = row.existingTracks?.some(track => track.id === id) ? id : '';
      choice.url = ''; choice.custom = ''; choice.selected = Boolean(choice.existingTrackId);
    } else return;
    this.choices[row.id] = choice; this.persist(); this.error('');
    if (action === 'custom') {
      element.querySelector('input[type=checkbox]').checked = choice.selected;
      element.querySelectorAll('input[type=radio]').forEach(input => { input.checked = false; });
      const existing = element.querySelector('[data-spotify-action=existing]'); if (existing) existing.value = '';
      element.querySelector('summary > span').textContent = choice.custom ? 'Your YouTube link' : 'Choose a video or paste a video link';
      const invalid = Boolean(choice.custom) && !choice.url;
      event.target.setAttribute('aria-invalid', String(invalid));
      const hint = element.querySelector('.spotify-custom-hint'); hint.classList.toggle('is-error', invalid); hint.textContent = invalid ? 'Paste a valid YouTube video link.' : 'Pasting a link selects this song using your chosen video.';
      element._signature = this.rowSignature(row, choice); this.renderSummary();
    } else this.render();
  }
  accept(session) {
    if (!session?.id) throw new Error('The playlist response could not be read.');
    const prior = this.session;
    this.source = session.source === 'youtube' ? 'youtube' : 'spotify';
    this.session = session; this.choices = reconcileChoices(session.rows, this.choices);
    if (!ACTIVE_SESSIONS.has(session.status)) this.stopping = false;
    this.$('#spotify-playlist-url').value = session.url || this.$('#spotify-playlist-url').value;
    if (!this.titleTouched) this.$('#spotify-playlist-name').value = session.title || '';
    const saved = (session.rows || []).filter(row => FINISHED_ROWS.has(row.status)).map(row => row.id).join(',');
    if (saved !== this.lastSaved) { this.lastSaved = saved; this.refreshLibrary({ quiet: true }); }
    if (prior?.status === 'importing' && session.status !== 'importing') this.toast(sessionSummary(session));
    this.persist(); this.render();
  }
  async find() {
    if (this.pending || this.active || this.restored?.id) return;
    const url = this.$('#spotify-playlist-url').value.trim(); if (!url) return;
    this.pending = true; this.error(''); this.render();
    const generation = ++this.generation; clearTimeout(this.timer);
    try {
      const useYouTubeAccount = this.source === 'youtube' && this.$('#playlist-youtube-account')?.checked === true;
      const result = await this.api('/api/playlist-imports', jsonOptions('POST', { url, source: this.source || 'spotify', allowNativeOpus: supportsNativeOpus(), ...(this.source === 'youtube' ? { useYouTubeAccount } : {}) }));
      if (generation !== this.generation) return;
      this.choices = {}; this.titleTouched = false; this.lastSaved = ''; this.$('#spotify-rows').replaceChildren();
      if (this.$('#playlist-youtube-account')) this.$('#playlist-youtube-account').checked = false;
      this.accept(result.session);
      if (!this.dialog.open && ['reading', 'matching'].includes(this.session.status)) { this.pending = false; await this.stop(); }
      else this.schedule();
    } catch (error) { if (generation === this.generation) this.error(error.message); }
    finally { if (generation === this.generation) { this.pending = false; this.render(); } }
  }
  schedule(delay = 1200) {
    clearTimeout(this.timer);
    if (!this.active || (!this.dialog.open && !this.importing)) return;
    this.timer = setTimeout(() => this.poll(), delay);
  }
  async poll() {
    if (!this.session || this.pending) { this.schedule(); return; }
    const generation = this.generation, id = this.session.id;
    try {
      const result = await this.api(`/api/playlist-imports/${encodeURIComponent(id)}`);
      if (generation !== this.generation || id !== this.session?.id) return;
      this.accept(result.session); this.schedule();
    } catch (error) {
      if (generation !== this.generation) return;
      if (error.status === 404) { this.clearExpired(error.message); return; }
      this.error(`${error.message} Reopen this dialog to check the import again.`);
    }
  }
  clearExpired(message) {
    ++this.generation; clearTimeout(this.timer);
    this.session = null; this.choices = {}; this.restored = null; this.pending = false; this.stopping = false;
    this.persist(); this.error(message || 'This playlist review has expired. Load the playlist again. Saved tracks remain in your library.'); this.render();
  }
  async recoverAction(error, generation) {
    if (generation !== this.generation || !this.session) return;
    try {
      const result = await this.api(`/api/playlist-imports/${encodeURIComponent(this.session.id)}`);
      if (generation !== this.generation) return;
      this.accept(result.session); this.error(error.message); this.schedule();
    } catch (refreshError) {
      if (generation !== this.generation) return;
      if (refreshError.status === 404) this.clearExpired(refreshError.message);
      else this.error(`${error.message} Current progress could not be checked. Close and reopen this dialog to reconnect.`);
    }
  }
  async importSelected() {
    if (this.pending || !REVIEW_SESSIONS.has(this.session?.status)) return;
    const selections = selectedTracks(this.session.rows, this.choices);
    if (!selections.length) return;
    const title = this.$('#spotify-playlist-name').value.trim();
    if (!title) { this.error('Give the playlist a name before importing.'); this.$('#spotify-playlist-name').focus(); return; }
    this.pending = true; this.error(''); this.render();
    const generation = ++this.generation; clearTimeout(this.timer);
    try {
      const result = await this.api(`/api/playlist-imports/${encodeURIComponent(this.session.id)}/import`, jsonOptions('POST', { title, selections, allowNativeOpus: this.session.allowNativeOpus !== false }));
      if (generation !== this.generation) return;
      this.accept(result.session); this.schedule();
    } catch (error) { await this.recoverAction(error, generation); }
    finally { if (generation === this.generation) { this.pending = false; this.render(); } }
  }
  async stop() {
    if (!this.session || this.pending || this.stopping || !this.active) return;
    this.pending = true; this.stopping = true; this.error(''); this.render();
    const generation = ++this.generation; clearTimeout(this.timer);
    try {
      const result = await this.api(`/api/playlist-imports/${encodeURIComponent(this.session.id)}`, { method: 'DELETE' });
      if (generation !== this.generation) return;
      this.accept(result.session); this.schedule();
    } catch (error) { if (generation === this.generation) { this.stopping = false; await this.recoverAction(error, generation); } }
    finally { if (generation === this.generation) { this.pending = false; this.render(); } }
  }
  rowSignature(row, choice) { return JSON.stringify([row, choice, !this.editable, this.source]); }
  renderRows() {
    const container = this.$('#spotify-rows'), rows = this.session?.rows || [];
    const existing = new Map([...container.children].map(element => [element.dataset.spotifyRow, element]));
    rows.forEach((row, index) => {
      let element = existing.get(row.id);
      if (!element) { element = document.createElement('article'); element.className = 'spotify-track'; element.dataset.spotifyRow = row.id; container.append(element); }
      existing.delete(row.id);
      const signature = this.rowSignature(row, this.choices[row.id]);
      if (element._signature === signature) return;
      const expanded = element.querySelector('details')?.open;
      const focused = element.contains(document.activeElement) ? document.activeElement.dataset.spotifyAction : null;
      element.innerHTML = rowMarkup(row, index, this.choices[row.id], !this.editable, this.source);
      if (expanded !== undefined && (element._hadCandidates || focused || this.choices[row.id]?.custom) && element.querySelector('details')) element.querySelector('details').open = expanded;
      element._hadCandidates = Boolean(row.candidates?.length);
      element._signature = signature;
      if (focused) element.querySelector(`[data-spotify-action="${focused}"]`)?.focus({ preventScroll: true });
    });
    for (const element of existing.values()) element.remove();
  }
  renderSummary() {
    const rows = this.session?.rows || [], selections = selectedTracks(rows, this.choices);
    const button = this.$('#spotify-import-selected');
    button.textContent = this.importing ? 'Importing…' : selections.length ? `Import ${selections.length} selected` : 'Import selected';
    button.disabled = this.pending || !REVIEW_SESSIONS.has(this.session?.status) || !selections.length;
    this.$('#spotify-selection-count').textContent = `${selections.length} selected · ${rows.filter(row => FINISHED_ROWS.has(row.status)).length} saved or in library`;
  }
  render() {
    const session = this.session, rows = session?.rows || [], direct = this.source === 'youtube';
    const sourceName = direct ? 'YouTube' : 'Spotify';
    const sourceSelector = this.$('#playlist-import-source'); sourceSelector.value = this.source; sourceSelector.disabled = this.pending || this.active || Boolean(this.restored?.id);
    this.$('#spotify-import-title').textContent = `From ${sourceName}`;
    this.$('#playlist-import-description').textContent = direct ? 'Bring a YouTube playlist into your library. Review the videos, choose what to keep, then import their audio in playlist order.' : 'Read a public playlist, find its songs on YouTube, then choose which matches to add. Audio comes from the YouTube videos you approve.';
    this.$('#playlist-import-url-label').textContent = `${sourceName} playlist link`;
    this.$('#spotify-playlist-url').placeholder = direct ? 'https://www.youtube.com/playlist?list=…' : 'https://open.spotify.com/playlist/…';
    this.$('#spotify-source-hint').textContent = direct ? 'Public and unlisted links work without an account. For private playlists you can access, connect your YouTube account in Settings and enable it below. Review up to 100 entries per import.' : 'Spotify may only expose part of a playlist. You’ll see the available songs and any limits before importing.';
    const accountField = this.$('#playlist-youtube-account-field'); if (accountField) accountField.hidden = !direct;
    const accountCheckbox = this.$('#playlist-youtube-account'); if (accountCheckbox) accountCheckbox.disabled = this.pending || this.active || Boolean(this.restored?.id);
    const accountNote = this.$('#playlist-youtube-session-account');
    if (accountNote) { accountNote.hidden = !session || !direct; accountNote.textContent = session?.useYouTubeAccount ? 'This playlist import uses the YouTube account selected when it was loaded.' : 'This playlist import does not use your YouTube account.'; }
    this.$('#spotify-review-title').textContent = direct ? 'Choose videos to import' : 'Review the matches';
    this.$('#playlist-import-review-hint').textContent = direct ? 'Available playlist videos are selected automatically. Uncheck anything you don’t want. Each selected video is saved as a whole audio track.' : 'Good matches are selected automatically. Check the recording, version, and length; a close match can still be wrong. Choose or paste a video for anything that needs review.';
    this.$('#spotify-select-good').textContent = direct ? 'Select available videos' : 'Select good matches';
    this.$('#spotify-rows').setAttribute('aria-label', direct ? 'YouTube playlist videos' : 'Playlist tracks and YouTube matches');
    this.$('#spotify-background-hint').textContent = `You can close this window while importing. Keep Stillroom running; reopen ${sourceName} playlist to see progress. Stop import keeps the tracks already saved.`;
    this.$('#spotify-find').disabled = this.pending || this.active || Boolean(this.restored?.id);
    this.$('#spotify-find').textContent = ['reading', 'matching'].includes(session?.status) ? direct ? 'Loading playlist…' : 'Finding matches…' : session ? 'Load another playlist' : direct ? 'Load playlist' : 'Find matches';
    this.$('#spotify-playlist-url').disabled = this.pending || this.active;
    this.$('#spotify-review').hidden = !session;
    this.$('#spotify-playlist-name').disabled = !this.editable || Boolean(session?.playlistId);
    this.$('#spotify-select-good').disabled = !this.editable || !rows.some(row => isRecommended(row.candidates?.[0]) && !FINISHED_ROWS.has(row.status) && !isUnavailable(row));
    this.$('#spotify-clear').disabled = !this.editable || !rows.length;
    const stop = this.$('#spotify-stop'); stop.hidden = !this.active; stop.disabled = this.pending || this.stopping; stop.textContent = this.stopping ? 'Stopping…' : this.importing ? 'Stop import' : direct ? 'Stop loading' : 'Stop matching';
    this.$('#spotify-view-playlist').hidden = !session?.playlistId;
    this.$('#spotify-view-playlist').disabled = this.pending;
    this.$('#spotify-background-hint').hidden = !this.importing;
    const progress = this.$('#spotify-progress'); progress.hidden = !session;
    const progressText = this.stopping ? 'Stopping… Tracks already saved will be kept.' : session?.status === 'error' ? session.message || 'The playlist could not be read.' : sessionSummary(session);
    if (progress.textContent !== progressText) progress.textContent = progressText;
    progress.classList.toggle('is-working', this.active);
    const coverage = this.$('#spotify-coverage');
    coverage.classList.toggle('is-partial', Boolean(session) && !session.complete && session.status !== 'reading');
    const coverageText = playlistCoverage(session);
    if (coverage.textContent !== coverageText) coverage.textContent = coverageText;
    const source = direct ? youtubePlaylistUrl(session?.url) : spotifyUrl(session?.url), link = this.$('#spotify-source-link'); link.textContent = `Open ${sourceName} ↗`; link.hidden = !source; if (source) link.href = source; else link.removeAttribute('href');
    const notes = this.$('#spotify-notes'); notes.hidden = !session?.notes?.length;
    notes.innerHTML = (session?.notes || []).map(note => `<li>${escapeText(note)}</li>`).join('');
    this.renderRows(); this.renderSummary();
  }
}
