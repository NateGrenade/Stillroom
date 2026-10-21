import { RecommendationPanel, connectRecommendationPlayback } from './recommendations.js';
import { MusicPlayer } from './player.js';
import { ImportDialog } from './import-dialog.js';
import { PlaylistImportDialog } from './playlist-import.js';
import { SettingsDialog } from './settings.js';
import { EditInfoDialog } from './edit-info.js';
import { ShareSongDialog } from './share-song.js';
import { PocketExportDialog } from './pocket-export.js';
import { SpeedControl, AdvancedPlaybackOptions, RecommendationDisclosure, normalizeReverb } from './playback-controls.js';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const paths = {
  library: '<rect x="3" y="4" width="4" height="16" rx="1"/><rect x="10" y="4" width="4" height="16" rx="1"/><path d="m17 5 3-1 4 15-3 1z"/>',
  clock: '<circle cx="12" cy="12" r="8"/><path d="M12 7v5l3 2"/>',
  repeat: '<path d="m17 2 4 4-4 4M3 11V8a2 2 0 0 1 2-2h16M7 22l-4-4 4-4m14-1v3a2 2 0 0 1-2 2H3"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/>',
  play: '<path d="m9 5 11 7-11 7z" fill="currentColor" stroke="none"/>',
  pause: '<path d="M7 5h4v14H7zm7 0h4v14h-4z" fill="currentColor" stroke="none"/>',
  previous: '<path d="M5 5v14m14-14L8 12l11 7z" fill="currentColor"/>',
  next: '<path d="M19 5v14M5 5l11 7-11 7z" fill="currentColor"/>',
  shuffle: '<path d="M3 5h3c5 0 7 14 12 14h3m-4-4 4 4-4 4M3 19h3c2.4 0 4.1-3.2 5.8-6.5M14 7c1.2-1.2 2.4-2 4-2h3m-4-4 4 4-4 4"/>',
  volume: '<path d="m11 4-6 5H2v6h3l6 5zM15 8a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14"/>',
  muted: '<path d="m11 4-6 5H2v6h3l6 5zM16 9l6 6m0-6-6 6"/>',
  edit: '<path d="m14 5 5 5M4 20l1-6L16 3a2 2 0 0 1 3 0l2 2a2 2 0 0 1 0 3L10 19z"/>',
  trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
  more: '<circle cx="5" cy="12" r="1" fill="currentColor"/><circle cx="12" cy="12" r="1" fill="currentColor"/><circle cx="19" cy="12" r="1" fill="currentColor"/>',
  music: '<path d="M9 18V5l12-2v13M9 8l12-2"/><ellipse cx="5.5" cy="18.5" rx="3.5" ry="2.5"/><ellipse cx="17.5" cy="16.5" rx="3.5" ry="2.5"/>',
  playlist: '<path d="M3 5h14M3 10h14M3 15h8m7-3v9m-4-4h8"/>',
  shield: '<path d="m12 2 8 3v6c0 5-8 10-8 10S4 16 4 11V5zM8 11l3 3 5-5"/>',
  upload: '<path d="M12 16V3m-5 5 5-5 5 5M4 15v5h16v-5"/>',
  share: '<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="m8.6 10.5 6.8-4m-6.8 7 6.8 4"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  chevron: '<path d="m6 9 6 6 6-6"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  alert: '<path d="m12 3 10 18H2zM12 9v5m0 3v.01"/>',
};

function icon(name) {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.music}</svg>`;
}
function hydrateIcons(root = document) {
  $$('[data-icon]', root).forEach((node) => { node.innerHTML = icon(node.dataset.icon); });
}
function escapeText(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
}
function formatTime(value) {
  const seconds = Math.max(0, Math.floor(Number(value) || 0));
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, '0')}`;
}
function trackTitle(track) { return track.title || 'Untitled track'; }
function trackArtist(track) { return track.artist || 'Unknown artist'; }
function readPreference(key, fallback) {
  try { return JSON.parse(localStorage.getItem(`stillroom:${key}`)) ?? fallback; } catch { return fallback; }
}
function savePreference(key, value) {
  try { localStorage.setItem(`stillroom:${key}`, JSON.stringify(value)); } catch { /* Listening works without local storage. */ }
}

const palettes = [
  ['#685679', '#c9a8bd', '#65546c', '#372d4b'],
  ['#617174', '#d0c0a2', '#4c6668', '#2a414c'],
  ['#866356', '#dbb194', '#805e61', '#443546'],
  ['#626987', '#c0b8d9', '#59607b', '#303748'],
  ['#75755a', '#d5caa0', '#66684e', '#343f3e'],
  ['#6e5167', '#c5a0b4', '#68475d', '#3a2c46'],
  ['#52657f', '#b4c4d2', '#425b72', '#263a50'],
];
function colorsFor(track) {
  let hash = 0;
  for (const character of String(track?.id || 'stillroom')) hash = ((hash << 5) - hash + character.charCodeAt(0)) | 0;
  return palettes[Math.abs(hash) % palettes.length];
}
function artStyle(track) {
  const colors = colorsFor(track);
  return `--art-sky:${colors[0]};--art-glow:${colors[1]};--art-mid:${colors[2]};--art-front:${colors[3]}`;
}
function applyArt(node, track) { node.style.cssText = artStyle(track); }
function artContents() { return '<span class="art-orb"></span><span class="art-ridge ridge-back"></span><span class="art-ridge ridge-front"></span>'; }

