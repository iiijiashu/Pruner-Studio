import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { api } from '../../web/js/api.js';
import { state } from '../../web/js/state.js';
import { player } from '../../web/js/player.js';
import { discoverManager as discover } from '../../web/js/discover.js';
import { modal } from '../../web/js/modal.js';
import { deckManager } from '../../web/js/deck.js';
import { stageManager } from '../../web/js/stage.js';
import { CHANNEL_CATALOG, cloudManager } from '../../web/js/cloud.js';
import { searchPalette } from '../../web/js/search_palette.js';
import { RuleEngine, rulesManager } from '../../web/js/rules.js';
import { catalogManager } from '../../web/js/catalog.js';
import { lyricsManager } from '../../web/js/lyrics.js';
import { buildSongContextMenuItems } from '../../web/js/context_menu.js';
import { toast } from '../../web/js/toast.js';

const song = id => ({id, title: '歌曲' + id, artists: [], duration: 200});
const ok = data => ({ok: true, data});
function pending() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
}

async function apiContracts(stub) {
  const calls = [];
  stub(api, 'request', async (url, options = {}) => {
    calls.push([url, options.method || 'GET', options.body && JSON.parse(options.body)]);
    return ok({});
  });
  const payload = {...state.writeScope(), song_id: 5, track_key: 'netease:5'};
  await api.postSmartRadio(payload);
  await api.postExploreBatch(payload);
  await api.trashPersonalFmTrack(payload);
  await api.submitFeedback(payload);
  await api.clearFeedback(payload);
  await api.importCatalog(payload);
  for (const [i, route] of ['recommend/smart-radio', 'recommend/explore-batch', 'recommend/fm/trash',
    'feedback', 'feedback', 'catalog/import'].entries()) {
    assert.deepEqual(calls[i], ['/api/' + route, i === 4 ? 'DELETE' : 'POST', payload]);
  }
  await api.getDailyRecommend({forceCheck: true, knownRawIds: [1, 2]});
  await api.getDailyHistory('2026-01-01');
  await api.getDustyGems({limit: 12, excludeIds: [3], excludeKeys: ['local:x&y']});
  await api.getLocalLibrary({q: '歌', limit: 25, offset: 50});
  await api.getDesktopLyricsWindow();
  const url = n => new URL(calls[n][0], 'http://localhost');
  assert.deepEqual(Object.fromEntries(url(6).searchParams), {force_check: '1', known_raw_ids: '1,2'});
  assert.equal(url(7).searchParams.get('date'), '2026-01-01');
  assert.deepEqual(Object.fromEntries(url(8).searchParams), {limit: '12', exclude_ids: '3', exclude_key: 'local:x&y'});
  assert.deepEqual(Object.fromEntries(url(9).searchParams), {q: '歌', limit: '25', offset: '50'});
  assert.equal(url(10).pathname, '/api/desktop/lyrics-window');
}

async function radioLifecycle(stub) {
  const calls = [], playback = [];
  let tracks = [song(1), song(2), song(3)];
  stub(api, 'postSmartRadio', async payload => {
    calls.push(payload);
    return ok({...payload, songs: tracks, exhausted: !tracks.length});
  });
  stub(player, 'playQueue', async (queue, index, mode) => {
    playback.push(queue[index]);
    state.set({playQueue: queue, playQueueIndex: index, playMode: mode, currentPlayingSong: queue[index]});
  });
  stub(player, 'scheduleSavePlaybackSession', () => {});
  player.resetRadioSession();
  state.set({user: {user_id: 24}, sessionGeneration: 7, playMode: 'order'});
  await player.requestRadio('start');
  state.set({playQueueIndex: 1});
  tracks = [song(4)];
  await player.requestRadio('rearrange');
  assert.deepEqual(state.get('playQueue').map(s => s.id), [1, 2, 4]);
  assert.equal(state.get('playQueueIndex'), 1);
  assert.equal(playback.length, 1);
  assert.deepEqual(calls[1].exclude_ids, [1, 2, 3]);
  assert.deepEqual(calls[1].exclude_keys, ['netease:1', 'netease:2', 'netease:3']);
  tracks = [];
  await Promise.all([player.refillSmartRadioQueue(), player.refillSmartRadioQueue()]);
  assert.equal(calls.length, 3);
  assert.equal(new Set(calls.map(p => p.request_id)).size, 3);
  assert.equal(new Set(calls.map(p => p.radio_session_id)).size, 1);
  assert.ok(calls.every(p => p.session_generation === 7 && p.expected_account_id === '24'));
  state.set({playQueueIndex: 2});
  assert.equal(player.getNextQueueIndex(true), -1);
  assert.equal(state.get('radioExhausted'), true);
  await player.refillSmartRadioQueue();
  assert.equal(calls.length, 3);
  tracks = [song(1)];
  await player.requestRadio('start', true);
  assert.equal(calls.at(-1).allow_replay, true);
  assert.equal(state.get('radioExhausted'), false);
  const gate = pending();
  stub(api, 'postSmartRadio', payload => gate.promise.then(() => ok({...payload, songs: [song(6)]})));
  const stale = player.refillSmartRadioQueue();
  state.set({sessionGeneration: 8});
  gate.resolve();
  assert.equal(await stale, false);
  assert.deepEqual(state.get('playQueue').map(s => s.id), [1]);
  stub(api, 'postSmartRadio', async () => ({ok: false, status: 409, error: {code: 'SESSION_GENERATION_MISMATCH'}}));
  assert.equal(await player.refillSmartRadioQueue(), false);
}

