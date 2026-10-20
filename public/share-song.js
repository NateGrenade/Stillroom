const GENERIC_FILENAME = 'stillroom-audio.m4a';
const MAX_EXPORT_BYTES = 512 * 1024 * 1024;

// The share endpoint only returns this canonical form. Never copy an arbitrary stored URL.
export function canonicalShareUrl(value) {
  return typeof value === 'string' && /^https:\/\/www\.youtube\.com\/watch\?v=[A-Za-z0-9_-]{11}$/.test(value) ? value : '';
}

export function shareFilename(disposition, includeInfo = true) {
  if (!includeInfo || typeof disposition !== 'string') return GENERIC_FILENAME;
  let filename = '';
  const encoded = disposition.match(/(?:^|;)\s*filename\*\s*=\s*UTF-8''([^;]*)/i);
  if (encoded) {
    try { filename = decodeURIComponent(encoded[1].trim()); } catch { return GENERIC_FILENAME; }
  } else {
    const plain = disposition.match(/(?:^|;)\s*filename\s*=\s*(?:"([^"]*)"|([^;]*))/i);
    filename = plain ? (plain[1] ?? plain[2].trim()) : '';
  }
  // Reject paths, platform-special filenames, hidden files, and control characters.
  if (!filename || filename.length > 180 || /[\x00-\x1f\x7f<>:"/\\|?*\u202a-\u202e\u2066-\u2069]/.test(filename)
      || /^[.]/.test(filename) || /[. ]$/.test(filename) || !/\.m4a$/i.test(filename)
      || /^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(filename)) return GENERIC_FILENAME;
  return filename;
}

export async function isM4aAudio(blob, contentType) {
  if (String(contentType || '').split(';')[0].trim().toLowerCase() !== 'audio/mp4' || !blob || blob.size < 12 || blob.size > MAX_EXPORT_BYTES) return false;
  const header = new Uint8Array(await blob.slice(0, 12).arrayBuffer());
  return String.fromCharCode(...header.slice(4, 8)) === 'ftyp';
}

export function saveAudioBlob(blob, filename, { document = globalThis.document, urlApi = globalThis.URL, schedule = setTimeout } = {}) {
  const url = urlApi.createObjectURL(blob);
  const anchor = document.createElement('a');
  try {
    anchor.href = url;
    anchor.download = filename;
    anchor.hidden = true;
    document.body.appendChild(anchor);
    anchor.click();
  } finally {
    anchor.remove();
    // Keep the object URL alive long enough for the browser to consume the download.
    schedule(() => urlApi.revokeObjectURL(url), 60000);
  }
}

function durationLabel(value) {
  const seconds = Math.max(0, Math.round(value));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

/** Share one saved song through an explicit source-copy or local audio-download action. */
export class ShareSongDialog {
  constructor({ dialog, api, showDialog, onMissing = () => {}, onClose = () => {}, toast,
    fetchAudio = (...args) => fetch(...args), clipboard = globalThis.navigator?.clipboard, downloadFile = saveAudioBlob }) {
    Object.assign(this, { dialog, api, showDialog, onMissing, onClose, toast, fetchAudio, clipboard, downloadFile });
    this.$ = selector => dialog.querySelector(selector);
    this.$$ = selector => [...dialog.querySelectorAll(selector)];
    this.trackId = null;
    this.snapshot = null;
    this.loading = false;
    this.busy = false;
    this.missing = false;
    this.requestToken = 0;
    this.$('#share-song-copy').addEventListener('click', () => this.copy());
    this.$('#share-song-download').addEventListener('click', () => this.download());
    this.$('#share-song-retry').addEventListener('click', () => this.load());
    this.$('#share-song-include-info').addEventListener('change', () => this.render());
    dialog.addEventListener('cancel', event => { if (this.locked) event.preventDefault(); });
    dialog.addEventListener('close', () => {
      const id = this.trackId;
      ++this.requestToken;
      this.trackId = null;
      this.snapshot = null;
      this.loading = this.busy = false;
      this.onClose(id);
    });
  }
  status(message = '') {
    const node = this.$('#share-song-status');
    node.textContent = message;
    node.hidden = !message;
  }
  get locked() { return this.busy && this.action === 'download'; }
  render() {
    const unavailable = this.busy || this.loading || this.missing || !this.snapshot;
    const includeInfo = this.$('#share-song-include-info').checked;
    this.$('#share-song-options').hidden = !this.snapshot;
    this.$('#share-song-source').hidden = !this.snapshot?.sourceUrl;
    this.$('#share-song-no-source').hidden = !this.snapshot || !!this.snapshot.sourceUrl;
    this.$('#share-song-copy').disabled = unavailable || !this.snapshot?.sourceUrl;
    this.$('#share-song-download').disabled = unavailable;
    this.$('#share-song-download').textContent = this.action === 'download' ? 'Preparing M4A…' : 'Download M4A copy';
    this.$('#share-song-include-info').disabled = unavailable;
    this.$('#share-song-info-preview').hidden = !includeInfo;
    this.$('#share-song-without-info').hidden = includeInfo;
    this.$('#share-song-retry').hidden = !!this.snapshot || this.loading || this.missing;
    this.$('#share-song-retry').textContent = this.changed ? 'Reload options' : 'Try again';
    this.$('#share-song-retry').disabled = this.busy || this.loading;
    this.$$('.close-dialog').forEach(node => { node.disabled = this.locked; });
    this.$('#share-song-options').setAttribute('aria-busy', String(this.busy || this.loading));
  }
  async open(track) {
    if (this.busy || this.dialog.open || !/^[a-f0-9]{32}$/.test(track?.id || '')) return;
    this.trackId = track.id;
    this.snapshot = null;
    this.missing = false;
    this.changed = false;
    this.action = '';
    this.$('#share-song-include-info').checked = true;
    this.$('#share-song-source-url').value = '';
    this.status('Loading sharing options…');
    this.loading = true;
    this.render();
    this.showDialog(this.dialog);
    this.$('#share-song-heading').focus();
    await this.load();
  }
  async load() {
    if (this.busy || !this.trackId || this.missing) return;
    const token = ++this.requestToken;
    const trackId = this.trackId;
    const reviewing = this.changed;
    this.loading = true;
    this.status('Loading sharing options…');
    this.render();
    try {
      const value = await this.api(`/api/tracks/${encodeURIComponent(trackId)}/share`, { cache: 'no-store' });
      if (token !== this.requestToken) return;
      if (!value || !['title', 'artist', 'album'].every(field => typeof value[field] === 'string')
          || !['original', 'loop'].includes(value.kind) || !Number.isFinite(value.exportDuration) || value.exportDuration <= 0
          || !/^[a-f0-9]{64}$/.test(value.revision || '')) {
        throw new Error('The sharing options could not be loaded. Please try again.');
      }
      this.snapshot = { ...value, sourceUrl: canonicalShareUrl(value.sourceUrl) };
      this.changed = false;
      for (const field of ['title', 'artist', 'album']) this.$(`#share-song-${field}`).textContent = value[field] || '(not set)';
      this.$('#share-song-source-url').value = this.snapshot.sourceUrl;
      this.$('#share-song-audio-summary').textContent = value.kind === 'loop'
        ? `One M4A audio file of the saved loop region (${durationLabel(value.exportDuration)}). The loop region is flattened into the audio; the recipient chooses whether to repeat it.`
        : `One M4A audio file of the saved song (${durationLabel(value.exportDuration)}).`;
      this.status();
    } catch (error) {
      if (token === this.requestToken) this.handleError(error, 'The sharing options could not be loaded. Please try again.');
    } finally {
      if (token === this.requestToken) {
        this.loading = false;
        this.render();
        if (this.changed) this.$('#share-song-retry').focus();
        else if (reviewing && this.snapshot) this.$('#share-song-heading').focus();
      }
    }
  }
  handleError(error, fallback) {
    if (error?.status === 404) {
      this.missing = true;
      this.status('This song was removed from your library. Close this window to continue.');
      this.onMissing();
    } else if (error?.status === 409) {
      this.snapshot = null;
      this.changed = true;
      this.status(error?.message || 'The sharing options are no longer available. Reload them and review the details before downloading.');
    } else this.status(error?.message || fallback);
  }
  async copy() {
    if (this.busy || this.loading || this.missing || !this.snapshot?.sourceUrl) return;
    const token = this.requestToken;
    this.busy = true;
    this.action = 'copy';
    this.render();
    try {
      if (!this.clipboard?.writeText) throw new Error('Clipboard unavailable');
      await this.clipboard.writeText(this.snapshot.sourceUrl);
      if (token === this.requestToken) { this.status('Source link copied.'); this.toast('Source link copied.'); }
    } catch {
      if (token === this.requestToken) {
        this.status('Clipboard access is unavailable. Select the link above and copy it manually.');
        this.$('#share-song-source-url').focus();
        this.$('#share-song-source-url').select();
      }
    } finally {
      if (token === this.requestToken) { this.busy = false; this.action = ''; this.render(); }
    }
  }
  async download() {
    if (this.busy || this.loading || this.missing || !this.snapshot || !this.trackId) return;
    const token = this.requestToken;
    const trackId = this.trackId;
    const includeInfo = this.$('#share-song-include-info').checked;
    this.busy = true;
    this.action = 'download';
    this.status('Preparing your M4A copy. Keep this window open…');
    this.render();
    try {
      const response = await this.fetchAudio(`/api/tracks/${encodeURIComponent(trackId)}/export`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ includeInfo, revision: this.snapshot.revision }), cache: 'no-store',
      });
      if (token !== this.requestToken) return;
      if (!response.ok) {
        let body;
        try { body = await response.json(); } catch { /* The server may be unavailable. */ }
        throw Object.assign(new Error(typeof body?.error === 'string' ? body.error : 'The audio copy could not be prepared. Please try again.'), { status: response.status });
      }
      if (Number(response.headers.get('Content-Length')) > MAX_EXPORT_BYTES) {
        try { await response.body?.cancel(); } catch { /* The body may already be closed. */ }
        throw new Error('This audio copy is too large to download. Choose a song or loop under 512 MB.');
      }
      const blob = await response.blob();
      if (!await isM4aAudio(blob, response.headers.get('Content-Type'))) throw new Error('The server did not return a valid M4A audio file. Please try again.');
      if (token !== this.requestToken) return;
      this.downloadFile(blob, shareFilename(response.headers.get('Content-Disposition'), includeInfo));
      this.status('Download requested. The file stays on this computer until you send it yourself.');
      this.toast('M4A download requested.');
    } catch (error) {
      if (token === this.requestToken) this.handleError(error, 'The audio copy could not be prepared. Please try again.');
    } finally {
      if (token === this.requestToken) {
        this.busy = false;
        this.action = '';
        this.render();
        if (this.changed) this.$('#share-song-retry').focus();
      }
    }
  }
}