const player = new MusicPlayer();
let library = { tracks: [], playlists: [] };
let view = 'library';
let filter = 'all';
let query = '';
let loaded = false;
let loadError = '';
let libraryRequest = 0;
let librarySignature = '';
let featuredTrack = null;
let playback = player.getState();
let lastTrackError = '';
let lastEffectsError = '';
let toastTimeout;
let playlistEditId = null;
let addingTrackId = null;
let pendingTrackForNewPlaylist = null;
let pendingDeleteId = null;
let pendingDeleteKind = 'playlist';
let seekInProgress = false;
let previousVolume = 0.4;
let menuTrackId = null;
let chooserBusy = false;
let playbackRowSignature = '';
let menuTrigger = null;
let recommendations = null;
let pocketExport = null;
let pocketExportReturnTrack = null;

hydrateIcons();
const volume = Number(readPreference('volume', 0.4));
player.setVolume(Number.isFinite(volume) ? Math.min(1, Math.max(0, volume)) : 0.4);
player.setPlaybackRate(readPreference('playbackRate', 1));
player.setPitchChanges(readPreference('pitchChanges', true) !== false);

function setSongPlaybackRate(value) {
  player.setPlaybackRate(value);
  savePreference('playbackRate', player.getState().playbackRate);
}
function setSongPitchChanges(value) {
  player.setPitchChanges(value);
  savePreference('pitchChanges', player.getState().pitchChanges);
}

async function setSongReverb(value) {
  try {
    await player.setReverb(normalizeReverb(value));
    savePreference('reverb', player.getState().reverb);
  } catch (error) {
    advancedOptions.setReverb(player.getState().reverb);
    toast(error.message || 'Reverb is unavailable for this playback.');
  }
}

const playerSpeedControl = new SpeedControl({
  root: $('#player-speed-control'),
  playbackRate: player.getState().playbackRate,
  pitchChanges: player.getState().pitchChanges,
  onPlaybackRateChange: setSongPlaybackRate,
  onPitchChangesChange: setSongPitchChanges,
});
const advancedOptions = new AdvancedPlaybackOptions({
  root: $('#player-advanced'), reverb: readPreference('reverb', 0), onReverbChange: setSongReverb,
});
const recommendationDisclosure = new RecommendationDisclosure({
  root: $('#recommendations'), collapsed: readPreference('recommendationsCollapsed', false) === true,
  onCollapseChange: collapsed => savePreference('recommendationsCollapsed', collapsed),
});
setSongReverb(readPreference('reverb', 0));

async function api(path, options = {}) {
  const response = await fetch(path, options);
  let body;
  try { body = await response.json(); } catch { body = null; }
  if (!response.ok) {
    const error = new Error(body?.error || body?.message || `The request could not be completed (${response.status}).`);
    error.status = response.status;
    throw error;
  }
  return body;
}
function jsonOptions(method, body) {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
}
function toast(message) {
  clearTimeout(toastTimeout);
  $('#toast').textContent = message;
  $('#toast').hidden = false;
  toastTimeout = setTimeout(() => { $('#toast').hidden = true; }, 4200);
}
function showStatus(node, message, success = false) {
  node.textContent = message;
  node.classList.toggle('is-success', success);
  node.hidden = !message;
}
function currentPlaylist() { return library.playlists.find((playlist) => playlist.id === view); }
function tracksForView() {
  if (view === 'loops') return library.tracks.filter((track) => track.kind === 'loop');
  if (view === 'recent') return [...library.tracks].sort((a, b) => new Date(b.addedAt || 0) - new Date(a.addedAt || 0));
  const playlist = currentPlaylist();
  if (playlist) {
    const byId = new Map(library.tracks.map((track) => [track.id, track]));
    return playlist.trackIds.map((id) => byId.get(id)).filter(Boolean);
  }
  return library.tracks;
}
function visibleTracks() {
  const search = query.trim().toLocaleLowerCase();
  return tracksForView().filter((track) => {
    if (filter !== 'all' && track.kind !== filter) return false;
    if (!search) return true;
    return [track.title, track.artist, track.album, ...(track.tags || [])].some((value) => String(value || '').toLocaleLowerCase().includes(search));
  });
}
function visibleIds() { return visibleTracks().map((track) => track.id); }
function setView(nextView) {
  view = nextView;
  filter = 'all';
  closeMenu();
  renderNavigation();
  renderCollection();
}
async function refreshLibrary({ quiet = false } = {}) {
  const request = ++libraryRequest;
  try {
    const result = await api('/api/library');
    if (request !== libraryRequest) return;
    const nextLibrary = { tracks: result.tracks || [], playlists: result.playlists || [] };
    const nextSignature = JSON.stringify(nextLibrary);
    if (loaded && !loadError && nextSignature === librarySignature) return;
    library = nextLibrary;
    librarySignature = nextSignature;
    loaded = true;
    loadError = '';
    if (!['library', 'recent', 'loops'].includes(view) && !currentPlaylist()) view = 'library';
    player.setCatalog(library.tracks);
    recommendations?.setLibrary(library.tracks);
    renderNavigation();
    renderCollection();
    updatePlayer(player.getState());
    if ($('#add-playlist-dialog').open && !chooserBusy) renderPlaylistChoices();
  } catch (error) {
    if (request !== libraryRequest) return;
    if (!loaded || !quiet) {
      loadError = error.message || 'Your music collection could not be loaded.';
      renderCollection();
    }
  }
}