async function feeds(stub) {
  discover.resetDiscovery();
  const daily = {songs: [song(8)], recommend_day: '2026-01-01', raw_song_ids: [8], checked_at: 100};
  let res = ok(daily);
  const requests = [];
  stub(api, 'getDailyRecommend', async options => {requests.push(options); return res;});
  await discover.loadDailyRecommend();
  for (const failure of [ok({songs: []}), ok({...daily, songs: [song(6)], degraded: true}), {ok: false}]) {
    res = failure;
    await discover.loadDailyRecommend(true);
    assert.deepEqual(state.get('dailyRecommend'), daily.songs);
    assert.deepEqual(requests.at(-1), {forceCheck: true, knownRawIds: [8]});
    assert.equal(state.get('dailyRecommendMeta').checked_at, 100);
  }
  const dusty = [];
  stub(api, 'getDustyGems', async options => {
    dusty.push(options);
    return ok({songs: [song(dusty.length)], exhausted: dusty.length === 2});
  });
  await discover.loadDustyGems();
  await discover.loadDustyGems();
  await discover.loadDustyGems();
  assert.deepEqual(dusty.map(x => x.excludeIds), [[], [1], []]);
  assert.deepEqual(dusty[1].excludeKeys, ['netease:1']);
  const local = [], remote = [];
  stub(api, 'submitFeedback', async p => {local.push(p); return ok({});});
  stub(api, 'trashPersonalFmTrack', async p => {remote.push(p); return ok({status: 'cooldown', cooldown_sec: 60});});
  state.set({playMode: 'order'});
  for (const enabled of [false, true]) {
    state.updateSettings({sync_fm_trash_to_netease: enabled});
    state.set({personalFmQueue: [song(8)]});
    await discover.markNotInterested(song(8), 'personal_fm');
    assert.deepEqual(state.get('personalFmQueue'), []);
    assert.equal(remote.length, Number(enabled));
    assert.deepEqual(local.at(-1), {...state.writeScope(), song_id: 8, track_key: 'netease:8', action: 'not_interested', context: 'personal_fm'});
  }
  const slow = pending();
  stub(api, 'getDailyRecommend', () => slow.promise);
  const loading = discover.loadDailyRecommend();
  discover.resetDiscovery();
  slow.resolve(ok(daily));
  await loading;
  assert.deepEqual(state.get('dailyRecommend'), []);
  assert.equal(discover.dustySeen.size, 0);
  assert.equal(player.radioSession, null);
}

async function controls(stub) {
  assert.equal(RuleEngine.evaluateClause({id: 12, liked: false}, {field: 'liked', operator: 'equals', value: false}), true);
  assert.equal(RuleEngine.validClause({field: 'title', operator: 'gt', value: 7}), false);
  assert.equal(RuleEngine.validClause({field: 'duration', operator: 'lt', value: 'invalid'}), false);
  const pages = [];
  stub(api, 'getLocalLibrary', async ({offset}) => {
    pages.push(offset);
    return ok({files: [{title: '曲目', path: '/song' + offset}], has_more: !offset});
  });
  await catalogManager.loadLocalSongs(true);
  assert.deepEqual(pages, [0, 1]);
  const localSongs = state.get('localSongs');
  const cloud = {...song(3), source_playlist_id: 'cloud'};
  state.set({cloudSongs: [cloud], catalog: [], catalogSelectedIds: new Set([cloud, ...localSongs].map(s => state.catalogSongKey(s)))});
  assert.equal(catalogManager.selectedSongs().length, 3);
  const queue = Array.from({length: 600}, (_, i) => song(i + 1));
  state.set({playQueue: queue, playQueueIndex: 318, playMode: 'fm'});
  let persisted;
  stub(api, 'savePlaybackSession', async p => {persisted = p; return ok({});});
  stub(player, 'sessionOwner', state.getUserId());
  stub(player, 'currentSong', queue[318]);
  await player.scheduleSavePlaybackSession(true, {keepalive: true});
  assert.equal(persisted.queue.length, 221);
  assert.equal(persisted.queue[persisted.queue_index].id, 319);
  let played;
  stub(player, 'playSong', (s, start, opts) => {played = [s, start, opts];});
  buildSongContextMenuItems(queue[320], {source: 'queue', queue, index: 320})[0].action();
  assert.equal(state.get('playMode'), 'fm');
  assert.equal(state.get('playQueueIndex'), 320);
  assert.equal(played[2].preserveQueue, true);
  player.removeFromQueue(2);
  assert.equal(state.get('playQueueIndex'), 319);
  stub(lyricsManager, 'lyricsData', [{time: 0, text: '原词', translation: '译词', romaji: 'roma'}]);
  stub(lyricsManager, 'showTranslation', false);
  stub(lyricsManager, 'timeOffset', 0);
  assert.equal(lyricsManager.getActiveLyricSnapshot(2).translation, '译词');
  assert.equal(lyricsManager.getActiveLyricSnapshot(2).romaji, 'roma');
}

