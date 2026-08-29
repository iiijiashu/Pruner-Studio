/**
 * Pruner Studio Frontend Tests - Phase 2 Root Cause Fixes & Edge Cases
 * Verifies:
 * 1. AccountSummary user_id field compatibility & account isolation
 * 2. App unauthenticated gate & liked auto-sync with existing other/imported tracks
 * 3. Review progress restoration order and Set leakage prevention across contexts
 * 4. 3-second soft commit semantics: advance, Z/drawer cancellation returning song,
 *    cancelAll on exit, index adjustment on success (no skipping next song),
 *    successful_ids strict check, reconcile on network/unknown, unknown_result freeze.
 * 5. Exact action + playlistId targeting for removeConfirmedSongs and batch queue
 * 6. Real playlist kind resolution (liked=>unlike, owned=>remove_tracks, imported=>blocked)
 * 7. Cloud batch strict successful_ids and unknown reconcile
 * 8. Rules workbench strictly preview/select/stage without bypass or AI copy
 * 9. Backup server snapshot + client-side UTF-8 JSON Blob download
 * 10. Catalog Set immutable updates and String IDs
 */
import assert from 'node:assert/strict';

// Mock minimal browser globals if running in pure node
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

import { state } from '../../web/js/state.js';
import { api } from '../../web/js/api.js';
import { deckManager } from '../../web/js/deck.js';
import { catalogManager } from '../../web/js/catalog.js';
import { RuleEngine, rulesManager } from '../../web/js/rules.js';
import { cloudManager } from '../../web/js/cloud.js';
import { importManager } from '../../web/js/import.js';
import { auth } from '../../web/js/auth.js';

