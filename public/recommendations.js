import { youtubeUrl } from './playlist-import.js';
import { supportsNativeOpus } from './audio-storage.js';
import { SpeedControl } from './playback-controls.js';
import { MEDIA_EFFECTS_UNSUPPORTED, NATIVE_MEDIA_REVERB_MESSAGE } from './audio-effects.js';

/** Recommendation audio is acquired only by listen(). Library playback remains separate. */
const terminal = new Set(['limbo', 'rejected', 'error']);
const escapeText = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const json = body => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const newId = () => globalThis.crypto.randomUUID();
const playbackRate = value => { const rate = Number(value); return Number.isFinite(rate) ? Math.max(.5, Math.min(2, rate)) : 1; };
const reverbAmount = value => { const amount = Number(value); return Number.isFinite(amount) ? Math.max(0, Math.min(1, amount)) : 0; };
const effectsMessage = error => error?.code === MEDIA_EFFECTS_UNSUPPORTED ? NATIVE_MEDIA_REVERB_MESSAGE : 'Reverb could not start. Playback can continue without the effect.';

/** Credits media seconds actually advanced while playing; pauses and buffer waits add nothing. */
export class ListenTracker {
  constructor(now = () => performance.now(), getPlayed = () => null) { this.now = now; this.getPlayed = getPlayed; this.reset(); }
  reset(rate = 1) { this.seconds = 0; this.position = 0; this.at = this.now(); this.playing = false; this.rate = playbackRate(rate); this.peakRate = this.rate; this.seeked = false; this.ended = false; }
  sample(position) {
    const at = this.now(), next = Math.max(0, Number(position) || 0);
    const delta = next - this.position, available = Math.max(0, (at - this.at) / 1000) * this.rate;
    if (this.playing && delta > 0) {
      // A large discontinuity without a seeking event is still not a full listen.
      if (delta > available + 1) this.seeked = true;
      this.seconds += Math.min(delta, available);
    }
    this.position = next; this.at = at;
    // Native media and JavaScript clocks do not advance in lockstep. In
    // particular, pause/playing events can expose a late media position. The
    // browser's played ranges retain that actual audio coverage without adding
    // paused or buffering wall time. Keep the sampled fallback for other media.
    try {
      const played = this.getPlayed(); let seconds = 0;
      if (!played || !Number.isInteger(played.length) || played.length < 0) return;
      for (let i = 0; i < played.length; i++) {
        const start = played.start(i), end = played.end(i);
        if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end < start) return;
        seconds += Math.max(0, Math.min(next, end) - start);
      }
      this.seconds = Math.max(this.seconds, seconds);
    } catch { /* Sampling still works when played ranges are unavailable. */ }
  }
  setPlaying(playing, position, rate = this.rate) { this.sample(position); this.playing = playing; this.rate = Math.max(.25, Math.min(4, Number(rate) || 1)); this.peakRate = Math.max(this.peakRate, this.rate); }
  seek(position) { this.sample(position); this.seeked = true; }
  snapshot(position) { this.sample(position); return { position: this.position, listenedSeconds: this.seconds, seeked: this.seeked, playbackRate: this.peakRate, peakRate: this.peakRate }; }
  completed(duration) { return !this.seeked && Number.isFinite(duration) && duration > 0 && this.seconds >= duration * .95; }
}