function mockDocument() {
  const nodes = new Map();
  function element(tagName = 'div') {
    return {tagName, children: [], attributes: {}, dataset: {}, style: {setProperty(){}},
      classList: {add(){}, remove(){}, toggle(){}},
      set textContent(text) { this.valueText = text; this.children = []; },
      get textContent() { return this.valueText || ''; },
      addEventListener(event, fn) { this[event] = fn; },
      appendChild(child) { this.children.push(child); },
      replaceChildren() { this.children = []; },
      setAttribute(key, val) { this.attributes[key] = val; },
      querySelector() {return null;}};
  }
  return {getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, element());
    return nodes.get(id);
  }, createElement: element, querySelectorAll: () => [], createTextNode: text => ({textContent: text})};
}
function descendants(node) {
  return [node, ...(node.children || []).flatMap(descendants)];
}

async function interactiveControls(stub) {
  const doc = mockDocument();
  stub(globalThis, 'document', doc);
  const node = id => doc.getElementById(id);
  const find = (root, label) => descendants(root).find(el => el.textContent === label);
  stub(modal, 'open', id => {modal.activeModal = {id: id.slice(1)};});
  stub(modal, 'activeModal', null);
  const fixture = Array.from({length: 101}, (_, i) => song(i + 1));
  const rule = {id: 'positive', name: '时长筛选', enabled: true, output_playlist_name: '规则结果',
    match_clauses: [{field: 'duration', operator: 'gt', value: '10'}]};
  state.set({catalog: fixture, cloudSongs: [], localSongs: [], rules: [rule]});
  stub(rulesManager, 'container', node('rules'));
  let emitted;
  stub(state, 'emit', (event, data) => {emitted = [event, data];});
  rulesManager.render();
  find(node('rules'), '生成歌单').click();
  assert.equal(emitted[0], 'playlist:create-from-rule');
  assert.deepEqual(emitted[1].songs, fixture);
  for (const field of ['createPlaylistIds', 'creatingRulePlaylist', 'createPlaylistScope']) stub(catalogManager, field, undefined);
  catalogManager.openPlaylistCreateModal(emitted[1].songs, emitted[1].name);
  assert.equal(node('create-playlist-name-input').value, '规则结果');
  assert.equal(catalogManager.createPlaylistIds.length, 101);
  assert.equal(modal.activeModal.id, 'create-playlist-modal');
  find(node('rules'), '预览').click();
  const preview = node('rule-preview-songs-list');
  assert.equal(preview.children.filter(el => el.className === 'preview-song-row').length, 100);
  find(preview, '下一页').click();
  assert.equal(preview.children.filter(el => el.className === 'preview-song-row').length, 1);
  assert.ok(find(preview, '歌曲101'));
  let summary;
  stub(state, 'resolveDestructiveAction', s => ({valid: s.id <= 3, action: 'unlike', playlistId: 1}));
  stub(state, 'stageBatchAction', s => s.id === 1);
  state.set({batchQueue: [{song: song(2), action: 'unlike', playlistId: 1}]});
  stub(toast, 'info', (_, text) => {summary = text;});
  find(node('rules'), '加入批处理').click();
  assert.equal(summary, '新增 1 首 · 已在队列 1 首 · 跳过 99 首');

  stub(deckManager, 'container', node('deck'));
  deckManager.renderStatePlaceholder('error');
  find(node('deck'), '重新尝试').click();
  assert.equal(emitted[0], 'deck:retry');
  state.set({deckQueue: [], deckIndex: 0});
  let seekTarget;
  stub(player, 'performChorusJump', s => { seekTarget = s; });
  await deckManager.jumpToChorus();
  assert.equal(seekTarget, undefined);
  const second = {...song(2), chorus_start: 35};
  state.set({deckQueue: [song(1), second], deckIndex: 1});
  stub(player, 'currentSong', song(1));
  stub(player, 'activeSrc', '/api/stream/current');
  stub(player, 'playSong', async (s, start, opts) => {
    assert.equal(opts.origin, 'deck'); assert.equal(opts.preserveQueue, true); player.currentSong = s;
  });
  await deckManager.jumpToChorus();
  assert.equal(seekTarget.chorus_start, 35);
  stub(stageManager, 'fluidCtx', {clearRect(){}});
  stub(stageManager, 'fluidCanvas', {width: 50, height: 20});
  stub(stageManager, 'mode', 'visualizer');
  stub(player, 'getFrequencyData', () => null);
  assert.equal(stageManager.drawFrame(20), false);
  assert.match(node('stage-spectrum-status').textContent, /无法提供频谱/);

  const provider = CHANNEL_CATALOG.find(c => c.secretFields?.length);
  stub(cloudManager, 'channelDrafts', new Map());
  const card = () => cloudManager.buildChannelCard(provider, {enabled: true}, null);
  const input = descendants(card()).find(el => el.attributes?.type === 'password');
  input.value = 'unsaved-fixture';
  input.input();
  assert.equal(descendants(card()).find(el => el.attributes?.type === 'password').value, 'unsaved-fixture');

  stub(api, 'getDailyHistory', async date => ok(date ? {songs: [song(12)]} : {dates: ['2026-11-22']}));
  await discover.loadPanel('history');
  const panel = node('discover-extra');
  await find(panel, '2026-11-22').click();
  assert.ok(find(panel, '歌曲12'));
  const requests = [];
  stub(api, 'postExploreBatch', async opts => {requests.push(opts); return ok({songs: [song(45)]});});
  await discover.loadPanel('explore');
  await discover.loadPanel('explore');
  assert.deepEqual(requests[1].exclude_ids, [45]);
  assert.deepEqual(requests[1].exclude_keys, ['netease:45']);

  const slider = node('discover-explore-slider');
  discover.bindSmartRadioControls();
  const settings = [];
  stub(api, 'saveSettings', async patch => {settings.push(patch); return ok(patch);});
  slider.change({target: {value: '60'}});
  slider.change({target: {value: '75'}});
  await new Promise(resolve => setTimeout(resolve, 350));
  assert.deepEqual(settings, [{smart_radio_explore_ratio: .75}]);

  const gate = pending();
  stub(api, 'searchMulti', () => gate.promise);
  for (const prop of ['activeChannel', 'query', 'results', 'localResults']) stub(searchPalette, prop, searchPalette[prop]);
  stub(searchPalette, 'renderResults', () => {});
  stub(searchPalette, 'runLocalSearch', () => {searchPalette.results = [];});
  searchPalette.activeChannel = 'all';
  searchPalette.query = '曲';
  const search = searchPalette.runOnlineSearch('曲');
  searchPalette.setChannel('local');
  gate.resolve(ok({tracks: [song(231)]}));
  await search;
  assert.equal(searchPalette.results.length, 0);
}

