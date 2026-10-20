import test from 'node:test';
import assert from 'node:assert/strict';
import { youtubeUrl, youtubePlaylistUrl, spotifyUrl, rowMarkup, reconcileChoices, selectedTracks, sessionSummary, playlistCoverage, PlaylistImportDialog } from '../public/playlist-import.js';

const video = id => `https://www.youtube.com/watch?v=${id}`;
const good = { url: video('Abcdef12345'), title: 'Song', channel: 'Artist', duration: 180, confidence: 'strong', reasons: ['Title and artist agree', 'Similar length'] };
const unsure = { ...good, url: video('ZYXwvut9876'), confidence: 'review' };
const row = (id, candidates = [good], status = 'matched') => ({ id, status, track: { title: `Song ${id}`, artist: 'Artist', duration: 180 }, candidates });

test('YouTube coverage preserves a known total when extraction completeness is unverified', () => {
  const coverage = playlistCoverage({source: 'youtube', status: 'review', rows: [row('a')], total: 1, complete: false});
  assert.match(coverage, /completeness unverified/);
  assert.doesNotMatch(coverage, /count unavailable/);
});

test('YouTube links canonicalize a single video and reject unsafe or ambiguous sources', () => {
  for (const url of ['https://youtu.be/Abcdef12345?t=10', 'https://music.youtube.com/watch?v=Abcdef12345&list=playlist', 'https://youtube.com/shorts/Abcdef12345', 'http://m.youtube.com/embed/Abcdef12345/']) assert.equal(youtubeUrl(url), good.url);
  for (const url of ['javascript:alert(1)', 'https://youtube.com.evil.test/watch?v=Abcdef12345', 'https://youtube.com@evil.test/watch?v=Abcdef12345', 'https://youtube.com/watch?v=Abcdef12345&v=ZYXwvut9876', 'https://youtube.com:8080/watch?v=Abcdef12345', 'https://youtube.com/playlist?list=123', 'https://youtube.com/watch?v=short', 'https://youtu.be/Abcdef12345\\anything', 'https://youtu.be/Abcdef12345?x=a b']) assert.equal(youtubeUrl(url), '', url);
});

test('Spotify source links only open the intended HTTPS host', () => {
  assert.equal(spotifyUrl('https://open.spotify.com/track/123'), 'https://open.spotify.com/track/123');
  for (const url of ['javascript:alert(1)', 'http://open.spotify.com/track/123', 'https://open.spotify.com.evil.test/track/123', 'https://name:secret@open.spotify.com/track/123', null]) assert.equal(spotifyUrl(url), '');
});

test('Only the strongest top candidate is selected by default', () => {
  const rows = [row('good'), row('review', [unsure, good]), row('none', [], 'unmatched'), row('invalid', [{ ...good, url: 'javascript:bad()' }])];
  const choices = reconcileChoices(rows);
  assert.deepEqual(selectedTracks(rows, choices), [{ rowId: 'good', url: good.url }]);
  assert.equal(choices.review.url, unsure.url);
  assert.equal(choices.review.selected, false);
  assert.equal(choices.none.selected, false);
});

test('New matches can be selected when results arrive, without changing an existing manual decision', () => {
  const pending = reconcileChoices([row('arriving', [], 'pending'), row('manual', [], 'matching')]);
  assert.deepEqual(pending, {});
  const choices = reconcileChoices([row('arriving'), row('manual')], { manual: { url: unsure.url, custom: unsure.url, selected: true, touched: true } });
  assert.equal(choices.arriving.selected, true);
  assert.equal(choices.manual.url, unsure.url);
  assert.equal(choices.manual.custom, unsure.url);
  assert.equal(choices.manual.selected, true);
});

test('A cleared choice remains cleared during matching and a failed import remains available to retry', () => {
  const choices = reconcileChoices([row('cleared'), row('retry', [good], 'error')], {
    cleared: { url: good.url, custom: '', selected: false, touched: true },
    retry: { url: good.url, custom: '', selected: true, touched: true },
  });
  assert.equal(choices.cleared.selected, false);
  assert.equal(choices.retry.selected, true);
});

