import assert from 'node:assert/strict';

if (typeof globalThis.window === 'undefined') {
  globalThis.window = { addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => {} };
}
if (typeof globalThis.navigator === 'undefined') {
  globalThis.navigator = { onLine: true };
}
if (typeof globalThis.localStorage === 'undefined') {
  const values = new Map();
  globalThis.localStorage = {
    getItem: key => values.get(key) || null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
    clear: () => values.clear(),
  };
}

import { api } from '../../web/js/api.js';
import { auth } from '../../web/js/auth.js';
import { App } from '../../web/js/app.js';
import { catalogManager } from '../../web/js/catalog.js';
import { cloudManager } from '../../web/js/cloud.js';
import { deckManager } from '../../web/js/deck.js';
import { modal } from '../../web/js/modal.js';
import { rulesManager } from '../../web/js/rules.js';
import { state } from '../../web/js/state.js';
import { toast } from '../../web/js/toast.js';

export async function runIntegrationContractTests() {
  console.log('Testing cross-layer frontend contracts...');

  const liked = { id: 10, name: '我喜欢的音乐', kind: 'liked', track_count: 3 };
  const owned = { id: 20, name: '自建歌单', kind: 'self_created', track_count: 1 };
  const imported = { id: 'imported', name: '本地导入', kind: 'imported', track_count: 1 };
  const s1 = { id: 1, title: '一', source_playlist_id: 10 };
  const s2 = { id: 2, title: '二', source_playlist_id: 10 };
  const s3 = { id: 3, title: '三', source_playlist_id: 10 };

  // Dropdown hydration must show the same canonical liked playlist stored in state,
  // even when that playlist is not the first item returned by the backend.
  const originalDocument = globalThis.document;
  const makeSelect = () => {
    const select = {
      options: [],
      value: '',
      appendChild(option) {
        this.options.push(option);
        if (!this.value) this.value = option.value;
      },
    };
    Object.defineProperty(select, 'textContent', {
      set() {
        this.options = [];
        this.value = '';
      },
    });
    return select;
  };
  const deckSelect = makeSelect();
  const catalogSelect = makeSelect();
  globalThis.document = {
    getElementById(id) {
      if (id === 'deck-playlist-select') return deckSelect;
      if (id === 'catalog-playlist-filter') return catalogSelect;
      return null;
    },
    createElement(tag) {
      return {
        tagName: String(tag).toUpperCase(),
        textContent: '',
        value: '',
        children: [],
        dataset: {},
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        setAttribute(name, value) { this[name] = String(value); },
        addEventListener() {},
        appendChild(child) { this.children.push(child); },
      };
    },
    createTextNode(text) { return { textContent: String(text) }; },
  };
  try {
    const canonicalId = state.resolveCanonicalPlaylistId([owned, liked]);
    state.set({ playlists: [owned, liked], selectedPlaylistId: canonicalId });
    cloudManager.populatePlaylistDropdowns([owned, liked], canonicalId);
    assert.equal(canonicalId, liked.id);
    assert.equal(deckSelect.value, String(liked.id));
    assert.equal(catalogSelect.value, 'all');
  } finally {
    globalThis.document = originalDocument;
  }

  state.set({
    authenticated: true,
    user: { user_id: 9911, display_name: '契约测试' },
    playlists: [owned, liked, imported],
    selectedPlaylistId: liked.id,
    deckFilter: 'undecided',
    deckOrder: 'oldest',
    catalog: [s1, s2, s3],
    deckQueue: [s1, s2, s3],
    deckIndex: 1,
    keptIds: new Set(['1']),
    skippedIds: new Set(),
    batchQueue: [],
    hasUnknownResult: false,
    destructiveFrozen: false,
    unknownResultDetail: null,
  });

  // Progress is anchored to the current song, not to an index that shifts when kept songs disappear.
  deckManager.saveCurrentProgress();
  deckManager.reloadDeck();
  assert.equal(deckManager.getCurrentSong().id, 2);
  assert.equal(state.get('deckIndex'), 0);

  // Imported songs cannot enter any cloud mutation queue.
  state.set({ batchQueue: [] });
  const importedSong = { id: 88, title: '导入曲目', source_playlist_id: 'imported' };
  assert.equal(state.stageBatchAction(importedSong, 'remove_tracks', 'imported', '本地导入'), false);
  assert.equal(state.get('batchQueue').length, 0);

  // The first destructive write in a session creates one shared snapshot.
  const originalBackup = api.backup;
  let backupCalls = 0;
  api.backup = async () => {
    backupCalls += 1;
    return { ok: true, data: { operation_id: 'snapshot' }, error: null };
  };
  deckManager.resetSessionSnapshot();
  assert.equal(await deckManager.ensureSessionSnapshot(), true);
  assert.equal(await deckManager.ensureSessionSnapshot(), true);
  assert.equal(backupCalls, 1);
  api.backup = originalBackup;

  const originalReconcile = api.reconcileOperations;

  // Reconcile consumes the backend's canonical successful_ids field.
  state.set({
    catalog: [s1, s2],
    deckQueue: [s1, s2],
    deckIndex: 1,
    hasUnknownResult: false,
    destructiveFrozen: false,
  });
  api.reconcileOperations = async () => ({
    ok: true,
    data: { successful_ids: [1], failed_items: [], unknown_items: [] },
    error: null,
  });
  await deckManager.handleCommitFailureWithReconcile(s1, 'unlike', 10, liked.name, 0);
  assert.equal(state.get('catalog').some(song => song.id === 1 && song.source_playlist_id === 10), false);
  assert.equal(state.get('destructiveFrozen'), false);

  // A known-not-applied reconcile restores the song and does not freeze the app.
  state.set({
    catalog: [s1, s2],
    deckQueue: [s1, s2],
    deckIndex: 1,
    hasUnknownResult: false,
    destructiveFrozen: false,
  });
  api.reconcileOperations = async () => ({
    ok: true,
    data: { successful_ids: [], failed_items: [{ id: 1, code: 'UNLIKE_NOT_APPLIED', reason: '仍在喜欢列表' }], unknown_items: [] },
    error: null,
  });
  await deckManager.handleCommitFailureWithReconcile(s1, 'unlike', 10, liked.name, 0);
  assert.equal(deckManager.getCurrentSong().id, 1);
  assert.equal(state.get('destructiveFrozen'), false);

  // If the read-only check itself cannot decide, the song remains local and writes freeze.
  state.set({ deckIndex: 1, hasUnknownResult: false, destructiveFrozen: false });
  api.reconcileOperations = async () => ({ ok: false, data: null, error: { code: 'UPSTREAM_HTTP_ERROR' } });
  await deckManager.handleCommitFailureWithReconcile(s1, 'unlike', 10, liked.name, 0);
  assert.equal(state.get('catalog').some(song => song.id === 1), true);
  assert.equal(deckManager.getCurrentSong().id, 1);
  assert.equal(state.get('destructiveFrozen'), true);

  api.reconcileOperations = originalReconcile;
  state.set({ hasUnknownResult: false, destructiveFrozen: false, unknownResultDetail: null });

  // Ignore is authoritative only after the backend journal confirms it. A
  // pending-list outage must retain the global freeze instead of falling back
  // to a browser-only marker.
  const originalIgnorePendingOperation = api.ignorePendingOperation;
  const originalPendingAfterIgnore = api.getPendingOperations;
  state.set({
    catalog: [s1, s2],
    deckQueue: [s1, s2],
    deckIndex: 0,
    hasUnknownResult: true,
    destructiveFrozen: true,
    unknownResultDetail: {
      song: s1,
      action: 'unlike',
      playlistId: 10,
      targetName: liked.name,
      originalIndex: 0,
      operationId: 'pending-op-1',
    },
  });
  api.ignorePendingOperation = async () => ({ ok: true, data: { state: 'ignored' } });
  api.getPendingOperations = async () => ({ ok: false, data: null, error: { code: 'LOCAL_DATA_UNREADABLE' } });
  await deckManager.dismissUnknownResultLocally();
  assert.equal(state.get('destructiveFrozen'), true);
  assert.equal(state.get('hasUnknownResult'), true);

  api.getPendingOperations = async () => ({ ok: true, data: { operations: [] }, error: null });
  await deckManager.dismissUnknownResultLocally();
  assert.equal(state.get('destructiveFrozen'), false);
  assert.equal(state.get('hasUnknownResult'), false);
  api.ignorePendingOperation = originalIgnorePendingOperation;
  api.getPendingOperations = originalPendingAfterIgnore;

  // A confirmed playlist creation with definite add failures preserves only
  // those failed tracks in the catalog selection and reports a warning.
  let createHandler = null;
  const createSubmit = {
    disabled: false,
    addEventListener(type, handler) {
      if (type === 'click') createHandler = handler;
    },
  };
  globalThis.document = {
    getElementById(id) {
      if (id === 'create-playlist-submit-btn') return createSubmit;
      if (id === 'create-playlist-name-input') return { value: 'Partial List' };
      return null;
    },
  };
  const originalCreatePlaylist = api.createPlaylist;
  const originalCatalogRender = catalogManager.render;
  const originalLoadPlaylists = cloudManager.loadPlaylists;
  const originalModalClose = modal.close;
  const originalToastInfo = toast.info;
  const originalToastWarning = toast.warning;
  const originalToastSuccess = toast.success;
  let warningTitle = null;
  try {
    state.set({ catalog: [s1, s2], catalogSelectedIds: new Set(['10:1', '10:2']) });
    api.createPlaylist = async () => ({
      ok: true,
      data: {
        successful_ids: [1],
        failed_items: [{ id: 2, code: 'UPSTREAM_REJECTED', reason: 'rejected' }],
        unknown_items: [],
        pending_operation_ids: [],
      },
      error: null,
    });
    catalogManager.render = () => {};
    cloudManager.loadPlaylists = async () => {};
    modal.close = () => {};
    toast.info = () => {};
    toast.success = () => {};
    toast.warning = title => { warningTitle = title; };
    new App().initModalsAndForms();
    assert.equal(typeof createHandler, 'function');
    await createHandler();
    assert.deepEqual([...state.get('catalogSelectedIds')], ['10:2']);
    assert.equal(warningTitle, '歌单已创建，部分歌曲未添加');
  } finally {
    globalThis.document = originalDocument;
    api.createPlaylist = originalCreatePlaylist;
    catalogManager.render = originalCatalogRender;
    cloudManager.loadPlaylists = originalLoadPlaylists;
    modal.close = originalModalClose;
    toast.info = originalToastInfo;
    toast.warning = originalToastWarning;
    toast.success = originalToastSuccess;
  }

  // A delayed single-track commit must re-check the global freeze at expiry.
  const originalCreateSoftCommit = state.createSoftCommit;
  const originalSingleBackup = api.backup;
  const originalSingleUnlike = api.unlike;
  let delayedCommit = null;
  let delayedBackupCalls = 0;
  let delayedUnlikeCalls = 0;
  state.createSoftCommit = (_song, _action, _playlistId, _targetName, commitCallback) => {
    delayedCommit = commitCallback;
    return {};
  };
  api.backup = async () => { delayedBackupCalls += 1; return { ok: true, data: {} }; };
  api.unlike = async () => { delayedUnlikeCalls += 1; return { ok: true, data: { successful_ids: [2] } }; };
  state.set({
    catalog: [s1, s2],
    deckQueue: [s1, s2],
    deckIndex: 1,
    selectedPlaylistId: 10,
    instantModeEnabled: true,
    hasUnknownResult: false,
    destructiveFrozen: false,
  });
  deckManager.resetSessionSnapshot();
  deckManager.handleDestructiveAction();
  assert.equal(typeof delayedCommit, 'function');
  state.set({ hasUnknownResult: true, destructiveFrozen: true });
  await delayedCommit();
  assert.equal(delayedBackupCalls, 0, 'Frozen delayed commit must not create a snapshot');
  assert.equal(delayedUnlikeCalls, 0, 'Frozen delayed commit must not write');
  state.createSoftCommit = originalCreateSoftCommit;
  api.backup = originalSingleBackup;
  api.unlike = originalSingleUnlike;
  state.set({ hasUnknownResult: false, destructiveFrozen: false });

  // A batch item already marked unknown is reconciled read-only and is never written again.
  const originalUnlike = api.unlike;
  const originalCloudBackup = api.backup;
  const originalRenderQueue = cloudManager.renderGroupedBatchQueue;
  const originalRenderLogs = cloudManager.renderLogs;
  const originalReloadCatalog = cloudManager.reloadCatalog;
  let unlikeCalls = 0;
  let reconcileCalls = 0;
  let batchBackupCalls = 0;
  api.unlike = async () => {
    unlikeCalls += 1;
    return { ok: true, data: { successful_ids: [1], failed_items: [], unknown_items: [] } };
  };
  api.backup = async () => {
    batchBackupCalls += 1;
    return { ok: true, data: {} };
  };
  api.reconcileOperations = async () => {
    reconcileCalls += 1;
    return {
      ok: true,
      data: { successful_ids: [], failed_items: [{ id: 1, code: 'UNLIKE_NOT_APPLIED', reason: '仍在喜欢列表' }], unknown_items: [] },
    };
  };
  cloudManager.renderGroupedBatchQueue = () => {};
  cloudManager.renderLogs = () => {};
  cloudManager.reloadCatalog = async () => {};
  state.set({
    batchQueue: [{ song: s1, action: 'unlike', playlistId: 10, targetName: liked.name, status: 'unknown' }],
  });
  await cloudManager.executeConfirmedBatch();
  assert.equal(unlikeCalls, 0, 'Unknown items must not repeat the cloud write');
  assert.equal(batchBackupCalls, 0, 'A read-only reconciliation does not need a new write snapshot');
  assert.equal(reconcileCalls, 1);
  assert.equal(state.get('batchQueue')[0].status, undefined, 'Known-not-applied items return to ordinary pending state');

  // A global unknown/frozen state blocks batch execution before snapshots,
  // writes, or even queue reconciliation begins.
  state.set({ hasUnknownResult: true, destructiveFrozen: true });
  await cloudManager.executeConfirmedBatch();
  assert.equal(unlikeCalls, 0, 'Frozen batch execution must not write');
  assert.equal(batchBackupCalls, 0, 'Frozen batch execution must not create a write snapshot');
  assert.equal(reconcileCalls, 1, 'Frozen batch execution must not start a second queue pass');

  // Logout cancels soft commits while the old account scope still exists.
  const originalLogout = api.logout;
  const originalUpdateUserUI = auth.updateUserUI;
  const originalUpdateLoginShell = auth.updateLoginShell;
  api.logout = async () => ({ ok: true, data: {}, error: null });
  auth.updateUserUI = () => {};
  auth.updateLoginShell = () => {};
  localStorage.removeItem('pruner_prog_9911_10_undecided_oldest');
  localStorage.removeItem('pruner_prog_guest_10_undecided_oldest');
  state.set({
    authenticated: true,
    user: { user_id: 9911 },
    selectedPlaylistId: 10,
    deckFilter: 'undecided',
    deckOrder: 'oldest',
    deckQueue: [s1],
    deckIndex: 0,
    pendingCommits: [],
  });
  state.createSoftCommit(s1, 'unlike', 10, liked.name, () => {}, () => deckManager.saveCurrentProgress(), 0);
  await auth.logout();
  assert.ok(localStorage.getItem('pruner_prog_9911_10_undecided_oldest'), 'Cancelled commit restores progress into the old account scope');
  assert.equal(localStorage.getItem('pruner_prog_guest_10_undecided_oldest'), null, 'Logout must not write review progress into guest scope');
  api.logout = originalLogout;
  auth.updateUserUI = originalUpdateUserUI;
  auth.updateLoginShell = originalUpdateLoginShell;

  // Structured API failures must surface as catalogError, not a silent empty catalog.
  const originalGetPlaylists = api.getPlaylists;
  const originalGetCatalog = api.getCatalog;
  api.getPlaylists = async () => ({ ok: true, data: { playlists: [] }, error: null });
  api.getCatalog = async () => ({ ok: false, data: null, error: { code: 'CATALOG_FAILED', message: '曲库读取失败' } });
  state.set({ authenticated: true, catalogError: null, hasUnknownResult: false, destructiveFrozen: false });
  const app = new App();
  app.updatePendingHeaderBadge = () => {};
  const originalRenderPlaylistsForApp = cloudManager.renderPlaylists;
  const originalPopulateDropdowns = cloudManager.populatePlaylistDropdowns;
  const originalRenderGroupedQueueForApp = cloudManager.renderGroupedBatchQueue;
  cloudManager.renderPlaylists = () => {};
  cloudManager.populatePlaylistDropdowns = () => {};
  cloudManager.renderGroupedBatchQueue = () => {};
  await app.loadInitialData();
  assert.equal(state.get('catalogError'), '曲库读取失败');
  assert.equal(deckManager.determineWorkspaceState(), 'error', 'Structured startup failures must render the error workspace');
  cloudManager.renderPlaylists = originalRenderPlaylistsForApp;
  cloudManager.populatePlaylistDropdowns = originalPopulateDropdowns;
  cloudManager.renderGroupedBatchQueue = originalRenderGroupedQueueForApp;
  api.getPlaylists = originalGetPlaylists;
  api.getCatalog = originalGetCatalog;

  // Auth startup must restore durable backend recovery state and freeze every
  // new destructive write until the recorded operation is reconciled/ignored.
  const recoveredOperation = {
    operation_id: 'a'.repeat(32),
    action: 'unlike',
    state: 'sent_unknown',
    target: { playlist_id: 10, source_playlist_id: 10, song_ids: [1] },
    resource_id: null,
  };
  const originalGetStatusForRecovery = api.getStatus;
  const originalRecoveryUserUi = auth.updateUserUI;
  const originalRecoveryLoginShell = auth.updateLoginShell;
  api.getStatus = async () => ({
    ok: true,
    data: {
      authenticated: true,
      user: { user_id: 9911, display_name: '恢复测试' },
      pending_operations: [recoveredOperation],
    },
    error: null,
  });
  auth.updateUserUI = () => {};
  auth.updateLoginShell = () => {};
  await auth.checkSession();
  assert.equal(state.get('destructiveFrozen'), true, 'Recovered pending writes must freeze destructive actions');
  assert.equal(state.get('hasUnknownResult'), true);
  assert.equal(state.get('pendingOperations')[0].operation_id, recoveredOperation.operation_id);

  const originalReconcilePending = api.reconcilePendingOperation;
  const originalGetPending = api.getPendingOperations;
  let recoveredReconcileCalls = 0;
  api.reconcilePendingOperation = async id => {
    recoveredReconcileCalls += 1;
    assert.equal(id, recoveredOperation.operation_id);
    return { ok: true, data: { successful_ids: [1] }, error: null };
  };
  api.getPendingOperations = async () => ({ ok: true, data: { operations: [] }, error: null });
  assert.equal(typeof deckManager.reconcileRecoveredOperation, 'function');
  await deckManager.reconcileRecoveredOperation(recoveredOperation);
  assert.equal(recoveredReconcileCalls, 1);
  assert.equal(state.get('destructiveFrozen'), false, 'Confirmed recovery must release the global write freeze');
  assert.deepEqual(state.get('pendingOperations'), []);

  api.getStatus = originalGetStatusForRecovery;
  auth.updateUserUI = originalRecoveryUserUi;
  auth.updateLoginShell = originalRecoveryLoginShell;
  api.reconcilePendingOperation = originalReconcilePending;
  api.getPendingOperations = originalGetPending;

  // The open pending drawer must refresh countdown labels and release its
  // interval when no soft commits remain.
  const originalSetInterval = globalThis.setInterval;
  const originalClearInterval = globalThis.clearInterval;
  const countdownDocument = globalThis.document;
  let intervalCallback = null;
  let clearedIntervals = 0;
  globalThis.setInterval = callback => {
    intervalCallback = callback;
    return 77;
  };
  globalThis.clearInterval = id => {
    if (id === 77) clearedIntervals += 1;
  };
  try {
    globalThis.document = {
      getElementById(id) {
        if (id === 'pending-actions-drawer') {
          return { classList: { contains: value => value === 'open' } };
        }
        return null;
      },
    };
    const countdownApp = new App();
    let countdownRenders = 0;
    countdownApp.renderPendingDrawer = () => { countdownRenders += 1; };
    state.set({ pendingCommits: [{ id: 'soft', expiresAt: Date.now() + 3000 }] });
    assert.equal(typeof countdownApp.startPendingDrawerRefresh, 'function');
    countdownApp.startPendingDrawerRefresh();
    assert.equal(typeof intervalCallback, 'function');
    intervalCallback();
    assert.equal(countdownRenders, 1);
    state.set({ pendingCommits: [] });
    intervalCallback();
    assert.equal(clearedIntervals, 1, 'Countdown interval must stop when the drawer has no soft commits');
  } finally {
    globalThis.setInterval = originalSetInterval;
    globalThis.clearInterval = originalClearInterval;
    globalThis.document = countdownDocument;
  }

  // Startup data from a logged-out or superseded account must never refill
  // the current account's state after its requests finish out of order.
  const originalLoadRules = rulesManager.loadRules;
  const originalSyncPlaylistForRace = api.syncPlaylist;
  const originalRaceRenderPlaylists = cloudManager.renderPlaylists;
  const originalRacePopulateDropdowns = cloudManager.populatePlaylistDropdowns;
  const deferred = () => {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return { promise, resolve };
  };
  const raceApp = new App();
  raceApp.onCatalogUpdated = () => {};
  rulesManager.loadRules = async () => {};
  cloudManager.renderPlaylists = () => {};
  cloudManager.populatePlaylistDropdowns = () => {};
  try {
    const logoutPlaylists = deferred();
    const logoutCatalog = deferred();
    api.getPlaylists = () => logoutPlaylists.promise;
    api.getCatalog = () => logoutCatalog.promise;
    state.set({
      authenticated: true,
      user: { user_id: 1001 },
      playlists: [],
      catalog: [],
      selectedPlaylistId: null,
    });
    const staleAfterLogout = raceApp.loadInitialData();
    state.set({ authenticated: false, user: null, playlists: [], catalog: [], selectedPlaylistId: null, catalogLoading: false });
    assert.equal(typeof raceApp.invalidateInitialDataLoads, 'function', 'App must expose a logout/session invalidation gate');
    raceApp.invalidateInitialDataLoads();
    logoutPlaylists.resolve({ ok: true, data: { playlists: [{ id: 10, name: '旧账号喜欢', kind: 'liked' }] } });
    logoutCatalog.resolve({ ok: true, data: { tracks: [{ id: 1, source_playlist_id: 10 }] } });
    await staleAfterLogout;
    assert.equal(state.get('authenticated'), false);
    assert.deepEqual(state.get('playlists'), [], 'Logged-out state must not be repopulated by an old request');
    assert.deepEqual(state.get('catalog'), [], 'Logged-out catalog must remain empty after an old request');

    const accountAPlaylists = deferred();
    const accountACatalog = deferred();
    const accountBPlaylists = deferred();
    const accountBCatalog = deferred();
    let playlistRequest = 0;
    let catalogRequest = 0;
    api.getPlaylists = () => (++playlistRequest === 1 ? accountAPlaylists.promise : accountBPlaylists.promise);
    api.getCatalog = () => (++catalogRequest === 1 ? accountACatalog.promise : accountBCatalog.promise);

    state.set({ authenticated: true, user: { user_id: 2001 }, playlists: [], catalog: [], selectedPlaylistId: null });
    const loadA = raceApp.loadInitialData();
    state.set({ authenticated: true, user: { user_id: 2002 }, playlists: [], catalog: [], selectedPlaylistId: null });
    const loadB = raceApp.loadInitialData();
    accountBPlaylists.resolve({ ok: true, data: { playlists: [{ id: 20, name: '账号 B 喜欢', kind: 'liked' }] } });
    accountBCatalog.resolve({ ok: true, data: { tracks: [{ id: 2, source_playlist_id: 20 }] } });
    await loadB;
    accountAPlaylists.resolve({ ok: true, data: { playlists: [{ id: 10, name: '账号 A 喜欢', kind: 'liked' }] } });
    accountACatalog.resolve({ ok: true, data: { tracks: [{ id: 1, source_playlist_id: 10 }] } });
    await loadA;
    assert.equal(state.get('user').user_id, 2002);
    assert.deepEqual(state.get('playlists').map(item => item.id), [20], 'A slower old-account load must not replace the new account');
    assert.deepEqual(state.get('catalog').map(item => item.id), [2], 'Catalog results must remain scoped to the newest account load');
    assert.equal(state.get('selectedPlaylistId'), 20);

    const syncStarted = deferred();
    const releaseOldSync = deferred();
    const syncCalls = [];
    api.getPlaylists = async () => ({
      ok: true,
      data: {
        playlists: [state.getUserId() === '3001'
          ? { id: 30, name: '账号 A 喜欢', kind: 'liked' }
          : { id: 40, name: '账号 B 喜欢', kind: 'liked' }],
      },
    });
    api.getCatalog = async () => ({ ok: true, data: { tracks: [] } });
    api.syncPlaylist = async id => {
      syncCalls.push(id);
      if (id === 30) {
        syncStarted.resolve();
        return releaseOldSync.promise;
      }
      return { ok: true, data: {}, error: null };
    };
    const autoSyncApp = new App();
    autoSyncApp.onCatalogUpdated = () => {};
    state.set({ authenticated: true, user: { user_id: 3001 }, playlists: [], catalog: [], selectedPlaylistId: null });
    const oldAccountSync = autoSyncApp.loadInitialData();
    await syncStarted.promise;
    state.set({ authenticated: true, user: { user_id: 3002 }, playlists: [], catalog: [], selectedPlaylistId: null });
    await autoSyncApp.loadInitialData();
    assert.deepEqual(syncCalls, [30, 40], 'A stale account sync must not suppress the new account initial sync');
    releaseOldSync.resolve({ ok: true, data: {}, error: null });
    await oldAccountSync;
  } finally {
    api.getPlaylists = originalGetPlaylists;
    api.getCatalog = originalGetCatalog;
    rulesManager.loadRules = originalLoadRules;
    api.syncPlaylist = originalSyncPlaylistForRace;
    cloudManager.renderPlaylists = originalRaceRenderPlaylists;
    cloudManager.populatePlaylistDropdowns = originalRacePopulateDropdowns;
  }

  api.unlike = originalUnlike;
  api.backup = originalCloudBackup;
  api.reconcileOperations = originalReconcile;
  cloudManager.renderGroupedBatchQueue = originalRenderQueue;
  cloudManager.renderLogs = originalRenderLogs;
  cloudManager.reloadCatalog = originalReloadCatalog;
  state.set({ batchQueue: [], hasUnknownResult: false, destructiveFrozen: false, unknownResultDetail: null });
  console.log('✓ cross-layer frontend contracts passed!');
}
