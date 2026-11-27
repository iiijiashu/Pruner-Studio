import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

import { api } from '../../web/js/api.js';
import { state } from '../../web/js/state.js';
import { player } from '../../web/js/player.js';
import { CHANNEL_CATALOG, cloudManager } from '../../web/js/cloud.js';
import { computeManifestHash } from '../../web/js/utils.js';
import { searchPalette } from '../../web/js/search_palette.js';
import { SearchViewManager } from '../../web/js/search_view.js';
import { toast } from '../../web/js/toast.js';

async function testBrowserBootstrap() {
  const token = 'browser-test-token';
  const origin = 'http://127.0.0.1:8765';
  const sessions = new Map();
  const caches = {
    open: async () => ({
      put: async (key, response) => sessions.set(key, response),
      match: async key => sessions.get(key)?.clone(),
    }),
  };
  const navigations = [];
  const serviceWorker = {
    controller: null,
    register: async (script, options) => {
      assert.equal(script, '/session-worker.js');
      assert.equal(options.scope, '/');
    },
    addEventListener: (name, listener) => {
      assert.equal(name, 'controllerchange');
      serviceWorker.controller = {};
      listener();
    },
  };
  await vm.runInNewContext(fs.readFileSync('web/bootstrap.js', 'utf8'), {
    caches, Response, navigator: { serviceWorker },
    document: {
      querySelector: selector => {
        assert.equal(selector, 'meta[name="pruner-browser-session"]');
        return { content: token };
      },
    },
    history: { replaceState: (_state, _title, url) => navigations.push(url) },
    location: { pathname: '/', replace: url => navigations.push(url) },
  });
  assert.deepEqual(navigations, ['/', '/']);

  const handlers = new Map();
  vm.runInNewContext(fs.readFileSync('web/session-worker.js', 'utf8'), {
    caches, Request, Headers, URL,
    self: { location: { origin }, addEventListener: (name, handler) => handlers.set(name, handler) },
    fetch: async request => request,
  });
  async function workerRequest(url, init) {
    let response;
    handlers.get('fetch')({
      request: new Request(url, init),
      respondWith: promise => { response = promise; },
    });
    return response;
  }
  for (const endpoint of ['/api/status', '/api/events', '/api/cover', '/api/stream/audio']) {
    const request = await workerRequest(origin + endpoint, { headers: { Range: 'bytes=0-' } });
    assert.equal(request.headers.get('X-Pruner-Token'), token);
    assert.equal(request.headers.get('Range'), 'bytes=0-');
    assert.equal(request.redirect, 'error');
    assert.equal(request.credentials, 'omit');
  }
  assert.equal(await workerRequest(origin + '/'), undefined);
  assert.equal(await workerRequest('http://127.0.0.1:8766/api/status'), undefined);
}

async function testGuestSearch() {
  const view = new SearchViewManager();
  const palette = new searchPalette.constructor();
  const previous = Object.fromEntries(
    ['offlineMode', 'guestLocalMode', 'authenticated'].map(key => [key, state.get(key)]),
  );
  const { searchMulti, search } = api;
  try {
    state.set({ offlineMode: true, guestLocalMode: true, authenticated: false });
    view.activeChannel = palette.activeChannel = 'all';
    const offsets = [];
    api.searchMulti = async (_query, channels, _limit, offset) => {
      assert.equal(channels, null);
      offsets.push(offset);
      return { ok: true, data: {
        tracks: [{ provider: 'webdav', external_id: String(offset), title: `Song ${offset}` }],
        offset, limit: 100, has_more: offset === 0,
      } };
    };
    api.search = async () => assert.fail('Guest search triggered NetEase login fallback');
    await view.search('Song');
    await view.loadMore();
    await palette.runOnlineSearch('Song');
    assert.deepEqual(offsets, [0, 100, 0]);
    assert.equal(view.tracks.length, 2);
    assert.equal(palette.results[0].provider, 'webdav');
    api.searchMulti = async () => ({ ok: false, error: { code: 'NETWORK_ERROR', message: 'Unavailable' } });
    await view.search('Song');
    await palette.runOnlineSearch('Song');
    state.set({ guestLocalMode: false });
    api.searchMulti = async () => assert.fail('Cached account started online search');
    await view.search('Song');
    await palette.runOnlineSearch('Song');
  } finally {
    view.query = '';
    Object.assign(api, { searchMulti, search });
    state.set(previous);
  }
}