test('Successfully saved and existing songs never enter a retry payload', () => {
  const rows = [row('saved', [good], 'saved'), row('existing', [good], 'existing'), row('failed', [good], 'error')];
  const old = Object.fromEntries(rows.map(entry => [entry.id, { url: good.url, selected: true, custom: '' }]));
  assert.deepEqual(selectedTracks(rows, old), [{ rowId: 'failed', url: good.url }]);
  const choices = reconcileChoices(rows, old);
  assert.equal(choices.saved.selected, false);
  assert.equal(choices.existing.selected, false);
  assert.equal(old.saved.selected, true, 'Reconciliation should not mutate saved preferences');
});

test('Invalid custom text is preserved during refresh but cannot be submitted', () => {
  const rows = [row('custom')];
  const choices = reconcileChoices(rows, { custom: { url: '', custom: 'https://you', selected: false, touched: true } });
  assert.equal(choices.custom.custom, 'https://you');
  assert.deepEqual(selectedTracks(rows, choices), []);
  assert.deepEqual(selectedTracks(rows, { custom: { selected: true, url: 'https://example.com/song' } }), []);
});

test('Existing copies require an explicit choice and only known library IDs can be submitted', () => {
  const rows = [{ ...row('copy'), existingTracks: [{ id: 'library-copy', title: 'Song', duration: 45 }] }];
  const defaults = reconcileChoices(rows);
  assert.deepEqual(selectedTracks(rows, defaults), [{ rowId: 'copy', url: good.url }]);
  const choices = reconcileChoices(rows, { copy: { selected: true, existingTrackId: 'library-copy', url: '', custom: '', touched: true } });
  assert.deepEqual(selectedTracks(rows, choices), [{ rowId: 'copy', existingTrackId: 'library-copy' }]);
  assert.deepEqual(selectedTracks(rows, { copy: { selected: true, existingTrackId: 'unknown-copy', url: good.url } }), []);
  rows[0].status = 'existing';
  assert.deepEqual(selectedTracks(rows, choices), []);
});

test('Summary separates newly saved songs, existing songs, and failures', () => {
  const session = { status: 'complete', rows: [row('a', [], 'saved'), row('b', [], 'existing'), row('c', [], 'error')] };
  assert.match(sessionSummary(session), /1 saved · 1 already in your library · 1 failed/);
  assert.match(sessionSummary({ status: 'matching', rows: [row('a'), row('b', [], 'matching')] }), /1 of 2 songs checked/);
  assert.match(sessionSummary({ status: 'review', rows: [row('a')] }), /Nothing downloads until/);
});

test('Playlist coverage distinguishes verified completeness, known partial lists, and unknown totals', () => {
  const session = { status: 'review', rows: [row('one'), row('two')], complete: false, total: null };
  assert.match(playlistCoverage(session), /2 public songs · full playlist count unavailable/);
  assert.doesNotMatch(playlistCoverage(session), /Partial track list/);
  assert.match(playlistCoverage({ ...session, total: 10 }), /Partial track list · 2 of 10/);
  assert.match(playlistCoverage({ ...session, total: 2, complete: true }), /Complete track list · 2 songs/);
  assert.doesNotMatch(playlistCoverage({ ...session, total: 2 }), /Partial track list/);
});

function controller(session, api) {
  const instance = Object.create(PlaylistImportDialog.prototype);
  const nodes = { '#spotify-playlist-url': { value: 'https://open.spotify.com/playlist/123', focus() {} }, '#spotify-playlist-name': { value: 'My playlist', focus() {} }, '#spotify-rows': { replaceChildren() {} } };
  Object.assign(instance, { source: session?.source || 'spotify', session, choices: {}, api, pending: false, generation: 0, timer: null, dialog: { open: true }, $: selector => nodes[selector], error() {}, render() {}, schedule() {}, persist() {}, showDialog() {}, refreshLibrary() {}, toast() {}, accept(next) { this.session = next; this.source = next.source || 'spotify'; } });
  return instance;
}

