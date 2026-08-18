/**
 * Pruner Studio Frontend Tests - Review Deck Engine & State Distinctions
 * Tests workspace state discrimination (loading, unauthenticated, not_synced, empty, complete, error),
 * exact keyboard map, skip-to-end round queueing, soft-commit rollback, and liked vs owned targets.
 */
import assert from 'node:assert/strict';

if (typeof globalThis.window === 'undefined') {
  globalThis.window = { addEventListener: () => {}, removeEventListener: () => {} };
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

export async function runDeckSwipeTests() {
  console.log('Testing deck review & workspace state distinctions...');

  const originalFetch = globalThis.fetch;

  try {
    const song1 = { id: 101, title: 'Keep Me', artists: ['Artist A'], duration: 180, source_playlist_id: 10 };
    const song2 = { id: 102, title: 'Prune Me', artists: ['Artist B'], duration: 210, source_playlist_id: 10 };
    const song3 = { id: 103, title: 'Skip Me', artists: ['Artist C'], duration: 195, source_playlist_id: 10 };

    // 1. Workspace State Discrimination (No false complete during load)
    // Loading State
    state.set({ authenticated: true, catalogLoading: true, catalogError: null });
    assert.equal(deckManager.determineWorkspaceState(), 'loading', 'Must be loading state when catalogLoading is true');

    // Unauthenticated State
    state.set({ authenticated: false, catalogLoading: false });
    assert.equal(deckManager.determineWorkspaceState(), 'unauthenticated', 'Must be unauthenticated state');

    // Error State
    state.set({ authenticated: true, catalogLoading: false, catalogError: 'Connection refused' });
    assert.equal(deckManager.determineWorkspaceState(), 'error', 'Must be error state');

    // Not Synced State (Playlist exists on remote with track_count > 0, but 0 tracks in local cache)
    state.set({
      authenticated: true,
      catalogLoading: false,
      catalogError: null,
      playlists: [{ id: 10, name: '我喜欢的音乐', kind: 'liked', track_count: 50 }],
      selectedPlaylistId: 10,
      catalog: [],
      deckQueue: [],
    });
    assert.equal(deckManager.determineWorkspaceState(), 'not_synced', 'Must distinguish not_synced from empty');

    // True Empty State (Playlist has 0 tracks on remote as well)
    state.set({
      playlists: [{ id: 10, name: '我喜欢的音乐', kind: 'liked', track_count: 0 }],
      catalog: [],
      deckQueue: [],
    });
    assert.equal(deckManager.determineWorkspaceState(), 'true_empty', 'Must be true_empty when playlist track_count is 0');

    // Filter Empty State (Catalog has songs for playlist, but active filter filters all out)
    state.set({
      playlists: [{ id: 10, name: '我喜欢的音乐', kind: 'liked', track_count: 3 }],
      catalog: [song1, song2, song3],
      deckFilter: 'vip', // None of song1,2,3 are VIP
    });
    const filteredForVip = deckManager.filterAndOrderTracks([song1, song2, song3], 10, 'vip', 'recent');
    state.set({ deckQueue: filteredForVip });
    assert.equal(deckManager.determineWorkspaceState(), 'filter_empty', 'Must distinguish filter_empty from true empty');

    // Active State
    const filteredAll = deckManager.filterAndOrderTracks([song1, song2, song3], 10, 'all', 'recent');
    state.set({ deckQueue: filteredAll, deckIndex: 0 });
    assert.equal(deckManager.determineWorkspaceState(), 'active', 'Must be active workspace when songs exist');

    // Complete State (When all items are reviewed)
    state.set({ deckIndex: filteredAll.length });
    assert.equal(deckManager.determineWorkspaceState(), 'complete', 'Must be complete when index reaches queue length');

    // 2. Skip-to-end (Spacebar Action)
    state.set({
      deckQueue: [song1, song2, song3],
      deckIndex: 0,
      skippedIds: new Set(),
    });

    deckManager.handleSkipToEndAction();

    // song1 should be shifted to end of queue: [song2, song3, song1]
    const updatedQueue = state.get('deckQueue');
    assert.equal(updatedQueue.length, 3);
    assert.equal(updatedQueue[0].id, 102, 'Song 2 should now be at index 0');
    assert.equal(updatedQueue[1].id, 103, 'Song 3 should now be at index 1');
    assert.equal(updatedQueue[2].id, 101, 'Song 1 should now be at end of round');
    assert.equal(state.get('skippedIds').has('101'), true, 'Song 1 must be recorded as skipped');

    // 3. Keep Action (ArrowRight / D)
    state.set({
      deckQueue: [song2, song3, song1],
      deckIndex: 0,
      keptIds: new Set(),
    });

    deckManager.handleKeepAction();
    assert.equal(state.get('deckIndex'), 1, 'Index must advance on keep');
    assert.equal(state.get('keptIds').has('102'), true, 'Song 2 must be recorded in keptIds');

    // 4. Instant Mode 3-Second Soft Commit & Z Undo
    state.setInstantMode(true);
    state.set({
      playlists: [{ id: 10, name: '我喜欢的音乐', kind: 'liked', track_count: 50 }],
      selectedPlaylistId: 10,
      deckQueue: [song3],
      deckIndex: 0,
      pendingCommits: [],
      catalog: [song1, song2, song3],
    });

    deckManager.handleDestructiveAction();

    // UI should advance immediately
    assert.equal(state.get('deckIndex'), 1, 'UI must advance immediately in instant mode');
    assert.equal(state.get('pendingCommits').length, 1, 'Soft commit must be registered');
    assert.equal(state.get('pendingCommits')[0].song.id, 103);
    assert.equal(state.get('pendingCommits')[0].action, 'unlike');

    // Press Z to cancel soft commit
    deckManager.handleUndoAction();
    assert.equal(state.get('pendingCommits').length, 0, 'Soft commit must be cancelled on undo');
    // Song 103 must still be in catalog
    assert.equal(state.get('catalog').some(s => s.id === 103), true);

    // 5. Owned Playlist Destructive Action (remove_tracks route)
    const ownedSong = { id: 808, title: 'Road Trip Song', source_playlist_id: 99 };
    state.set({
      playlists: [
        { id: 10, kind: 'liked' },
        { id: 99, name: 'Road Trip', kind: 'self_created' },
      ],
      selectedPlaylistId: 99,
      deckQueue: [ownedSong],
      deckIndex: 0,
      pendingCommits: [],
    });

    deckManager.handleDestructiveAction();
    assert.equal(state.get('pendingCommits').length, 1);
    assert.equal(state.get('pendingCommits')[0].action, 'remove_tracks', 'Owned playlist must trigger remove_tracks action');
    assert.equal(state.get('pendingCommits')[0].playlistId, 99);
    state.cancelAllCommits();

    console.log('✓ deck_swipe.test.js tests passed!');
  } finally {
    globalThis.fetch = originalFetch;
  }
}