async function testWebDavScanAndFavoriteRollback() {
  const doc = globalThis.document;
  const { scanWebDav, setFavorite } = api;
  const { success, error } = toast;
  const messages = [];
  const track = { id: 0, provider: 'webdav', external_id: 'fav-rollback', title: 'Track' };
  const wasLiked = state.isSongLiked(track);
  let scans = 0;
  const unsubscribe = state.on('webdav:scanned', () => scans++);
  const node = tag => ({
    tagName: tag.toUpperCase(), children: [], dataset: {}, listeners: {},
    setAttribute() {}, addEventListener(name, handler) { this.listeners[name] = handler; },
    appendChild(child) { this.children.push(child); }, classList: { toggle() {} },
  });
  const findScanButton = n => n.textContent === '扫描 WebDAV 曲库'
    ? n : n.children?.map(findScanButton).find(Boolean);
  try {
    toast.success = (...args) => messages.push(['success', ...args]);
    toast.error = (...args) => messages.push(['error', ...args]);
    const grid = node('div');
    globalThis.document = {
      createElement: node, getElementById: id => id === 'channel-config-grid' ? grid : null,
    };
    const card = cloudManager.buildChannelCard(CHANNEL_CATALOG.find(ch => ch.id === 'webdav'), {}, null);
    const button = findScanButton(card);
    assert.ok(button);
    let finish;
    api.scanWebDav = () => new Promise(resolve => { finish = resolve; });
    const scan = button.listeners.click();
    assert.equal(button.disabled, true);
    finish({ ok: true, data: { scanned_count: 42 } });
    await scan;
    assert.equal(button.disabled, false);
    assert.equal(scans, 1);
    assert.match(messages.at(-1)[2], /42/);
    api.scanWebDav = async () => ({ ok: false, error: { message: 'DAV unavailable' } });
    await button.listeners.click();
    assert.deepEqual(messages.at(-1), ['error', 'WebDAV 扫描失败', 'DAV unavailable']);
    assert.equal(scans, 1);

    globalThis.document = undefined;
    state.setSongLiked(track, false);
    api.setFavorite = async () => {
      assert.equal(state.isSongLiked(track), true);
      return { ok: false, error: { code: 'NETWORK_ERROR', message: 'Offline' } };
    };
    await player.toggleLikeCurrentSong(track);
    assert.equal(state.isSongLiked(track), false);
    assert.deepEqual(messages.at(-1), ['error', '收藏更新失败', 'Offline']);
  } finally {
    globalThis.document = doc;
    Object.assign(api, { scanWebDav, setFavorite });
    Object.assign(toast, { success, error });
    state.setSongLiked(track, wasLiked);
    unsubscribe();
  }
}