export class RecommendationPlayer {
  constructor({ audio, api, audioRoute = null, reverb = 0, onChange = () => {}, onLibraryChange = () => {}, onPlaybackRateChange = () => {}, beforePlay = () => {}, playbackRate: rate = 1, pitchChanges = true, now, id = newId, storage = null, schedule = (...args) => setTimeout(...args), cancel = timer => clearTimeout(timer) }) {
    Object.assign(this, { audio, api, onChange, onLibraryChange, onPlaybackRateChange, beforePlay, id, storage });
    this.schedule = (...args) => schedule(...args); this.cancel = timer => cancel(timer);
    this.tracker = new ListenTracker(now, () => audio.played); this.session = null; this.busy = false; this.error = ''; this.generation = 0;
    this.queue = Promise.resolve(); this.pendingAction = null; this.pollTimer = null; this.progressTimer = null; this.closed = false; this.playIntent = 0; this.active = false; this.retainOnly = false;
    this.libraryIds = new Set(); this.removedTrackIds = new Set();
    this.audioRoute = audioRoute; this.reverb = reverbAmount(reverb); this.effectsError = ''; this.effectsIntent = 0;
    if (this.audioRoute) void this.audioRoute.setReverb(this.reverb);
    this.audio.preload = 'none'; this.audio.loop = false;
    this.playbackRate = playbackRate(rate); this.pitchChanges = Boolean(pitchChanges); this.tracker.reset(this.playbackRate); this.applyPlaybackSettings();
    this.handlers = {
      play: () => { if (this.session && !terminal.has(this.session.status)) this.beforePlay(); },
      playing: () => { this.tracker.setPlaying(true, audio.currentTime, audio.playbackRate); this.emit(); },
      pause: () => { this.tracker.setPlaying(false, audio.currentTime); this.emit(); },
      waiting: () => this.tracker.setPlaying(false, audio.currentTime),
      timeupdate: () => this.tracker.sample(audio.currentTime),
      ratechange: () => {
        const rate = playbackRate(audio.playbackRate), changed = rate !== this.playbackRate;
        this.setPlaybackRate(rate);
        if (changed) this.onPlaybackRateChange(rate);
      },
      loadedmetadata: () => this.applyPlaybackSettings(),
      seeking: () => this.tracker.seek(audio.currentTime),
      ended: () => { if (this.tracker.ended) return; this.tracker.sample(audio.currentTime); this.tracker.ended = true; this.tracker.playing = false; void this.finish(this.tracker.completed(Number(audio.duration) || Number(this.session?.track?.duration)) ? 'complete' : 'skip'); },
      error: () => { if (!this.session || terminal.has(this.session.status)) return; this.error = 'This recommendation could not play. Skip to release its temporary audio, or try Play again.'; this.emit(); },
    };
    for (const [name, handler] of Object.entries(this.handlers)) audio.addEventListener(name, handler);
  }
  emit() { this.onChange(this); }
  applyPlaybackSettings() {
    // Account for the interval at its old rate before mutating the media element.
    this.tracker.setPlaying(this.tracker.playing, this.audio.currentTime, this.playbackRate);
    if (this.audio.defaultPlaybackRate !== this.playbackRate) this.audio.defaultPlaybackRate = this.playbackRate;
    if (this.audio.playbackRate !== this.playbackRate) this.audio.playbackRate = this.playbackRate;
    for (const name of ['preservesPitch', 'webkitPreservesPitch', 'mozPreservesPitch']) {
      if (name in this.audio) { try { this.audio[name] = !this.pitchChanges; } catch {} }
    }
  }
  setPlaybackRate(value) { this.playbackRate = playbackRate(value); this.applyPlaybackSettings(); this.emit(); }
  setPitchChanges(value) { this.pitchChanges = Boolean(value); this.applyPlaybackSettings(); this.emit(); }
  async setReverb(value) {
    const intent = ++this.effectsIntent;
    this.reverb = reverbAmount(value); this.effectsError = '';
    try {
      const result = this.audioRoute ? await this.audioRoute.setReverb(this.reverb) : true;
      if (intent !== this.effectsIntent || this.closed) return;
      if (result === false) this.effectsError = effectsMessage(this.audioRoute?.error);
    } catch { if (intent !== this.effectsIntent || this.closed) return; this.effectsError = 'Reverb could not start. Playback can continue without the effect.'; }
    if (!this.closed) this.emit();
  }
  setVolume(value) {
    const volume = Number(value); if (!Number.isFinite(volume)) return;
    if (this.audioRoute) this.audioRoute.setVolume(volume);
    else this.audio.volume = Math.max(0, Math.min(1, volume));
  }
  async playAudio(generation = this.generation, intent = this.playIntent) {
    const prepared = this.audioRoute ? await this.audioRoute.prepare() : true;
    if (generation !== this.generation || intent !== this.playIntent || this.closed) return false;
    if (prepared === false && this.reverb) this.effectsError = effectsMessage(this.audioRoute?.error);
    await this.audio.play(); return true;
  }
  async resume() {
    if (this.closed || this.busy || this.pendingAction || !['ready', 'retained'].includes(this.session?.status)) return;
    const generation = this.generation, intent = this.playIntent;
    this.beforePlay();
    try { if (!await this.playAudio(generation, intent)) return; this.error = ''; }
    catch { if (generation !== this.generation || intent !== this.playIntent || this.closed) return; this.error = 'Ready to listen. Press Play below to start.'; }
    if (!this.closed) this.emit();
  }
  remember(id) { try { if (id) this.storage?.setItem('stillroom:recommendation-session', id); else this.storage?.removeItem('stillroom:recommendation-session'); } catch {} }
  async recover() {
    let previous; try { previous = this.storage?.getItem('stillroom:recommendation-session'); } catch {}
    if (!previous) return;
    // Reloads never infer a completion. The server also expires abandoned sessions.
    try { await this.api(`/api/recommendation-sessions/${encodeURIComponent(previous)}/events`, json({ eventId: this.id(), type: 'skip', position: 0, listenedSeconds: 0, seeked: false })); this.remember(null); }
    catch { this.error = 'The previous listening session could not be closed yet. Its temporary audio will be cleaned up automatically.'; this.emit(); }
  }
  stopAudio() { this.tracker.setPlaying(false, this.audio.currentTime); this.audio.pause(); this.audio.removeAttribute('src'); this.audio.load(); this.cancel(this.progressTimer); }
  setLibrary(tracks) {
    const ids = new Set(tracks.map(track => track.id)); let removed = false;
    for (const id of this.libraryIds) if (!ids.has(id)) { this.removedTrackIds.add(id); removed = true; }
    for (const id of ids) this.removedTrackIds.delete(id);
    this.libraryIds = ids;
    const trackId = this.session?.status === 'retained' ? this.session.track?.id : null;
    if (trackId && !ids.has(trackId)) this.removedTrackIds.add(trackId);
    return { removed, cleared: this.clearDeletedTrack() };
  }
  clearDeletedTrack() {
    const previous = this.session;
    if (previous?.status !== 'retained' || !this.removedTrackIds.has(previous.track?.id)) return false;
    const generation = ++this.generation; ++this.playIntent;
    this.cancel(this.pollTimer); this.cancel(this.progressTimer);
    this.busy = false; this.active = false; this.retainOnly = false; this.pendingAction = null; this.error = '';
    this.session = { ...previous, status: 'limbo', track: null, completedListens: 0,
      message: 'Deleted from your library. Listen again when you want this song.',
      ...(previous.candidate ? { candidate: { ...previous.candidate, state: 'limbo', ownedTrackId: null, completedListens: 0 } } : {}) };
    this.stopAudio(); this.remember(null); this.emit();
    // Fetch current metadata without rating, reacquiring, or replaying the song.
    void this.queue.then(() => this.api(`/api/recommendation-sessions/${encodeURIComponent(previous.id)}`)).then(({ session }) => {
      if (generation !== this.generation || this.closed || session?.id !== this.session?.id || !terminal.has(session.status)) return;
      this.session = session; this.emit();
    }).catch(() => {});
    return true;
  }
  async listen(candidateId, { retainOnly = false } = {}) {
    if (this.busy || this.closed || this.pendingAction) return;
    if (this.active && this.session && !terminal.has(this.session.status)) {
      await this.finish('skip');
      if (this.pendingAction) return;
    }
    const generation = ++this.generation;
    this.busy = true; this.active = !retainOnly; this.retainOnly = retainOnly; this.error = ''; this.session = null; this.pendingAction = null; this.stopAudio(); this.tracker.reset(this.playbackRate); this.emit();
    if (!retainOnly) this.beforePlay();
    try {
      const { session } = await this.api(`/api/recommendations/${encodeURIComponent(candidateId)}/listen`, json({ requestId: this.id(), allowNativeOpus: supportsNativeOpus(() => this.audio) }));
      if (generation !== this.generation || this.closed) { void this.closeStale(session); return; }
      this.session = session; this.remember(retainOnly ? null : session.id);
      if (session.status === 'preparing') this.poll(generation); else await this.ready(session, generation);
    } catch (error) { if (generation === this.generation) { this.active = false; this.error = error.message; } }
    finally { if (generation === this.generation) { this.busy = false; this.emit(); if (!this.active && !this.retainOnly && ['preparing', 'ready'].includes(this.session?.status)) void this.finish('skip'); } }
  }
  async closeStale(session) {
    if (!session?.id) return;
    try { await this.api(`/api/recommendation-sessions/${encodeURIComponent(session.id)}/events`, { ...json({ eventId: this.id(), type: 'skip', position: 0, listenedSeconds: 0, seeked: false }), keepalive: true }); } catch {}
  }
  poll(generation) {
    this.cancel(this.pollTimer);
    this.pollTimer = this.schedule(async () => {
      if (generation !== this.generation || this.closed) return;
      try {
        const { session } = await this.api(`/api/recommendation-sessions/${encodeURIComponent(this.session.id)}`);
        if (generation !== this.generation || this.closed) return;
        this.session = session;
        if (session.status === 'preparing') this.poll(generation); else await this.ready(session, generation);
      } catch (error) { if (generation === this.generation) { this.error = error.message; this.poll(generation); } }
      this.emit();
    }, 1200);
  }
  async ready(session, generation) {
    if (this.clearDeletedTrack()) return;
    if (terminal.has(session.status)) { this.active = false; this.error = session.status === 'error' ? session.message || 'Audio could not be prepared. Try listening again.' : ''; this.remember(null); return; }
    if (session.status === 'retained' && this.retainOnly) { this.active = false; this.remember(null); this.onLibraryChange(); return; }
    if (!session.track?.audioUrl) throw new Error('The audio is not available yet. Try listening again.');
    this.audio.src = session.track.audioUrl; this.audio.loop = false; this.applyPlaybackSettings();
    this.beforePlay();
    try { await this.playAudio(generation); } catch { if (generation === this.generation) this.error = 'Ready to listen. Press Play below to start.'; }
    if (generation !== this.generation || this.closed) return;
    this.scheduleProgress(generation);
  }
  scheduleProgress(generation) {
    this.cancel(this.progressTimer);
    this.progressTimer = this.schedule(() => {
      if (generation !== this.generation || this.closed || !this.session || terminal.has(this.session.status)) return;
      if (this.session.status === 'ready' && !this.busy) void this.progress();
      this.scheduleProgress(generation);
    }, 5000);
  }
  async progress() {
    if (!this.session || this.progressPending || this.busy) return;
    this.progressPending = true;
    const sessionId = this.session.id, payload = { eventId: this.id(), type: 'progress', ...this.tracker.snapshot(this.audio.currentTime) };
    const request = this.queue.then(() => this.api(`/api/recommendation-sessions/${encodeURIComponent(sessionId)}/events`, json(payload)));
    this.queue = request.catch(() => {});
    try { const { session } = await request; if (this.session?.id === sessionId && !this.busy && terminal.has(session.status)) { this.session = session; this.stopAudio(); this.remember(null); this.emit(); } } catch { /* A terminal event carries the cumulative progress again. */ }
    finally { this.progressPending = false; }
  }
  async finish(type) {
    if (!this.session || this.busy || (terminal.has(this.session.status) && !(type === 'down' || (type === 'up' && this.session.status === 'limbo')))) return;
    if (this.session.status === 'retained' && !['down', 'skip'].includes(type)) return;
    const sessionId = this.session.id, generation = ++this.generation, playIntent = this.playIntent;
    const payload = this.pendingAction || { eventId: this.id(), type, ...this.tracker.snapshot(this.audio.currentTime) };
    this.pendingAction = payload; this.busy = true; this.error = ''; this.cancel(this.pollTimer);
    const wasPlaying = !this.audio.paused, position = this.audio.currentTime;
    if (payload.type === 'up' && this.session.status === 'limbo') { this.retainOnly = true; this.tracker.reset(this.playbackRate); }
    this.audio.pause();
    if (payload.type !== 'up') { this.active = false; this.stopAudio(); }
    this.emit();
    const request = this.queue.then(() => this.api(`/api/recommendation-sessions/${encodeURIComponent(sessionId)}/events`, json(payload)));
    this.queue = request.catch(() => {});
    try {
      const { session } = await request;
      if (generation !== this.generation || this.closed) return;
      this.session = session; this.pendingAction = null;
      if (this.clearDeletedTrack()) return;
      if (session.status !== 'ready') this.remember(null);
      if (session.status === 'preparing') this.poll(generation);
      if (payload.type === 'up' && session.status === 'retained' && !this.retainOnly) {
        if (session.track?.audioUrl && this.audio.getAttribute('src') !== session.track.audioUrl) { this.audio.src = session.track.audioUrl; this.audio.currentTime = position; this.applyPlaybackSettings(); }
        if (wasPlaying && playIntent === this.playIntent) { this.beforePlay(); try { await this.playAudio(generation, playIntent); } catch {} }
      }
      if (['retained', 'rejected'].includes(session.status)) this.onLibraryChange();
    } catch (error) { if (generation === this.generation) this.error = `${error.message} Your action has not been confirmed. Use Retry to send it again safely.`; }
    finally { if (generation === this.generation) { this.busy = false; this.emit(); } }
  }
  interrupt() {
    ++this.playIntent;
    if (!this.active && !this.busy) return;
    this.active = false;
    // Cancels a pending acquisition response too; it will be closed when it arrives.
    if (this.busy && !this.session) { ++this.generation; this.busy = false; this.stopAudio(); this.emit(); return; }
    this.audio.pause();
    if (!this.busy && this.session && !terminal.has(this.session.status)) void this.finish('skip');
  }
  pagehide() {
    ++this.generation; this.closed = true; this.cancel(this.pollTimer); this.cancel(this.progressTimer);
    const session = this.session, payload = this.pendingAction || { eventId: this.id(), type: 'skip', ...this.tracker.snapshot(this.audio.currentTime) };
    this.stopAudio();
    if (session && ((!terminal.has(session.status) && !this.retainOnly) || this.pendingAction)) void this.api(`/api/recommendation-sessions/${encodeURIComponent(session.id)}/events`, { ...json(payload), keepalive: true }).catch(() => {});
  }
  destroy() { this.pagehide(); for (const [name, handler] of Object.entries(this.handlers)) this.audio.removeEventListener(name, handler); this.audioRoute?.destroy(); }
}