async function previewIsolation() {
  const html = readFileSync(new URL('../../web/desktop_lyrics.html', import.meta.url), 'utf8');
  const nodes = new Map(), requests = [], events = [];
  const element = id => {
    if (!nodes.has(id)) nodes.set(id, {dataset: {}, style: {setProperty(){}}, classList: {add(){}, remove(){}, toggle(){}},
      addEventListener(k, fn){this[k] = fn;}, setAttribute(){}, appendChild(){}});
    return nodes.get(id);
  };
  const page = {location: {search: '?preview=1'}, URLSearchParams, window: {close(){throw Error('closed preview');}},
    document: {documentElement: element('root'), getElementById: element, querySelector: element, addEventListener(){},
      createElement: () => element(Symbol()), createElementNS: () => element(Symbol())},
    fetch: async (path, opts) => {requests.push(opts?.method || 'GET'); return {ok: true, json: async () => ok({})};},
    EventSource: class {constructor(url){events.push(url);} addEventListener(){} }};
  vm.runInNewContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], page);
  for (const id of ['lock', 'close', 'preset', 'heart', 'next']) await element('dl-' + id + '-btn').click({});
  assert.deepEqual(events, ['/api/events?lyrics_preview=1']);
  assert.deepEqual(requests, ['GET']);
}

export async function runPhase6B() {
  const originalState = state.get();
  try {
    for (const test of [apiContracts, radioLifecycle, feeds, controls, interactiveControls, previewIsolation]) {
      const restore = [];
      const stub = (target, key, value) => {
        const original = target[key];
        restore.push(() => {target[key] = original;});
        target[key] = value;
      };
      for (const key of ['info', 'quiet', 'warning', 'error']) stub(toast, key, () => {});
      try {await test(stub);} finally {restore.reverse().forEach(fn => fn());}
    }
  } finally {
    discover.resetDiscovery();
    state.set(originalState);
  }
}
