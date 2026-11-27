import assert from 'node:assert/strict';
import { api } from '../../web/js/api.js';
import { player } from '../../web/js/player.js';
import { state } from '../../web/js/state.js';
import { cloudManager } from '../../web/js/cloud.js';
import { toast } from '../../web/js/toast.js';

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

export async function runPhase5Fixes() {
  const undo = [];
  function stub(object, key, value) {
    const desc = Object.getOwnPropertyDescriptor(object, key);
    undo.push(() => desc ? Object.defineProperty(object, key, desc) : delete object[key]);
    object[key] = value;
  }
  try {
    state.set({ user: { user_id: 42 }, sessionGeneration: 2 });
    const first = state.writeScope();
    state.set({ user: { user_id: 43 }, sessionGeneration: 3 });
    const requests = [];
    stub(api, 'request', async (url, options) => {
      requests.push(JSON.parse(options.body));
      return { ok: true };
    });
    await api.createPlaylist('old', [7], 0, first);
    await api.addTracksToPlaylist(1, [7], first);
    await api.removeTracks(1, [7], 1, first);
    await api.unlike([7], 1, first);
    await api.savePlaybackSession({ ...first, queue: [] });
    await api.recordPlayback({ ...first, id: 7 }, 16000);
    await api.deletePlaybackHistory('netease:7', first);
    await api.likeTrack(7, true);
    assert.equal(requests.length, 8);
    requests.forEach((body, i) => {
      assert.equal(body.expected_account_id, i === 7 ? '43' : '42');
      assert.equal(body.session_generation, i === 7 ? 3 : 2);
    });

    const session = deferred();
    stub(api, 'getPlaybackSession', () => session.promise);
    const p = Object.create(player);
    p.stopAndReset = () => {};
    p.scheduleSavePlaybackSession = () => {};
    const restore = p.restorePlaybackSession();
    state.set({ playQueue: [{ id: 33 }], playQueueIndex: 0 });
    session.resolve({ ok: true, data: { account_id: '43', queue: [{ id: 44 }] } });
    await restore;
    assert.deepEqual(state.get('playQueue'), [{ id: 33 }]);

    const pending = [deferred(), deferred()];
    const hits = [];
    stub(api, 'getAudio', id => { hits.push(id); return pending[hits.length - 1].promise; });
    stub(toast, 'error', () => {});
    p.updatePlayButtonUI = p.updateSourceBadgeUI = () => {};
    p.isLoadingTrack = false;
    function startError(id) {
      p.requestSequence = id;
      p.currentSong = { id };
      p.retriedCurrentTrack = false;
      p.activeSrc = '/api/stream/' + id;
      p.audio = { src: p.activeSrc, currentTime: 0 };
      return p.handleAudioElementError();
    }
    const oldError = startError(10);
    const newError = startError(11);
    assert.deepEqual(hits, [10, 11]);
    pending[0].resolve({ ok: false });
    await oldError;
    assert.equal(p._retryingAudioForSeq, 11);
    pending[1].resolve({ ok: false });
    await newError;
    assert.match(state.get('playbackError'), /音频流加载失败/);

    const events = new Map();
    const label = { textContent: '' };
    stub(globalThis, 'document', { getElementById: () => label });
    stub(globalThis, 'EventSource', class {
      addEventListener(k, fn) { events.set(k, fn); }
      close() {}
    });
    cloudManager.disconnectEvents();
    cloudManager.connectEvents();
    const send = (type, payload) => events.get(type)({ data: JSON.stringify(payload) });
    state.set({ syncProgress: null });
    for (const [uid, gen] of [['42', 3], ['43', 2]]) {
      const event = { account_id: uid, session_generation: gen, processed: 1, total: 2 };
      send('sync_progress', event);
      send('operation_progress', event);
      assert.equal(state.get('syncProgress'), null);
      assert.equal(label.textContent, '');
    }
    send('sync_progress', { account_id: '43', session_generation: 3, progress: .5 });
    assert.equal(state.get('syncProgress').progress, .5);
    cloudManager.disconnectEvents();
  } finally {
    for (const fn of undo.reverse()) fn();
  }
}