function renderNavigation() {
  $('#library-count').textContent = library.tracks.length;
  $('#loop-count').textContent = library.tracks.filter((track) => track.kind === 'loop').length;
  $$('.primary-nav .nav-item').forEach((button) => {
    const selected = button.dataset.view === view;
    button.classList.toggle('is-active', selected);
    if (selected) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
  });
  $('#playlist-nav').innerHTML = library.playlists.map((playlist) => `<button class="nav-item${view === playlist.id ? ' is-active' : ''}" data-playlist-id="${escapeText(playlist.id)}" title="${escapeText(playlist.name)}"${view === playlist.id ? ' aria-current="page"' : ''}><span class="playlist-dot">${icon('playlist')}</span><span class="playlist-name">${escapeText(playlist.name)}</span></button>`).join('');
  $('#playlist-empty').hidden = library.playlists.length > 0;
}
function renderCollection() {
  recommendations?.setVisible(view === 'library');
  const playlist = currentPlaylist();
  const tracks = visibleTracks();
  const total = tracksForView().length;
  const isSearching = Boolean(query.trim());
  const name = playlist?.name || ({ library: 'Your library', recent: 'Recently added', loops: 'Loop collection' }[view] || 'Your library');
  $('#view-title').textContent = name;
  const period = document.createElement('span');
  period.className = 'title-period';
  period.textContent = '.';
  $('#view-title').append(period);
  $('#view-eyebrow').textContent = playlist ? 'YOUR PLAYLIST' : view === 'loops' ? 'A LITTLE LONGER' : view === 'recent' ? 'NEW TO YOUR ROOM' : 'YOUR COLLECTION';
  $('#view-description').textContent = playlist ? `${total} ${total === 1 ? 'track' : 'tracks'} gathered for a moment of your own.` : view === 'loops' ? 'Prepared to flow from the end right back to the beginning.' : view === 'recent' ? 'The latest sounds to find a place in your collection.' : 'Something for the moment. Something to come back to.';
  $('#playlist-actions').hidden = !playlist;
  $('#filter-tabs').hidden = view === 'loops';
  $$('.filter-tab').forEach((button) => {
    button.classList.toggle('is-active', button.dataset.filter === filter);
    button.setAttribute('aria-pressed', String(button.dataset.filter === filter));
  });
  $('#track-heading').textContent = isSearching ? 'Search results' : filter === 'loop' || view === 'loops' ? 'Seamless loops' : filter === 'original' ? 'Original tracks' : playlist ? 'In this playlist' : view === 'recent' ? 'Freshly added' : 'All tracks';
  $('#track-total').textContent = `${tracks.length} ${tracks.length === 1 ? 'track' : 'tracks'}`;
  $('#shuffle-view').disabled = tracks.length === 0;
  $('#export-pocket').disabled = library.tracks.length === 0 && !pocketExport?.job && !pocketExport?.unknownCreation;
  $('#library-error').hidden = !loadError;
  $('#library-error-text').textContent = loadError ? `We couldn’t open your collection. ${loadError}` : '';
  $('.track-table-header').hidden = tracks.length === 0;
  $('#track-list').innerHTML = tracks.map((track, index) => {
    const title = escapeText(trackTitle(track));
    const id = escapeText(track.id);
    return `<article class="track-row" data-track-id="${id}" aria-label="${title}, ${escapeText(trackArtist(track))}"><span class="track-number" data-number="${index + 1}">${String(index + 1).padStart(2, '0')}</span><div class="track-main"><button class="track-art landscape" style="${artStyle(track)}" data-action="play" aria-label="Play ${title}">${artContents()}<span class="track-play-icon">${icon('play')}</span></button><div class="track-copy"><button class="track-title" data-action="play" title="Play ${title}">${title}</button><span class="track-artist">${escapeText(trackArtist(track))}</span></div></div><span class="track-album" title="${escapeText(track.album || '')}">${escapeText(track.album || '—')}</span><span class="track-kind"><span class="kind-badge${track.kind === 'loop' ? ' is-loop' : ''}">${track.kind === 'loop' ? `${icon('repeat')} Loop` : 'Original'}</span></span><span class="track-duration">${track.duration ? formatTime(track.duration) : '—'}</span><div class="row-actions"><button class="icon-button loop-track" data-action="loop" aria-label="Play and loop ${title}" title="Play on repeat">${icon('repeat')}</button><button class="icon-button track-more" data-action="more" aria-label="More options for ${title}" aria-haspopup="true" aria-expanded="false" title="More options">${icon('more')}</button></div></article>`;
  }).join('');
  updateTrackPlayback(true);
  $('#empty-state').hidden = tracks.length > 0 || Boolean(loadError);
  const emptyTitle = !loaded ? 'Opening your collection…' : isSearching ? 'No tracks found.' : filter !== 'all' ? 'Nothing here just yet.' : playlist ? 'A playlist with possibility.' : view === 'loops' ? 'Let a little music linger.' : 'A room waiting for music.';
  const emptyDescription = !loaded ? 'Your listening room will be ready in a moment.' : isSearching ? 'Try another title, artist, album, or tag.' : playlist ? 'Add a track from your library using its ••• menu.' : filter === 'loop' || view === 'loops' ? 'Prepare a loop from a file or a YouTube link to keep your favorite moments going.' : 'Bring in audio files, a YouTube link, or a YouTube or Spotify playlist. They’ll live right here.';
  $('#empty-title').textContent = emptyTitle;
  $('#empty-description').textContent = emptyDescription;
  $('#empty-action').hidden = !loaded || isSearching;
  $('#empty-action').dataset.action = playlist ? 'library' : 'import';
  $('#empty-action').innerHTML = `${icon(playlist ? 'library' : 'plus')}<span>${playlist ? 'Browse your library' : 'Add your first tracks'}</span>`;
  renderFeature(tracks);
}
function renderFeature(tracks) {
  featuredTrack = tracks.find((track) => track.kind === 'loop') || tracks[0] || null;
  const title = $('#feature-title');
  const description = $('#feature-description');
  const button = $('#feature-play');
  if (featuredTrack) {
    title.textContent = trackTitle(featuredTrack);
    description.textContent = `${trackArtist(featuredTrack)}${featuredTrack.album ? ` · ${featuredTrack.album}` : ''}`;
    $('#feature-eyebrow').textContent = featuredTrack.kind === 'loop' ? 'A MOMENT WORTH KEEPING' : 'SOMETHING TO SETTLE INTO';
    button.innerHTML = `${icon('play')}<span>${featuredTrack.kind === 'loop' ? 'Play on repeat' : 'Start listening'}</span>`;
    button.setAttribute('aria-label', `Play ${trackTitle(featuredTrack)}${featuredTrack.kind === 'loop' ? ' on repeat' : ''}`);
    $('#feature-note').innerHTML = `${icon(featuredTrack.kind === 'loop' ? 'repeat' : 'music')}${featuredTrack.kind === 'loop' ? 'Made to flow' : 'From your collection'}`;
    $('.art-caption b').textContent = featuredTrack.kind === 'loop' ? 'A MOMENT, UNBROKEN' : 'IN GOOD COMPANY';
    applyArt($('#feature-art'), featuredTrack);
  } else {
    title.textContent = 'Let the moment linger.';
    description.textContent = 'Your favorite tracks and seamless loops, together in one quiet corner.';
    $('#feature-eyebrow').textContent = 'SETTLE INTO A SOUND';
    button.innerHTML = `${icon('plus')}<span>Add your music</span>`;
    button.setAttribute('aria-label', 'Add music to your library');
    $('#feature-note').innerHTML = `${icon('repeat')}Find your rhythm`;
    $('.art-caption b').textContent = 'IN GOOD COMPANY';
    applyArt($('#feature-art'), null);
  }
}
function updateTrackPlayback(force = false) {
  const signature = `${playback.track?.id || ''}:${playback.playing}:${playback.repeat}`;
  if (!force && signature === playbackRowSignature) return;
  playbackRowSignature = signature;
  $$('.track-row').forEach((row) => {
    const isCurrent = playback.track?.id === row.dataset.trackId;
    row.classList.toggle('is-current', isCurrent);
    row.classList.toggle('is-playing', isCurrent && playback.playing);
    const number = $('.track-number', row);
    if (isCurrent) number.innerHTML = '<span class="equalizer" aria-hidden="true"><i></i><i></i><i></i></span>';
    else number.textContent = String(number.dataset.number).padStart(2, '0');
    $('.track-play-icon', row).innerHTML = icon(isCurrent && playback.playing ? 'pause' : 'play');
    const title = $('.track-title', row).textContent;
    $('.track-art', row).setAttribute('aria-label', `${isCurrent && playback.playing ? 'Pause' : 'Play'} ${title}`);
    $('.track-title', row).setAttribute('aria-label', `${isCurrent && playback.playing ? 'Pause' : 'Play'} ${title}`);
    $('.loop-track', row).classList.toggle('is-active', isCurrent && playback.repeat === 'one');
  });
}

