import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { api } from '../../web/js/api.js';
import { discoverManager } from '../../web/js/discover.js';
import { player } from '../../web/js/player.js';
import { state } from '../../web/js/state.js';
import { toast } from '../../web/js/toast.js';

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

export async function runPlaybackResumeFixesTests() {
  console.log('Testing cold-start playback resume, AudioContext sync unlock, and stream error recovery...');
  const savedState = state.get();
  const undo = [];
  const stub = (obj, key, value) => {
    const desc = Object.getOwnPropertyDescriptor(obj, key);
    undo.push(() => (desc ? Object.defineProperty(obj, key, desc) : delete obj[key]));
    Object.defineProperty(obj, key, { configurable: true, writable: true, value });
  };

  try {
    // 0. P0 invariant: ZERO `.src = ''` or `.src = ""` anywhere in web/js/player.js
    const playerSrc = fs.readFileSync(path.join(process.cwd(), 'web', 'js', 'player.js'), 'utf8');
    assert.equal(/\.src\s*=\s*['"]['"]/.test(playerSrc), false, 'player.js must never assign empty string to .src');

    stub(toast, 'info', () => {});
    stub(toast, 'warning', () => {});
    stub(toast, 'error', () => {});
    stub(toast, 'quiet', () => {});
    stub(api, 'getLyrics', async () => ({ ok: false, data: null, error: null }));
    stub(api, 'savePlaybackSession', async () => ({ ok: true }));

    // 1. Cold-start restorePlaybackSession + togglePlay unlocks AudioContext synchronously BEFORE await,
    //    preserves timeline during load, clamps out-of-range resumeSeekSec to 0 (no EOF auto-skip), and plays.
    const songA = { id: 1897964061, title: '雨不停。流', artists: ['孙盛希'], duration: 265935 };
    const songB = { id: 1897964062, title: 'Grand Blue', artists: ['湘南乃风'], duration: 313000 };
    state.set({ user: { user_id: 88 }, sessionGeneration: 1, authenticated: true, offlineMode: false });

    stub(api, 'getPlaybackSession', async () => ({
      ok: true,
      data: {
        account_id: '88',
        current_track: songA,
        queue: [songA, songB],
        queue_index: 0,
        position_sec: 221.76,
        duration_sec: 265.93,
        play_mode: 'order',
        volume: 0.4,
        muted: false,
        playback_rate: 1.0,
      },
    }));

    await player.restorePlaybackSession();
    assert.equal(player.pendingResumeSeekSec, 221.76);
    assert.equal(player.activeSrc, '');

    let syncUnlockCalls = 0;
    let audioFetchStarted = false;
    let unlockedBeforeFetch = false;
    const audioGate = deferred();
    stub(player, 'unlockAudioContextSync', () => {
      syncUnlockCalls += 1;
      if (!audioFetchStarted) unlockedBeforeFetch = true;
    });
    stub(api, 'getAudio', (id, opts) => {
      audioFetchStarted = true;
      assert.equal(id, 1897964061);
      assert.equal(opts.title, '雨不停。流');
      return audioGate.promise;
    });

    const playPromise = player.togglePlay();
    assert.ok(syncUnlockCalls >= 1, 'togglePlay must unlock AudioContext synchronously');
    assert.equal(unlockedBeforeFetch, true, 'AudioContext unlock must occur BEFORE async getAudio');
    assert.equal(state.get('currentTime'), 221.76, 'Play start must not flash timeline back to 00:00');

    // Explicit togglePlay(true) (e.g. #discover-resume click) during in-flight load MUST NOT cancel playback!
    player.togglePlay(true);
    assert.equal(player.userPausedSequence, -1, 'Explicit togglePlay(true) while loading must not pause');

    audioGate.resolve({ ok: true, data: { url: '/api/stream/resume_tok_1', format: 'flac', level: 'jyeffect' } });
    await playPromise;

    // Simulate a 30s stream (shorter than 221.76s saved position): must fall back to 0 instead of seeking past EOF!
    player.audio.duration = 30.0;
    player.audio.currentTime = 0;
    player.audio.dispatchEvent('loadedmetadata');
    assert.equal(player.audio.currentTime, 0, 'Out-of-range resumeSeekSec >= duration must restart cleanly from 0');
    assert.equal(player.audio.paused, false, 'Audio must start playing after clamped resume seek');

    // 2. Normal in-range resumeSeekSec (e.g. 221.76s on a 266s track) seeks accurately and plays
    player.stopAndReset();
    state.set({ user: { user_id: 88 }, sessionGeneration: 1, authenticated: true, offlineMode: false });
    await player.restorePlaybackSession();
    stub(api, 'getAudio', async () => ({
      ok: true,
      data: { url: '/api/stream/resume_tok_2', format: 'flac', level: 'jyeffect' },
    }));
    await player.togglePlay(true);
    player.audio.duration = 266.0;
    player.audio.dispatchEvent('loadedmetadata');
    assert.equal(player.audio.currentTime, 221.76);
    assert.equal(player.audio.paused, false);

    // 3. Deadlock regression test: after stream error + failed retry, element is cleanly reset,
    //    `paused: false` with `error != null` is NOT treated as playing, and next `togglePlay()` recovers!
    player.retriedCurrentTrack = false;
    player.lastSavedPosition = 42.5;
    player.audio.paused = false;
    player.audio.error = { code: 2, message: 'PIPELINE_ERROR_READ' };
    stub(api, 'getAudio', async () => ({
      ok: false,
      status: 502,
      error: { code: 'STREAM_UPSTREAM_FORBIDDEN', message: '外部音频流链接已过期' },
    }));

    await player.handleAudioElementError();
    assert.equal(player.activeSrc, '', 'Final stream error must clear activeSrc so togglePlay can recover');
    assert.equal(player.audio.getAttribute('src'), null, 'Final stream error must removeAttribute("src")');
    assert.equal(player.pendingResumeSeekSec, 42.5, 'Final stream error must preserve resume position for retry');
    assert.match(state.get('playbackError'), /音频流加载失败/);

    // Simulate Chromium quirk where errored element still reports paused=false + error!=null
    player.audio.paused = false;
    player.audio.error = { code: 2, message: 'PIPELINE_ERROR_READ' };
    let recoveryCalls = 0;
    stub(api, 'getAudio', async () => {
      recoveryCalls += 1;
      return { ok: true, data: { url: '/api/stream/recovered_tok', format: 'flac', level: 'lossless' } };
    });

    const recoverPromise = player.togglePlay();
    player.audio.error = null;
    await recoverPromise;
    assert.equal(recoveryCalls, 1, 'Clicking Play after stream error must re-fetch instead of deadlocking');
    player.audio.duration = 200;
    player.audio.dispatchEvent('loadedmetadata');
    assert.equal(player.audio.currentTime, 42.5);
    assert.equal(player.audio.paused, false);

    // 4. loadedmetadata play() NotAllowedError is NOT silently swallowed
    player.stopAndReset();
    state.set({ user: { user_id: 88 }, sessionGeneration: 1, authenticated: true, offlineMode: false });
    await player.restorePlaybackSession();
    const origPlay = player.directAudio.play;
    let notAllowedTriggered = false;
    player.directAudio.play = async () => {
      notAllowedTriggered = true;
      const err = new Error('play() failed because user did not interact');
      err.name = 'NotAllowedError';
      throw err;
    };
    try {
      stub(api, 'getAudio', async () => ({
        ok: true,
        data: { url: '/api/stream/not_allowed_tok', format: 'mp3', level: 'exhigh' },
      }));
      await player.togglePlay(true);
      player.audio.duration = 266;
      player.audio.dispatchEvent('loadedmetadata');
      await new Promise((r) => setTimeout(r, 10));
      assert.equal(notAllowedTriggered, true);
      assert.equal(state.get('isPlaying'), false);
      assert.match(state.get('playbackError'), /自动播放/);
    } finally {
      player.directAudio.play = origPlay;
    }

    // 5. Transparent single retry on transient 409 SESSION_CHANGED during startup hydration
    player.stopAndReset();
    let sessionAttempts = 0;
    stub(api, 'getAudio', async () => {
      sessionAttempts += 1;
      if (sessionAttempts === 1) {
        return { ok: false, status: 409, error: { code: 'SESSION_CHANGED', message: '登录会话已变更，请重试' } };
      }
      return { ok: true, data: { url: '/api/stream/after_409_tok', format: 'flac', level: 'jymaster' } };
    });
    await player.playSong(songA, true);
    assert.equal(sessionAttempts, 2, 'playSong must retry once transparently on 409 SESSION_CHANGED');
    assert.equal(player.activeSrc, '/api/stream/after_409_tok');
    assert.equal(player.audio.paused, false);

    // 6. Null-safe #discover-resume banner when resumableSession has null current_track
    const resumeBtn = {
      hidden: true,
      textContent: '',
      addEventListener() {},
    };
    stub(globalThis, 'document', {
      getElementById: (id) => (id === 'discover-resume' ? resumeBtn : null),
    });
    discoverManager.init('view-discover');
    state.set({ resumableSession: { current_track: null, position_sec: 15 } });
    assert.equal(resumeBtn.hidden, false);
    assert.match(resumeBtn.textContent, /继续上次播放/);
    state.set({ resumableSession: null });
    assert.equal(resumeBtn.hidden, true);

    // 7. Regression: play() rejection arriving while handleAudioElementError() retry is in flight
    //    must NOT reset the element, clear activeSrc, or prematurely emit playbackError.
    player.stopAndReset();
    state.set({ user: { user_id: 88 }, sessionGeneration: 1, authenticated: true, offlineMode: false });
    const retryGate = deferred();
    let getAudioCalls = 0;
    stub(api, 'getAudio', (id, opts) => {
      getAudioCalls += 1;
      if (!opts?.force_refresh) {
        return Promise.resolve({ ok: true, data: { url: '/api/stream/fail_503_tok', format: 'flac', level: 'jyeffect' } });
      }
      return retryGate.promise;
    });
    let rejectInitialPlay;
    const origDirectPlay2 = player.directAudio.play;
    let directPlayAttempt = 0;
    player.directAudio.play = function () {
      directPlayAttempt += 1;
      if (directPlayAttempt === 1) {
        return new Promise((_res, rej) => { rejectInitialPlay = rej; });
      }
      this.paused = false;
      return Promise.resolve();
    };
    try {
      await player.playSong(songA, true);
      const seqDuringRetry = player.requestSequence;
      player.audio.error = { code: 4, message: 'MEDIA_ERR_SRC_NOT_SUPPORTED' };
      const retryPromise = player.handleAudioElementError();
      assert.equal(player._retryingAudioForSeq, seqDuringRetry);
      assert.equal(player.retriedCurrentTrack, true);

      // Now the initial el.play() rejects while retry is still awaiting force_refresh!
      const playErr = new Error('Failed to load because no supported source was found');
      playErr.name = 'NotSupportedError';
      rejectInitialPlay(playErr);
      await new Promise((r) => setTimeout(r, 10));

      // Must NOT have clobbered activeSrc, src attribute, or playbackError mid-retry
      assert.equal(player.activeSrc, '/api/stream/fail_503_tok', 'In-flight retry must own activeSrc');
      assert.equal(player.audio.getAttribute('src'), '/api/stream/fail_503_tok', 'In-flight retry must not strip src mid-retry');
      assert.equal(state.get('playbackError'), null, 'In-flight retry must not prematurely publish playbackError');

      // Now retry resolves with fresh stream token -> must transition cleanly into playing!
      player.audio.error = null;
      retryGate.resolve({ ok: true, data: { url: '/api/stream/fresh_ok_tok', format: 'flac', level: 'jyeffect' } });
      await retryPromise;
      assert.equal(getAudioCalls, 2);
      assert.equal(player.activeSrc, '/api/stream/fresh_ok_tok');
      assert.equal(player.audio.paused, false);
      assert.equal(state.get('playbackError'), null);
    } finally {
      player.directAudio.play = origDirectPlay2;
      player.audio.error = null;
    }

    // 8. Regression: errored prefetched standbyAudio swapped in by reserveOutgoing MUST be reset and reloaded
    player.stopAndReset();
    const erroredUrl = '/api/stream/prefetched_502_tok';
    player.standbyAudio.src = erroredUrl;
    player.standbyAudio.error = { code: 2, message: 'PIPELINE_ERROR_READ' };
    player.streamAudio.src = '/api/stream/outgoing_ok_tok';
    player.streamAudio.paused = false;
    player.audio = player.streamAudio;
    player.activeSrc = '/api/stream/outgoing_ok_tok';
    player.activeUsesWebAudio = true;
    const origPrepare = player.prepareAudioElementForUrl;
    const origReserve = player.reserveOutgoing;
    let resetCalledOnSwapped = 0;
    const origReset = player.resetMediaElement;
    player.resetMediaElement = function (el) {
      if (el === player.audio && el.error) {
        resetCalledOnSwapped += 1;
        el.error = null;
      }
      return origReset.call(this, el);
    };
    player.prepareAudioElementForUrl = function () {
      // Keep the swapped streamAudio (previously errored standbyAudio) active
      this.audio = this.streamAudio;
      return this.audio;
    };
    player.reserveOutgoing = function () {
      const outgoing = this.streamAudio;
      this.streamAudio = this.standbyAudio;
      this.standbyAudio = outgoing;
      this.audio = this.streamAudio;
      this.fade = { outgoing, started: false, timer: null };
    };
    stub(api, 'getAudio', async () => ({ ok: true, data: { url: erroredUrl, format: 'flac', level: 'jyeffect' } }));
    try {
      await player.playSong(songB, true);
      assert.equal(resetCalledOnSwapped, 1, 'Errored standbyAudio swapped in by crossfade must be reset before reloading');
      assert.equal(player.audio.getAttribute('src'), erroredUrl, 'Reloaded audio element must re-attach src');
      assert.equal(player.audio.paused, false);
    } finally {
      player.prepareAudioElementForUrl = origPrepare;
      player.reserveOutgoing = origReserve;
      player.resetMediaElement = origReset;
      player.standbyAudio.error = null;
      player.streamAudio.error = null;
    }
  } finally {
    player.stopAndReset();
    while (undo.length) undo.pop()();
    state.set(savedState);
  }
}