export async function testMilestone1Upgrades() {
  await testBrowserBootstrap();
  await testGuestSearch();
  await testWebDavScanAndFavoriteRollback();
  const release = fs.readFileSync('.github/workflows/release.yml', 'utf8');
  assert.match(release, /Stage Android APK artifact\s+if: steps\.android_build\.outputs\.signed == '1'[\s\S]*?cp app\/build\/outputs\/apk\/release\/app-release\.apk/);
  assert.match(release, /if: needs\.build-android\.outputs\.signed == '1'/);
  assert.doesNotMatch(release, /(?:cp|Copy-Item)[^\n]*app-debug\.apk/);
  // The worker supplies the browser credential to both fetch and media subresources.
  const origDoc = globalThis.document;
  const origFetch = globalThis.fetch;
  try {
    let capturedHeaders = null;
    globalThis.document = {
      querySelector: (sel) => sel === 'meta[name="pruner-local-token"]'
        ? { getAttribute: (k) => (k === 'content' ? 'proc-token-xyz' : null) }
        : null,
    };
    globalThis.fetch = async (_url, cfg) => {
      capturedHeaders = cfg.headers;
      return {
        ok: true,
        status: 200,
        headers: { get: () => 'application/json' },
        json: async () => ({ ok: true, data: { status: 'ok' }, error: null }),
      };
    };
    const res = await api.health();
    assert.equal(res.ok, true);
    assert.equal(capturedHeaders['X-Pruner-Token'], undefined);
  } finally {
    globalThis.document = origDoc;
    globalThis.fetch = origFetch;
  }

  // 2. Canonical track_key favorites & hybrid playlist pruning even in offline/guest mode
  const prevState = {
    offlineMode: state.get('offlineMode'),
    playlists: state.get('playlists'),
    likedSongIds: state.get('likedSongIds'),
    likedTrackKeys: state.get('likedTrackKeys'),
  };
  try {
    state.set({
      offlineMode: true,
      playlists: [
        { id: 'hybrid:abc123', name: '混合歌单', kind: 'hybrid' },
        { id: 888, name: '网易云收藏', kind: 'subscribed' },
      ],
    });
    const navSong = { id: 0, provider: 'opensubsonic', external_id: 'os-42', track_key: 'opensubsonic:os-42', title: 'Navidrome Track', source_playlist_id: 'hybrid:abc123' };
    state.setSongLiked(navSong, true);
    assert.equal(state.isSongLiked(navSong), true);
    assert.equal(state.isSongLiked('opensubsonic:os-42'), true);

    const hybridAction = state.resolveDestructiveAction(navSong, 'hybrid:abc123');
    assert.equal(hybridAction.valid, true);
    assert.equal(hybridAction.action, 'remove_hybrid_tracks');
    assert.equal(hybridAction.trackKey, 'opensubsonic:os-42');

    // Offline NetEase subscribed playlist remains strictly blocked
    const neteaseSub = state.resolveDestructiveAction({ id: 555, source_playlist_id: 888 }, 888);
    assert.equal(neteaseSub.valid, false);
  } finally {
    state.set(prevState);
  }

  // 3. Player ReplayGain per-track gain compensation & SMTC NaN/Infinity safety
  const savedCtx = player.audioCtx;
  const savedComp = player.compressorNode;
  const savedGain = player.gainNode;
  const savedRg = player.replayGainNode;
  const savedLoudness = player.currentLoudness;
  try {
    let rgTarget = null;
    let compRatio = null;
    player.audioCtx = { currentTime: 10 };
    player.replayGainNode = { gain: { setTargetAtTime: (v) => { rgTarget = v; } } };
    player.compressorNode = {
      threshold: { setValueAtTime: () => {} },
      knee: { setValueAtTime: () => {} },
      ratio: { setValueAtTime: (v) => { compRatio = v; } },
      attack: { setValueAtTime: () => {} },
      release: { setValueAtTime: () => {} },
    };
    player.gainNode = { gain: { setTargetAtTime: () => {} } };
    player.currentLoudness = { source: 'replaygain_tag', computed_gain_linear: 0.62 };
    state.updateSettings({ loudness_normalization: true });
    player.applyWebAudioSettings();
    assert.equal(rgTarget, 0.62, 'ReplayGain tag must apply exact per-track linear gain');
    assert.equal(compRatio, 12, 'Tagged track should engage transparent safety limiter');

    // Untagged track falls back to adaptive leveling with makeup gain
    player.currentLoudness = { source: 'none', computed_gain_linear: 1.0 };
    player.applyWebAudioSettings();
    assert.equal(rgTarget, 1.15);
    assert.equal(compRatio, 3.5);
  } finally {
    player.audioCtx = savedCtx;
    player.compressorNode = savedComp;
    player.gainNode = savedGain;
    player.replayGainNode = savedRg;
    player.currentLoudness = savedLoudness;
  }

  // 4. Built-in channels preserved alongside first-class OpenSubsonic & WebDAV + 4-way onboarding HTML hooks
  const ids = new Set(CHANNEL_CATALOG.map(c => c.id));
  for (const required of ['kugou_lite', 'migu', 'qq', 'kuwo', 'bilibili', 'gdstudio', 'custom', 'opensubsonic', 'webdav']) {
    assert.equal(ids.has(required), true, `CHANNEL_CATALOG missing ${required}`);
  }
  const html = fs.readFileSync(path.join('web', 'index.html'), 'utf8');
  for (const hookId of [
    'shell-guest-local',
    'shell-start-protocol',
    'shell-start-resolver',
    'resolver-subscribe-btn',
    'sidecar-toggle-btn',
    'webdav-sync-push-btn',
    'webdav-sync-pull-btn',
    'migration-preview-btn',
    'migration-commit-btn',
  ]) {
    assert.equal(html.includes(`id="${hookId}"`), true, `web/index.html missing #${hookId}`);
  }

  // 5. Canonical liked symmetry across numeric ID, netease:ID, and non-NetEase song objects
  state.setSongLiked(778899, true);
  assert.equal(state.isSongLiked('netease:778899'), true, 'netease:ID should match likedSongIds');
  state.setSongLiked(778899, false);
  const osTrack = { id: 'opensubsonic:t9', provider: 'opensubsonic', external_id: 't9', track_key: 'opensubsonic:t9', title: 'Subsonic Song' };
  state.setSongLiked(osTrack, true);
  assert.equal(state.isSongLiked(osTrack), true);
  assert.equal(state.isSongLiked('opensubsonic:t9'), true);
  state.setSongLiked(osTrack, false);

  // 6. Multi-track hybrid batchQueue isolation (multiple id===0 tracks never collide on unstage, hash, or commit)
  const prevBatchState = {
    offlineMode: state.get('offlineMode'),
    playlists: state.get('playlists'),
    catalog: state.get('catalog'),
    batchQueue: state.get('batchQueue'),
  };
  const origRemoveHybrid = api.removeTracksFromHybridPlaylist;
  const origGetPending = api.getPendingOperations;
  const origReload = cloudManager.reloadCatalog;
  const origBatchDoc = globalThis.document;
  try {
    const mkNode = () => ({
      className: '', textContent: '', style: {}, dataset: {}, children: [],
      setAttribute() {}, addEventListener() {}, appendChild(c) { this.children.push(c); return c; },
      remove() {}, contains() { return true; }, classList: { add() {}, remove() {}, toggle() {} },
    });
    globalThis.document = {
      body: mkNode(),
      createElement: () => mkNode(),
      getElementById: () => null,
      querySelector: () => null,
      querySelectorAll: () => [],
    };
    const hTrack1 = { id: 0, provider: 'local', local_path: '/m/a.flac', track_key: 'local:/m/a.flac', title: 'Local A', source_playlist_id: 'hybrid:mix1' };
    const hTrack2 = { id: 0, provider: 'opensubsonic', external_id: 'os-2', track_key: 'opensubsonic:os-2', title: 'Nav B', source_playlist_id: 'hybrid:mix1' };
    const hTrack3 = { id: 0, provider: 'webdav', external_id: 'wd-3', track_key: 'webdav:wd-3', title: 'Dav C', source_playlist_id: 'hybrid:mix1' };
    state.set({
      offlineMode: true,
      playlists: [{ id: 'hybrid:mix1', name: 'Mix 1', kind: 'hybrid' }],
      catalog: [hTrack1, hTrack2, hTrack3],
      batchQueue: [],
    });
    assert.equal(state.stageBatchAction(hTrack1, 'remove_hybrid_tracks', 'hybrid:mix1', 'Mix 1'), true);
    assert.equal(state.stageBatchAction(hTrack2, 'remove_hybrid_tracks', 'hybrid:mix1', 'Mix 1'), true);
    assert.equal(state.stageBatchAction(hTrack3, 'remove_hybrid_tracks', 'hybrid:mix1', 'Mix 1'), true);
    assert.equal(state.get('batchQueue').length, 3);

    // Manifest hash must distinguish different id===0 hybrid tracks
    assert.notEqual(
      computeManifestHash([{ action: 'remove_hybrid_tracks', playlistId: 'hybrid:mix1', song: hTrack1 }]),
      computeManifestHash([{ action: 'remove_hybrid_tracks', playlistId: 'hybrid:mix1', song: hTrack2 }]),
    );

    // Unstaging one id===0 track must leave the other two staged
    state.unstageBatchAction('webdav:wd-3', 'hybrid:mix1', 'remove_hybrid_tracks');
    assert.equal(state.get('batchQueue').length, 2);

    // Committing remaining 2 hybrid tracks must remove ONLY those 2 from catalog and clear batchQueue completely
    let removedKeysSent = null;
    api.removeTracksFromHybridPlaylist = async (_pid, keys) => {
      removedKeysSent = keys;
      return { ok: true, data: { removed: keys.length } };
    };
    api.getPendingOperations = async () => ({ ok: true, data: { operations: [] } });
    cloudManager.reloadCatalog = async () => {};
    await cloudManager.executeConfirmedBatch();
    assert.deepEqual(removedKeysSent, ['local:/m/a.flac', 'opensubsonic:os-2']);
    assert.equal(state.get('batchQueue').length, 0);
    assert.equal(state.get('catalog').length, 1);
    assert.equal(state.get('catalog')[0].track_key, 'webdav:wd-3');
  } finally {
    globalThis.document = origBatchDoc;
    api.removeTracksFromHybridPlaylist = origRemoveHybrid;
    api.getPendingOperations = origGetPending;
    cloudManager.reloadCatalog = origReload;
    state.set(prevBatchState);
  }

  // 7. api.getLyrics supports canonical non-NetEase providers (OpenSubsonic / Resolver v2)
  const origReq = api.request;
  try {
    let calledUrl = '';
    api.request = async (u) => { calledUrl = u; return { ok: true, data: { lyrics: '[00:01.00]Hi' } }; };
    await api.getLyrics(0, null, {
      provider: 'opensubsonic',
      external_id: 'os-7',
      track_key: 'opensubsonic:os-7',
      title: 'SubSong',
      artist: 'SubArt',
    });
    assert.equal(calledUrl.includes('provider=opensubsonic'), true);
    assert.equal(calledUrl.includes('external_id=os-7'), true);
  } finally {
    api.request = origReq;
  }

  console.log('✓ Milestone 0 & Milestone 1 frontend upgrade tests passed!');
}