function playTrack(id, repeatOne = false) {
  if (!id) return;
  const queue = visibleIds();
  const result = player.playTrack(id, queue.includes(id) ? queue : [id, ...queue], { repeatOne });
  if (result?.catch) result.catch((error) => toast(error.message || 'This track could not be played.'));
}
function updatePlayer(state) {
  playback = state;
  playerSpeedControl.setPlaybackSettings(state);
  advancedOptions.setReverb(state.reverb);
  advancedOptions.setEffectsError(state.effectsError);
  recommendations?.setPlaybackSettings({ playbackRate: state.playbackRate, pitchChanges: state.pitchChanges, reverb: state.reverb });
  const track = state.track;
  $('#now-title').textContent = track ? trackTitle(track) : 'A moment of quiet';
  $('#now-artist').textContent = state.error ? 'Unable to play this track' : track ? trackArtist(track) : 'Choose something to play';
  $('#now-art').classList.toggle('has-track', Boolean(track));
  if (track) applyArt($('#now-art'), track);
  $('#now-kind').hidden = !track;
  $('#now-kind').textContent = track?.kind === 'loop' ? 'LOOP' : 'ORIGINAL';
  $('#player-toggle').innerHTML = icon(state.playing ? 'pause' : 'play');
  $('#player-toggle').setAttribute('aria-label', state.loading ? 'Loading audio' : state.playing ? 'Pause' : 'Play');
  $('#player-toggle').title = state.loading ? 'Loading audio' : state.playing ? 'Pause' : 'Play';
  $('#player-toggle').classList.toggle('is-loading', state.loading);
  $('#player-toggle').disabled = !track && library.tracks.length === 0;
  $('#player-prev').disabled = !track;
  $('#player-next').disabled = !track;
  $('#player-shuffle').classList.toggle('is-active', state.shuffle);
  $('#player-shuffle').setAttribute('aria-pressed', String(state.shuffle));
  $('#player-shuffle').setAttribute('aria-label', `${state.shuffle ? 'Disable' : 'Enable'} shuffle`);
  $('#player-repeat').classList.toggle('is-active', state.repeat !== 'off');
  $('#player-repeat .repeat-one').hidden = state.repeat !== 'one';
  $('#player-repeat').title = `Repeat ${state.repeat}`;
  $('#player-repeat').setAttribute('aria-label', `Repeat ${state.repeat}; click to ${state.repeat === 'off' ? 'repeat all' : state.repeat === 'all' ? 'repeat one' : 'turn repeat off'}`);
  const duration = Number(state.duration) || Number(track?.duration) || 0;
  const currentTime = Number(state.currentTime) || 0;
  $('#current-time').textContent = formatTime(currentTime);
  $('#total-time').textContent = formatTime(duration);
  $('#player-progress').disabled = !track || !duration;
  $('#player-progress').max = String(duration || 100);
  if (!seekInProgress) {
    $('#player-progress').value = String(Math.min(currentTime, duration));
    $('#player-progress').style.setProperty('--range-progress', `${duration ? currentTime / duration * 100 : 0}%`);
    $('#player-progress').setAttribute('aria-valuetext', `${formatTime(currentTime)} of ${formatTime(duration)}`);
  }
  $('#player-volume').value = String(state.volume);
  $('#player-volume').style.setProperty('--range-progress', `${state.volume * 100}%`);
  $('#player-volume').setAttribute('aria-valuetext', `${Math.round(state.volume * 100)} percent`);
  $('#player-mute').innerHTML = icon(state.volume === 0 ? 'muted' : 'volume');
  $('#player-mute').setAttribute('aria-label', state.volume === 0 ? 'Unmute' : 'Mute');
  $('#player-mute').title = state.volume === 0 ? 'Unmute' : 'Mute';
  $('#playback-label').innerHTML = `<span class="tiny-dot"></span>${state.loading ? 'SETTLING IN' : state.playing ? state.repeat === 'one' ? 'A MOMENT ON REPEAT' : 'IN GOOD COMPANY' : track ? 'TAKE YOUR TIME' : 'READY WHEN YOU ARE'}`;
  $('#playback-label').classList.toggle('is-playing', state.playing);
  if (state.error && state.error !== lastTrackError) toast(typeof state.error === 'string' ? state.error : 'This track could not be played.');
  lastTrackError = state.error || '';
  if (state.effectsError && state.effectsError !== lastEffectsError) toast(state.effectsError);
  lastEffectsError = state.effectsError || '';
  updateTrackPlayback();
  document.title = state.playing && track ? `${trackTitle(track)} — Stillroom` : 'Stillroom — your music, at home';
}
player.addEventListener('statechange', (event) => updatePlayer(event.detail || player.getState()));