test('Finding a playlist requests matches without authorizing an audio import', async () => {
  const calls = [];
  const instance = controller(null, async (url, options) => { calls.push({ url, options }); return { session: { id: 'session', status: 'matching', rows: [] } }; });
  await instance.find();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/playlist-imports');
  assert.deepEqual(JSON.parse(calls[0].options.body), { url: 'https://open.spotify.com/playlist/123', source: 'spotify' });
  assert.equal(instance.session.status, 'matching');
});

test('The import click sends only explicitly selected valid video links and the edited playlist name', async () => {
  const calls = [];
  const session = { id: 'session', status: 'review', rows: [row('good'), row('unsure', [unsure]), row('saved', [good], 'saved')] };
  const instance = controller(session, async (url, options) => { calls.push({ url, options }); return { session: { ...session, status: 'importing' } }; });
  instance.choices = { good: { selected: true, url: good.url }, unsure: { selected: false, url: unsure.url }, saved: { selected: true, url: good.url } };
  await instance.importSelected();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/playlist-imports/session/import');
  assert.deepEqual(JSON.parse(calls[0].options.body), { title: 'My playlist', selections: [{ rowId: 'good', url: good.url }] });
});

test('Import is disabled while matching and cannot start with no valid selection', async () => {
  let requests = 0;
  const instance = controller({ id: 'session', status: 'matching', rows: [row('good')] }, async () => { requests += 1; });
  instance.choices = { good: { selected: true, url: good.url } };
  await instance.importSelected();
  instance.session.status = 'review'; instance.choices.good.selected = false;
  await instance.importSelected();
  assert.equal(requests, 0);
});

test('A stale polling response cannot overwrite a later stop or import action', async () => {
  let resolve;
  const instance = controller({ id: 'session', status: 'matching', rows: [] }, () => new Promise(done => { resolve = done; }));
  const polling = instance.poll();
  instance.generation += 1; instance.session.status = 'review';
  resolve({ session: { id: 'session', status: 'matching', rows: [row('late')] } });
  await polling;
  assert.equal(instance.session.status, 'review');
  assert.deepEqual(instance.session.rows, []);
});

test('A failed import response reconciles server progress without submitting twice', async () => {
  const requests = [], session = { id: 'session', status: 'review', rows: [row('good')] };
  const instance = controller(session, async (url, options = {}) => {
    requests.push({ url, method: options.method || 'GET' });
    if (options.method === 'POST') throw new Error('The response was interrupted.');
    return { session: { ...session, status: 'importing', playlistId: 'created-playlist' } };
  });
  instance.choices = { good: { selected: true, url: good.url } };
  await instance.importSelected();
  assert.deepEqual(requests.map(request => request.method), ['POST', 'GET']);
  assert.equal(instance.session.status, 'importing');
  assert.equal(instance.session.playlistId, 'created-playlist');
  assert.equal(instance.pending, false);
});

test('A failed stop response refreshes status and leaves saved tracks untouched', async () => {
  const requests = [], session = { id: 'session', status: 'importing', rows: [row('saved', [good], 'saved')] };
  const instance = controller(session, async (url, options = {}) => {
    requests.push(options.method || 'GET');
    if (options.method === 'DELETE') throw new Error('The response was interrupted.');
    return { session: { ...session, status: 'cancelled' } };
  });
  await instance.stop();
  assert.deepEqual(requests, ['DELETE', 'GET']);
  assert.equal(instance.session.status, 'cancelled');
  assert.equal(instance.session.rows[0].status, 'saved');
  assert.equal(instance.pending, false);
  assert.equal(instance.stopping, false);
});

test('Expired active sessions clear the stuck state so another playlist can be loaded', async () => {
  const instance = controller({ id: 'expired', status: 'importing', rows: [] }, async () => { throw Object.assign(new Error('Review expired.'), { status: 404 }); });
  instance.choices = { old: { selected: true, url: good.url } };
  await instance.poll();
  assert.equal(instance.session, null);
  assert.deepEqual(instance.choices, {});
  assert.equal(instance.active, false);
  assert.equal(instance.pending, false);
});

