import assert from 'node:assert/strict';
import { createMockDom } from './wave4_experience.test.js';
import { api } from '../../web/js/api.js';
import { auth } from '../../web/js/auth.js';
import { cloudManager } from '../../web/js/cloud.js';
import { historyManager } from '../../web/js/history.js';
import { lyricsManager } from '../../web/js/lyrics.js';
import { EQ_PRESETS, player } from '../../web/js/player.js';
import { normalizeMultiTrack } from '../../web/js/search_palette.js';
import { state } from '../../web/js/state.js';
import { toast } from '../../web/js/toast.js';
import { getTrackKey, serializeTrackForStorage } from '../../web/js/utils.js';

export async function runPhase2BTests() {
  const undo = [];
  function stub(obj, key, value) {
    const desc = Object.getOwnPropertyDescriptor(obj, key);
    undo.push(() => desc ? Object.defineProperty(obj, key, desc) : delete obj[key]);
    Object.defineProperty(obj, key, { configurable: true, writable: true, value });
  }
  const netease = { id: 42, title: 'Cloud' };
  const qq = normalizeMultiTrack({ provider: 'qq', external_id: '42', title: 'Other' });
  const migu = normalizeMultiTrack({ provider: 'migu', id: 42, title: 'Third' });
  const local = { id: 'file_1', local_path: 'D:/test.wav', title: 'Local', duration: 180000 };
  for (const [track, key] of [[netease, 'netease:42'], [qq, 'qq:42'], [migu, 'migu:42'], [local, 'local:D:/test.wav']]) {
    assert.equal(getTrackKey(track), key);
    assert.equal(getTrackKey(serializeTrackForStorage(track)), key);
  }
  assert.equal(getTrackKey({ ...local, track_key: 'netease:42' }), 'local:D:/test.wav');
  assert.equal(getTrackKey({ title: 'Legacy', track_key: 'legacy:key' }), 'legacy:key');
  assert.equal(getTrackKey({ title: 'Demo', artists: ['A'] }), 'track:Demo:A');

  const dom = createMockDom();
  try {
    stub(globalThis, 'document', dom.mockDoc);
    stub(globalThis, 'Node', dom.MockNode);
    stub(globalThis, 'window', { addEventListener() {} });
    for (const method of ['info', 'warning', 'quiet']) stub(toast, method, () => {});
    const events = new Map();
    stub(globalThis, 'EventSource', class {
      constructor(url) { assert.equal(url, '/api/events'); }
      addEventListener(k, fn) { events.set(k, fn); }
      close() {}
    });
    state.set({ authenticated: false, offlineMode: false, desktopLyricsOpen: false });
    cloudManager.disconnectEvents();
    cloudManager.connectEvents();
    assert.ok(cloudManager.eventSource);
    events.get('window_visibility')({ data: '{"visible":false,"minimized":true}' });
    assert.equal(state.get('windowMinimized'), true);
    assert.equal(state.get('windowVisible'), false);
    stub(auth, 'updateLoginShell', main => assert.ok(main));
    stub(api, 'selectOfflineAccount', async () => ({ok: true}));
    await auth.enterGuestLocalMode();
    assert.equal(state.get('activeOfflineAccountId'), 'local');
    assert.equal(state.get('guestLocalMode'), true);
    assert.equal(state.get('offlineMode'), true);

    const calls = [];
    stub(api, 'savePlaybackSession', async (payload, options) => { calls.push({ payload, options }); return { ok: true }; });
    stub(api, 'getPlaybackSession', async () => ({ ok: true, data: {
      account_id: 'local', current_track: local, queue: [qq, local, migu], queue_index: 1, position_sec: 72.25,
      duration_sec: 180, play_mode: 'loop', volume: 0.61, muted: true, playback_rate: 1.25,
    } }));
    const current = dom.registerId('player-time-current');
    const total = dom.registerId('player-time-total');
    await player.restorePlaybackSession();
    assert.equal(current.textContent, '01:12');
    assert.equal(total.textContent, '03:00');
    assert.equal(player.audio.paused, true);
    assert.equal(player.muted, true);
    assert.equal(player.targetVolume, 0.61);
    assert.equal(player.directAudio.volume, 0);
    assert.equal(player.audio.playbackRate, 1.25);
    assert.equal(state.get('playQueueIndex'), 1);
    assert.equal(state.get('playMode'), 'loop');
    assert.equal(state.get('resumableSession').current_track.local_path, local.local_path);
    state.replaceRecentPlayed([]);
    let resolved;
    stub(api, 'getLocalStream', () => new Promise(r => { resolved = r; }));
    const playing = player.togglePlay(true);
    assert.equal(state.get('recentPlayed').length, 0);
    resolved({ ok: true, data: { url: '/api/stream/local' } });
    await playing;
    assert.ok(player.audio.paused);
    player.audio.dispatchEvent('loadedmetadata');
    assert.equal(player.audio.currentTime, 72.25);
    assert.equal(player.audio.paused, false);
    assert.equal(state.get('recentPlayed')[0].local_path, local.local_path);

    const reports = [];
    stub(api, 'recordPlayback', async (song, ms, completed) => {
      reports.push({ song, ms, completed });
      return { ok: true };
    });
    player.listenAccumMs = 30000;
    player.maybeRecordGenuineListen();
    player.maybeRecordGenuineListen();
    player.listenAccumMs = 180000;
    player.maybeRecordGenuineListen(true);
    assert.deepEqual(reports.map(r => [r.ms, r.completed]), [[30000, false], [180000, true]]);
    assert.ok(reports[0].song.event_id);
    assert.equal(reports[1].song.event_id, reports[0].song.event_id);
    assert.equal(reports[0].song.track_key, getTrackKey(local));
    stub(api, 'getAudio', async () => ({ ok: false }));
    await player.playSong(qq);
    assert.equal(state.get('recentPlayed').length, 1);
    assert.notEqual(player.currentPlaybackEventId, reports[0].song.event_id);

    const { App } = await import('../../web/js/app.js');
    const app = new App();
    const list = dom.registerId('play-queue-list');
    state.set({ playQueue: [qq, local, migu], playQueueIndex: 1 });
    app.renderPlayQueueDrawer();
    assert.equal(list.querySelector('[data-queue-index="0"]').getAttribute('draggable'), 'true');
    const drag = new Map();
    const dataTransfer = { setData: (k, v) => drag.set(k, v), getData: k => drag.get(k) };
    list.querySelector('[data-queue-index="1"]').dispatchEvent({ type: 'dragstart', dataTransfer });
    list.querySelector('[data-queue-index="2"]').dispatchEvent({ type: 'drop', dataTransfer, preventDefault() {} });
    assert.equal(state.get('playQueueIndex'), 2);
    assert.deepEqual(state.get('playQueue').map(getTrackKey), [getTrackKey(qq), getTrackKey(migu), getTrackKey(local)]);
    app.queueTab = 'history';
    app.renderPlayQueueDrawer();
    assert.equal(list.children[0].tagName, 'BUTTON');
    assert.match(list.textContent, /Local/);
    await player.scheduleSavePlaybackSession(true, { keepalive: true });
    assert.ok(calls.at(-1).options.keepalive);
    assert.equal(calls.at(-1).payload.queue_index, 2);

    const history = dom.registerId('history-list');
    dom.registerId('history-filter').value = 'local';
    stub(api, 'getPlaybackHistory', async ({ q }) => {
      assert.equal(q, 'local');
      return { ok: true, data: { items: [] } };
    });
    await historyManager.load();
    assert.equal(history.dataset.state, 'no_match');
    stub(api, 'getPlaybackHistory', async () => ({ ok: false, error: { code: 'NETWORK_ERROR' } }));
    await historyManager.load();
    assert.equal(history.dataset.state, 'offline');
    assert.match(history.textContent, /Local/);
    assert.match(history.textContent, /本地/);
    stub(api, 'deletePlaybackHistory', async key => { assert.equal(key, getTrackKey(local)); return { ok: true }; });
    await historyManager.remove(local);
    assert.equal(state.get('recentPlayed').length, 0);

    stub(player, 'eqFilters', Array.from({ length: 10 }, () => ({ gain: {} })));
    player.applyEqualizerSettings({ eq_enabled: true, eq_preset: 'vocal' });
    assert.deepEqual(player.eqFilters.map(f => f.gain.value), EQ_PRESETS.vocal);
    player.applyEqualizerSettings({ eq_enabled: false });
    assert.ok(player.eqFilters.every(f => f.gain.value === 0));
    stub(api, 'saveSettings', async () => ({ ok: true }));
    player.setPlaybackRate(1.5);
    assert.equal(state.get('settings').playback_rate, 1.5);
    assert.equal(player.streamAudio.playbackRate, 1.5);
    player.setSleepTimer('15');
    assert.ok(state.get('sleepRemaining') >= 899);
    player.sleepDeadline = Date.now() - 100;
    player.updateSleepTimer();
    assert.equal(player.audio.paused, true);
    assert.equal(state.get('sleepRemaining'), 0);
    player.currentSong = netease;
    state.setSongLiked(42, true);
    state.set({ desktopLyricsOpen: true });
    lyricsManager.setSongOffset(0.5);
    let payload;
    stub(api, 'pushDesktopPlaybackState', async data => { payload = data; return { ok: true }; });
    player.pushDesktopLyricsState(true);
    assert.equal(payload.offset_sec, 0.5);
    assert.equal(payload.playback_rate, 1.5);
    assert.equal(payload.liked, true);
    assert.equal(payload.playback_seq, player.requestSequence);
    player.handleGlobalHotkeyAction('lyric_offset_minus');
    assert.equal(payload.offset_sec, 0);
    await player.sessionWriter;
  } finally {
    cloudManager.disconnectEvents();
    state.set({ desktopLyricsOpen: false });
    player.stopAndReset();
    undo.reverse().forEach(fn => fn());
  }
}