function showDialog(dialog) {
  closeMenu();
  if (!dialog.open) dialog.showModal();
}
function closeDialog(dialog) {
  if (dialog.id === 'import-dialog' && importer.locked) return;
  if (dialog.id === 'edit-info-dialog' && editInfo.busy) return;
  if (dialog.id === 'share-song-dialog' && shareSong.locked) return;
  if (dialog.id === 'confirm-dialog' && $('#confirm-delete').disabled) return;
  dialog.close();
}
$('#confirm-dialog').addEventListener('cancel', event => { if ($('#confirm-delete').disabled) event.preventDefault(); });
$$('.close-dialog').forEach((button) => button.addEventListener('click', () => closeDialog(button.closest('dialog'))));
$$('dialog').forEach((dialog) => {
  dialog.addEventListener('click', (event) => {
    if (event.target !== dialog) return;
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeDialog(dialog);
  });
});
const importer = new ImportDialog({
  dialog: $('#import-dialog'), api, refreshLibrary, toast,
  showDialog, pauseMain: () => { player.pause(); recommendations?.interrupt(); }, getVolume: () => player.getState().volume,
});
const playlistImporter = new PlaylistImportDialog({
  dialog: $('#spotify-import-dialog'), api, refreshLibrary, toast, showDialog,
  onPlaylist: id => setView(id),
});
const settings = new SettingsDialog({
  dialog: $('#settings-dialog'), api, showDialog, toast,
  beforeInstall: () => { player.pause(); importer.pausePreview(); recommendations?.interrupt(); },
  onUpdate: status => { $('#update-badge').hidden = !['available', 'ready'].includes(status.state); },
});
const editInfo = new EditInfoDialog({
  dialog: $('#edit-info-dialog'), api, showDialog, toast,
  onSave: track => {
    // A pending library read may contain the old labels. The saved row is current.
    ++libraryRequest;
    library.tracks = library.tracks.map(entry => entry.id === track.id ? track : entry);
    librarySignature = JSON.stringify(library);
    player.setCatalog(library.tracks);
    recommendations?.setLibrary(library.tracks);
    renderCollection();
    updatePlayer(player.getState());
  },
  onMissing: () => refreshLibrary({ quiet: true }),
  onClose: id => {
    const row = $$('.track-row').find(entry => entry.dataset.trackId === id);
    (row ? $('.track-more', row) : $('#search')).focus();
  },
});
const shareSong = new ShareSongDialog({
  dialog: $('#share-song-dialog'), api, showDialog, toast,
  onMissing: () => refreshLibrary({ quiet: true }),
  onClose: id => {
    const row = $$('.track-row').find(entry => entry.dataset.trackId === id);
    (row ? $('.track-more', row) : $('#search')).focus();
  },
});
pocketExport = new PocketExportDialog({
  dialog: $('#pocket-export-dialog'), api, showDialog, getLibrary: () => library,
  onClose: () => {
    const row = $$('.track-row').find(entry => entry.dataset.trackId === pocketExportReturnTrack);
    (row ? $('.track-more', row) : $('#export-pocket')).focus();
  },
  onChange: state => {
    const tracked = !!state.job || state.pending || state.unknownCreation;
    $('#pocket-export-notice').hidden = !tracked;
    $('#pocket-export-notice-text').textContent = state.message;
    $('#export-pocket-label').textContent = state.active || state.pending || state.unknownCreation ? 'iPhone transfer…' : 'Export to iPhone';
  },
});
function openPocketExport(selection, trackId = null) {
  pocketExportReturnTrack = trackId;
  const playlist = currentPlaylist();
  const scope = selection || (playlist ? { mode: 'playlist', playlistId: playlist.id } : query.trim() || filter !== 'all' || view === 'loops' ? { mode: 'tracks', trackIds: visibleIds() } : { mode: 'all' });
  pocketExport.open(scope).catch(error => toast(error.message));
}
$('#export-pocket').addEventListener('click', () => openPocketExport());
$('#pocket-export-reopen').addEventListener('click', () => { pocketExportReturnTrack = null; pocketExport.open().catch(error => toast(error.message)); });
pocketExport.resume();
$('#open-settings').addEventListener('click', () => settings.open());
$('#import-spotify-open').addEventListener('click', () => {
  if (importer.busy) return;
  $('#import-dialog').close();
  playlistImporter.open('spotify');
});
$('#import-youtube-playlist-open').addEventListener('click', () => {
  if (importer.busy) return;
  $('#import-dialog').close();
  playlistImporter.open('youtube');
});
recommendations = new RecommendationPanel({ root: $('#recommendations'), api, refreshLibrary, pauseMain: () => { player.pause(); importer.pausePreview(); }, getVolume: () => player.getState().volume, playbackRate: player.getState().playbackRate, pitchChanges: player.getState().pitchChanges, reverb: player.getState().reverb, createMediaRoute: (audio, onError, onPlaybackError) => player.createMediaRoute(audio, onError, onPlaybackError), onPlaybackRateChange: setSongPlaybackRate, onPitchChangesChange: setSongPitchChanges, storage: (() => { try { return sessionStorage; } catch { return null; } })() });
connectRecommendationPlayback(player, recommendations, () => importer.pausePreview());
window.addEventListener('pagehide', () => recommendations.pagehide());
window.addEventListener('pageshow', event => { if (event.persisted) location.reload(); });
function openImport() { importer.open(); }

