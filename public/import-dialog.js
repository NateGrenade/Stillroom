import { storageDescription, supportsNativeOpus } from './audio-storage.js';

/** Reviewable audio imports. A prepared job is only saved after the user accepts it. */
export function parseTimestamp(value) {
  const text = String(value ?? '').trim();
  if (!text) return null;
  if (!/^\d+(?::\d{1,2}){0,2}(?:\.\d+)?$/.test(text)) throw new Error('Use seconds, m:ss, or h:mm:ss for timestamps.');
  const parts = text.split(':').map(Number);
  if (parts.slice(1).some(part => part >= 60)) throw new Error('Minutes and seconds after a colon must be less than 60.');
  const seconds = parts.reduce((total, part) => total * 60 + part, 0);
  if (!Number.isFinite(seconds)) throw new Error('Enter a valid timestamp.');
  return seconds;
}
export function validateTimes(start, end) {
  const first = parseTimestamp(start), last = parseTimestamp(end);
  if (last !== null && last <= (first ?? 0)) throw new Error('The end time must be later than the start time.');
  return { start: first, end: last };
}
export function preciseTime(value) {
  const total = Math.round(Math.max(0, Number(value) || 0) * 1000);
  const seconds = Math.floor(total / 1000);
  const fraction = total % 1000 ? `.${String(total % 1000).padStart(3, '0').replace(/0+$/, '')}` : '';
  const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds / 60) % 60;
  return `${hours ? `${hours}:${String(minutes).padStart(2, '0')}` : Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}${fraction}`;
}
function playbackTime(value) {
  return preciseTime(Math.floor(Number(value) || 0));
}
function metadataHeader(metadata) {
  const bytes = new TextEncoder().encode(JSON.stringify(metadata));
  return btoa(Array.from(bytes, byte => String.fromCharCode(byte)).join(''));
}
function jsonOptions(method, body) {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
}
export function loopRegion(job, buffer) {
  const first = Math.round((Number(job.loopStart) || 0) * buffer.sampleRate);
  const last = job.loopEnd == null ? buffer.length : Math.round(Number(job.loopEnd) * buffer.sampleRate);
  if (first < 0 || last <= first || last > buffer.length + Math.round(buffer.sampleRate * .005)) throw new Error('The prepared loop has invalid playback boundaries.');
  return { start: first / buffer.sampleRate, end: Math.min(last, buffer.length) / buffer.sampleRate };
}

/** A separate preview transport avoids changing the library queue or Media Session. */
export class LoopPreview {
  constructor({ onChange, pauseMain, fetchAudio, contextFactory, getVolume } = {}) {
    this.onChange = onChange || (() => {});
    this.pauseMain = pauseMain || (() => {});
    this.getVolume = getVolume || (() => .4);
    this.fetchAudio = fetchAudio || ((...args) => fetch(...args));
    this.contextFactory = contextFactory || (() => new (window.AudioContext || window.webkitAudioContext)());
    this.context = null; this.gain = null; this.voice = null; this.buffer = null; this.job = null;
    this.offset = 0; this.duration = 0; this.start = 0; this.end = 0; this.startedAt = 0;
    this.loading = false; this.playing = false; this.error = ''; this.version = 0;
    this.controller = null; this.timer = null;
  }
  position() {
    if (!this.duration) return 0;
    return (this.offset + (this.playing ? Math.max(0, this.context.currentTime - this.startedAt) : 0)) % this.duration;
  }
  emit() { this.onChange({ playing: this.playing, loading: this.loading, error: this.error, currentTime: this.position(), duration: this.duration }); }
  setJob(job) {
    this.stop(); this.job = job; this.buffer = null; this.error = '';
    this.duration = Math.max(0, Number(job?.loopEnd) - Number(job?.loopStart)) || Number(job?.duration) || 0;
    this.emit();
  }
  haltVoice() {
    if (this.voice) { try { this.voice.stop(); } catch {} this.voice.disconnect(); this.voice = null; }
    clearInterval(this.timer); this.timer = null;
  }
  pause() { this.offset = this.position(); this.haltVoice(); this.playing = false; this.emit(); }
  stop() { ++this.version; this.controller?.abort(); this.controller = null; this.haltVoice(); this.playing = false; this.loading = false; this.offset = 0; this.emit(); }
  async play(nearJoin = false) {
    if (!this.job || this.loading) return;
    const version = ++this.version;
    this.pause(); this.error = ''; this.loading = true; this.emit();
    try {
      if (!this.context) {
        this.context = this.contextFactory(); this.gain = this.context.createGain(); this.gain.connect(this.context.destination);
      }
      this.gain.gain.value = Math.max(0, Math.min(1, Number(this.getVolume()) || 0));
      this.pauseMain();
      await this.context.resume();
      if (version !== this.version) return;
      if (!this.buffer) {
        this.controller = new AbortController();
        const response = await this.fetchAudio(this.job.audioUrl, { signal: this.controller.signal });
        if (!response.ok) throw new Error('The preview audio could not be loaded.');
        const buffer = await this.context.decodeAudioData(await response.arrayBuffer());
        if (version !== this.version) return;
        const region = loopRegion(this.job, buffer);
        this.buffer = buffer; this.start = region.start; this.end = region.end; this.duration = this.end - this.start;
      }
      if (version !== this.version) return;
      if (nearJoin) this.offset = Math.max(0, this.duration - Math.min(3, this.duration / 2));
      this.offset %= this.duration;
      const voice = this.context.createBufferSource();
      voice.buffer = this.buffer; voice.loop = true; voice.loopStart = this.start; voice.loopEnd = this.end;
      voice.connect(this.gain);
      this.startedAt = this.context.currentTime;
      voice.start(this.startedAt, this.start + this.offset);
      this.voice = voice; this.loading = false; this.playing = true;
      this.timer = setInterval(() => this.emit(), 200); this.emit();
    } catch (error) {
      if (version !== this.version || error.name === 'AbortError') return;
      this.loading = false; this.playing = false; this.error = error.message || 'The loop preview could not be played.'; this.emit();
    }
  }
  seek(seconds) {
    const resume = this.playing; this.pause();
    this.offset = Math.max(0, Math.min(Number(seconds) || 0, this.duration)) % (this.duration || 1);
    if (resume) this.play(); else this.emit();
  }
  destroy() { this.stop(); this.job = null; this.buffer = null; this.context?.close().catch(() => {}); this.context = null; this.gain = null; }
}