export class RecommendationPanel {
  constructor({ root, api, pauseMain, getVolume, refreshLibrary, playbackRate = 1, pitchChanges = true, reverb = 0, createMediaRoute = null, speedControlFactory = options => new SpeedControl(options), onPlaybackRateChange = null, onPitchChangesChange = null, storage = null, schedule = (...args) => setTimeout(...args), cancel = timer => clearTimeout(timer) }) {
    Object.assign(this, { root, api }); this.schedule = (...args) => schedule(...args); this.cancel = timer => cancel(timer); this.closed = false; this.snapshot = null; this.loading = false; this.error = ''; this.refreshTimer = null; this.loadGeneration = 0; this.lastTerminal = '';
    this.$ = selector => root.querySelector(selector);
    this.onPlaybackRateChange = onPlaybackRateChange || (value => this.setPlaybackSettings({ playbackRate: value }));
    this.onPitchChangesChange = onPitchChangesChange || (value => this.setPlaybackSettings({ pitchChanges: value }));
    const audio = this.$('audio');
    const audioRoute = createMediaRoute?.(audio, error => {
      if (!this.closed && this.player?.reverb) { this.player.effectsError = effectsMessage(error); this.renderPlayer(); }
    }, () => {
      if (!this.closed && this.player) { this.player.error = 'Ready to listen. Press Play below to start.'; this.renderPlayer(); }
    });
    this.player = new RecommendationPlayer({ audio, api, storage, playbackRate, pitchChanges, reverb, audioRoute, onPlaybackRateChange: value => this.onPlaybackRateChange(value), beforePlay: () => { pauseMain(); this.player.setVolume(getVolume()); }, onChange: () => this.renderPlayer(), onLibraryChange: () => { void refreshLibrary({ quiet: true }); } });
    this.speedControl = speedControlFactory({ root: this.$('#recommendation-speed-control'), playbackRate, pitchChanges, onPlaybackRateChange: value => this.onPlaybackRateChange(value), onPitchChangesChange: value => this.onPitchChangesChange(value) });
    root.addEventListener('click', event => {
      const button = event.target.closest('button'); if (!button) return;
      if (button.dataset.listen) void this.player.listen(button.dataset.listen, { retainOnly: button.dataset.keep === 'true' });
      if (button.dataset.feedback) void this.player.finish(button.dataset.feedback);
      if (button.id === 'recommendation-refresh') void this.load(true);
      if (button.id === 'recommendation-retry') { if (this.player.pendingAction) void this.player.finish(this.player.pendingAction.type); else void this.load(); }
    });
  }
  async start() { await this.player.recover(); await this.load(); }
  async load(refresh = false) {
    if (this.closed || (refresh && this.loading)) return;
    this.cancel(this.refreshTimer); const generation = ++this.loadGeneration;
    this.loading = true; this.error = ''; this.render();
    try { const snapshot = await this.api('/api/recommendations' + (refresh ? '/refresh' : ''), refresh ? json(this.$('#recommendation-seed').value ? { seedId: this.$('#recommendation-seed').value } : {}) : {}); if (generation !== this.loadGeneration) return; this.snapshot = snapshot; }
    catch (error) { if (generation === this.loadGeneration) this.error = error.message; }
    finally { if (generation === this.loadGeneration) { this.loading = false; this.render(); if (['working', 'running', 'searching', 'refreshing', 'pending', 'queued'].includes(this.snapshot?.refresh?.status)) this.refreshTimer = this.schedule(() => this.load(), 2000); } }
  }
  render() {
    const snapshot = this.snapshot, working = this.loading || ['working', 'running', 'searching', 'refreshing', 'pending', 'queued'].includes(snapshot?.refresh?.status);
    this.$('#recommendation-refresh').disabled = working;
    this.$('#recommendation-refresh').textContent = working ? 'Finding suggestions…' : 'Find recommendations';
    this.$('#recommendation-status').textContent = this.error || snapshot?.refresh?.message || (snapshot ? `${snapshot.candidates?.length || 0} suggestions · Audio downloads only when you choose Listen.` : 'Loading suggestions…');
    const candidates = snapshot?.candidates || [], limbo = snapshot?.limbo || [];
    this.$('#recommendation-list').innerHTML = candidates.map(candidate => this.card(candidate)).join('');
    this.$('#recommendation-empty').hidden = candidates.length > 0 || working;
    this.$('#recommendation-limbo').hidden = limbo.length === 0;
    this.$('#recommendation-limbo-count').textContent = limbo.length;
    this.$('#recommendation-limbo-list').innerHTML = limbo.map(candidate => this.card(candidate, true)).join('');
    this.renderPlayer();
  }
  card(candidate, limbo = false) {
    const title = candidate.title || candidate.track?.title || 'Untitled track', artist = candidate.artist || candidate.track?.artist || 'Unknown artist';
    const reasons = (candidate.reasons || []).map(reason => typeof reason === 'string' ? reason : reason.description || reason.label || '').filter(Boolean);
    const count = Number(candidate.completedListens) || 0, keeping = candidate.rating === 'up';
    const listenLabel = keeping ? 'Finish keeping' : limbo ? 'Listen again' : 'Listen';
    const source = youtubeUrl(candidate.sourceUrl);
    return `<article class="recommendation-card" data-recommendation-id="${escapeText(candidate.id)}"><div class="recommendation-copy"><h3>${escapeText(title)}</h3><p class="recommendation-artist">${escapeText(artist)}</p><p class="recommendation-reason">${escapeText(reasons.join(' · ') || 'Based on your collection')}${candidate.message ? `<br>${escapeText(candidate.message)}` : ''}</p><span class="recommendation-meta">${limbo ? `In limbo · ${count} of 3 full listens · No audio stored` : `Suggested${count ? ` · ${count} of 3 full listens` : ''} · No audio stored`}</span>${source ? `<a class="recommendation-source" href="${escapeText(source)}" target="_blank" rel="noopener noreferrer" aria-label="Inspect the YouTube source for ${escapeText(title)}">YouTube source ↗</a>` : ''}</div><button class="button button-quiet" data-listen="${escapeText(candidate.id)}" data-keep="${keeping}" aria-label="${escapeText(listenLabel)} ${escapeText(title)}">${listenLabel}</button></article>`;
  }
  renderPlayer() {
    const player = this.player; if (!player) return;
    this.speedControl?.setPlaybackSettings({ playbackRate: player.playbackRate, pitchChanges: player.pitchChanges });
    const session = player.session;
    this.$('#recommendation-now').hidden = !session && !player.busy && !player.error;
    this.$('#recommendation-now-title').textContent = session?.track?.title || session?.candidate?.title || 'Preparing your recommendation…';
    this.$('#recommendation-now-artist').textContent = session?.track?.artist || session?.candidate?.artist || '';
    const messages = { preparing: 'Preparing audio for this listen…', ready: `Listening temporarily · ${session?.completedListens || 0} of 3 full listens`, retained: 'Kept in your library.', limbo: `Back in limbo · ${session?.completedListens || 0} of 3 full listens. Temporary audio released; its details stay for another day.`, rejected: 'Removed from suggestions. Your feedback will shape future recommendations.', error: 'Audio could not be prepared. You can try listening again.' };
    this.$('#recommendation-now-status').textContent = player.error || player.effectsError || (player.busy ? (session ? 'Saving your choice…' : 'Preparing audio for this listen…') : session?.message || messages[session?.status] || '');
    this.$('audio').hidden = !session || !['ready', 'retained'].includes(session.status) || !!player.pendingAction || !player.audio.getAttribute('src');
    this.$('#recommendation-feedback').hidden = !session || !['ready', 'preparing', 'retained', 'limbo'].includes(session.status);
    this.root.querySelectorAll('[data-feedback]').forEach(button => { button.disabled = (button.dataset.feedback === 'skip' && (!player.active || player.retainOnly)) || player.busy || !!player.pendingAction || (button.dataset.feedback === 'up' && !['ready', 'limbo'].includes(session?.status)); });
    this.$('#recommendation-retry').hidden = !(player.pendingAction || this.error);
    this.root.querySelectorAll('[data-listen]').forEach(button => { button.disabled = player.busy || session?.status === 'preparing' || !!player.pendingAction; });
    this.root.querySelectorAll('[data-recommendation-id]').forEach(card => {
      const meta = card.querySelector('.recommendation-meta');
      if (!meta.dataset.idleLabel) meta.dataset.idleLabel = meta.textContent;
      const current = card.dataset.recommendationId === session?.candidateId;
      meta.textContent = current && session.status === 'preparing' ? (player.retainOnly ? 'Preparing audio to keep' : 'Preparing temporary audio…') : current && session.status === 'ready' ? 'Temporary audio for this listen' : current && session.status === 'retained' ? 'Kept in your library' : meta.dataset.idleLabel;
    });
    const terminalKey = session && ['retained', 'limbo', 'rejected', 'error'].includes(session.status) ? `${session.id}:${session.status}` : '';
    if (terminalKey && terminalKey !== this.lastTerminal) { this.lastTerminal = terminalKey; void this.load(); }
  }
  setPlaybackSettings({ playbackRate = this.player.playbackRate, pitchChanges = this.player.pitchChanges, reverb = this.player.reverb }) {
    if (playbackRate !== this.player.playbackRate) this.player.setPlaybackRate(playbackRate);
    if (pitchChanges !== this.player.pitchChanges) this.player.setPitchChanges(pitchChanges);
    if (reverb !== this.player.reverb) void this.player.setReverb(reverb);
    this.renderPlayer();
  }
  setLibrary(tracks) {
    const select = this.$('#recommendation-seed'), selected = select.value;
    select.innerHTML = '<option value="">All your library</option>' + tracks.map(track => `<option value="${escapeText(track.id)}">${escapeText(track.title)} — ${escapeText(track.artist || 'Unknown artist')}</option>`).join('');
    if (tracks.some(track => track.id === selected)) select.value = selected;
    const { removed, cleared } = this.player.setLibrary(tracks);
    if (removed && !cleared) void this.load();
  }
  setVisible(visible) { this.root.hidden = !visible; if (!visible) this.player.interrupt(); }
  interrupt() { this.player.interrupt(); }
  pagehide() { this.closed = true; ++this.loadGeneration; this.cancel(this.refreshTimer); this.speedControl?.destroy(); this.player.destroy(); }
}

/** Loading only interrupts when the main player still intends to autoplay. */
export function connectRecommendationPlayback(mainPlayer, recommendations, pausePreview) {
  const listener = event => {
    if (event.detail?.playing || (event.detail?.loading && mainPlayer.autoplayRequested)) { pausePreview(); recommendations.interrupt(); }
  };
  mainPlayer.addEventListener('statechange', listener);
  return () => mainPlayer.removeEventListener('statechange', listener);
}