function openPlaylistDialog(playlist = null, withTrackId = null) {
  playlistEditId = playlist?.id || null;
  pendingTrackForNewPlaylist = withTrackId;
  $('#playlist-dialog-title').textContent = playlist ? 'Rename playlist' : 'Create playlist';
  $('#playlist-submit').textContent = playlist ? 'Save name' : 'Create playlist';
  $('#playlist-name').value = playlist?.name || '';
  showStatus($('#playlist-status'), '');
  showDialog($('#playlist-dialog'));
  $('#playlist-name').focus();
}
$('#playlist-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const name = $('#playlist-name').value.trim();
  if (!name) { $('#playlist-name').focus(); return; }
  $('#playlist-submit').disabled = true;
  try {
    let playlist = await api(playlistEditId ? `/api/playlists/${encodeURIComponent(playlistEditId)}` : '/api/playlists', jsonOptions(playlistEditId ? 'PATCH' : 'POST', { name }));
    playlist = playlist.playlist || playlist;
    if (pendingTrackForNewPlaylist && playlist.id) await api(`/api/playlists/${encodeURIComponent(playlist.id)}`, jsonOptions('PATCH', { trackIds: [...new Set([...(playlist.trackIds || []), pendingTrackForNewPlaylist])] }));
    $('#playlist-dialog').close();
    await refreshLibrary();
    if (!playlistEditId && playlist.id) setView(playlist.id);
    toast(playlistEditId ? 'Playlist renamed.' : pendingTrackForNewPlaylist ? 'Playlist created. Track added.' : 'Your new playlist is ready.');
    pendingTrackForNewPlaylist = null;
  } catch (error) { showStatus($('#playlist-status'), error.message); }
  finally { $('#playlist-submit').disabled = false; }
});
function openAddToPlaylist(trackId) {
  addingTrackId = trackId;
  const track = library.tracks.find((entry) => entry.id === trackId);
  $('#add-playlist-track').textContent = track ? `${trackTitle(track)} · ${trackArtist(track)}` : '';
  showStatus($('#add-playlist-status'), '');
  renderPlaylistChoices();
  showDialog($('#add-playlist-dialog'));
}
function renderPlaylistChoices() {
  $('#add-playlist-options').innerHTML = library.playlists.length ? library.playlists.map((playlist) => {
    const added = playlist.trackIds.includes(addingTrackId);
    return `<button class="playlist-option${added ? ' is-added' : ''}" data-playlist-id="${escapeText(playlist.id)}"${added ? ' disabled' : ''}><span>${icon('playlist')}</span><span>${escapeText(playlist.name)}<small>${playlist.trackIds.length} ${playlist.trackIds.length === 1 ? 'track' : 'tracks'}</small></span>${icon(added ? 'check' : 'plus')}</button>`;
  }).join('') : '<p class="playlist-options-empty">Your first playlist starts here.</p>';
}
$('#add-playlist-options').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-playlist-id]');
  if (!button || chooserBusy) return;
  const playlist = library.playlists.find((entry) => entry.id === button.dataset.playlistId);
  if (!playlist || !addingTrackId) return;
  chooserBusy = true;
  button.disabled = true;
  try {
    await api(`/api/playlists/${encodeURIComponent(playlist.id)}`, jsonOptions('PATCH', { trackIds: [...new Set([...playlist.trackIds, addingTrackId])] }));
    await refreshLibrary();
    renderPlaylistChoices();
    toast(`Added to ${playlist.name}.`);
  } catch (error) { showStatus($('#add-playlist-status'), error.message); button.disabled = false; }
  finally { chooserBusy = false; }
});
$('#create-from-chooser').addEventListener('click', () => {
  const id = addingTrackId;
  $('#add-playlist-dialog').close();
  openPlaylistDialog(null, id);
});
$('#delete-playlist').addEventListener('click', () => {
  const playlist = currentPlaylist();
  if (!playlist) return;
  pendingDeleteId = playlist.id;
  pendingDeleteKind = 'playlist';
  $('#confirm-title').textContent = 'Delete playlist?';
  $('#confirm-delete').textContent = 'Delete playlist';
  $('#confirm-description').textContent = `“${playlist.name}” will be removed. Its tracks will stay in your library.`;
  showStatus($('#confirm-status'), '');
  showDialog($('#confirm-dialog'));
});
$('#confirm-delete').addEventListener('click', async () => {
  if (!pendingDeleteId || $('#confirm-delete').disabled) return;
  const id = pendingDeleteId, kind = pendingDeleteKind;
  $('#confirm-delete').disabled = true;
  try {
    const result = await api(`/api/${kind === 'track' ? 'tracks' : 'playlists'}/${encodeURIComponent(id)}`, { method: 'DELETE' });
    pendingDeleteId = null;
    $('#confirm-dialog').close();
    if (kind === 'track') {
      library.tracks = library.tracks.filter(track => track.id !== id);
      for (const playlist of library.playlists) playlist.trackIds = playlist.trackIds.filter(trackId => trackId !== id);
      player.setCatalog(library.tracks);
      recommendations?.setLibrary(library.tracks);
      renderNavigation(); renderCollection();
    } else setView('library');
    await refreshLibrary();
    toast(kind === 'track' ? result.cleanupPending ? 'Song removed. Restart Stillroom to finish freeing its audio storage.' : 'Song deleted from your library.' : 'Playlist deleted.');
  } catch (error) { showStatus($('#confirm-status'), error.message); }
  finally { $('#confirm-delete').disabled = false; }
});
function confirmDeleteTrack(trackId) {
  const track = library.tracks.find(entry => entry.id === trackId);
  if (!track) return;
  pendingDeleteId = trackId; pendingDeleteKind = 'track';
  $('#confirm-title').textContent = 'Delete song?';
  $('#confirm-delete').textContent = 'Delete song';
  $('#confirm-description').textContent = `“${trackTitle(track)}” will be deleted from your library and all playlists, including its saved audio. Original files you uploaded will stay where they are. This cannot be undone.`;
  showStatus($('#confirm-status'), '');
  showDialog($('#confirm-dialog'));
}
async function removeFromPlaylist(trackId) {
  const playlist = currentPlaylist();
  if (!playlist) return;
  try {
    await api(`/api/playlists/${encodeURIComponent(playlist.id)}`, jsonOptions('PATCH', { trackIds: playlist.trackIds.filter((id) => id !== trackId) }));
    await refreshLibrary();
    toast('Track removed from this playlist.');
  } catch (error) { toast(error.message); }
}