export class ImportDialog {
  constructor({ dialog, api, refreshLibrary, toast, showDialog, pauseMain, getVolume = () => .4 }) {
    this.dialog = dialog; this.api = api; this.refreshLibrary = refreshLibrary;
    this.toast = toast; this.showDialog = showDialog; this.pauseMain = pauseMain; this.getVolume = getVolume;
    this.$ = selector => dialog.querySelector(selector);
    this.$$ = selector => [...dialog.querySelectorAll(selector)];
    this.source = 'file'; this.metadataMode = 'auto'; this.files = []; this.phase = 'idle'; this.job = null; this.generation = 0;
    this.preview = new LoopPreview({ pauseMain, getVolume, onChange: state => this.renderLoopPlayback(state) });
    this.bind(); this.render();
  }
  get busy() { return ['preparing', 'committing', 'batch'].includes(this.phase); }
  get locked() { return ['committing', 'batch'].includes(this.phase); }
  get batch() { return this.source === 'file' && this.files.length > 1; }
  status(message, success = false) {
    const node = this.$('#import-status'); node.textContent = message; node.hidden = !message;
    node.classList.toggle('is-success', success); node.classList.toggle('is-working', this.busy);
  }
  open() {
    if (!this.dialog.open) {
      this.cleanup(); this.$('#import-form').reset(); this.files = []; this.source = 'file'; this.metadataMode = 'auto';
      this.$('#import-options').open = false; this.phase = 'idle'; this.status(''); this.render();
    }
    this.showDialog(this.dialog);
  }
  discard(job) { if (job?.id) this.api(`/api/import-jobs/${encodeURIComponent(job.id)}`, { method: 'DELETE' }).catch(() => {}); }
  cleanup() {
    ++this.generation; this.discard(this.job); this.job = null; this.phase = 'idle';
    this.pausePreview(); this.preview.destroy();
    const audio = this.$('#import-preview-audio'); audio.removeAttribute('src'); audio.load();
  }
  pausePreview() { this.$('#import-preview-audio').pause(); this.preview.stop(); }
  invalidate() {
    if (this.busy) return;
    const hadPreview = this.job?.status === 'ready';
    ++this.generation; this.discard(this.job); this.job = null; this.pausePreview(); this.phase = 'idle';
    this.status(hadPreview ? 'Settings changed. Prepare again to hear the updated track.' : ''); this.render();
  }
  selectFiles(files) {
    if (this.busy) return;
    this.invalidate(); this.files = [...files];
    if (this.files.length > 1) { this.$('#import-track-title').value = ''; this.$('#import-options').open = false; }
    this.render();
  }
  bind() {
    this.dialog.addEventListener('cancel', event => { if (this.locked) event.preventDefault(); });
    this.dialog.addEventListener('close', () => this.cleanup());
    this.$$('[data-import-source]').forEach(button => button.addEventListener('click', () => {
      if (this.busy || this.source === button.dataset.importSource) return;
      this.invalidate(); this.source = button.dataset.importSource; this.render();
      if (this.source === 'youtube') this.$('#import-youtube-url').focus();
    }));
    this.$$('[data-metadata-mode]').forEach(button => button.addEventListener('click', () => {
      if (this.busy || this.metadataMode === button.dataset.metadataMode) return;
      this.invalidate(); this.metadataMode = button.dataset.metadataMode; this.render();
    }));
    this.$('#audio-files').addEventListener('change', event => this.selectFiles(event.target.files));
    for (const type of ['dragenter', 'dragover', 'dragleave', 'drop']) this.$('#upload-zone').addEventListener(type, event => {
      event.preventDefault();
      this.$('#upload-zone').classList.toggle('is-dragging', !this.busy && ['dragenter', 'dragover'].includes(type));
    });
    this.$('#upload-zone').addEventListener('drop', event => {
      if (this.busy) return;
      const files = [...event.dataTransfer.files].filter(file => file.type.startsWith('audio/') || /\.(mp3|wav|m4a|aac|ogg|opus|flac|aiff?|webm)$/i.test(file.name));
      this.selectFiles(files);
      if (!files.length) this.status('Choose an audio file to add to your library.');
    });
    this.$$('#import-youtube-url, #import-start, #import-end').forEach(input => input.addEventListener('input', () => this.invalidate()));
    this.$$('#import-exact, #import-loop, #import-youtube-account').forEach(input => input.addEventListener('change', () => this.invalidate()));
    this.$('#import-form').addEventListener('submit', event => { event.preventDefault(); this.submit(); });
    this.$('#import-preview-audio').addEventListener('play', () => { this.pauseMain(); this.preview.stop(); });
    this.$('#import-preview-audio').addEventListener('error', () => {
      if (this.job?.status === 'ready' && this.$('#import-preview-audio').getAttribute('src')) this.status('The preview could not be played. Try preparing this track again.');
    });
    this.$('#import-loop-play').addEventListener('click', () => { if (this.preview.playing) this.preview.pause(); else this.preview.play(); });
    this.$('#import-loop-join').addEventListener('click', () => this.preview.play(true));
    this.$('#import-loop-seek').addEventListener('input', event => this.preview.seek(Number(event.target.value)));
  }
  metadata() { return { title: this.$('#import-track-title').value.trim(), artist: this.$('#import-artist').value.trim(), album: this.$('#import-album').value.trim() }; }
  options() {
    const start = this.$('#import-start').value.trim(), end = this.$('#import-end').value.trim();
    validateTimes(start, end);
    return { metadataMode: this.metadataMode, metadata: this.metadataMode === 'manual' ? this.metadata() : {}, start, end, exact: this.$('#import-exact').checked, loop: this.$('#import-loop').checked, allowNativeOpus: supportsNativeOpus() };
  }
  render() {
    const ready = this.job?.status === 'ready';
    this.$('#import-form').setAttribute('aria-busy', String(this.busy));
    this.$$('input, select, button').forEach(element => { element.disabled = this.busy; });
    this.$$('.close-dialog').forEach(button => { button.disabled = this.locked; });
    this.$$('[data-import-source]').forEach(button => {
      const active = this.source === button.dataset.importSource; button.classList.toggle('is-active', active); button.setAttribute('aria-pressed', String(active));
    });
    this.$$('[data-metadata-mode]').forEach(button => {
      const active = this.metadataMode === button.dataset.metadataMode; button.classList.toggle('is-active', active); button.setAttribute('aria-pressed', String(active));
    });
    this.$('#import-file-source').hidden = this.source !== 'file'; this.$('#import-youtube-source').hidden = this.source !== 'youtube';
    this.$('#import-youtube-url').disabled = this.busy || this.source !== 'youtube';
    this.$('#upload-label').textContent = this.files.length === 1 ? this.files[0].name : this.files.length ? `${this.files.length} audio files selected` : 'Choose audio files';
    this.$('#upload-description').textContent = this.files.length ? 'Click to choose different files' : 'or drop them here';
    this.$('#import-metadata-fields').hidden = this.metadataMode === 'auto' && !ready;
    this.$('#import-metadata-hint').textContent = ready ? 'Check the details below. Video titles and channel names may need a correction.' : this.metadataMode === 'auto' ? this.batch ? 'Use each file’s tags and name. Add one file at a time to review its details before saving.' : 'Use file tags or video details when available. You can edit the result before saving.' : this.batch ? 'Creator and album apply to every selected file. Titles come from the file names.' : 'Fill in what you know. A blank title uses the source name.';
    this.$('#import-track-title').disabled = this.busy || this.batch;
    this.$('#import-batch-options').hidden = !this.batch; this.$('#import-options').hidden = this.batch;
    this.$$('#import-options input').forEach(input => { input.disabled = this.busy || this.batch; });
    const exact = this.$('#import-exact').checked, loop = this.$('#import-loop').checked;
    this.$('#import-exact-hint').textContent = exact ? 'On: keep your selected boundaries exactly, including for loops.' : 'Off: look near your times for a cleaner musical boundary.';
    this.$('#import-exact-loop-note').hidden = !(exact && loop);
    const range = !!(this.$('#import-start').value || this.$('#import-end').value);
    this.$('#import-options-summary').textContent = `${range ? exact ? 'Exact excerpt' : 'Smart excerpt' : 'Whole track'}${loop ? ' · Loop' : ''}`;
    this.$('#import-preview').hidden = !ready;
    const submit = this.$('#import-submit');
    submit.textContent = this.phase === 'committing' ? 'Saving…' : this.phase === 'batch' ? 'Adding tracks…' : this.phase === 'preparing' ? 'Preparing…' : ready ? 'Add to library' : this.batch ? `Add ${this.files.length} tracks` : 'Prepare track';
    submit.disabled = this.busy || (!ready && (this.source === 'file' ? !this.files.length : !this.$('#import-youtube-url').value.trim()));
    this.$('#import-cancel').textContent = this.phase === 'preparing' ? 'Cancel preparation' : 'Cancel';
  }
  renderLoopPlayback(state) {
    const button = this.$('#import-loop-play'); if (!button) return;
    button.textContent = state.loading ? 'Loading…' : state.playing ? 'Pause loop' : 'Play loop';
    button.disabled = this.busy || state.loading;
    this.$('#import-loop-join').disabled = this.busy || state.loading;
    this.$('#import-loop-time').textContent = playbackTime(state.currentTime);
    this.$('#import-loop-duration').textContent = playbackTime(state.duration);
    const seek = this.$('#import-loop-seek'); seek.max = state.duration || 1; seek.value = state.currentTime;
    seek.style.setProperty('--range-progress', `${state.duration ? state.currentTime / state.duration * 100 : 0}%`);
    seek.setAttribute('aria-valuetext', `${playbackTime(state.currentTime)} of ${playbackTime(state.duration)}`);
    if (state.error) this.status(state.error);
  }
  showPreview(job) {
    this.job = job; this.phase = 'ready';
    for (const [name, id] of [['title', 'import-track-title'], ['artist', 'import-artist'], ['album', 'import-album']]) this.$(`#${id}`).value = job.metadata?.[name] || '';
    const analysis = job.analysis || {}, isLoop = !!analysis.loop;
    this.$('#import-preview-title').textContent = isLoop ? 'Listen to the loop' : 'Ready to listen';
    this.$('#import-preview-kind').textContent = isLoop ? 'Loop' : 'Original';
    const facts = this.$('#import-range-facts'); facts.replaceChildren();
    const addFact = (label, value) => { const term = document.createElement('dt'), detail = document.createElement('dd'); term.textContent = label; detail.textContent = value; facts.append(term, detail); };
    addFact('Source range', `${preciseTime(analysis.appliedStart)} – ${preciseTime(analysis.appliedEnd)}`);
    addFact(isLoop ? 'Loop length' : 'Track length', preciseTime(job.duration));
    if (job.storage) {
      addFact('Source audio', storageDescription(job.storage.source));
      addFact('Saved audio', storageDescription(job.storage.stored));
    }
    if (analysis.requestedStart != null || analysis.requestedEnd != null) addFact('Requested', `${preciseTime(analysis.requestedStart ?? 0)} – ${preciseTime(analysis.requestedEnd ?? analysis.sourceDuration)}`);
    const notes = this.$('#import-analysis-notes'); notes.replaceChildren();
    for (const note of new Set([...(analysis.notes || []), ...(job.storage?.note ? [job.storage.note] : [])])) { const item = document.createElement('li'); item.textContent = String(note); notes.append(item); }
    notes.hidden = !notes.children.length;
    const audio = this.$('#import-preview-audio'); audio.pause(); audio.hidden = isLoop; audio.volume = this.getVolume();
    this.$('#import-loop-controls').hidden = !isLoop;
    if (isLoop) { audio.removeAttribute('src'); audio.load(); this.preview.setJob(job); }
    else { this.preview.setJob(null); audio.src = job.audioUrl; }
    this.status('Preview ready. Check the sound and track information, then add it to your library.', true);
    this.render(); this.$('#import-preview').focus({ preventScroll: true });
    this.$('#import-preview').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
  async submit() {
    if (this.busy) return;
    if (this.job?.status === 'ready') return this.commit();
    if (this.batch) return this.importBatch();
    let options;
    try {
      options = this.options();
      if (this.source === 'youtube') {
        const url = this.$('#import-youtube-url').value.trim();
        if (!url) throw new Error('Paste a YouTube video link first.');
        options = { ...options, source: 'youtube', url, useYouTubeAccount: this.$('#import-youtube-account')?.checked === true };
      } else if (!this.files.length) throw new Error('Choose an audio file first.');
    } catch (error) { this.status(error.message); return; }
    this.discard(this.job); this.job = null;
    const generation = ++this.generation; this.phase = 'preparing'; this.render();
    this.status(this.source === 'youtube' ? 'Reading the video and preparing its audio…' : 'Reading your file and preparing its audio…');
    try {
      const file = this.files[0];
      const result = await this.api(this.source === 'youtube' ? '/api/import-jobs' : '/api/import-jobs/file', this.source === 'youtube' ? jsonOptions('POST', options) : { method: 'POST', headers: { 'Content-Type': 'application/octet-stream', 'X-Filename': encodeURIComponent(file.name), 'X-Track-Metadata': metadataHeader(options) }, body: file });
      if (generation !== this.generation) { this.discard(result.job); return; }
      this.job = result.job;
      while (generation === this.generation) {
        const job = this.job;
        if (!job?.id) throw new Error('The import could not be started. Try again.');
        if (job.status === 'cancelled') throw new Error('This preparation was cancelled. Prepare it again to continue.');
        if (job.status === 'error') throw new Error(job.error || job.message || 'This track could not be prepared.');
        if (job.status === 'ready') { this.showPreview(job); return; }
        this.status(job.message || 'Preparing your audio. Longer recordings may take a little time…');
        await new Promise(resolve => setTimeout(resolve, 1000));
        if (generation !== this.generation) return;
        const next = await this.api(`/api/import-jobs/${encodeURIComponent(job.id)}`);
        if (generation !== this.generation) return;
        this.job = next.job;
      }
    } catch (error) {
      if (generation !== this.generation) return;
      this.phase = 'idle'; this.render(); this.status(error.message);
    }
  }
  async commit() {
    this.pausePreview(); this.phase = 'committing'; this.render(); this.status('Saving to your library…');
    try {
      const result = await this.api(`/api/import-jobs/${encodeURIComponent(this.job.id)}/commit`, jsonOptions('POST', { metadata: this.metadata() }));
      this.job = null; this.phase = 'idle';
      await this.refreshLibrary(); this.dialog.close();
      this.toast(`${result.track?.title || 'Your track'} added to your library.`);
    } catch (error) { this.phase = 'ready'; this.render(); this.status(error.message); }
  }
  async importBatch() {
    this.phase = 'batch'; this.render();
    const files = [...this.files], failed = []; let added = 0;
    const metadata = { kind: this.$('#import-kind').value, metadataMode: this.metadataMode, allowNativeOpus: supportsNativeOpus(), ...(this.metadataMode === 'manual' ? this.metadata() : {}) };
    delete metadata.title;
    for (const [index, file] of files.entries()) {
      this.status(`Adding ${index + 1} of ${files.length}: ${file.name}`);
      try {
        await this.api('/api/import', { method: 'POST', headers: { 'X-Filename': encodeURIComponent(file.name), 'X-Track-Metadata': metadataHeader(metadata), 'Content-Type': 'application/octet-stream' }, body: file }); added++;
      } catch (error) { failed.push({ file, error: error.message }); }
    }
    this.phase = 'idle'; await this.refreshLibrary();
    if (failed.length) {
      this.files = failed.map(({ file }) => file); this.render();
      this.status(`${added ? `${added} ${added === 1 ? 'track added' : 'tracks added'}. ` : ''}${failed.length} ${failed.length === 1 ? 'file couldn’t be added' : 'files couldn’t be added'}: ${failed[0].error}`);
    } else { this.dialog.close(); this.toast(`${added} tracks added to your library.`); }
  }
}