test('Reopening refreshes a review session, including newly available library copies', async () => {
  let calls = 0;
  const before = { id: 'session', status: 'review', rows: [row('good')] };
  const after = { ...before, rows: [{ ...row('good'), existingTracks: [{ id: 'new-copy', duration: 180 }] }] };
  const instance = controller(before, async () => { calls += 1; return { session: after }; });
  await instance.open();
  assert.equal(calls, 1);
  assert.equal(instance.session.rows[0].existingTracks[0].id, 'new-copy');
});

test('A transient restore failure retains the session ID for reconnecting', async () => {
  const instance = controller(null, async () => { throw new Error('Network unavailable.'); });
  instance.restored = { id: 'session', choices: {}, title: 'My playlist' };
  await instance.open();
  assert.equal(instance.restored.id, 'session');
  assert.equal(instance.pending, false);
});

test('Newly matched rows collapse automatic empty details while preserving an ongoing review', () => {
  const oldDocument = globalThis.document;
  globalThis.document = { activeElement: null };
  try {
    const details = { open: true };
    const element = {
      dataset: { spotifyRow: 'song' }, _hadCandidates: false,
      querySelector: () => details, contains: () => false,
      set innerHTML(value) { details.open = /<details[^>]* open>/.test(value); },
    };
    const instance = controller({ id: 'session', status: 'matching', rows: [row('song')] }, async () => {});
    instance.$ = () => ({ children: [element] });
    instance.choices = reconcileChoices(instance.session.rows);
    instance.renderRows();
    assert.equal(details.open, false, 'The automatic pending expansion should collapse when candidates arrive');
    details.open = true;
    instance.session.rows[0].message = 'Updated while reviewing';
    instance.renderRows();
    assert.equal(details.open, true, 'An expanded candidate review should remain open during later updates');
    element._hadCandidates = false;
    instance.choices.song.custom = 'https://you';
    instance.renderRows();
    assert.equal(details.open, true, 'A partially entered custom link should remain visible');
  } finally { globalThis.document = oldDocument; }
});


const directVideo = { ...good, confidence: 'direct', reasons: ['This is the video listed in the YouTube playlist.'] };
const youtubeRow = (id, status = 'matched') => ({ ...row(id, [directVideo], status), track: { title: `Video ${id}`, artist: 'Creator', youtubeUrl: good.url, available: true, duration: 180 } });
const unavailableRow = id => ({ ...youtubeRow(id, 'skipped'), track: { title: '[Private video]', available: false }, candidates: [], message: 'This video is unavailable.' });

test('YouTube playlist source links retain the list and reject unsafe or ambiguous URLs', () => {
  const expected = 'https://www.youtube.com/playlist?list=PLabcdefghij12345';
  for (const url of [expected, 'https://music.youtube.com/playlist?list=PLabcdefghij12345&si=share', 'https://m.youtube.com/watch?v=Abcdef12345&list=PLabcdefghij12345']) assert.equal(youtubePlaylistUrl(url), expected);
  for (const url of ['javascript:alert(1)', 'https://youtube.com.evil.test/playlist?list=PLabcdefghij12345', 'https://user:password@youtube.com/playlist?list=PLabcdefghij12345', 'https://youtube.com/playlist?list=short', 'https://youtube.com/playlist?list=PLabcdefghij12345&list=PLotherlist123', 'https://youtube.com:8080/playlist?list=PLabcdefghij12345', 'https://youtube.com/channel?list=PLabcdefghij12345', 'https://youtube.com/playlist?list=PLabcdefghij12345\\oops', null]) assert.equal(youtubePlaylistUrl(url), '', url);
});

test('Direct playlist videos are selected by default while unavailable entries never enter import payloads', () => {
  const rows = [youtubeRow('first'), unavailableRow('private'), youtubeRow('retry', 'error')];
  const choices = reconcileChoices(rows);
  assert.deepEqual(selectedTracks(rows, choices), [{ rowId: 'first', url: good.url }, { rowId: 'retry', url: good.url }]);
  assert.equal(choices.private.selected, false);
  const staleChoices = { ...choices, private: { selected: true, url: good.url } };
  assert.equal(reconcileChoices(rows, staleChoices).private.selected, false);
  assert.equal(selectedTracks(rows, staleChoices).length, 2);
  assert.equal(reconcileChoices(rows, { first: { selected: false, url: good.url, touched: true } }).first.selected, false);
});

