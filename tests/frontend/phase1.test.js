import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { api } from '../../web/js/api.js';
import { cloudManager } from '../../web/js/cloud.js';
import { importManager } from '../../web/js/import.js';
import { player } from '../../web/js/player.js';
import { state } from '../../web/js/state.js';
import { toast } from '../../web/js/toast.js';

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

export async function runPhase1Tests() {
  const restore = [];
  function stub(target, key, value) {
    const original = target[key];
    restore.push(() => { target[key] = original; });
    target[key] = value;
  }
  const messages = [];
  for (const level of ['success', 'info', 'warning', 'error']) {
    stub(toast, level, (...args) => messages.push([level, ...args]));
  }
  const label = {};
  const replaceToggle = { checked: false };
  try {
    state.set({ authenticated: true, offlineMode: false, sessionGeneration: 17 });
    state.restorePendingOperations([]);
    const song = { id: 571, title: 'A', artists: [] };
    const requests = [];
    stub(globalThis, 'fetch', async (url, opts) => {
      requests.push([url, JSON.parse(opts.body)]);
      return new Response(JSON.stringify({ ok: true, data: {} }), { headers: { 'content-type': 'application/json' } });
    });
    await api.likeTrack(song.id);
    await api.importCatalog('csv', 'id,title');
    assert.equal(requests[0][1].session_generation, 17);
    assert.equal(requests[1][1].mode, 'merge');

    state.setSongLiked(song.id, false);
    stub(api, 'getPendingOperations', async () => ({ ok: true, data: { operations: [
      { action: 'like', operation_id: 'pending', target: { song_ids: [song.id] } },
    ] } }));
    stub(api, 'likeTrack', async () => ({ ok: true, data: { pending_operation_ids: ['pending'], unknown_items: [{ id: song.id }] } }));
    await player.toggleLikeCurrentSong(song);
    assert.equal(state.isSongLiked(song.id), false);
    assert.equal(state.get('destructiveFrozen'), true);
    assert.equal(messages.at(-1)[0], 'warning');
    state.restorePendingOperations([]);
    for (const data of [{}, { failed_items: [{ id: song.id }] }]) {
      api.likeTrack = async () => ({ ok: true, data });
      await player.toggleLikeCurrentSong(song);
      assert.equal(state.isSongLiked(song.id), false);
      assert.equal(messages.at(-1)[0], 'error');
    }
    stub(api, 'submitFeedback', async () => ({}));
    api.likeTrack = async () => ({ ok: true, data: { confirmed_ids: [String(song.id)] } });
    await player.toggleLikeCurrentSong(song);
    assert.equal(state.isSongLiked(song.id), true);
    assert.equal(messages.at(-1)[0], 'success');

    for (const [mode, method, refill] of [
      ['fm', 'getPersonalFm', 'refillPersonalFmQueue'],
      ['smart_radio', 'postSmartRadio', 'refillSmartRadioQueue'],
    ]) {
      for (const switchTo of [{ playMode: 'order' }, { user: { user_id: 22 } }, { playQueue: [{ id: 44 }] }]) {
        state.set({ playMode: mode, user: { user_id: 1 }, playQueue: [song] });
        const pending = deferred();
        stub(api, method, () => pending.promise);
        const refilling = player[refill]();
        state.set(switchTo);
        pending.resolve({ ok: true, data: { songs: [{ id: 100 }] } });
        await refilling;
        assert.ok(!state.get('playQueue').some(row => row.id === 100));
      }
    }

    const slowLyrics = deferred();
    const applied = [];
    stub(api, 'getLyrics', () => slowLyrics.promise);
    stub(api, 'getAudio', async (id, opts) => ({ ok: true, data: { url: '/api/stream/' + id + (opts?.force_refresh ? 'retry' : '') } }));
    stub(player, 'applyLyricsPayload', data => applied.push(data));
    const playing = player.playSong(song);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(player.audio.src, '/api/stream/571');
    assert.equal(player.audio.paused, false);
    await playing;
    player.lastSavedPosition = 71;
    await player.handleAudioElementError();
    const retriedEl = player.audio;
    assert.equal(typeof player._pendingRetrySeekCleanup, 'function');
    api.getLyrics = async () => ({ ok: false });
    await player.playSong({ ...song, id: 572 });
    slowLyrics.resolve({ ok: true, data: { lyrics: 'late' } });
    await Promise.resolve();
    retriedEl.currentTime = 4;
    retriedEl.duration = 200;
    retriedEl.dispatchEvent('loadedmetadata');
    assert.equal(retriedEl.currentTime, 4);
    assert.equal(player._pendingRetrySeekCleanup, null);
    assert.deepEqual(applied, []);
    player.stopAndReset();

    const html = readFileSync(new URL('../../web/index.html', import.meta.url), 'utf8');
    assert.equal(html.includes('动态视觉与省电模式'), false);
    assert.ok(html.includes('同源歌曲交叉淡化时长') && html.includes('500 ms'));
    stub(globalThis, 'document', { getElementById: id => id === 'import-replace' ? replaceToggle : label });
    stub(api, 'importCatalog', async payload => {
      assert.equal(payload.session_generation, state.get('sessionGeneration'));
      assert.equal(payload.mode, replaceToggle.checked ? 'replace' : 'merge');
      return { ok: true, data: { imported_count: 0, tracks: [song] } };
    });
    stub(api, 'getCatalog', async () => ({ok: true, data: {tracks: []}}));
    for (const checked of [false, true]) {
      messages.length = 0;
      replaceToggle.checked = checked;
      await importManager.handleFile({ name: 'tracks.json', text: async () => '[]' });
      assert.ok(messages.some(entry => entry[0] === 'success' && entry[1] === '导入完成'));
      assert.equal(replaceToggle.checked, false);
    }

    const handlers = new Map();
    stub(globalThis, 'EventSource', class {
      addEventListener(name, fn) { handlers.set(name, fn); }
      close() {}
    });
    cloudManager.disconnectEvents();
    cloudManager.connectEvents();
    handlers.get('operation_progress')({ data: JSON.stringify({ account_id: state.writeScope().expected_account_id, session_generation: state.get("sessionGeneration"), processed: 8, total: 11, succeeded: 6, cooldown_sec: 60 }) });
    assert.ok(label.textContent.includes('8/11'));
    assert.equal(messages.at(-1)[0], 'warning');
    cloudManager.disconnectEvents();

    const id = 'b'.repeat(32);
    const bytes = new Blob(['{"verified":true}']);
    stub(globalThis, 'fetch', async url => {
      assert.equal(url, '/api/operations/backup/' + id + '/download');
      return { ok: true, blob: async () => bytes };
    });
    assert.equal(await api.downloadBackup(id), bytes);
    stub(api, 'backup', async () => ({ ok: true, data: { backup_id: id } }));
    globalThis.fetch = async () => ({ ok: false });
    await cloudManager.triggerBackup();
    assert.equal(messages.at(-1)[0], 'error');
  } finally {
    for (const undo of restore.reverse()) undo();
    state.restorePendingOperations([]);
  }
}
