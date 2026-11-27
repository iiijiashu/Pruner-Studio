import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { createMockDom } from './wave4_experience.test.js';
import { api } from '../../web/js/api.js';
import { auth } from '../../web/js/auth.js';
import { catalogManager } from '../../web/js/catalog.js';
import { discoverManager } from '../../web/js/discover.js';
import { player, volumeToGain } from '../../web/js/player.js';
import { RuleEngine } from '../../web/js/rules.js';
import { stageManager } from '../../web/js/stage.js';
import { state } from '../../web/js/state.js';
import { toast } from '../../web/js/toast.js';
import { durationSeconds, getTrackKey, writableNeteaseTrack } from '../../web/js/utils.js';
import { App } from '../../web/js/app.js';

export async function runPhase7fAndPhase8Tests() {
  const undo = [];
  const stub = (obj, key, val) => {
    const desc = Object.getOwnPropertyDescriptor(obj, key);
    undo.push(() => (desc ? Object.defineProperty(obj, key, desc) : delete obj[key]));
    Object.defineProperty(obj, key, { configurable: true, writable: true, value: val });
  };

  try {
    // 1. [P1] writableNeteaseTrack, getTrackKey & toggleLikeCurrentSong with stale netease: track_key
    const staleLocal = { id: 123, local_path: 'C:/Music/demo.flac', track_key: 'netease:123', title: 'Local' };
    const staleExt = { id: 123, provider: 'qq', external_id: 'qq_1', track_key: 'netease:123', title: 'QQ' };
    const staleSrc = { id: 123, source: 'qq', external_id: 'qq_2', track_key: 'netease:123', title: 'QQ2' };
    const mismatched = { id: 123, track_key: 'netease:999', title: 'Mismatch' };

    assert.equal(writableNeteaseTrack(staleLocal), false);
    assert.equal(writableNeteaseTrack(staleExt), false);
    assert.equal(writableNeteaseTrack(staleSrc), false);
    assert.equal(writableNeteaseTrack(mismatched), false);
    assert.equal(writableNeteaseTrack({ id: true }), false);
    assert.equal(writableNeteaseTrack({ id: 1.5 }), false);
    assert.equal(writableNeteaseTrack({ id: 123, source: 'netease' }), true);

    assert.equal(getTrackKey(staleLocal), 'local:C:/Music/demo.flac');
    assert.equal(getTrackKey(staleExt), 'qq:qq_1');
    assert.equal(getTrackKey(staleSrc), 'qq:qq_2');
    assert.equal(getTrackKey(mismatched), 'netease:123');

    let likeCalls = 0;
    stub(api, 'likeTrack', async () => { likeCalls++; return { ok: true }; });
    stub(toast, 'info', () => {});
    stub(toast, 'warning', () => {});
    stub(toast, 'success', () => {});
    stub(toast, 'error', () => {});
    state.set({ authenticated: true, offlineMode: false });
    await player.toggleLikeCurrentSong(staleLocal);
    await player.toggleLikeCurrentSong(staleExt);
    assert.equal(likeCalls, 0);

    // 2. [P2] auth.checkSession restores explicit offline account view on reload
    const emittedOffline = [];
    let shellShown = null;
    stub(auth, 'updateUserUI', () => {});
    stub(auth, 'updateLoginShell', v => { shellShown = v; });
    stub(auth, 'renderOfflineEntryPanel', () => {});
    stub(state, 'emit', (ev, payload) => { if (ev === 'auth:offline') emittedOffline.push(payload); });
    stub(api, 'getStatus', async () => ({
      ok: true,
      data: {
        authenticated: false,
        offline: true,
        active_account_id: '17',
        session_generation: 9,
        cached_accounts: [{ account_id: '17', display_name: '离线测试17', track_count: 5 }],
      },
    }));
    const isAuth = await auth.checkSession();
    assert.equal(isAuth, false);
    assert.equal(state.get('offlineMode'), true);
    assert.equal(state.get('activeOfflineAccountId'), '17');
    assert.equal(state.get('user').display_name, '离线测试17');
    assert.equal(shellShown, true);
    assert.deepEqual(emittedOffline, [{ accountId: '17', restored: true }]);

    // Without explicit offline, stays on login shell
    stub(api, 'getStatus', async () => ({
      ok: true,
      data: { authenticated: false, offline: false, active_account_id: null, session_generation: 10, cached_accounts: [] },
    }));
    await auth.checkSession();
    assert.equal(state.get('offlineMode'), false);
    assert.equal(shellShown, false);

    // 3. [P2] createInBatches incomplete/409 receipts & 1,002-track count split
    stub(api, 'createPlaylist', async () => ({ ok: true, data: {} }));
    const emptyReceipt = await catalogManager.createInBatches('Empty', [1, 2], {});
    assert.equal(emptyReceipt.ok, false);
    assert.equal(emptyReceipt.ambiguous, true);

    stub(api, 'createPlaylist', async (_n, ids) => ({
      ok: true,
      data: { playlist_id: 77, successful_ids: ids },
    }));
    stub(api, 'addTracksToPlaylist', async () => ({
      ok: false,
      status: 409,
      error: { code: 'OPERATION_PENDING', message: '存在待处理操作' },
      data: { pending_operation_ids: ['op_1'] },
    }));
    const pendingRes = await catalogManager.createInBatches('Pending', Array.from({ length: 505 }, (_, i) => i + 1), {});
    assert.equal(pendingRes.ok, false);
    assert.equal(pendingRes.ambiguous, true);
    assert.equal(pendingRes.confirmedIds.length, 500);
    assert.equal(pendingRes.unknownItems.length, 5);
    assert.equal(pendingRes.unsentIds.length, 0);

    // 1,002 tracks: batch 0 (500) ok, batch 1 (500) network error, 2 unsent
    const ids1002 = Array.from({ length: 1002 }, (_, i) => i + 1);
    stub(api, 'addTracksToPlaylist', async () => ({
      ok: false,
      status: 0,
      error: { code: 'NETWORK_ERROR', message: '连接断开' },
      data: null,
    }));
    let createClick = null;
    const dom = createMockDom();
    const submitBtn = { disabled: false, addEventListener(t, fn) { if (t === 'click') createClick = fn; } };
    const nameInput = { value: '1002歌单' };
    stub(globalThis, 'document', {
      ...dom.mockDoc,
      getElementById(id) {
        if (id === 'create-playlist-submit-btn') return submitBtn;
        if (id === 'create-playlist-name-input') return nameInput;
        return dom.mockDoc.getElementById(id);
      },
    });
    catalogManager.createPlaylistIds = ids1002;
    catalogManager.createPlaylistScope = {};
    stub(api, 'getPendingOperations', async () => ({ ok: true, data: { operations: [] } }));
    stub(catalogManager, 'render', () => {});
    const { cloudManager } = await import('../../web/js/cloud.js');
    const { modal } = await import('../../web/js/modal.js');
    stub(cloudManager, 'loadPlaylists', async () => {});
    stub(modal, 'close', () => {});
    let warnMsg = '';
    stub(toast, 'warning', (_t, m) => { warnMsg = m; });
    new App().initModalsAndForms();
    await createClick();
    catalogManager.createPlaylistIds = null;
    assert.match(warnMsg, /已确认添加 500 首/);
    assert.match(warnMsg, /结果未知 500 首/);
    assert.match(warnMsg, /尚未发送 2 首/);

    // 4. [P2] requestRadio('refill') reclaims on network reject
    player.resetRadioSession();
    state.set({ playQueue: [{ id: 1, title: 'Seed' }], playQueueIndex: 0, playMode: 'smart_radio', user: { user_id: 7 } });
    stub(player, 'scheduleSavePlaybackSession', () => {});
    stub(api, 'postSmartRadio', async () => { throw new Error('network drop'); });
    const refillOk = await player.requestRadio('refill');
    assert.equal(refillOk, false);
    assert.equal(player.radioSession.supersededRequestIds.size, 1);
    const lostReqId = [...player.radioSession.supersededRequestIds][0];
    let seenSuperseded = null;
    stub(api, 'postSmartRadio', async payload => {
      seenSuperseded = payload.superseded_request_ids;
      return { ok: true, data: { ...payload, songs: [{ id: 2, title: 'Next' }], exhausted: false, breakdown: {} } };
    });
    await player.requestRadio('rearrange');
    assert.deepEqual(seenSuperseded, [lostReqId]);
    assert.equal(player.radioSession.supersededRequestIds.size, 0);

    // 5. [P2] loadLocalSongs unified local:<path> key & durationSeconds source provenance
    stub(api, 'getLocalLibrary', async () => ({
      ok: true,
      data: { files: [{ title: 'FLAC', path: 'C:/Music/test.flac', duration_ms: 180000 }], has_more: false },
    }));
    await catalogManager.loadLocalSongs(true);
    assert.equal(getTrackKey(state.get('localSongs')[0]), 'local:C:/Music/test.flac');
    assert.equal(durationSeconds({ source: 'netease', duration: 3000 }), 3);
    assert.equal(durationSeconds({ source: 'local', duration: 3000 }), 3);
    assert.equal(durationSeconds({ source: 'netease', duration: 3000, duration_unit: 'sec' }), 3000);

    // 6. [P3] Shared rule_validation.json parity (0.00001, true, surrogate escapes)
    const ruleCases = JSON.parse(fs.readFileSync('tests/rule_validation.json', 'utf8'));
    for (const [field, operator, value, expected] of ruleCases) {
      assert.equal(
        RuleEngine.validClause({ field, operator, value }),
        expected,
        `Rule parity mismatch for ${JSON.stringify({ field, operator, value })}`,
      );
    }

    // 7. [Phase 8] Perceptual volume, adjustVolume, hotkeys, game_mode, FM checkbox & CSS check
    assert.equal(volumeToGain(0), 0);
    assert.equal(volumeToGain(0.5), 0.25);
    assert.equal(volumeToGain(1), 1);

    state.set({ desktopLyricsOpen: true, desktopLyricsLocked: false });
    const pushedPayloads = [];
    stub(api, 'pushDesktopPlaybackState', async p => { pushedPayloads.push(p); return { ok: true }; });
    player.setVolume(0.5, false, { showOsd: true });
    assert.equal(player.targetVolume, 0.5);
    assert.equal(player.directAudio.volume, 0.25);
    assert.equal(pushedPayloads.at(-1).osd.text, '音量 50%');

    player.handleGlobalHotkeyAction('volume_up');
    assert.equal(player.targetVolume, 0.55);
    assert.equal(pushedPayloads.at(-1).osd.text, '音量 55%');
    player.handleGlobalHotkeyAction('toggle_mute');
    assert.equal(player.muted, true);
    assert.equal(pushedPayloads.at(-1).osd.text, '已静音');

    // Stage game_mode pause + offscreen mesh release
    const stage = new stageManager.constructor();
    state.set({ settings: { visual_mode: 'soft_dynamic', gpu_performance_mode: 'balanced', game_mode: true }, isPlaying: true, windowVisible: true, windowMinimized: false, isStageOpen: true });
    assert.equal(stage.shouldPauseAmbient(), true);
    const fakeMesh = { width: 160, height: 160 };
    stage.mesh = fakeMesh;
    stage.outputCtx = {};
    stage.fluidCtx = {};
    state.set({ windowMinimized: true });
    stage.syncAmbientPauseState();
    assert.equal(fakeMesh.width, 0);
    assert.equal(stage.mesh, null);

    // Discover FM card has zero raw checkboxes; index.html retains #settings-fm-sync & #settings-game-mode-toggle
    const fmHost = dom.registerId('discover-personal-fm-card');
    state.set({ personalFmQueue: [{ id: 8, title: 'FM', artists: ['Singer'] }] });
    discoverManager.renderFmCard();
    const hasCheckbox = (node) => {
      if (!node || typeof node !== 'object') return false;
      if (node.tagName === 'INPUT' && node.attributes?.type === 'checkbox') return true;
      return Array.isArray(node.children) && node.children.some(hasCheckbox);
    };
    assert.equal(hasCheckbox(fmHost), false);

    const indexHtml = fs.readFileSync('web/index.html', 'utf8');
    assert.ok(indexHtml.includes('id="settings-fm-sync"'));
    assert.ok(indexHtml.includes('id="settings-game-mode-toggle"'));
    assert.ok(indexHtml.includes('id="stage-customize-popover"'));

    // Zero 10px/11px font-size across web/css/*.css
    for (const file of fs.readdirSync('web/css')) {
      if (!file.endsWith('.css')) continue;
      const cssText = fs.readFileSync(`web/css/${file}`, 'utf8');
      assert.doesNotMatch(cssText, /font-size:\s*(?:10|11)px\b/, `Legacy 10/11px font found in web/css/${file}`);
    }

    // Desktop lyrics OSD & volume VM test
    const dlHtml = fs.readFileSync('web/desktop_lyrics.html', 'utf8');
    const dlEls = new Map();
    const dlEvents = new Map();
    const dlEl = (id) => {
      if (dlEls.has(id)) return dlEls.get(id);
      const node = {
        style: { setProperty(k, v) { this[k] = v; } },
        dataset: {},
        parentElement: {},
        classList: {
          set: new Set(),
          add(c) { this.set.add(c); },
          remove(c) { this.set.delete(c); },
          toggle(c, force) { if (force === undefined) force = !this.set.has(c); if (force) this.set.add(c); else this.set.delete(c); },
          contains(c) { return this.set.has(c); },
        },
        children: [],
        appendChild(n) { this.children.push(n); },
        setAttribute(k, v) { this[k] = v; },
        addEventListener(k, fn) { this[k] = fn; },
      };
      dlEls.set(id, node);
      return node;
    };
    const dlPage = {
      performance: { now: () => 0 },
      window: { close() {} },
      document: {
        getElementById: dlEl,
        documentElement: dlEl('root'),
        addEventListener() {},
        createElement: () => dlEl(Symbol()),
        createElementNS: () => dlEl(Symbol()),
      },
      EventSource: class { addEventListener(k, fn) { dlEvents.set(k, fn); } },
      fetch: async () => ({ ok: true, json: async () => ({ ok: true, data: {} }) }),
    };
    vm.runInNewContext(dlHtml.match(/<script>([\s\S]*?)<\/script>/)[1], dlPage);
    dlEvents.get('desktop_lyrics')({
      data: JSON.stringify({
        playback_seq: 1,
        game_mode: true,
        volume: 0.45,
        muted: false,
        osd: { text: '音量 45%', progress: 0.45, kind: 'volume' },
      }),
    });
    assert.equal(dlEl('dl-shell').dataset.gameMode, 'true');
    assert.equal(dlEl('dl-volume-slider').value, '45');
    assert.equal(dlEl('dl-osd-text').textContent, '音量 45%');
    assert.equal(dlEl('dl-osd-bar').style.width, '45%');
    assert.equal(dlEl('dl-osd-track').classList.contains('hidden'), false);
    dlEvents.get('desktop_lyrics')({
      data: JSON.stringify({
        playback_seq: 1,
        osd: { text: '已解锁', kind: 'lock' },
      }),
    });
    assert.equal(dlEl('dl-osd-track').classList.contains('hidden'), true);
  } finally {
    while (undo.length) undo.pop()();
  }
}