test('YouTube review shows the actual playlist source and does not describe it as a guessed match', () => {
  const entry = youtubeRow('first');
  const markup = rowMarkup(entry, 0, reconcileChoices([entry]).first, false, 'youtube');
  assert.match(markup, /Ready to import/);
  assert.match(markup, /Playlist video/);
  assert.match(markup, /YouTube ↗/);
  assert.match(markup, /href="https:\/\/www.youtube.com\/watch\?v=Abcdef12345"/);
  assert.doesNotMatch(markup, /Good match|Check match|Spotify|possible match/);
  const unavailable = rowMarkup(unavailableRow('private'), 1, { selected: true }, false, 'youtube');
  assert.match(unavailable, /type="checkbox"[^>]+ disabled/);
  assert.doesNotMatch(unavailable, / checked|<details|type="url"/);
  assert.match(unavailable, /Unavailable/);
});

test('YouTube summaries distinguish unavailable entries and the 100-entry review limit', () => {
  const session = { source: 'youtube', status: 'review', rows: [youtubeRow('first'), unavailableRow('private')], total: 2, complete: true };
  assert.match(sessionSummary({ ...session, status: 'reading' }), /Reading the YouTube playlist/);
  assert.match(sessionSummary(session), /1 of 2 playlist videos are available to select/);
  assert.doesNotMatch(sessionSummary(session), /matches|Spotify/);
  assert.equal(playlistCoverage(session), 'Complete video list · 2 entries · 1 available · 1 unavailable');
  const large = { ...session, rows: Array.from({ length: 100 }, (_, index) => youtubeRow(String(index))), total: 142, complete: false };
  assert.match(playlistCoverage(large), /Partial video list · 100 of 142 entries shown · 100 available/);
  assert.match(playlistCoverage({ ...large, total: null }), /100 playlist entries shown · 100 available · full playlist count unavailable/);
  assert.doesNotMatch(playlistCoverage(large), /Spotify|Complete/);
});

test('Loading a YouTube playlist sends its source without starting audio downloads', async () => {
  const calls = [];
  const instance = controller(null, async (url, options) => { calls.push({ url, options }); return { session: { id: 'youtube-session', source: 'youtube', status: 'reading', rows: [] } }; });
  instance.source = 'youtube'; instance.$('#spotify-playlist-url').value = 'https://www.youtube.com/playlist?list=PLabcdefghij12345';
  await instance.find();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/playlist-imports');
  assert.deepEqual(JSON.parse(calls[0].options.body), { source: 'youtube', url: 'https://www.youtube.com/playlist?list=PLabcdefghij12345', useYouTubeAccount: false });
  assert.equal(instance.session.status, 'reading');
});

test('Changing playlist source clears only a finished review and cannot lose an active import', () => {
  const session = { id: 'spotify-session', source: 'spotify', status: 'review', rows: [row('one')] };
  const instance = controller(session, async () => {});
  instance.choices = reconcileChoices(session.rows);
  assert.equal(instance.selectSource('youtube'), true);
  assert.equal(instance.source, 'youtube'); assert.equal(instance.session, null); assert.deepEqual(instance.choices, {});
  assert.equal(instance.$('#spotify-playlist-url').value, ''); assert.equal(instance.$('#spotify-playlist-name').value, '');
  instance.session = { ...session, source: 'youtube', status: 'importing' };
  assert.equal(instance.selectSource('spotify'), false);
  assert.equal(instance.source, 'youtube'); assert.equal(instance.session.id, 'spotify-session');
  instance.session = null; instance.restored = { id: 'restoring-session' };
  assert.equal(instance.selectSource('spotify'), false);
  assert.equal(instance.restored.id, 'restoring-session');
});

