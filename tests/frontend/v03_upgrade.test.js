/**
 * Pruner Studio Frontend Tests - v0.3 Full Upgrade Verification Suite
 * Covers:
 * 1. Discrete cover resizing (?param=64/96/192/384), ms->sec normalization, real pixel palette quantization
 * 2. YRC word-by-word lyrics parser + romaji/translation + single visible container rendering
 * 3. Precompiled regex caching in RuleEngine
 * 4. Global AudioPlayer: parallel audio+lyrics + audio_id alignment + /api/stream/{token} + chorus ms->sec jump
 *    + single transparent retry preserving position + honest quality/source badges + FM auto-refill + logout reset
 * 5. Deck: 'L' relisten-later middle state & filter, e.repeat / IME e.isComposing keyboard guard,
 *    soft-commit cancellation through backup wait, session_generation verification, subscribed/cloud/local read-only guard
 * 6. Cloud: frozen batch confirmation manifest (exact items only) + session_generation guard + per-group manifest_hash
 * 7. Catalog: O(1) incremental checkbox selection (zero whole-catalog re-filter on toggle)
 * 8. Offline read-only catalog entry via cached_accounts
 * 9. Immersive stage bounded ambient mount/unmount & pause lifecycle + zero full-screen blur(100px) in base.css
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

if (typeof globalThis.window === 'undefined') {
  globalThis.window = {
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => {},
  };
}
if (typeof globalThis.navigator === 'undefined') {
  globalThis.navigator = { onLine: true };
}
if (typeof globalThis.localStorage === 'undefined') {
  const store = new Map();
  globalThis.localStorage = {
    getItem: (key) => store.get(key) || null,
    setItem: (key, val) => store.set(key, String(val)),
    removeItem: (key) => store.delete(key),
    clear: () => store.clear(),
  };
}

import { api } from '../../web/js/api.js';
import { auth } from '../../web/js/auth.js';
import { state } from '../../web/js/state.js';
import { player, formatHonestAudioBadge, isPlayableAudioUrl, isSameOriginStreamUrl } from '../../web/js/player.js';
import { LyricsEngine, lyricsManager } from '../../web/js/lyrics.js';
import { RuleEngine } from '../../web/js/rules.js';
import { deckManager } from '../../web/js/deck.js';
import { catalogManager } from '../../web/js/catalog.js';
import { cloudManager, CHANNEL_CATALOG } from '../../web/js/cloud.js';
import { discoverManager } from '../../web/js/discover.js';
import { searchPalette, SEARCH_CHANNEL_CHIPS } from '../../web/js/search_palette.js';
import { App } from '../../web/js/app.js';
import { stageManager } from '../../web/js/stage.js';
import { createIcon, ICON_NAMES } from '../../web/js/icons.js';
import {
  getSizedCoverUrl,
  normalizeTimeToSeconds,
  quantizePixelsToPalette,
  extractQuadrantHsls,
  balancedShuffleFallback,
  computeManifestHash,
} from '../../web/js/utils.js';

export async function runV03UpgradeTests() {
  console.log('Testing v0.3 upgraded architecture & safety contracts...');

  // ------------------------------------------------------------------------
  // 1. Discrete cover resizing, ms->sec normalization & real pixel quantization
  // ------------------------------------------------------------------------
  assert.equal(
    getSizedCoverUrl('http://p1.music.126.net/abc/123.jpg', 96),
    'https://p1.music.126.net/abc/123.jpg?param=96y96',
    '126.net URL must upgrade to https and append discrete ?param=96y96',
  );
  assert.equal(
    getSizedCoverUrl('https://p2.music.126.net/xyz.jpg?param=1000y1000', 180),
    'https://p2.music.126.net/xyz.jpg?param=192y192',
    '126.net URL must clamp to discrete 192y192 bucket',
  );
  assert.equal(
    getSizedCoverUrl('https://p2.music.126.net/xyz.jpg', 900),
    'https://p2.music.126.net/xyz.jpg?param=384y384',
    '126.net URL must cap at 384y384',
  );
  assert.equal(
    getSizedCoverUrl('https://cdn.example.com/art.png', 192),
    'https://cdn.example.com/art.png',
    'Non-126.net safe URL must remain unchanged',
  );
  assert.ok(
    getSizedCoverUrl('javascript:alert(1)', 96).startsWith('data:image/svg+xml'),
    'Unsafe cover URL must return safe SVG placeholder',
  );

  // ms-vs-sec normalization
  assert.equal(normalizeTimeToSeconds(52000, 210), 52, '52000ms must normalize to 52s');
  assert.equal(normalizeTimeToSeconds(8000, 210), 8, '8000ms with 210s track must normalize to 8s');
  assert.equal(normalizeTimeToSeconds(52, 210), 52, '52s must stay 52s');
  assert.equal(normalizeTimeToSeconds(240000, 0), 240, '240000ms without duration must normalize to 240s');

  // Real pixel quantization (bright teal + warm coral RGBA buffer)
  const pixels = new Uint8ClampedArray([
    30, 180, 190, 255,
    35, 175, 185, 255,
    210, 110, 90, 255,
    10, 10, 10, 255, // near-black ignored
  ]);
  const palette = quantizePixelsToPalette(pixels);
  assert.ok(palette && palette.accent.startsWith('hsl('), 'Must produce normalized HSL primary accent');
  assert.ok(palette.accentSubtle.startsWith('hsla('), 'Must produce subtle alpha accent');
  assert.ok(palette.accent2.startsWith('hsl('), 'Must produce secondary accent');

  // ------------------------------------------------------------------------
  // 2. YRC word-by-word lyrics parser + romaji + translation
  // ------------------------------------------------------------------------
  const sampleYrc = [
    '{"t":0,"c":[{"tx":"作词: 测试"}]}',
    '[1000,3000](1000,1000,0)你(2000,1000,0)好(3000,1000,0)世界',
    '[5000,2000](5000,1000,0)晚(6000,1000,0)安',
  ].join('\n');
  const sampleTrans = '[00:01.00]Hello World\n[00:05.00]Good Night';
  const sampleRoma = '[00:01.00]ni hao shi jie\n[00:05.00]wan an';

  const parsedYrc = LyricsEngine.parseYrc(sampleYrc, sampleTrans, sampleRoma);
  assert.equal(parsedYrc.length, 2, 'Should parse 2 YRC lines and skip JSON credit line');
  assert.equal(parsedYrc[0].time, 1);
  assert.equal(parsedYrc[0].text, '你好世界');
  assert.equal(parsedYrc[0].words.length, 3);
  assert.equal(parsedYrc[0].words[1].text, '好');
  assert.equal(parsedYrc[0].translation, 'Hello World');
  assert.equal(parsedYrc[0].romaji, 'ni hao shi jie');
  assert.equal(LyricsEngine.computeWordProgress(parsedYrc[0].words[0], 1.5), 0.5);
  assert.equal(LyricsEngine.computeWordProgress(parsedYrc[0].words[0], 2.5), 1);
  assert.equal(LyricsEngine.computeWordProgress(parsedYrc[0].words[0], 0.5), 0);

  // ------------------------------------------------------------------------
  // 3. Precompiled regex cache in RuleEngine
  // ------------------------------------------------------------------------
  RuleEngine._regexCache.clear();
  const r1 = RuleEngine.getCompiledRegex('(live|remix)');
  const r2 = RuleEngine.getCompiledRegex('(live|remix)');
  assert.equal(r1, r2, 'RuleEngine must reuse cached RegExp instance');
  assert.equal(RuleEngine.getCompiledRegex('[invalid('), null, 'Invalid regex must return null without throwing');

  // ------------------------------------------------------------------------
  // 4. Global AudioPlayer upgrades: honest badges, stream URL, audio_id lyrics, chorus ms->sec, retry, logout reset
  // ------------------------------------------------------------------------
  assert.equal(isPlayableAudioUrl('/api/stream/tok_abc123'), true, 'Relative /api/stream/{token} URL must be accepted');
  assert.equal(isPlayableAudioUrl('https://m8.music.126.net/song.flac'), true);
  assert.equal(isPlayableAudioUrl('javascript:alert(1)'), false);

  const hqBadge = formatHonestAudioBadge({ level: 'hires', format: 'flac', sample_rate: 96000, source: 'original' });
  assert.ok(hqBadge.text.includes('Hi-Res') && hqBadge.text.includes('96kHz'), 'Honest badge shows verified Hi-Res + 96kHz');

  const localWavBadge = formatHonestAudioBadge({ source: 'local', level: 'lossless', format: 'wav', bitrate: 705600 });
  assert.equal(localWavBadge.text, '本地 · 无损 WAV · 706kbps', 'Local WAV lossless badge must show WAV, never FLAC');
  const localFlacBadge = formatHonestAudioBadge({ source: 'local', level: 'lossless', format: 'flac', bitrate: 1024000 });
  assert.equal(localFlacBadge.text, '本地 · 无损 FLAC · 1024kbps');
  const genericLosslessBadge = formatHonestAudioBadge({ source: 'original', level: 'lossless' });
  assert.equal(genericLosslessBadge.text, '无损', 'Lossless without format must not fabricate FLAC');

  const extBadge = formatHonestAudioBadge({ source: 'external', source_provider: 'kuwo', format: 'flac', bitrate: 920000 });
  assert.ok(extBadge.text.includes('外部源 · 酷我') && extBadge.text.includes('FLAC') && extBadge.text.includes('920kbps'));

  const altArtistBadge = formatHonestAudioBadge({
    source: 'alternative',
    is_alternative: true,
    format: 'flac',
    alternative_info: { title: '晴天 (Live)', artists: ['周杰伦', '合唱团'] },
  });
  assert.ok(altArtistBadge.detail.includes('周杰伦 / 合唱团'), 'alternative_info.artists array must be formatted cleanly');

  const bareBadge = formatHonestAudioBadge({});
  assert.equal(bareBadge, null, 'Empty metadata must not fabricate any badge');

  // Parallel audio+lyrics & audio_id mismatch refetch (including resolvedAudioData.id fallback) + Local Library playback
  const origGetAudio = api.getAudio;
  const origGetLyrics = api.getLyrics;
  const origGetLocalStream = api.getLocalStream;
  const origGetFm = api.getPersonalFm;
  const origLikeTrack = api.likeTrack;
  const lyricsCalls = [];
  const localStreamCalls = [];
  let audioCalls = 0;
  let likeCalls = 0;
  try {
    api.getAudio = async (id, opts) => {
      audioCalls += 1;
      if (opts?.force_refresh) {
        return { ok: true, data: { url: 'https://cdn.example.com/refreshed.flac', level: 'lossless', format: 'flac' }, error: null };
      }
      return {
        ok: true,
        data: {
          url: '/api/stream/local_token_1',
          id: 999002,
          source: 'alternative',
          is_alternative: true,
          format: 'flac',
          chorus_start: 62000,
          chorus_end: 95000,
        },
        error: null,
      };
    };
    api.getLocalStream = async (filePath) => {
      localStreamCalls.push(filePath);
      return {
        ok: true,
        data: {
          id: 0,
          url: '/api/stream/local_tok_77',
          stream_url: '/api/stream/local_tok_77',
          level: 'lossless',
          format: 'flac',
          bitrate: 1024000,
          source: 'local',
          source_provider: 'local_library',
        },
        error: null,
      };
    };
    api.getLyrics = async (id, audioId = null) => {
      lyricsCalls.push({ id, audioId });
      if (audioId === 999002) {
        return { ok: true, data: { lyrics: '[00:02.00]替代版本匹配歌词', tlyrics: '' }, error: null };
      }
      return { ok: true, data: { lyrics: '[00:01.00]原曲歌词', tlyrics: '' }, error: null };
    };
    api.likeTrack = async () => {
      likeCalls += 1;
      return { ok: true, data: {}, error: null };
    };

    const songForAudioId = { id: 999001, title: '原曲', artists: ['歌手'], duration: 200000 };
    await player.playSong(songForAudioId, false);
    assert.equal(lyricsCalls.length, 2, 'Must fetch song.id lyrics in parallel AND refetch for mismatched resolvedAudioData.id');
    assert.equal(lyricsCalls[1].audioId, 999002);
    assert.equal(lyricsManager.lyricsData[0].text, '替代版本匹配歌词', 'Lyrics must align to actual playing audio_id');
    assert.equal(player.audio.src, '/api/stream/local_token_1');

    // Local Library track with local_path must use api.getLocalStream and skip getAudio/getLyrics/likeTrack
    const audioCallsBeforeLocal = audioCalls;
    const lyricsCallsBeforeLocal = lyricsCalls.length;
    const localSong = { id: 'local_0_D:\\Music\\test.flac', local_path: 'D:\\Music\\test.flac', title: '无损本地曲', artists: ['本地歌手'], duration: 180000 };
    await player.playSong(localSong, false);
    assert.deepEqual(localStreamCalls, ['D:\\Music\\test.flac'], 'Local track must stream via POST /api/local-library/stream');
    assert.equal(audioCalls, audioCallsBeforeLocal, 'Local track must NOT call GET /api/audio');
    assert.equal(lyricsCalls.length, lyricsCallsBeforeLocal, 'Local track must NOT call GET /api/lyrics with non-numeric ID');
    assert.equal(player.audio.src, '/api/stream/local_tok_77');
    await player.toggleLikeCurrentSong(localSong);
    assert.equal(likeCalls, 0, 'Local track must NOT call POST /api/operations/like');
    // Restore NetEase song for chorus + retry checks
    await player.playSong(songForAudioId, false);

    // Chorus jump ms -> sec normalization
    player.audio.duration = 200;
    player.performChorusJump(player.currentSong);
    assert.equal(player.audio.currentTime, 62, 'performChorusJump must normalize 62000ms to 62s');

    // Single transparent retry on expired CDN URL preserving playback position
    player.lastSavedPosition = 48.5;
    player.audio.currentTime = 48.5;
    await player.handleAudioElementError();
    assert.equal(player.retriedCurrentTrack, true);
    assert.equal(player.audio.src, 'https://cdn.example.com/refreshed.flac', 'Expired URL must transparently refresh once');

    // Personal FM auto-refill
    let fmRefills = 0;
    api.getPersonalFm = async () => {
      fmRefills += 1;
      return { ok: true, data: { songs: [{ id: 5003, title: 'FM 3', duration: 180 }] }, error: null };
    };
    state.set({
      authenticated: true,
      playMode: 'fm',
      playQueue: [{ id: 5001, title: 'FM 1', duration: 180 }, { id: 5002, title: 'FM 2', duration: 180 }],
      playQueueIndex: 0,
    });
    await player.playNext(true);
    assert.equal(fmRefills, 1, 'FM mode must auto-refill queue when <= 2 tracks remain');
    assert.equal(state.get('playQueue').length, 3);

    // Clean stopAndReset on logout
    player.stopAndReset();
    assert.equal(player.currentSong, null);
    assert.equal(player.audio.src, '');
    assert.equal(state.get('playQueue').length, 0);
  } finally {
    api.getAudio = origGetAudio;
    api.getLyrics = origGetLyrics;
    api.getLocalStream = origGetLocalStream;
    api.getPersonalFm = origGetFm;
    api.likeTrack = origLikeTrack;
  }

  // ------------------------------------------------------------------------
  // 5. Deck: 'L' relisten-later, e.repeat / IME guard, soft-commit cancel through backup, sessionGeneration
  // ------------------------------------------------------------------------
  state.cancelAllCommits();
  const likedPl = { id: 10, name: '我喜欢的音乐', kind: 'liked', track_count: 3 };
  const subPl = { id: 30, name: '别人歌单', kind: 'subscribed', track_count: 2 };
  const track1 = { id: 701, title: 'Track 1', source_playlist_id: 10 };
  const track2 = { id: 702, title: 'Track 2', source_playlist_id: 10 };
  const subTrack = { id: 703, title: 'Sub Track', source_playlist_id: 30 };

  state.set({
    authenticated: true,
    offlineMode: false,
    user: { user_id: 5501 },
    sessionGeneration: 5,
    playlists: [likedPl, subPl],
    selectedPlaylistId: 10,
    catalog: [track1, track2, subTrack],
    deckQueue: [track1, track2],
    deckIndex: 0,
    deckFilter: 'undecided',
    deckOrder: 'oldest',
    relistenIds: new Set(),
    keptIds: new Set(),
    skippedIds: new Set(),
    deckHistory: [],
    hasUnknownResult: false,
    destructiveFrozen: false,
    activeTab: 'deck',
    isStageOpen: false,
    autoplayEnabled: false,
  });

  // Subscribed playlist must be strictly read-only
  assert.equal(state.resolveDestructiveAction(subTrack, 30).valid, false, 'Subscribed playlist cannot be mutated');

  // Keyboard guards: e.repeat, IME e.isComposing, and non-deck tab must be ignored
  assert.equal(deckManager.shouldHandleDeckKeyEvent({ key: 'd', repeat: true }), false, 'e.repeat must be ignored');
  assert.equal(deckManager.shouldHandleDeckKeyEvent({ key: 'd', isComposing: true }), false, 'IME e.isComposing must be ignored');
  assert.equal(deckManager.shouldHandleDeckKeyEvent({ key: 'd', keyCode: 229 }), false, 'IME keyCode 229 must be ignored');
  assert.equal(deckManager.shouldHandleDeckKeyEvent({ key: 'd', ctrlKey: true }), false, 'Ctrl modifier must be ignored');
  assert.equal(deckManager.shouldHandleDeckKeyEvent({ key: 'd', target: { tagName: 'INPUT' } }), false, 'Input focus must be ignored');

  // 'L' relisten-later action & relisten filter
  deckManager.handleRelistenLaterAction();
  assert.equal(state.isRelistenLater(701), true, 'Track 701 must be marked relisten-later');
  assert.equal(state.get('deckIndex'), 1, 'Deck must advance after relisten-later');
  const undecidedList = deckManager.filterAndOrderTracks([track1, track2], 10, 'undecided', 'oldest');
  assert.deepEqual(undecidedList.map(s => s.id), [702], 'Relisten-later tracks must leave undecided filter');
  const relistenList = deckManager.filterAndOrderTracks([track1, track2], 10, 'relisten', 'oldest');
  assert.deepEqual(relistenList.map(s => s.id), [701], 'Relisten-later tracks must appear in relisten filter');

  // Undo 'L' via Z
  deckManager.handleUndoAction();
  assert.equal(state.isRelistenLater(701), false, 'Z undo must unmark relisten-later');
  assert.equal(state.get('deckIndex'), 0);

  // Soft-commit cancellation DURING backup wait (before HTTP write fires)
  const origBackup = api.backup;
  const origUnlike = api.unlike;
  const origSetTimeout = globalThis.setTimeout;
  try {
    let timerCallback = null;
    globalThis.setTimeout = (fn) => {
      timerCallback = fn;
      return 12345;
    };
    let resolveBackup;
    const backupWaitPromise = new Promise(r => { resolveBackup = r; });
    let unlikeFired = 0;
    api.backup = () => backupWaitPromise;
    api.unlike = async () => {
      unlikeFired += 1;
      return { ok: true, data: { successful_ids: [701] } };
    };

    state.setInstantMode(true);
    state.set({ deckQueue: [track1, track2], deckIndex: 0, pendingCommits: [], sessionGeneration: 5 });
    deckManager.resetSessionSnapshot();
    deckManager.handleDestructiveAction();
    assert.equal(typeof timerCallback, 'function');

    // Timer expires -> enters backup wait (status === 'preparing')
    const commitPromise = timerCallback();
    // User presses Z DURING backup wait!
    const cancelledDuringBackup = state.cancelNewestCommit();
    assert.equal(cancelledDuringBackup, true, 'Must cancel soft commit even while awaiting backup');
    // Now backup resolves
    resolveBackup({ ok: true, data: {} });
    await commitPromise;
    assert.equal(unlikeFired, 0, 'Cancelled commit during backup wait must NEVER send cloud write');

    // Session generation mismatch aborts soft-commit before write
    state.set({ deckQueue: [track1, track2], deckIndex: 0, pendingCommits: [], sessionGeneration: 5 });
    deckManager.resetSessionSnapshot();
    api.backup = async () => ({ ok: true, data: {} });
    deckManager.handleDestructiveAction();
    // Simulate account switch/logout incrementing sessionGeneration
    state.set({ sessionGeneration: 6 });
    await timerCallback();
    assert.equal(unlikeFired, 0, 'Session generation mismatch must abort soft-commit write');
  } finally {
    globalThis.setTimeout = origSetTimeout;
    api.backup = origBackup;
    api.unlike = origUnlike;
    state.cancelAllCommits();
  }

  // ------------------------------------------------------------------------
  // 6. Cloud frozen batch confirmation manifest & per-group manifest_hash
  // ------------------------------------------------------------------------
  const origCloudBackup = api.backup;
  const origCloudUnlike = api.unlike;
  const origCloudPending = api.getPendingOperations;
  const origCloudReload = cloudManager.reloadCatalog;
  const origCloudRenderQ = cloudManager.renderGroupedBatchQueue;
  const origCloudRenderL = cloudManager.renderLogs;
  try {
    const sentPayloads = [];
    api.backup = async () => ({ ok: true, data: {} });
    api.unlike = async (ids, pid, extra) => {
      sentPayloads.push({ ids, pid, extra });
      return { ok: true, data: { successful_ids: ids, failed_items: [], unknown_items: [] } };
    };
    api.getPendingOperations = async () => ({ ok: true, data: { operations: [] } });
    cloudManager.reloadCatalog = async () => {};
    cloudManager.renderGroupedBatchQueue = () => {};
    cloudManager.renderLogs = () => {};

    state.set({
      sessionGeneration: 9,
      hasUnknownResult: false,
      destructiveFrozen: false,
      batchQueue: [{ song: track1, action: 'unlike', playlistId: 10, targetName: '我喜欢的音乐' }],
    });

    // Freeze manifest at modal open
    cloudManager.openBatchConfirmationModal();
    // Mutate batchQueue AFTER modal opened (should NOT be executed!)
    state.set({
      batchQueue: [
        { song: track1, action: 'unlike', playlistId: 10, targetName: '我喜欢的音乐' },
        { song: track2, action: 'unlike', playlistId: 10, targetName: '我喜欢的音乐' },
      ],
    });

    await cloudManager.executeConfirmedBatch();
    assert.equal(sentPayloads.length, 1);
    assert.deepEqual(sentPayloads[0].ids, [701], 'Batch execution must send ONLY the frozen manifest items');
    assert.equal(sentPayloads[0].extra.session_generation, 9);
    assert.equal(
      sentPayloads[0].extra.manifest_hash,
      computeManifestHash([{ song: track1, action: 'unlike', playlistId: 10 }]),
      'Batch execution must include exact per-group manifest_hash',
    );
    assert.equal(state.get('batchQueue').some(i => i.song.id === 702), true, 'Unfrozen item added later must stay untouched in queue');
  } finally {
    api.backup = origCloudBackup;
    api.unlike = origCloudUnlike;
    api.getPendingOperations = origCloudPending;
    cloudManager.reloadCatalog = origCloudReload;
    cloudManager.renderGroupedBatchQueue = origCloudRenderQ;
    cloudManager.renderLogs = origCloudRenderL;
    state.set({ batchQueue: [] });
  }

  // ------------------------------------------------------------------------
  // 7. Catalog O(1) incremental checkbox selection (zero whole-catalog re-filter)
  // ------------------------------------------------------------------------
  const origGetFiltered = catalogManager.getFilteredSongs;
  let filterCallsDuringToggle = 0;
  try {
    catalogManager._cachedPageKeys = ['10:701', '10:702'];
    state.set({ catalogSelectedIds: new Set() });
    catalogManager.getFilteredSongs = () => {
      filterCallsDuringToggle += 1;
      return [];
    };
    const fakeRow = { classList: { toggled: null, toggle(_c, v) { this.toggled = v; } } };
    catalogManager.toggleRowSelection('10:701', true, fakeRow);
    assert.equal(filterCallsDuringToggle, 0, 'Checkbox click must NEVER re-filter the whole catalog');
    assert.equal(state.get('catalogSelectedIds').has('10:701'), true);
    assert.equal(fakeRow.classList.toggled, true);
  } finally {
    catalogManager.getFilteredSongs = origGetFiltered;
  }

  // ------------------------------------------------------------------------
  // 8. Offline read-only catalog entry via cached_accounts
  // ------------------------------------------------------------------------
  const origSelectOffline = api.selectOfflineAccount;
  const origGetStatus = api.getStatus;
  const origUpdateUserUI = auth.updateUserUI;
  const origUpdateShell = auth.updateLoginShell;
  try {
    api.getStatus = async () => ({
      ok: true,
      data: {
        authenticated: false,
        session_generation: 12,
        cached_accounts: [{ account_id: '8888', display_name: '离线老歌单', track_count: 42 }],
      },
      error: null,
    });
    auth.updateUserUI = () => {};
    auth.updateLoginShell = () => {};

    const authed = await auth.checkSession();
    assert.equal(authed, false);
    assert.equal(state.get('sessionGeneration'), 12);
    assert.equal(state.get('cachedAccounts').length, 1);

    api.selectOfflineAccount = async () => ({ok: true});
    await auth.enterOfflineMode(state.get('cachedAccounts')[0]);
    assert.equal(state.get('offlineMode'), true, 'Must enter offline read-only library');
    assert.equal(state.getUserId(), '8888');
    assert.equal(state.resolveDestructiveAction(track1, 10).valid, false, 'Offline mode must block cloud mutations');
    state.set({ offlineMode: false, activeOfflineAccountId: null });
  } finally {
    api.getStatus = origGetStatus;
    api.selectOfflineAccount = origSelectOffline;
    auth.updateUserUI = origUpdateUserUI;
    auth.updateLoginShell = origUpdateShell;
  }

  // ------------------------------------------------------------------------
  // 9. Icons factory, Immersive Stage ambient lifecycle & CSS performance contract
  // ------------------------------------------------------------------------
  assert.ok(ICON_NAMES.length >= 30, 'SVG icon registry should cover UI controls');
  const playIcon = createIcon('play', { size: 16 });
  assert.equal(playIcon.dataset.icon, 'play');

  // Verify base.css has NO full-screen blur(100px) and NO background-image transition on ambient-cover-field
  const baseCss = fs.readFileSync(path.join(process.cwd(), 'web', 'css', 'base.css'), 'utf8');
  assert.equal(baseCss.includes('blur(100px)'), false, 'base.css must NOT contain full-screen blur(100px)');
  assert.equal(baseCss.includes('transition: background-image'), false, 'base.css must NOT transition background-image');

  // Verify web/index.html defines custom squircle brand SVG favicon & streamlined Chinese labels
  const indexHtml = fs.readFileSync(path.join(process.cwd(), 'web', 'index.html'), 'utf8');
  assert.equal(indexHtml.includes('rel="icon" type="image/svg+xml"'), true, 'web/index.html must include inline SVG favicon');
  assert.equal(indexHtml.includes('%23141518') && indexHtml.includes('%2362a368'), true, 'SVG favicon must match dark obsidian squircle + sage-emerald brand mark');
  assert.equal(indexHtml.includes('id="tab-discover"') && indexHtml.includes('>发现<'), true, 'Sidebar must show streamlined 发现 label');
  assert.equal(indexHtml.includes('id="tab-catalog"') && indexHtml.includes('>音乐库<'), true, 'Sidebar must show streamlined 音乐库 label');
  assert.equal(indexHtml.includes('id="tab-deck"') && indexHtml.includes('>歌单清理<'), true, 'Sidebar must show streamlined 歌单清理 label');
  assert.equal(indexHtml.includes('id="tab-cloud"') && indexHtml.includes('>设置<'), true, 'Sidebar must show pinned 设置 label');
  assert.equal(indexHtml.includes('id="stage-close-btn"') && indexHtml.includes('data-icon-slot="chevron-down"') && indexHtml.includes('>收起<'), true, 'Stage close button must use chevron-down + 收起 label');
  assert.equal(indexHtml.includes('id="cloud-subtab-settings"') && indexHtml.includes('偏好设置'), true, 'Settings & Sync must provide 偏好设置 subtab');
  assert.equal(indexHtml.includes('id="cloud-subtab-sync"') && indexHtml.includes('同步与备份'), true, 'Settings & Sync must provide 同步与备份 subtab');
  assert.equal(indexHtml.includes('rail-footer'), false, 'Redundant rail-footer Ctrl+K button must be removed from sidebar');
  assert.equal(indexHtml.includes('安全整理台'), false, 'Out-of-place 安全整理台 card must be removed from Discover hero');
  assert.equal(indexHtml.includes('返回工作台'), false, 'Confusing 返回工作台 label must be replaced by 收起');
  assert.equal(indexHtml.includes('工作台'), false, 'web/index.html must not contain confusing 工作台 phrasing');

  // Verify cloudManager 3-tab switcher (偏好设置 | 多渠道与会员 | 同步与备份)
  assert.equal(indexHtml.includes('id="cloud-subtab-channels"') && indexHtml.includes('多渠道与会员'), true, 'Settings & Sync must provide 多渠道与会员 subtab');
  cloudManager.setSubTab('channels');
  assert.equal(cloudManager.activeSubTab, 'channels', 'cloudManager.setSubTab must switch to channels panel');
  cloudManager.setSubTab('sync');
  assert.equal(cloudManager.activeSubTab, 'sync', 'cloudManager.setSubTab must switch to sync panel');
  cloudManager.setSubTab('settings');
  assert.equal(cloudManager.activeSubTab, 'settings', 'cloudManager.setSubTab must switch back to settings panel');

  // Verify Wave 3 HTML anchors & web/desktop_lyrics.html
  assert.equal(indexHtml.includes('id="discover-explore-slider"') && indexHtml.includes('心动电台'), true, 'Discover must showcase 心动电台 with explore slider');
  assert.equal(indexHtml.includes('id="discover-dusty-grid"') && indexHtml.includes('沉睡宝藏'), true, 'Discover must include 沉睡宝藏 section');
  assert.equal(indexHtml.includes('id="discover-insights-bar"'), true, 'Discover must include local listening insights bar');
  assert.equal(indexHtml.includes('id="search-palette-channel-bar"'), true, 'Search palette must include multi-channel chip bar');
  assert.equal(indexHtml.includes('id="stage-offset-minus"') && indexHtml.includes('id="stage-offset-plus"'), true, 'Stage must include per-song lyric offset controls');
  assert.equal(indexHtml.includes('id="stage-ttml-badge"'), true, 'Stage must include AMLL TTML pill');
  assert.equal(indexHtml.includes('id="settings-loudness-toggle"') && indexHtml.includes('id="settings-cache-limit-select"'), true, 'Preferences must include loudness normalization & audio cache limit');

  const desktopLyricsHtml = fs.readFileSync(path.join(process.cwd(), 'web', 'desktop_lyrics.html'), 'utf8');
  assert.equal(desktopLyricsHtml.includes("new EventSource('/api/events')"), true, 'desktop_lyrics.html must subscribe to /api/events SSE');
  assert.equal(desktopLyricsHtml.includes('desktop_lyrics'), true, 'desktop_lyrics.html must listen for desktop_lyrics event');
  assert.equal(desktopLyricsHtml.includes('dl-lock-btn') && desktopLyricsHtml.includes('dl-close-btn'), true, 'desktop_lyrics.html must provide lock & close controls');

  // Verify deck.css does NOT animate font-size or use transition: all on lyric lines
  const deckCss = fs.readFileSync(path.join(process.cwd(), 'web', 'css', 'deck.css'), 'utf8');
  assert.equal(deckCss.includes('transition: all'), false, 'deck.css must NOT use transition: all');

  // Verify stageManager ambient pause logic for quiet_eco and paused playback
  state.updateSettings({ visual_mode: 'quiet_eco' });
  state.set({ isPlaying: true });
  assert.equal(stageManager.shouldPauseAmbient(), true, 'quiet_eco visual mode must pause stage ambient');
  state.updateSettings({ visual_mode: 'soft_dynamic' });
  state.set({ isPlaying: false });
  assert.equal(stageManager.shouldPauseAmbient(), true, 'Paused playback must pause stage ambient');
  state.set({ isPlaying: true });
  stageManager.windowBlurred = true;
  assert.equal(stageManager.shouldPauseAmbient(), true, 'Blurred window must pause stage ambient');
  stageManager.windowBlurred = false;
  assert.equal(stageManager.shouldPauseAmbient(), false, 'Focused playing soft_dynamic stage allows drift');

  // ------------------------------------------------------------------------
  // 10. Mid-song Immersive Stage open immediately renders, highlights, scrolls & starts YRC sync
  // ------------------------------------------------------------------------
  const FakeNode = class FakeNode {};
  const hadNode = Object.prototype.hasOwnProperty.call(globalThis, 'Node');
  const origNode = globalThis.Node;
  const origDoc = globalThis.document;
  const origStageOverlay = stageManager.overlay;
  const origStageAmbient = stageManager.ambientHost;

  const makeMockEl = (tag = 'div', id = '') => {
    const el = Object.create(FakeNode.prototype);
    const styles = new Map();
    const classes = new Set();
    let text = '';
    const scrollCalls = [];
    Object.assign(el, {
      tagName: String(tag).toUpperCase(),
      id,
      children: [],
      dataset: {},
      attributes: {},
      offsetTop: 120,
      offsetHeight: 44,
      clientHeight: 360,
      scrollTop: 0,
      scrollCalls,
      style: {
        setProperty: (k, v) => styles.set(k, v),
        getPropertyValue: (k) => styles.get(k) || '',
      },
      classList: {
        add: (...cls) => cls.forEach(c => classes.add(c)),
        remove: (...cls) => cls.forEach(c => classes.delete(c)),
        contains: (c) => classes.has(c),
        toggle: (c, force) => {
          const on = force === undefined ? !classes.has(c) : Boolean(force);
          if (on) classes.add(c); else classes.delete(c);
          return on;
        },
      },
      setAttribute(name, value) {
        this.attributes[name] = String(value);
        if (name === 'id') this.id = String(value);
      },
      addEventListener() {},
      focus() {},
      appendChild(child) {
        if (child && child._isFragment) {
          this.children.push(...child.children);
        } else {
          this.children.push(child);
        }
        return child;
      },
      scrollTo(opts) {
        scrollCalls.push(opts);
        if (opts && typeof opts.top === 'number') this.scrollTop = opts.top;
      },
      querySelector(sel) {
        return this.querySelectorAll(sel)[0] || null;
      },
      querySelectorAll(sel) {
        const results = [];
        const walk = (node) => {
          for (const ch of (node.children || [])) {
            const matchStageIdx = /^\[data-stage-index="(-?\d+)"\]$/.exec(sel);
            const matchStageActive = sel === '.stage-lyric-line.active';
            const matchStageLine = sel === '.stage-lyric-line';
            const matchInterlude = sel === '.stage-interlude-dots';
            const matchWord = sel === '.yrc-word';
            if (matchStageIdx && String(ch.dataset?.stageIndex) === matchStageIdx[1]) results.push(ch);
            else if (matchStageActive && ch.classList?.contains('stage-lyric-line') && ch.classList?.contains('active')) results.push(ch);
            else if (matchStageLine && ch.classList?.contains('stage-lyric-line')) results.push(ch);
            else if (matchInterlude && ch.classList?.contains('stage-interlude-dots')) results.push(ch);
            else if (matchWord && ch.classList?.contains('yrc-word')) results.push(ch);
            walk(ch);
          }
        };
        walk(this);
        return results;
      },
    });
    Object.defineProperty(el, 'className', {
      get: () => [...classes].join(' '),
      set: (val) => {
        classes.clear();
        String(val || '').split(/\s+/).filter(Boolean).forEach(c => classes.add(c));
      },
    });
    Object.defineProperty(el, 'textContent', {
      get: () => text,
      set: (val) => {
        text = String(val);
        if (text === '') el.children = [];
      },
    });
    return el;
  };

  try {
    const stageOverlay = makeMockEl('div', 'immersive-stage-overlay');
    const stageAmbient = makeMockEl('div', 'stage-ambient-host');
    const stageLyrics = makeMockEl('div', 'stage-lyrics-container');
    const stageCloseBtn = makeMockEl('button', 'stage-close-btn');
    const domMap = {
      'immersive-stage-overlay': stageOverlay,
      'stage-ambient-host': stageAmbient,
      'stage-lyrics-container': stageLyrics,
      'stage-close-btn': stageCloseBtn,
    };

    globalThis.Node = FakeNode;
    globalThis.document = {
      hidden: false,
      activeElement: null,
      getElementById: (id) => domMap[id] || null,
      createElement: (tag) => makeMockEl(tag),
      createDocumentFragment: () => ({
        _isFragment: true,
        children: [],
        appendChild(c) { this.children.push(c); return c; },
      }),
      createTextNode: (t) => ({ textContent: String(t), nodeType: 3 }),
      querySelectorAll: () => [],
      querySelector: () => null,
    };

    stageManager.overlay = stageOverlay;
    stageManager.ambientHost = stageAmbient;

    // Load YRC lyrics while stage is CLOSED (simulates playback starting on Discover/Catalog)
    lyricsManager.loadLyrics('', sampleTrans, { yrc: sampleYrc, romalrc: sampleRoma });
    assert.equal(stageLyrics.children.length, 0, 'Closed stage must not eagerly render lyrics DOM');

    // Open stage mid-song at t = 5.5s (inside line index 1 "[5000,2000](5000,1000,0)晚(6000,1000,0)安")
    state.set({
      currentPlayingSong: { id: 999001, title: '夜曲', artists: ['周杰伦'], duration: 200 },
      isPlaying: true,
      currentTime: 5.5,
      duration: 200,
    });
    player.audio.currentTime = 5.5;
    stageManager.open();

    assert.equal(stageOverlay.classList.contains('open'), true, 'Stage overlay must be open');
    assert.equal(stageLyrics.children.length, 2, 'Mid-song stage.open() must immediately render all lyrics lines');
    assert.equal(stageLyrics.children[1].classList.contains('active'), true, 'Line at t=5.5s must be highlighted immediately');
    assert.equal(stageLyrics.scrollCalls.length >= 1, true, 'Stage container must immediately scroll to active line');
    assert.equal(stageLyrics.scrollCalls[0].behavior, 'auto', 'Initial mid-song open must jump smoothly without delay (behavior: auto)');
    const activeWords = stageLyrics.children[1].querySelectorAll('.yrc-word');
    assert.equal(activeWords[0].style.getPropertyValue('--word-progress'), '50%', 'Active YRC word progress must reflect t=5.5s immediately');

    // Closing stage clears DOM and invalidates renderedVersions.stage so reopening re-renders cleanly
    stageManager.close();
    assert.equal(stageLyrics.children.length, 0, 'Closing stage must clear stage lyrics DOM');
    assert.equal(lyricsManager.renderedVersions.stage, -1, 'Closing stage must invalidate stage render version');

    // Reopening mid-song at t = 1.5s re-renders and highlights line 0
    player.audio.currentTime = 1.5;
    state.set({ currentTime: 1.5 });
    stageManager.open();
    assert.equal(stageLyrics.children.length, 2, 'Reopening stage must re-render lyrics cleanly');
    assert.equal(stageLyrics.children[0].classList.contains('active'), true, 'Reopened stage must highlight line 0 at t=1.5s');
    assert.equal(stageLyrics.children[0].dataset.distance, '0', 'Active line must have data-distance="0"');
    assert.equal(stageLyrics.children[1].dataset.distance, '1', 'Adjacent line must have data-distance="1"');

    // Verify 160x160 fluid canvas mounted inside stageAmbient and zeroed on close
    assert.equal(stageManager.fluidCanvas?.width, 160, 'Fluid canvas must mount at bounded 160x160');
    const canvasRef = stageManager.fluidCanvas;
    stageManager.close();
    assert.equal(canvasRef.width, 0, 'Closing stage must zero fluid canvas backing store');
    assert.equal(stageManager.fluidCanvas, null, 'Closing stage must release fluid canvas reference');

    // Verify >5s interlude 3-dot indicator & per-song lyric time offset + AMLL ttml_source
    lyricsManager.loadLyrics('[00:08.00]前奏之后第一句\n[00:22.00]长间奏之后第二句', '', {
      ttml_source: 'amll-ttml-db',
      songId: 4242,
    });
    assert.equal(lyricsManager.ttmlSource, 'amll-ttml-db');
    assert.equal(lyricsManager.getActiveInterludeAfterIndex(3.0), -1, 'Intro >5s must activate intro interlude');
    assert.equal(lyricsManager.getActiveInterludeAfterIndex(15.0), 0, 'Mid-song >5s gap must activate mid interlude');
    assert.equal(lyricsManager.adjustSongOffset(0.5), 0.5, 'adjustSongOffset must shift by +0.5s');
    assert.equal(lyricsManager.loadSongOffset(4242), 0.5, 'Per-song offset must persist per songId');
    assert.equal(lyricsManager.setSongOffset(0), 0);
  } finally {
    stageManager.overlay = origStageOverlay;
    stageManager.ambientHost = origStageAmbient;
    lyricsManager.setPlaybackState(false);
    if (hadNode) globalThis.Node = origNode;
    else delete globalThis.Node;
    globalThis.document = origDoc;
  }

  // ------------------------------------------------------------------------
  // 11. Wave 3: 4-Quadrant Palette, Fiedler Balanced Shuffle + playHistoryStack,
  //     Safe Same-Origin Web Audio Boundary, Multi-Channel Search & Direct External Playback
  // ------------------------------------------------------------------------
  // 4-quadrant RGBA extraction (32x32 buffer with 4 distinct quadrant colors)
  const quadBuf = new Uint8ClampedArray(32 * 32 * 4);
  for (let y = 0; y < 32; y++) {
    for (let x = 0; x < 32; x++) {
      const i = (y * 32 + x) * 4;
      if (y < 16 && x < 16) { quadBuf[i] = 210; quadBuf[i + 1] = 60; quadBuf[i + 2] = 60; }
      else if (y < 16 && x >= 16) { quadBuf[i] = 60; quadBuf[i + 1] = 190; quadBuf[i + 2] = 90; }
      else if (y >= 16 && x < 16) { quadBuf[i] = 60; quadBuf[i + 1] = 110; quadBuf[i + 2] = 220; }
      else { quadBuf[i] = 200; quadBuf[i + 1] = 150; quadBuf[i + 2] = 50; }
      quadBuf[i + 3] = 255;
    }
  }
  const quadHsls = extractQuadrantHsls(quadBuf, 32, 32);
  assert.equal(quadHsls.filter(Boolean).length, 4, 'Must extract 4 distinct quadrant HSL values');
  const quadPalette = quantizePixelsToPalette(quadBuf, 32, 32);
  assert.equal(Array.isArray(quadPalette.colors) && quadPalette.colors.length, 4, 'Palette must include 4 RGB colors for AMLL fluid canvas');

  // Same-origin Web Audio safety boundary (NEVER wire external CDN URLs through Web Audio!)
  assert.equal(isSameOriginStreamUrl('/api/stream/tok_xyz'), true, 'Relative /api/stream/{token} is eligible for Web Audio graph');
  assert.equal(isSameOriginStreamUrl('https://m8.music.126.net/song.flac'), false, 'Cross-origin CDN URL MUST bypass Web Audio graph');

  // Balanced Shuffle fallback: zero repeats in a round & artist spread
  const shuffleTracks = [
    { id: 1, title: 'A1', artists: ['ArtistA'] },
    { id: 2, title: 'A2', artists: ['ArtistA'] },
    { id: 3, title: 'A3', artists: ['ArtistA'] },
    { id: 4, title: 'B1', artists: ['ArtistB'] },
    { id: 5, title: 'B2', artists: ['ArtistB'] },
    { id: 6, title: 'C1', artists: ['ArtistC'] },
  ];
  let seedVal = 42;
  const detRng = () => {
    seedVal = (seedVal * 1664525 + 1013904223) % 4294967296;
    return seedVal / 4294967296;
  };
  const shuffled = balancedShuffleFallback(shuffleTracks, null, detRng);
  assert.equal(shuffled.length, shuffleTracks.length, 'Balanced shuffle must preserve every track');
  assert.equal(new Set(shuffled.map(t => t.id)).size, shuffleTracks.length, 'Balanced shuffle must contain 0 duplicates per round');
  let backToBackSameArtist = 0;
  for (let i = 1; i < shuffled.length; i++) {
    if (shuffled[i].artists[0] === shuffled[i - 1].artists[0]) backToBackSameArtist += 1;
  }
  assert.equal(backToBackSameArtist, 0, 'Balanced shuffle must separate same-artist tracks whenever feasible');

  // Balanced Shuffle Bag + exact playHistoryStack traversal + smart_radio auto-refill + direct external playback
  const origGetAudioW3 = api.getAudio;
  const origGetLyricsW3 = api.getLyrics;
  const origGetSmartRadio = api.postSmartRadio;
  const origSearchMulti = api.searchMulti;
  try {
    const audioReqs = [];
    api.getAudio = async (id, opts) => {
      audioReqs.push({ id, opts });
      return {
        ok: true,
        data: {
          url: '/api/stream/ext_tok_1',
          source: 'external',
          source_provider: opts?.provider || 'kugou_lite',
          level: 'lossless',
          format: 'flac',
          quality_upgraded_from: 'standard (128kbps)',
        },
        error: null,
      };
    };
    api.getLyrics = async () => ({ ok: false, data: null, error: null });
    let smartRadioRefills = 0;
    api.postSmartRadio = async payload => {
      smartRadioRefills += 1;
      return { ok: true, data: { ...payload, songs: [{ id: 8804, title: 'Radio 4', artists: ['D'], duration: 180 }] }, error: null };
    };
    api.searchMulti = async (q, channels) => ({
      ok: true,
      data: {
        tracks: [{
          id: 0,
          external_id: 'kg_hash_99',
          provider: 'kugou_lite',
          title: '晴天 (无损)',
          artists: ['周杰伦'],
          duration: 269000,
          available_channels: ['kugou_lite', 'migu'],
        }],
        channel_errors: {},
      },
      error: null,
    });

    // Multi-channel search palette normalization
    assert.equal(SEARCH_CHANNEL_CHIPS.length, 8, 'Search palette must provide 8 channel chips');
    assert.equal(CHANNEL_CATALOG.some(c => c.id === 'kugou_lite') && CHANNEL_CATALOG.some(c => c.id === 'migu'), true, 'Channel hub must include kugou_lite & migu');
    state.set({ offlineMode: false });
    searchPalette.activeChannel = 'all';
    await searchPalette.runOnlineSearch('晴天');
    assert.equal(searchPalette.results.length, 1);
    assert.deepEqual(searchPalette.results[0].available_channels, ['kugou_lite', 'migu']);

    // Direct cross-channel playback sends id=0 + provider + external_id
    await player.playSong(searchPalette.results[0], false);
    assert.equal(audioReqs[audioReqs.length - 1].id, 0, 'Direct external track must request GET /api/audio?id=0');
    assert.equal(audioReqs[audioReqs.length - 1].opts.provider, 'kugou_lite');
    assert.equal(audioReqs[audioReqs.length - 1].opts.external_id, 'kg_hash_99');

    // Badge shows quality_upgraded_from detail honestly (both string and backend object shape, zero [object Object])
    const upgradedBadge = formatHonestAudioBadge({
      source: 'external',
      source_provider: 'kugou_lite',
      level: 'lossless',
      format: 'flac',
      quality_upgraded_from: 'standard',
    });
    assert.ok(upgradedBadge.text.includes('酷狗概念版') && upgradedBadge.detail.includes('跨渠道音质择优升级'));

    const upgradedObjBadge = formatHonestAudioBadge({
      source: 'external',
      source_provider: 'kugou_lite',
      level: 'lossless',
      format: 'flac',
      quality_upgraded_from: { source: 'original', source_provider: 'netease', level: 'standard', format: 'mp3', bitrate: 128000 },
    });
    assert.equal(upgradedObjBadge.detail.includes('[object Object]'), false, 'Object quality_upgraded_from must never stringify to [object Object]');
    assert.ok(upgradedObjBadge.detail.includes('网易云') && upgradedObjBadge.detail.includes('128kbps'), 'Object quality_upgraded_from must format provider, level, format & bitrate');

    // Phase 2A supports genuine listens from every provider.
    const origRecordPlay = api.recordPlayback;
    const recordPlayCalls = [];
    try {
      api.recordPlayback = async (song, ms, completed) => {
        recordPlayCalls.push({ song, ms, completed });
        return { ok: true, data: { recorded: true }, error: null };
      };
      state.set({ authenticated: true, offlineMode: false, activeTab: 'discover' });
      player.currentSong = { id: 0, provider: 'kugou_lite', external_id: 'kg_1', title: '外链曲' };
      player.currentPlaybackEventId = 'test-external';
      player.listenAccumMs = 45000;
      player.listenRecordedForSeq = -1;
      player.maybeRecordGenuineListen(true);
      player.currentSong = { id: 'local_0_a.flac', local_path: 'D:\\a.flac', title: '本地曲' };
      player.maybeRecordGenuineListen(true);
      assert.deepEqual(recordPlayCalls.map(call => call.song.provider), ['kugou_lite', 'local']);
      player.listenRecordedForSeq = -1;
      player.currentSong = { id: 2149019899, title: '正版网易云曲', artists: ['歌手'], duration: 200000 };
      player.maybeRecordGenuineListen(false);
      assert.equal(recordPlayCalls.length, 3, 'Genuine >=30s NetEase song listen must record playback');
    } finally {
      api.recordPlayback = origRecordPlay;
    }

    // Regression: discoverManager reads breakdown.multi_seed_similar into blend-bar summary
    const origDocDiscover = globalThis.document;
    try {
      const blendBarNode = { attributes: {}, dataset: {}, setAttribute(k, v) { this.attributes[k] = v; } };
      globalThis.document = {
        getElementById: (id) => (id === 'discover-radio-blend-bar' ? blendBarNode : null),
        createElement: (tag) => ({ tagName: tag, className: '', textContent: '', setAttribute() {}, appendChild() {} }),
        querySelectorAll: () => [],
      };
      discoverManager.smartRadioBreakdown = { dusty_gems: 3, fresh_rotation: 1, multi_seed_similar: 4, exploration: 2 };
      discoverManager.renderHeroCards();
      assert.equal(String(blendBarNode.dataset.breakdownSummary).includes('共同相似 4'), true, 'Smart Radio breakdown must preserve 共同相似 count from multi_seed_similar');
    } finally {
      globalThis.document = origDocDiscover;
      discoverManager.smartRadioBreakdown = null;
    }

    // Regression: ensureWordSync rAF must respect timeOffset (no un-offset updateActiveLineWords overwrite)
    const lyricsSource = fs.readFileSync(path.join(process.cwd(), 'web', 'js', 'lyrics.js'), 'utf8');
    const ensureSyncBody = lyricsSource.slice(lyricsSource.indexOf('ensureWordSync()'), lyricsSource.indexOf('stopWordSync()'));
    assert.equal(ensureSyncBody.includes('this.updateActiveLineWords('), false, 'ensureWordSync must delegate word progress to updateTime(t) so timeOffset is never overwritten');

    // Regression: desktop_lyrics SSE window sync & Ctrl+Alt+D hotkey does NOT issue duplicate POST /api/desktop/lyrics-window
    const origSetDesktopWin = api.setDesktopLyricsWindow;
    const origPushDesktop = api.pushDesktopPlaybackState;
    let desktopWinPosts = 0;
    let desktopStatePushes = 0;
    try {
      api.setDesktopLyricsWindow = async () => { desktopWinPosts += 1; return { ok: true, data: { open: true, locked: false } }; };
      api.pushDesktopPlaybackState = async () => { desktopStatePushes += 1; return { ok: true, data: {} }; };
      state.set({ desktopLyricsOpen: false, desktopLyricsLocked: false });

      // SSE event from backend tray/hotkey updates state and pushes lyrics when opened
      cloudManager.handleDesktopLyricsSseEvent({ window: { open: true, locked: true, supported: true } });
      assert.equal(state.get('desktopLyricsOpen'), true, 'desktop_lyrics SSE must sync window.open');
      assert.equal(state.get('desktopLyricsLocked'), true, 'desktop_lyrics SSE must sync window.locked');
      assert.equal(desktopStatePushes, 1, 'Opening via desktop_lyrics SSE must push current lyric snapshot');

      // Tray unlock event clears locked state
      cloudManager.handleDesktopLyricsSseEvent({ window: { open: true, locked: false, supported: true } });
      assert.equal(state.get('desktopLyricsLocked'), false, 'Tray unlock SSE must clear desktopLyricsLocked');

      // Ctrl+Alt+D hotkey action must NOT double-toggle POST /api/desktop/lyrics-window
      player.handleGlobalHotkeyAction('toggle_desktop_lyrics');
      assert.equal(desktopWinPosts, 0, 'toggle_desktop_lyrics hotkey must NOT send conflicting second POST /api/desktop/lyrics-window');
      assert.equal(desktopLyricsHtml.includes('payload.window?.locked'), true, 'desktop_lyrics.html must sync payload.window.locked from SSE');
      state.set({ desktopLyricsOpen: false, desktopLyricsLocked: false });
    } finally {
      api.setDesktopLyricsWindow = origSetDesktopWin;
      api.pushDesktopPlaybackState = origPushDesktop;
    }

    // Balanced shuffle round + exact playPrev() back-traversal
    player.stopAndReset();
    const q4 = [
      { id: 9101, title: 'S1', artists: ['A'], duration: 180 },
      { id: 9102, title: 'S2', artists: ['B'], duration: 180 },
      { id: 9103, title: 'S3', artists: ['C'], duration: 180 },
      { id: 9104, title: 'S4', artists: ['D'], duration: 180 },
    ];
    await player.playQueue(q4, 0, 'shuffle');
    const firstId = player.currentSong.id;
    await player.playNext(true);
    const secondId = player.currentSong.id;
    assert.notEqual(secondId, firstId, 'Shuffle playNext must move to a different track');
    player.audio.currentTime = 0;
    await player.playPrev();
    assert.equal(player.currentSong.id, firstId, 'playPrev() must traverse playHistoryStack back to exact previous track');

    // Smart Radio auto-refill when <= 3 tracks remain
    state.set({
      playMode: 'smart_radio',
      playQueue: [
        { id: 8801, title: 'R1', artists: ['A'], duration: 180 },
        { id: 8802, title: 'R2', artists: ['B'], duration: 180 },
        { id: 8803, title: 'R3', artists: ['C'], duration: 180 },
      ],
      playQueueIndex: 0,
    });
    await player.playNext(true);
    assert.equal(smartRadioRefills, 1, 'smart_radio mode must auto-refill when <= 3 tracks remain');
    assert.equal(state.get('playQueue').length, 4);
    player.stopAndReset();
  } finally {
    api.getAudio = origGetAudioW3;
    api.getLyrics = origGetLyricsW3;
    api.postSmartRadio = origGetSmartRadio;
    api.searchMulti = origSearchMulti;
  }

  // ------------------------------------------------------------------------
  // 12. Wave 3.5: Ghost Audio Pause Hardening, Global Spacebar Play/Pause & Cache Bust
  // ------------------------------------------------------------------------
  assert.equal(indexHtml.includes('css/main.css?v=w4') && indexHtml.includes('js/app.js?v=w4'), true, 'web/index.html must include ?v=w4 cache-busting query string');

  // 12a. pauseAllAudio pauses BOTH directAudio and streamAudio, suspends running audioCtx, clears isPlaying
  const origAudioCtx = player.audioCtx;
  try {
    let suspendCalls = 0;
    player.audioCtx = {
      state: 'running',
      suspend: async () => { suspendCalls += 1; },
      resume: async () => {},
    };
    player.directAudio.paused = false;
    player.streamAudio.paused = false;
    state.set({ isPlaying: true });

    player.pauseAllAudio({ markUserPause: true });
    assert.equal(player.directAudio.paused, true, 'pauseAllAudio must pause directAudio');
    assert.equal(player.streamAudio.paused, true, 'pauseAllAudio must pause streamAudio');
    assert.equal(suspendCalls, 1, 'pauseAllAudio must suspend running AudioContext');
    assert.equal(state.get('isPlaying'), false, 'pauseAllAudio must immediately set isPlaying=false');
    assert.equal(player.userPausedSequence, player.requestSequence, 'pauseAllAudio(markUserPause: true) must record userPausedSequence');

    // 12b. Inactive audio element firing play/playing immediately gets paused & stripped of src
    player.audio = player.directAudio;
    player.streamAudio.paused = false;
    player.streamAudio.src = '/api/stream/ghost_tok';
    player.streamAudio.dispatchEvent({ type: 'play' });
    assert.equal(player.streamAudio.paused, true, 'Inactive element play event must immediately pause ghost element');
    assert.equal(player.streamAudio.src, '', 'Inactive element play event must strip ghost src');

    player.streamAudio.paused = false;
    player.streamAudio.src = '/api/stream/ghost_tok_2';
    player.streamAudio.dispatchEvent({ type: 'playing' });
    assert.equal(player.streamAudio.paused, true, 'Inactive element playing event must immediately pause ghost element');
    assert.equal(player.streamAudio.src, '', 'Inactive element playing event must strip ghost src');
  } finally {
    player.audioCtx = origAudioCtx;
    player.stopAndReset();
  }

  // 12c. Pausing mid-load during in-flight playSong() cancels delayed autoplay when getAudio resolves
  const origGetAudioMid = api.getAudio;
  const origGetLyricsMid = api.getLyrics;
  const origDirectPlay = player.directAudio.play;
  try {
    let resolveInFlightAudio;
    const inFlightAudioPromise = new Promise(r => { resolveInFlightAudio = r; });
    let playInvocations = 0;
    player.directAudio.play = async function () {
      playInvocations += 1;
      this.paused = false;
    };
    api.getAudio = () => inFlightAudioPromise;
    api.getLyrics = async () => ({ ok: false, data: null, error: null });

    const loadPromise = player.playSong({ id: 777001, title: '缓冲测试', artists: ['歌手'], duration: 180000 }, true);
    assert.equal(player.isLoadingTrack, true, 'playSong must set isLoadingTrack=true while awaiting audio');

    // User clicks Pause (or presses Space) BEFORE audio URL arrives!
    player.togglePlay();
    assert.equal(player.userPausedSequence, player.requestSequence, 'Mid-load togglePlay must mark userPausedSequence instead of re-fetching');

    // Now the audio URL resolves late
    resolveInFlightAudio({
      ok: true,
      data: { url: 'https://m8.music.126.net/late.mp3', format: 'mp3', level: 'standard' },
      error: null,
    });
    await loadPromise;
    assert.equal(player.isLoadingTrack, false, 'isLoadingTrack must reset after playSong finishes');
    assert.equal(playInvocations, 0, 'Delayed audio resolve after mid-load Pause must NEVER auto-play');
    assert.equal(state.get('isPlaying'), false, 'Player must remain paused after mid-load cancellation');
  } finally {
    player.directAudio.play = origDirectPlay;
    api.getAudio = origGetAudioMid;
    api.getLyrics = origGetLyricsMid;
    player.stopAndReset();
  }

  // 12d. Global Space Play/Pause guards in App.shouldHandleGlobalSpaceKey
  const appInstance = new App();
  const origDocSpace = globalThis.document;
  try {
    let modalOpen = false;
    let activeEl = null;
    globalThis.document = {
      querySelector: (sel) => (modalOpen && sel === '.modal-backdrop.open' ? {} : null),
      getElementById: () => null,
      get activeElement() { return activeEl; },
    };

    state.set({ activeTab: 'discover' });
    assert.equal(appInstance.shouldHandleGlobalSpaceKey({ key: ' ', code: 'Space' }), true, 'Space must toggle play/pause on Discover tab');
    state.set({ activeTab: 'catalog' });
    assert.equal(appInstance.shouldHandleGlobalSpaceKey({ key: ' ', code: 'Space' }), true, 'Space must toggle play/pause on Catalog tab');
    state.set({ activeTab: 'deck' });
    assert.equal(appInstance.shouldHandleGlobalSpaceKey({ key: ' ', code: 'Space' }), false, 'Space must NOT intercept Deck tab (Deck owns Space skip)');

    state.set({ activeTab: 'discover' });
    assert.equal(appInstance.shouldHandleGlobalSpaceKey({ key: ' ', repeat: true }), false, 'Space must ignore e.repeat');
    assert.equal(appInstance.shouldHandleGlobalSpaceKey({ key: ' ', isComposing: true }), false, 'Space must ignore IME e.isComposing');
    assert.equal(appInstance.shouldHandleGlobalSpaceKey({ key: ' ', keyCode: 229 }), false, 'Space must ignore IME keyCode 229');
    assert.equal(appInstance.shouldHandleGlobalSpaceKey({ key: ' ', ctrlKey: true }), false, 'Space must ignore Ctrl modifier');
    assert.equal(appInstance.shouldHandleGlobalSpaceKey({ key: ' ', metaKey: true }), false, 'Space must ignore Meta modifier');
    assert.equal(appInstance.shouldHandleGlobalSpaceKey({ key: ' ', altKey: true }), false, 'Space must ignore Alt modifier');
    assert.equal(appInstance.shouldHandleGlobalSpaceKey({ key: ' ', target: { tagName: 'INPUT' } }), false, 'Space must ignore INPUT target');
    assert.equal(appInstance.shouldHandleGlobalSpaceKey({ key: ' ', target: { tagName: 'TEXTAREA' } }), false, 'Space must ignore TEXTAREA target');
    assert.equal(appInstance.shouldHandleGlobalSpaceKey({ key: ' ', target: { tagName: 'SELECT' } }), false, 'Space must ignore SELECT target');
    assert.equal(appInstance.shouldHandleGlobalSpaceKey({ key: ' ', target: { tagName: 'BUTTON' } }), false, 'Space must ignore focused BUTTON target');
    assert.equal(appInstance.shouldHandleGlobalSpaceKey({ key: ' ', target: { tagName: 'DIV', isContentEditable: true } }), false, 'Space must ignore contentEditable target');

    modalOpen = true;
    assert.equal(appInstance.shouldHandleGlobalSpaceKey({ key: ' ', code: 'Space' }), false, 'Space must ignore when any modal is open');
    modalOpen = false;
    activeEl = { tagName: 'BUTTON' };
    assert.equal(appInstance.shouldHandleGlobalSpaceKey({ key: ' ', code: 'Space' }), false, 'Space must ignore when activeElement is a focused button');
  } finally {
    globalThis.document = origDocSpace;
  }

  // ------------------------------------------------------------------------
  // 13. Wave 4: P0 Ghost Audio Fix, Deck Queue/Lyrics Safety, Slider, Overflow, Toasts
  // ------------------------------------------------------------------------
  const playerSrc = fs.readFileSync(path.join(process.cwd(), 'web', 'js', 'player.js'), 'utf8');
  const deckCssW4 = fs.readFileSync(path.join(process.cwd(), 'web', 'css', 'deck.css'), 'utf8');
  const discoverCssW4 = fs.readFileSync(path.join(process.cwd(), 'web', 'css', 'discover.css'), 'utf8');

  // 13a. ZERO `.src = ''` or `.src = ""` anywhere in player.js
  assert.equal(/\.src\s*=\s*['"]['"]/.test(playerSrc), false, 'web/js/player.js must NEVER assign empty string to .src');

  // Empty/absent src & isLoadingTrack error events must NOT trigger handleAudioElementError / api.getAudio retries
  const origGetAudioW4 = api.getAudio;
  const origGetLyricsW4 = api.getLyrics;
  const origDirectPlayW4 = player.directAudio.play;
  try {
    let audioCalls = 0;
    api.getAudio = async (id) => {
      audioCalls += 1;
      return { ok: true, data: { url: `https://m8.music.126.net/track_${id}.mp3`, format: 'mp3', level: 'exhigh' }, error: null };
    };
    api.getLyrics = async () => ({ ok: true, data: { lrc: '[00:01.00]Wave 4 Line', yrc: '[1000,2000](1000,1000,0)Wave(2000,1000,0)4' }, error: null });
    player.directAudio.play = async function () { this.paused = false; };

    player.stopAndReset();
    assert.equal(player.directAudio.getAttribute('src'), null, 'stopAndReset must removeAttribute("src") on directAudio');
    assert.equal(player.streamAudio.getAttribute('src'), null, 'stopAndReset must removeAttribute("src") on streamAudio');

    // Stray error on empty src must be ignored
    player.directAudio.dispatchEvent({ type: 'error' });
    await new Promise(r => setTimeout(r, 10));
    assert.equal(audioCalls, 0, 'Error event on element without active src must NOT trigger retry');

    // Single getAudio call per playSong
    await player.playSong({ id: 441001, title: 'W4 Track', artists: ['Artist'], duration: 180000 }, true);
    assert.equal(audioCalls, 1, 'playSong must invoke api.getAudio exactly once');

    // Error during isLoadingTrack must be ignored
    player.isLoadingTrack = true;
    player.directAudio.dispatchEvent({ type: 'error' });
    await new Promise(r => setTimeout(r, 10));
    assert.equal(audioCalls, 1, 'Error event while isLoadingTrack=true must NOT trigger retry');
    player.isLoadingTrack = false;

    // Dual-playback timeupdate guard pauses & strips ghost element
    player.audio = player.directAudio;
    player.streamAudio.paused = false;
    player.streamAudio.setAttribute('src', '/api/stream/ghost_dual');
    player.directAudio.dispatchEvent({ type: 'timeupdate' });
    assert.equal(player.streamAudio.paused, true, 'timeupdate guard must immediately pause secondary audio element');
    assert.equal(player.streamAudio.getAttribute('src'), null, 'timeupdate guard must strip src from secondary audio element');

    // YRC words + sent_at in getActiveLyricSnapshot
    const snap = lyricsManager.getActiveLyricSnapshot(1.2);
    assert.equal(Array.isArray(snap.words) && snap.words.length === 2, true, 'getActiveLyricSnapshot must emit parsed YRC words array');
    assert.equal(snap.words[0].text, 'Wave');
    assert.equal(typeof snap.sent_at === 'number' && snap.sent_at > 0, true, 'getActiveLyricSnapshot must include sent_at timestamp');

    // 13b. Deck Queue Protection: playSong with { preserveQueue: true, origin: 'deck' } preserves playQueue & stops on ended
    const qTracks = [
      { id: 442001, title: 'Queue A', artists: ['QA'], duration: 180000 },
      { id: 442002, title: 'Queue B', artists: ['QB'], duration: 180000 },
    ];
    await player.playQueue(qTracks, 0, 'order');
    assert.equal(state.get('playQueue').length, 2);
    assert.equal(state.get('playQueueIndex'), 0);

    const deckTrack = { id: 442099, title: 'Deck Preview', artists: ['DP'], duration: 180000 };
    await player.playSong(deckTrack, true, { preserveQueue: true, origin: 'deck' });
    assert.equal(state.get('playQueue').length, 2, 'Deck playback must NOT overwrite global playQueue');
    assert.equal(state.get('playQueue')[0].id, 442001);
    assert.equal(player.currentSong._origin, 'deck', 'Deck playback must stamp _origin = "deck"');

    const beforeEndedId = player.currentSong.id;
    await player.handleTrackEnded();
    assert.equal(player.currentSong.id, beforeEndedId, 'handleTrackEnded on deck track must NOT advance global queue');
    assert.equal(state.get('playQueueIndex'), 0, 'Global playQueueIndex must stay intact after deck track ends');
  } finally {
    player.directAudio.play = origDirectPlayW4;
    api.getAudio = origGetAudioW4;
    api.getLyrics = origGetLyricsW4;
    player.stopAndReset();
  }

  // 13c. Deck wrong-song lyrics isolation + stale async guard & Deck CSS no-overlap
  const origDocDeckLyr = globalThis.document;
  const origGetLyricsDeck = api.getLyrics;
  try {
    const statusNode = { textContent: '' };
    const previewNode = {
      hidden: true,
      classList: {
        add(c) { if (c === 'hidden') previewNode.hidden = true; },
        remove(c) { if (c === 'hidden') previewNode.hidden = false; },
      },
    };
    const deckLyricsBox = {
      dataset: {},
      _text: '',
      get textContent() { return this._text; },
      set textContent(v) { this._text = String(v); if (!v) this.children = []; },
      children: [],
      appendChild(n) { this.children.push(n); return n; },
    };
    globalThis.document = {
      getElementById: (id) => {
        if (id === 'deck-lyrics-status-text') return statusNode;
        if (id === 'deck-lyrics-preview-btn') return previewNode;
        if (id === 'deck-lyrics-container') return deckLyricsBox;
        return null;
      },
      createElement: (tag) => {
        const el = {
          tagName: tag.toUpperCase(),
          className: '',
          textContent: '',
          dataset: {},
          children: [],
          setAttribute() {},
          addEventListener() {},
          appendChild(c) { this.children.push(c); return c; },
        };
        return el;
      },
    };

    const songPlaying = { id: 50001, title: 'Playing Song', artists: ['A'] };
    const songDeckA = { id: 50002, title: 'Deck Song A', artists: ['B'] };
    const songDeckB = { id: 50003, title: 'Deck Song B', artists: ['C'] };
    state.set({
      activeTab: 'deck',
      currentPlayingSong: songPlaying,
      deckQueue: [songDeckA, songDeckB],
      deckIndex: 0,
    });
    lyricsManager.loadLyrics('[00:01.00]Playing Song Lyric', '', { songId: songPlaying.id });

    let resolveA;
    api.getLyrics = (id) => {
      if (String(id) === '50002') {
        return new Promise((r) => { resolveA = r; });
      }
      return Promise.resolve({
        ok: true,
        data: { lyrics: '[00:01.00]Deck B Static Lyric', tlyrics: '[00:01.00]Deck B 翻译' },
      });
    };

    deckManager.deckLyricsCache.clear();
    const pA = deckManager.syncDeckLyricsForSong(songDeckA);
    assert.equal(statusNode.textContent, '未在播放 · 按 P 试听', 'Mismatched deck song must display not-playing status header');
    assert.equal(previewNode.hidden, false, 'Mismatched deck song must show compact preview play button');
    assert.equal(deckLyricsBox.dataset.deckSynced, 'false', 'Mismatched deck lyrics must set deckSynced=false');

    // User advances deck to song B before song A's lyrics return
    state.set({ deckIndex: 1 });
    await deckManager.syncDeckLyricsForSong(songDeckB);
    // Now stale response for song A resolves late — MUST be ignored!
    resolveA({ ok: true, data: { lyrics: '[00:01.00]Stale Deck A Lyric', tlyrics: '' } });
    await pA;

    const collectText = (node) => [node?.textContent || '', ...(node?.children || []).map(collectText)].join(' ');
    const renderedText = collectText(deckLyricsBox);
    assert.equal(renderedText.includes('Deck B Static Lyric'), true, 'Deck must render song B static lyrics from backend { lyrics, tlyrics } shape');
    assert.equal(renderedText.includes('Deck B 翻译'), true, 'Deck must render song B translation from backend tlyrics field');
    assert.equal(renderedText.includes('Stale Deck A Lyric'), false, 'Stale async lyrics response must be discarded');
    assert.equal(lyricsManager.lyricsData[0]?.text, 'Playing Song Lyric', 'Mismatched deck static lyrics must NOT overwrite active playing song lyricsData');
  } finally {
    api.getLyrics = origGetLyricsDeck;
    globalThis.document = origDocDeckLyr;
    deckManager.deckLyricsCache.clear();
  }

  const appSrc = fs.readFileSync(path.join(process.cwd(), 'web', 'js', 'app.js'), 'utf8');
  assert.equal(appSrc.includes('searchView.open(detail.query || \'\', detail.channel ? { channel: detail.channel } : {})'), true, 'app.js switch-tab must forward detail.channel to searchView.open');

  const deckHeaderBlock = deckCssW4.slice(deckCssW4.indexOf('.deck-lyrics-header'), deckCssW4.indexOf('.deck-lyrics-scroller'));
  assert.equal(deckHeaderBlock.includes('position: absolute'), false, '.deck-lyrics-header must be normal flow above scroller, NOT position: absolute');
  const deckWrapperBlock = deckCssW4.slice(deckCssW4.indexOf('.deck-lyrics-wrapper'), deckCssW4.indexOf('.deck-lyrics-header'));
  assert.equal(deckWrapperBlock.includes('mask-image'), false, '.deck-lyrics-wrapper must NOT carry mask-image over header');

  assert.equal(indexHtml.includes('id="view-search"'), true, 'index.html must include #view-search host');
  assert.equal(indexHtml.includes('id="settings-appearance-host"'), true, 'index.html must include #settings-appearance-host');
  assert.equal(indexHtml.includes('id="context-menu-host"'), true, 'index.html must include #context-menu-host');
  assert.equal(indexHtml.includes('id="player-inline-lyric"'), true, 'index.html must include #player-inline-lyric');
  assert.equal(indexHtml.includes('id="player-more-btn"'), true, 'index.html must include #player-more-btn');
  assert.equal(indexHtml.includes('id="player-more-popover"'), true, 'index.html must include #player-more-popover');
  assert.equal(indexHtml.includes('id="shortcuts-help-modal"'), true, 'index.html must include #shortcuts-help-modal');
  assert.equal(indexHtml.includes('>词</button>'), true, 'Player bar must render explicit text-glyph button 词');
  assert.equal(indexHtml.includes('Personal FM'), false, 'Discover header must not include English "Personal FM"');
  assert.equal(indexHtml.includes('id="discover-explore-slider"') && indexHtml.includes('step="1"'), true, 'Smart Radio slider must use continuous step="1"');
  assert.equal(discoverCssW4.includes('var(--radio-amber) var(--fill)'), true, 'Smart Radio precision tuner slider track must use var(--radio-amber) and var(--fill)');
  assert.equal(/\.discover-dusty-grid\s*\{[^}]*display:\s*grid[^}]*grid-template-columns:\s*repeat\(4,\s*minmax\(0,\s*1fr\)\)/s.test(discoverCssW4), true, 'Dusty gems showcase must use 4-column 2-row responsive grid');
  assert.equal(indexHtml.includes('id="discover-dusty-prev"') && indexHtml.includes('id="discover-dusty-refresh"') && indexHtml.includes('id="discover-dusty-view-all"'), true, 'Dusty gems showcase must expose 上一批 / 换一批 / 查看全部 controls');
  assert.equal(indexHtml.includes('id="view-dusty"') && indexHtml.includes('id="discover-radio-stage"'), true, 'index.html must mount dedicated #view-dusty and Smart Radio vinyl stage');
  assert.equal(discoverManager.computeSliderFillPercent(37), '37%', 'Slider --fill must map 0..100 range accurately for 37%');
  assert.equal(discoverManager.computeSliderFillPercent(0), '0%', 'Slider --fill must map min=0 to 0%');
  assert.equal(discoverManager.computeSliderFillPercent(100), '100%', 'Slider --fill must map max=100 to 100%');
  assert.equal(discoverManager.computeSliderFillPercent(37, { min: '20', max: '80' }), '28.33%', 'Slider --fill must honor explicit min/max attributes');
  assert.equal(indexHtml.includes('data-deck-mode="instant"') && indexHtml.includes('data-deck-mode="batch"'), true, 'Deck mode toggle must be a 2-option segmented control');

  // 13d. Toast max-3 cap, 2s dedupe, and toast.quiet() max-1 cap
  const { toast } = await import('../../web/js/toast.js');
  const origDocToast = globalThis.document;
  try {
    const fakeChildren = [];
    const fakeContainer = {
      get children() { return fakeChildren; },
      appendChild(node) {
        node.parentNode = fakeContainer;
        fakeChildren.push(node);
        return node;
      },
      removeChild(node) {
        const idx = fakeChildren.indexOf(node);
        if (idx >= 0) fakeChildren.splice(idx, 1);
      },
      querySelectorAll(sel) {
        if (sel === '.toast-quiet') return fakeChildren.filter(c => String(c.className || '').includes('toast-quiet'));
        return [...fakeChildren];
      },
    };
    const makeFakeEl = (tag) => {
      const el = {
        tagName: tag.toUpperCase(),
        className: '',
        textContent: '',
        style: {},
        dataset: {},
        children: [],
        parentNode: null,
        classList: {
          add(...cls) { el.className = `${el.className} ${cls.join(' ')}`.trim(); },
          remove(...cls) {
            el.className = el.className.split(/\s+/).filter(c => c && !cls.includes(c)).join(' ');
          },
          contains(c) { return el.className.split(/\s+/).includes(c); },
        },
        setAttribute() {},
        addEventListener() {},
        appendChild(child) { child.parentNode = el; el.children.push(child); return child; },
        removeChild(child) { const i = el.children.indexOf(child); if (i >= 0) el.children.splice(i, 1); },
      };
      return el;
    };
    globalThis.document = {
      getElementById: (id) => (id === 'toast-container' ? fakeContainer : null),
      createElement: makeFakeEl,
      createElementNS: () => makeFakeEl('svg'),
    };
    toast.container = null;
    toast.clearAll();

    // Dedupe identical toast within 2s
    toast.info('重复测试', '相同内容', 5000);
    toast.info('重复测试', '相同内容', 5000);
    assert.equal(fakeChildren.length, 1, 'Identical toast within 2s must be deduped');

    // Cap at max 3 visible toasts
    toast.info('消息二', '描述2', 5000);
    toast.info('消息三', '描述3', 5000);
    toast.info('消息四', '描述4', 5000);
    assert.equal(fakeChildren.length, 3, 'Visible toasts must be capped at max 3');

    // toast.quiet caps at max 1 quiet toast
    toast.clearAll();
    toast.quiet('音量 85%');
    toast.quiet('音量 90%');
    assert.equal(fakeContainer.querySelectorAll('.toast-quiet').length, 1, 'toast.quiet must stack at most 1 quiet toast');
    toast.clearAll();
  } finally {
    toast.container = null;
    globalThis.document = origDocToast;
  }

  console.log('✓ v0.3 upgrade architecture & safety tests passed!');
}