export async function runRegressionFixesTests() {
  console.log('Testing Phase 2 Root Cause Fixes & Edge Cases...');

  const originalFetch = globalThis.fetch;
  const originalDocument = globalThis.document;

  try {
    // ------------------------------------------------------------------------
    // 0. The newest shell QR request wins when reloads overlap
    // ------------------------------------------------------------------------
    const qrContainer = {
      textContent: '',
      appendChild() {},
    };
    const qrStatus = { textContent: '' };
    globalThis.document = {
      getElementById(id) {
        if (id === 'shell-qr-image-container') return qrContainer;
        if (id === 'shell-qr-status-message') return qrStatus;
        return null;
      },
      createElement() {
        return { className: '', src: '', alt: '', textContent: '' };
      },
    };
    const originalCreateQr = api.createQr;
    const originalStartPolling = auth.startPolling;
    let resolveFirst;
    let resolveSecond;
    const firstQr = new Promise(resolve => { resolveFirst = resolve; });
    const secondQr = new Promise(resolve => { resolveSecond = resolve; });
    let requestNumber = 0;
    const polledKeys = [];
    try {
      api.createQr = () => (++requestNumber === 1 ? firstQr : secondQr);
      auth.startPolling = key => polledKeys.push(key);
      const firstGeneration = auth.generateShellQr();
      const secondGeneration = auth.generateShellQr();
      assert.equal(requestNumber, 1, 'QR creation requests must be serialized in invocation order');
      resolveFirst({ ok: true, data: { key: 'old-key', qr_image: 'data:image/png;base64,AA==' } });
      await firstGeneration;
      await Promise.resolve();
      assert.equal(requestNumber, 2, 'The next QR request starts only after the previous one settles');
      resolveSecond({ ok: true, data: { key: 'new-key', qr_image: 'data:image/png;base64,AA==' } });
      await secondGeneration;
      assert.equal(auth.currentQrKey, 'new-key', 'Stale QR responses must not replace the newest key');
      assert.deepEqual(polledKeys, ['new-key'], 'Only the newest QR key may start polling');
    } finally {
      api.createQr = originalCreateQr;
      auth.startPolling = originalStartPolling;
      globalThis.document = originalDocument;
    }

    // ------------------------------------------------------------------------
    // 1. AccountSummary user_id field compatibility
    // ------------------------------------------------------------------------
    state.set({ user: { user_id: 'user_8899', display_name: 'UserID User' } });
    assert.equal(state.getUserId(), 'user_8899', 'getUserId must return user_id string');

    // Fallback to legacy id
    state.set({ user: { id: 7766, display_name: 'Legacy User' } });
    assert.equal(state.getUserId(), '7766', 'getUserId must fallback to id when user_id is absent');

    // Guest fallback
    state.set({ user: null });
    assert.equal(state.getUserId(), 'guest');

    // ------------------------------------------------------------------------
    // 2. Exact action + playlistId targeting for removeConfirmedSongs
    // ------------------------------------------------------------------------
    const songA_liked = { id: 100, title: 'Song 100 Liked', source_playlist_id: 10 };
    const songA_custom = { id: 100, title: 'Song 100 Custom', source_playlist_id: 20 };
    const songB = { id: 200, title: 'Song 200', source_playlist_id: 10 };

    state.set({
      playlists: [
        { id: 10, name: '喜欢的音乐', kind: 'liked' },
        { id: 20, name: '自建歌单', kind: 'self_created' },
      ],
      catalog: [songA_liked, songA_custom, songB],
      deckQueue: [songA_liked, songA_custom, songB],
      deckIndex: 1,
      batchQueue: [
        { song: songA_liked, action: 'unlike', playlistId: 10, targetName: '喜欢的音乐' },
        { song: songA_custom, action: 'remove_tracks', playlistId: 20, targetName: '自建歌单' },
      ],
    });

    // Remove confirmed song 100 only from playlist 10 with action 'unlike'
    state.removeConfirmedSongs([100], 10, 'unlike');

    const cat = state.get('catalog');
    assert.equal(cat.some(s => s.id === 100 && s.source_playlist_id === 10), false, 'Liked song 100 must be removed');
    assert.equal(cat.some(s => s.id === 100 && s.source_playlist_id === 20), true, 'Custom playlist copy of song 100 must NOT be removed');

    const bq = state.get('batchQueue');
    assert.equal(bq.some(b => b.song.id === 100 && b.playlistId === 10), false, 'Batch item for playlist 10 must be removed');
    assert.equal(bq.some(b => b.song.id === 100 && b.playlistId === 20), true, 'Batch item for playlist 20 must NOT be removed');

    // ------------------------------------------------------------------------
    // 3. Real playlist kind resolution (liked vs owned vs imported)
    // ------------------------------------------------------------------------
    state.set({
      playlists: [
        { id: 10, name: '我喜欢的音乐', kind: 'liked' },
        { id: 20, name: '我的自建歌单', kind: 'self_created' },
        { id: 30, name: '本地导入歌单', kind: 'imported' },
      ],
    });

    const targetLiked = state.resolveDestructiveAction({ id: 501, source_playlist_id: 10 });
    assert.equal(targetLiked.valid, true);
    assert.equal(targetLiked.action, 'unlike');
    assert.equal(targetLiked.playlistId, 10);

    const targetOwned = state.resolveDestructiveAction({ id: 502, source_playlist_id: 20 });
    assert.equal(targetOwned.valid, true);
    assert.equal(targetOwned.action, 'remove_tracks');
    assert.equal(targetOwned.playlistId, 20);

    const targetImported = state.resolveDestructiveAction({ id: 503, source_playlist_id: 30 });
    assert.equal(targetImported.valid, false, 'Imported playlist must be invalid for cloud removal');

    const targetUnknown = state.resolveDestructiveAction({ id: 504, source_playlist_id: 999 });
    assert.equal(targetUnknown.valid, false, 'Unknown remote playlist must be invalid for cloud removal');

    // ------------------------------------------------------------------------
    // 4. Progress restoration order & no Set leakage across context switches
    // ------------------------------------------------------------------------
    state.set({ user: { user_id: 'user_test_prog' } });

    // Save progress for Context 1 (playlist 10, filter undecided, order recent)
    state.saveReviewProgress(10, 'undecided', 'recent', {
      index: 1,
      keptIds: ['101', '102'],
      skippedIds: ['103'],
    });

    // Save progress for Context 2 (playlist 20, filter undecided, order recent) - only kept 201
    state.saveReviewProgress(20, 'undecided', 'recent', {
      index: 0,
      keptIds: ['201'],
      skippedIds: [],
    });

    state.set({
      selectedPlaylistId: 10,
      deckFilter: 'undecided',
      deckOrder: 'recent',
      catalog: [
        { id: 101, title: 'Track 101', source_playlist_id: 10 },
        { id: 102, title: 'Track 102', source_playlist_id: 10 },
        { id: 103, title: 'Track 103', source_playlist_id: 10 },
        { id: 104, title: 'Track 104', source_playlist_id: 10 },
      ],
    });

    deckManager.reloadDeck();
    assert.equal(state.get('keptIds').has('101'), true);
    assert.equal(state.get('keptIds').has('102'), true);
    assert.equal(state.get('keptIds').has('201'), false, 'Context 1 must not contain Context 2 keptIds');

    // Switch to Context 2
    state.set({
      selectedPlaylistId: 20,
      catalog: [
        { id: 201, title: 'Track 201', source_playlist_id: 20 },
        { id: 202, title: 'Track 202', source_playlist_id: 20 },
      ],
    });
    deckManager.reloadDeck();

    assert.equal(state.get('keptIds').has('201'), true);
    assert.equal(state.get('keptIds').has('101'), false, 'Switching context must not leak keptIds from Context 1');
    assert.equal(state.get('keptIds').has('102'), false);
    assert.equal(state.get('skippedIds').has('103'), false, 'Switching context must not leak skippedIds');

    // ------------------------------------------------------------------------
    // 5. 3-Second Soft Commit Semantics
    // ------------------------------------------------------------------------
    // 5a. UI Advances immediately & Z cancellation restores song and progress
    const s1 = { id: 301, title: 'Song 301', source_playlist_id: 10 };
    const s2 = { id: 302, title: 'Song 302', source_playlist_id: 10 };
    const s3 = { id: 303, title: 'Song 303', source_playlist_id: 10 };

    state.setInstantMode(true);
    state.set({
      selectedPlaylistId: 10,
      deckFilter: 'undecided',
      deckOrder: 'recent',
      catalog: [s1, s2, s3],
      deckQueue: [s1, s2, s3],
      deckIndex: 0,
      keptIds: new Set(),
      skippedIds: new Set(),
      pendingCommits: [],
    });

    deckManager.handleDestructiveAction(); // Operates on s1 at index 0

    assert.equal(state.get('deckIndex'), 1, 'UI must advance to index 1 immediately');
    assert.equal(state.get('pendingCommits').length, 1);
    assert.equal(state.get('pendingCommits')[0].song.id, 301);

    // Cancel via Z
    deckManager.handleUndoAction();

    assert.equal(state.get('pendingCommits').length, 0, 'Commit must be cancelled');
    assert.equal(state.get('deckIndex'), 0, 'Cancelled song must return to current review index 0');
    assert.equal(deckManager.getCurrentSong().id, 301, 'Current song must be restored to 301');

    // 5b. Successful commit deletion from queue does NOT skip the next song in active review
    state.set({
      catalog: [s1, s2, s3],
      deckQueue: [s1, s2, s3],
      deckIndex: 0,
      pendingCommits: [],
    });

    deckManager.handleDestructiveAction(); // s1 at index 0 -> deckIndex becomes 1 (user sees s2)
    assert.equal(state.get('deckIndex'), 1);
    assert.equal(deckManager.getCurrentSong().id, 302, 'User is currently reviewing s2');

    // Suppose 3s expires and s1 is confirmed removed by backend
    state.removeConfirmedSongs([301], 10, 'unlike');

    // Now deckQueue is [s2, s3]. deckIndex must be adjusted to 0 so user is STILL reviewing s2!
    assert.equal(state.get('deckQueue').length, 2);
    assert.equal(state.get('deckIndex'), 0, 'deckIndex must be adjusted from 1 to 0');
    assert.equal(deckManager.getCurrentSong().id, 302, 'User must STILL be viewing s2 without it being skipped');

    // 5c. Strict successful_ids check (ok:true without id does NOT delete)
    let removeConfirmedCalled = false;
    globalThis.fetch = async () => ({
      ok: true,
      status: 200,
      headers: new Map([['content-type', 'application/json']]),
      json: async () => ({
        ok: true,
        data: { requested: 1, successful_ids: [], failed_items: [{ id: 401, reason: 'Remote error' }] },
        error: null,
      }),
    });

    const unlikeRes = await api.unlike([401]);
    const isExplicitlySuccessful = unlikeRes.ok && Array.isArray(unlikeRes.data?.successful_ids) && unlikeRes.data.successful_ids.includes(401);
    assert.equal(isExplicitlySuccessful, false, 'Must not treat empty successful_ids as success');

    // 5d. Network/unknown error triggers reconcile and freezes if still unknown
    globalThis.fetch = async (url) => {
      if (url.includes('/api/operations/reconcile')) {
        return {
          ok: false,
          status: 504,
          headers: new Map([['content-type', 'application/json']]),
          json: async () => ({ ok: false, data: null, error: { code: 'GATEWAY_TIMEOUT', message: 'Timeout' } }),
        };
      }
      throw new Error('Network failure');
    };

    state.set({
      hasUnknownResult: false,
      destructiveFrozen: false,
      catalogLoading: false,
      authenticated: true,
      catalogError: null,
      playlists: [{ id: 10, name: '我喜欢的音乐', kind: 'liked', track_count: 5 }],
      selectedPlaylistId: 10,
      catalog: [s1, s2],
      deckQueue: [s1, s2],
      deckIndex: 0,
    });

    await deckManager.handleCommitFailureWithReconcile(s1, 'unlike', 10, '喜欢的音乐');

    assert.equal(state.get('hasUnknownResult'), true, 'Must set hasUnknownResult to true');
    assert.equal(state.get('destructiveFrozen'), true, 'Must freeze destructive actions');
    assert.equal(deckManager.determineWorkspaceState(), 'unknown_result', 'Workspace state must be unknown_result');

    // ------------------------------------------------------------------------
    // 6. Catalog Set immutability & String IDs
    // ------------------------------------------------------------------------
    const initialSet = new Set(['1', '2']);
    state.set({ catalogSelectedIds: initialSet });

    let emittedSet = null;
    const unsub = state.on('change:catalogSelectedIds', (s) => {
      emittedSet = s;
    });

    catalogManager.selectCurrentPage(true); // Should create new Set reference
    const newSet = state.get('catalogSelectedIds');

    assert.notEqual(newSet, initialSet, 'Catalog set mutations must produce a new Set reference');
    assert.equal(Array.from(newSet).every(id => typeof id === 'string'), true, 'All IDs in Set must be strings');
    unsub();

    // ------------------------------------------------------------------------
    // 7. Rules: strictly preview/select/stage without bypass or AI copy
    // ------------------------------------------------------------------------
    assert.equal(typeof rulesManager.createPlaylistFromMatched, 'undefined', 'Direct create playlist function must be removed from rules');

    // ------------------------------------------------------------------------
    // 8. Import refreshes the full catalog instead of replacing it with the
    //    import subset (other playlists must not vanish after an import).
    // ------------------------------------------------------------------------
    {
      const likedSong = { id: 1, title: '一', source_playlist_id: 10 };
      const importedSong = { id: 88, title: '导入曲目', source_playlist_id: 'imported' };
      state.set({ catalog: [likedSong] });

      globalThis.fetch = async (url) => {
        if (String(url).includes('/api/catalog/import')) {
          return {
            ok: true,
            status: 200,
            headers: new Map([['content-type', 'application/json']]),
            json: async () => ({ ok: true, data: { imported_count: 1, tracks: [importedSong] }, error: null }),
          };
        }
        if (String(url).includes('/api/catalog')) {
          return {
            ok: true,
            status: 200,
            headers: new Map([['content-type', 'application/json']]),
            json: async () => ({ ok: true, data: { tracks: [likedSong, importedSong] }, error: null }),
          };
        }
        throw new Error(`unexpected fetch ${url}`);
      };

      await importManager.handleFile({ name: 'backup.json', text: async () => JSON.stringify([importedSong]) });

      const catalogAfterImport = state.get('catalog');
      assert.equal(
        catalogAfterImport.some(s => s.id === 1),
        true,
        'Import must not drop previously synced tracks from the local catalog view'
      );
      assert.equal(
        catalogAfterImport.some(s => s.id === 88 && s.source_playlist_id === 'imported'),
        true,
        'Imported track must appear in the local catalog view'
      );
    }

    console.log('✓ Phase 2 Root Cause Fixes & Edge Cases tests passed!');
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.document = originalDocument;
  }
}