function closeMenu(restoreFocus = false) {
  const trigger = menuTrigger;
  $('#track-menu').hidden = true;
  $$('.track-more[aria-expanded="true"]').forEach((button) => button.setAttribute('aria-expanded', 'false'));
  menuTrackId = null;
  menuTrigger = null;
  if (restoreFocus && trigger?.isConnected) trigger.focus();
}
function openMenu(button, trackId) {
  const sameMenu = menuTrackId === trackId && !$('#track-menu').hidden;
  closeMenu();
  if (sameMenu) return;
  menuTrackId = trackId;
  menuTrigger = button;
  const menu = $('#track-menu');
  menu.innerHTML = `<button role="menuitem" data-menu-action="edit">${icon('edit')}Edit info</button><button role="menuitem" data-menu-action="share">${icon('share')}Share song</button><button role="menuitem" data-menu-action="pocket-export">${icon('upload')}Export to iPhone</button><button role="menuitem" data-menu-action="add">${icon('playlist')}Add to playlist</button>${currentPlaylist() ? `<button role="menuitem" data-menu-action="remove" class="danger-action">${icon('close')}Remove from playlist</button>` : ''}<button role="menuitem" data-menu-action="delete" class="danger-action">${icon('trash')}Delete from library</button>`;
  menu.hidden = false;
  button.setAttribute('aria-expanded', 'true');
  const rect = button.getBoundingClientRect();
  const menuHeight = menu.offsetHeight;
  menu.style.left = `${Math.max(10, Math.min(rect.right - 195, window.innerWidth - 205))}px`;
  menu.style.top = `${rect.bottom + menuHeight + 10 > window.innerHeight - 110 ? rect.top - menuHeight - 5 : rect.bottom + 5}px`;
  $('button', menu).focus();
}
$('#track-menu').addEventListener('click', (event) => {
  const button = event.target.closest('[data-menu-action]');
  if (!button) return;
  const id = menuTrackId;
  closeMenu();
  if (button.dataset.menuAction === 'edit') editInfo.open(library.tracks.find(track => track.id === id));
  else if (button.dataset.menuAction === 'share') shareSong.open(library.tracks.find(track => track.id === id));
  else if (button.dataset.menuAction === 'pocket-export') openPocketExport({ mode: 'tracks', trackIds: [id] }, id);
  else if (button.dataset.menuAction === 'add') openAddToPlaylist(id);
  else if (button.dataset.menuAction === 'delete') confirmDeleteTrack(id);
  else if (button.dataset.menuAction === 'remove') removeFromPlaylist(id);
});
document.addEventListener('click', (event) => { if (!event.target.closest('#track-menu, .track-more')) closeMenu(); });
window.addEventListener('resize', () => closeMenu());
window.addEventListener('scroll', () => closeMenu(), true);

