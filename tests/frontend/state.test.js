/**
 * Pruner Studio Frontend Tests - State & Store
 * Tests playlist/catalog race conditions, real liked playlist ID resolution,
 * account isolation, soft-commit queue, batch persistence/grouping, and table/card shared state.
 */
import assert from 'node:assert/strict';

// Mock minimal browser globals if running in pure node
if (typeof globalThis.window === 'undefined') {
  globalThis.window = {
    addEventListener: () => {},
    removeEventListener: () => {},
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

export async function runStateTests() {
  console.log('Testing state.js...');

  // 1. Initial State
  assert.equal(typeof state.get('instantModeEnabled'), 'boolean');
  assert.equal(Array.isArray(state.get('catalog')), true);

  // 2. State Mutation & Event Subscriptions
  let changedUser = null;
  const unsubscribe = state.on('change:user', (user) => {
    changedUser = user;
  });

  const mockUser = { user_id: 12345, display_name: 'TestUser', avatar_url: 'https://example.com/avatar.jpg' };
  state.set({ authenticated: true, user: mockUser });

  assert.equal(state.get('authenticated'), true);
  assert.deepEqual(state.get('user'), mockUser);
  assert.deepEqual(changedUser, mockUser);
  assert.equal(state.getUserId(), '12345');
  unsubscribe();

  // 3. Real Liked Playlist ID Resolution (Never synthetic 'liked')
  const mockPlaylists = [
    { id: 987654, name: '我喜欢的音乐', kind: 'liked', track_count: 120 },
    { id: 112233, name: '车载歌单', kind: 'self_created', track_count: 35 },
  ];
  state.set({ playlists: mockPlaylists });

  const realLiked = state.getRealLikedPlaylist();
  assert.ok(realLiked !== null, 'Real liked playlist must be found');
  assert.equal(realLiked.id, 987654);
  assert.equal(typeof realLiked.id, 'number');

  // resolveCanonicalPlaylistId must return realLiked.id instead of synthetic 'liked'
  state.set({ selectedPlaylistId: null });
  const canonicalId = state.resolveCanonicalPlaylistId();
  assert.equal(canonicalId, 987654);
  assert.notEqual(canonicalId, 'liked');

  // 4. Playlist / Catalog Response Order Race
  // Scenario A: Catalog response arrives BEFORE playlists response
  const catalogSongs = [
    { id: 1, title: 'Track 1', source_playlist_id: 987654, liked: true },
    { id: 2, title: 'Track 2', source_playlist_id: 112233, liked: false },
  ];
  state.set({
    playlists: [],
    selectedPlaylistId: null,
    catalog: catalogSongs,
  });
  // Now playlists arrive later
  state.set({ playlists: mockPlaylists });
  const resolvedA = state.resolveCanonicalPlaylistId();
  assert.equal(resolvedA, 987654, 'Should resolve real liked ID when playlists arrive after catalog');

  // Scenario B: Playlists response arrives BEFORE catalog response
  state.set({
    playlists: mockPlaylists,
    selectedPlaylistId: null,
    catalog: [],
  });
  const resolvedB = state.resolveCanonicalPlaylistId();
  state.set({ selectedPlaylistId: resolvedB });
  assert.equal(state.get('selectedPlaylistId'), 987654);
  // Catalog arrives
  state.set({ catalog: catalogSongs });
  assert.equal(state.get('selectedPlaylistId'), 987654, 'Selection remains canonical when catalog arrives');

  // 5. Account Isolation & Preference Persistence Fallback
  // User A
  state.set({ user: { user_id: 1001, display_name: 'User A' } });
  state.setInstantMode(true);
  state.stageBatchAction({ id: 501, title: 'Song User A' }, 'unlike', 987654, '喜欢的音乐');

  assert.equal(state.get('instantModeEnabled'), true);
  assert.equal(state.get('batchQueue').length, 1);
  assert.equal(localStorage.getItem('pruner_instant_mode_1001'), 'true');

  // Switch to User B (Isolated data)
  state.set({ user: { user_id: 2002, display_name: 'User B' } });
  state.loadUserPreferences();

  // User B defaults to instant mode and empty queue
  assert.equal(state.get('instantModeEnabled'), true);
  assert.equal(state.get('batchQueue').length, 0);

  state.setInstantMode(false);
  assert.equal(localStorage.getItem('pruner_instant_mode_2002'), 'false');
  // Verify User A's preference was untouched
  assert.equal(localStorage.getItem('pruner_instant_mode_1001'), 'true');

  // Switch back to User A
  state.set({ user: { user_id: 1001, display_name: 'User A' } });
  state.loadUserPreferences();
  assert.equal(state.get('instantModeEnabled'), true);
  assert.equal(state.get('batchQueue').length, 1);
  assert.equal(state.get('batchQueue')[0].song.id, 501);

  // 6. Soft Commit Lifecycle (Instant Mode 3s Countdown)
  let committed = false;
  let cancelled = false;

  const testSong = { id: 777, title: 'Countdown Song', source_playlist_id: 987654 };
  const softCommit = state.createSoftCommit(
    testSong,
    'unlike',
    987654,
    '喜欢的音乐',
    () => { committed = true; },
    () => { cancelled = true; }
  );

  assert.equal(state.get('pendingCommits').length, 1);
  assert.equal(state.get('pendingCommits')[0].song.id, 777);

  // Cancel via Z (cancelNewestCommit)
  const didCancel = state.cancelNewestCommit();
  assert.equal(didCancel, true);
  assert.equal(cancelled, true);
  assert.equal(committed, false);
  assert.equal(state.get('pendingCommits').length, 0);

  // 7. removeConfirmedSongs: Only confirmed IDs are removed
  const songA = { id: 10, title: 'Song 10', source_playlist_id: 987654, liked: true };
  const songB = { id: 20, title: 'Song 20 (Liked)', source_playlist_id: 987654, liked: true };
  const songBCopy = { id: 20, title: 'Song 20 (Custom)', source_playlist_id: 112233, liked: false };
  const songC = { id: 30, title: 'Song 30', source_playlist_id: 112233, liked: false };

  state.set({
    playlists: mockPlaylists,
    catalog: [songA, songB, songBCopy, songC],
    deckQueue: [songA, songB, songBCopy, songC],
  });

  // Remove confirmed ID 20 from liked playlist
  state.removeConfirmedSongs([20], 987654);

  assert.equal(state.get('catalog').some(s => s.id === 20 && s.source_playlist_id === 987654), false, 'Liked copy of song 20 must be removed');
  assert.equal(state.get('catalog').some(s => s.id === 20 && s.source_playlist_id === 112233), true, 'Custom playlist copy of song 20 must remain');
  assert.equal(state.get('catalog').some(s => s.id === 10), true, 'Song 10 must remain in catalog');
  assert.equal(state.get('catalog').some(s => s.id === 30), true, 'Song 30 must remain in catalog');

  // 8. Operation History Logging (Last 100 Minimal Records)
  state.set({ opsHistory: [] });
  for (let i = 0; i < 105; i++) {
    state.addOperationLog({ type: 'TEST_OP', requested: 1, successful: 1, failed: 0, targetName: `Target ${i}` });
  }
  assert.equal(state.get('opsHistory').length, 100, 'Operations history must be capped at 100 records');

  console.log('✓ state.js tests passed!');
}