test('Opening another source preserves and reports the active import', async () => {
  const session = { id: 'youtube-session', source: 'youtube', status: 'importing', rows: [youtubeRow('one')] };
  const instance = controller(session, async () => ({ session }));
  let message = ''; instance.error = value => { message = value; };
  await instance.open('spotify');
  assert.equal(instance.source, 'youtube'); assert.equal(instance.session.id, 'youtube-session');
  assert.match(message, /YouTube playlist import is still in progress/);
});

test('YouTube source and choices are persisted so a review can resume after refresh', () => {
  const instance = controller({ id: 'youtube-session', source: 'youtube', status: 'review', rows: [youtubeRow('one')] }, async () => {});
  instance.choices = reconcileChoices(instance.session.rows); instance.titleTouched = true;
  let saved; instance.storage = { setItem(key, value) { saved = JSON.parse(value); } };
  PlaylistImportDialog.prototype.persist.call(instance);
  assert.equal(saved.source, 'youtube'); assert.equal(saved.id, 'youtube-session');
  assert.equal(saved.choices.one.selected, true); assert.equal(saved.title, 'My playlist');
});

test('YouTube dialog labels, links, and stop controls follow the session source', () => {
  const session = { id: 'youtube-session', source: 'youtube', status: 'reading', rows: [], url: 'https://www.youtube.com/playlist?list=PLabcdefghij12345' };
  const instance = controller(session, async () => {});
  const nodes = new Map();
  instance.$ = selector => {
    if (!nodes.has(selector)) nodes.set(selector, { value: '', textContent: '', classList: { toggle() {} }, setAttribute(name, value) { this[name] = value; }, removeAttribute(name) { delete this[name]; } });
    return nodes.get(selector);
  };
  instance.renderRows = () => {}; instance.renderSummary = () => {};
  PlaylistImportDialog.prototype.render.call(instance);
  assert.equal(instance.$('#spotify-import-title').textContent, 'From YouTube');
  assert.equal(instance.$('#playlist-import-source').disabled, true);
  assert.equal(instance.$('#spotify-find').textContent, 'Loading playlist…');
  assert.equal(instance.$('#spotify-stop').textContent, 'Stop loading');
  assert.equal(instance.$('#spotify-source-link').textContent, 'Open YouTube ↗');
  assert.equal(instance.$('#spotify-source-link').href, session.url);
  assert.match(instance.$('#spotify-source-hint').textContent, /100 entries/);
  assert.equal(instance.$('#spotify-select-good').textContent, 'Select available videos');
  assert.doesNotMatch(instance.$('#playlist-import-review-hint').textContent, /match/i);
});


test('Private playlist account choice applies to its session and resets before a later import', async () => {
  const bodies = [], checkbox = { checked: true };
  const instance = controller(null, async (url, options) => {
    const body = JSON.parse(options.body); bodies.push(body);
    return { session: { id: `session-${bodies.length}`, source: 'youtube', status: 'review', rows: [], useYouTubeAccount: body.useYouTubeAccount } };
  });
  const original = instance.$;
  instance.$ = selector => selector === '#playlist-youtube-account' ? checkbox : original(selector);
  instance.source = 'youtube';
  await instance.find();
  assert.equal(bodies[0].useYouTubeAccount, true);
  assert.equal(instance.session.useYouTubeAccount, true);
  assert.equal(checkbox.checked, false, 'A new import must opt in again');
  await instance.find();
  assert.equal(bodies[1].useYouTubeAccount, false);
});

test('Importing an existing playlist review cannot switch the account from its original session', async () => {
  const calls = [], checkbox = { checked: false };
  const session = { id: 'private-session', source: 'youtube', status: 'review', rows: [youtubeRow('one')], useYouTubeAccount: true };
  const instance = controller(session, async (url, options) => { calls.push(JSON.parse(options.body)); return { session: { ...session, status: 'importing' } }; });
  const original = instance.$;
  instance.$ = selector => selector === '#playlist-youtube-account' ? checkbox : original(selector);
  instance.choices = reconcileChoices(session.rows);
  await instance.importSelected();
  assert.equal(Object.hasOwn(calls[0], 'useYouTubeAccount'), false, 'The backend session retains its account authorization');
  assert.equal(instance.session.useYouTubeAccount, true);
});