$('.primary-nav').addEventListener('click', (event) => { const button = event.target.closest('[data-view]'); if (button) setView(button.dataset.view); });
$('#playlist-nav').addEventListener('click', (event) => { const button = event.target.closest('[data-playlist-id]'); if (button) setView(button.dataset.playlistId); });
$('#filter-tabs').addEventListener('click', (event) => { const button = event.target.closest('[data-filter]'); if (button) { filter = button.dataset.filter; renderCollection(); } });
$('#search').addEventListener('input', (event) => { query = event.target.value; renderCollection(); });
$('#track-list').addEventListener('click', (event) => {
  const action = event.target.closest('[data-action]');
  const row = event.target.closest('.track-row');
  if (!action || !row) return;
  const id = row.dataset.trackId;
  if (action.dataset.action === 'more') openMenu(action, id);
  else if (action.dataset.action === 'loop') playTrack(id, true);
  else if (playback.track?.id === id) player.toggle();
  else playTrack(id);
});
$('#feature-play').addEventListener('click', () => { if (featuredTrack) playTrack(featuredTrack.id, featuredTrack.kind === 'loop'); else openImport(); });
$('#shuffle-view').addEventListener('click', () => {
  const ids = visibleIds();
  if (!ids.length) return;
  player.setShuffle(true);
  playTrack(ids[Math.floor(Math.random() * ids.length)]);
});
$('#add-music').addEventListener('click', openImport);
$('#empty-action').addEventListener('click', () => { if ($('#empty-action').dataset.action === 'library') setView('library'); else openImport(); });
$('#new-playlist').addEventListener('click', () => openPlaylistDialog());
$('#rename-playlist').addEventListener('click', () => openPlaylistDialog(currentPlaylist()));
$('#retry-library').addEventListener('click', () => refreshLibrary());
$('#player-toggle').addEventListener('click', () => { if (playback.track) player.toggle(); else if (visibleIds().length || library.tracks.length) playTrack(visibleIds()[0] || library.tracks[0].id); });
$('#player-prev').addEventListener('click', () => player.previous());
$('#player-next').addEventListener('click', () => player.next());
$('#player-shuffle').addEventListener('click', () => player.setShuffle(!playback.shuffle));
$('#player-repeat').addEventListener('click', () => player.setRepeat(playback.repeat === 'off' ? 'all' : playback.repeat === 'all' ? 'one' : 'off'));
$('#player-progress').addEventListener('input', (event) => {
  seekInProgress = true;
  const current = Number(event.target.value);
  $('#current-time').textContent = formatTime(current);
  event.target.style.setProperty('--range-progress', `${current / Number(event.target.max) * 100}%`);
  event.target.setAttribute('aria-valuetext', `${formatTime(current)} of ${formatTime(event.target.max)}`);
});
$('#player-progress').addEventListener('change', (event) => { player.seek(Number(event.target.value)); seekInProgress = false; });
$('#player-progress').addEventListener('blur', () => { seekInProgress = false; });
$('#player-volume').addEventListener('input', (event) => { const nextVolume = Number(event.target.value); player.setVolume(nextVolume); savePreference('volume', nextVolume); if (nextVolume > 0) previousVolume = nextVolume; });
$('#player-mute').addEventListener('click', () => {
  if (playback.volume > 0) { previousVolume = playback.volume; player.setVolume(0); savePreference('volume', 0); }
  else { player.setVolume(previousVolume || 0.4); savePreference('volume', previousVolume || 0.4); }
});
document.addEventListener('keydown', (event) => {
  if (event.defaultPrevented) return;
  if (event.key === 'Escape') { closeMenu(true); return; }
  const editing = event.target.closest('input, textarea, select, button, [contenteditable="true"]');
  if (event.key === '/' && !editing && !$$('dialog[open]').length) { event.preventDefault(); $('#search').focus(); }
  if (event.code === 'Space' && !editing && !$$('dialog[open]').length) {
    if (event.target.closest('audio')) return;
    const discovery = recommendations.player;
    if (discovery.active && ['ready', 'retained'].includes(discovery.session?.status)) {
      event.preventDefault();
      if (discovery.audio.paused) discovery.resume().catch(() => toast('Press Play in the recommendation player to resume.')); else discovery.audio.pause();
    } else if (playback.track) { event.preventDefault(); player.toggle(); }
  }
  if (!$('#track-menu').hidden && ['ArrowDown', 'ArrowUp'].includes(event.key)) {
    const items = $$('button', $('#track-menu'));
    const index = items.indexOf(document.activeElement);
    event.preventDefault();
    items[(index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]?.focus();
  }
});
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') refreshLibrary({ quiet: true }); });
setInterval(() => { if (document.visibilityState === 'visible' && !importer.busy) refreshLibrary({ quiet: true }); }, 20000);
renderNavigation();
renderCollection();
updatePlayer(player.getState());
refreshLibrary();
recommendations.start();

settings.startAutoChecks();
