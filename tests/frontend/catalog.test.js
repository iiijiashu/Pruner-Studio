/**
 * Pruner Studio Frontend Tests - Catalog Workspace & Shared State
 * Tests shared search, filter, sort, page, selection, and playback between Table and Card views.
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
import { catalogManager } from '../../web/js/catalog.js';

export async function runCatalogTests() {
  console.log('Testing catalog.js shared state & pagination...');

  const songs = [];
  for (let i = 1; i <= 120; i++) {
    songs.push({
      id: i,
      title: `Song ${i.toString().padStart(3, '0')}`,
      artists: [i % 2 === 0 ? 'Alpha Artist' : 'Beta Singer'],
      album: i % 3 === 0 ? 'Gamma Album' : 'Delta Record',
      duration: 120 + (i % 60),
      source_playlist_id: i <= 60 ? 10 : 20,
      liked: i <= 60,
      availability: i % 10 === 0 ? 'unavailable' : (i % 7 === 0 ? 'vip' : 'available'),
    });
  }

  state.set({
    playlists: [
      { id: 10, name: '我喜欢的音乐', kind: 'liked', track_count: 60 },
      { id: 20, name: '自建歌单', kind: 'owned', track_count: 60 },
    ],
    catalog: songs,
    catalogPage: 1,
    catalogPageSize: 50,
    catalogSearchQuery: '',
    catalogActiveFilter: 'all',
    catalogSelectedPlaylistId: 'all',
    catalogSortColumn: 'title',
    catalogSortDirection: 'asc',
    catalogSelectedIds: new Set(),
  });

  // 1. Pagination & 50 items/page limit
  const page1 = catalogManager.getCurrentPageSongs();
  assert.equal(page1.length, 50, 'Page 1 must contain exactly 50 songs');
  assert.equal(page1[0].id, 1);
  assert.equal(page1[49].id, 50);

  state.set({ catalogPage: 2 });
  const page2 = catalogManager.getCurrentPageSongs();
  assert.equal(page2.length, 50, 'Page 2 must contain exactly 50 songs');
  assert.equal(page2[0].id, 51);

  state.set({ catalogPage: 3 });
  const page3 = catalogManager.getCurrentPageSongs();
  assert.equal(page3.length, 20, 'Page 3 must contain remaining 20 songs');
  assert.equal(page3[19].id, 120);

  // 2. Table / Card Shared Selection State
  state.set({ catalogPage: 1 });
  catalogManager.selectCurrentPage(true);
  assert.equal(state.get('catalogSelectedIds').size, 50, 'Current page selection must select 50 songs');
  assert.ok(state.get('catalogSelectedIds').has('10:1'), 'Selection keys must include source playlist and song ID');
  assert.equal(state.get('catalogSelectedIds').has('1'), false, 'Bare song IDs are not valid catalog selection keys');

  // Switching view (table <-> cards) preserves selection
  state.setCatalogView('cards');
  assert.equal(state.get('catalogView'), 'cards');
  assert.equal(state.get('catalogSelectedIds').size, 50, 'View change must preserve selection');

  state.setCatalogView('table');
  assert.equal(state.get('catalogView'), 'table');
  assert.equal(state.get('catalogSelectedIds').size, 50, 'View switch back must preserve selection');

  // 3. Selection Clearing on Filter / Search / Playlist Change with Notice
  catalogManager.onFilterCriteriaChanged();
  assert.equal(state.get('catalogSelectedIds').size, 0, 'Changing filter criteria must clear selection');

  // 4. Search Filter Test
  state.set({ catalogSearchQuery: 'alpha', catalogPage: 1 });
  const alphaResults = catalogManager.getFilteredSongs();
  assert.ok(alphaResults.length > 0);
  assert.ok(alphaResults.every(s => s.artists.some(a => a.toLowerCase().includes('alpha'))));

  // 5. Playlist Filter Test
  state.set({ catalogSearchQuery: '', catalogSelectedPlaylistId: 10, catalogPage: 1 });
  const playlist10Results = catalogManager.getFilteredSongs();
  assert.equal(playlist10Results.length, 60);
  assert.ok(playlist10Results.every(s => s.source_playlist_id === 10));

  // 6. Staging Selected Songs for Pruning
  state.set({
    catalogSelectedPlaylistId: 'all',
    catalogSelectedIds: new Set(['10:1', '10:2', '10:3']),
    batchQueue: [],
  });
  catalogManager.stageSelectedForPruning();

  assert.equal(state.get('batchQueue').length, 3, 'Selected songs must be added to batch queue');
  assert.equal(state.get('catalogSelectedIds').size, 0, 'Selection must be cleared after staging');

  // 7. The same song in two playlists is two distinct selectable rows.
  const likedCopy = { ...songs[0], id: 999, source_playlist_id: 10, title: 'Duplicate Liked' };
  const ownedCopy = { ...songs[0], id: 999, source_playlist_id: 20, title: 'Duplicate Owned' };
  state.set({
    catalog: [likedCopy, ownedCopy],
    catalogSelectedIds: new Set(['20:999']),
    batchQueue: [],
  });
  catalogManager.stageSelectedForPruning();
  assert.equal(state.get('batchQueue').length, 1, 'Only one selected playlist/song instance is staged');
  assert.equal(state.get('batchQueue')[0].action, 'remove_tracks');
  assert.equal(String(state.get('batchQueue')[0].playlistId), '20');

  console.log('✓ catalog.test.js tests passed!');
}
