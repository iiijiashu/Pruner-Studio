/**
 * Pruner Studio - Application State Store
 * Central reactive store with pub/sub event emitters, account-scoped caching,
 * soft-commit countdown management, and canonical playlist resolution.
 */

import { getCoverAccent, deepClone } from './utils.js';

const getStorageItem = (key) => {
  try {
    return typeof localStorage !== 'undefined' ? localStorage.getItem(key) : null;
  } catch {
    return null;
  }
};

const setStorageItem = (key, val) => {
  try {
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(key, val);
    }
  } catch {}
};

class Store {
  constructor() {
    this.listeners = new Map();

    // Initial State
    this.state = {
      // User / Session
      authenticated: false,
      user: null, // AccountSummary: { user_id, display_name, avatar_url }
      isOnline: typeof navigator !== 'undefined' ? Boolean(navigator.onLine) : true,

      // Playlists & Catalog
      playlists: [],
      selectedPlaylistId: null, // Canonical real playlist ID (never synthetic 'liked')
      catalog: [], // Full Song[]
      catalogLoading: false,
      catalogError: null,
      playlistSyncing: false,

      // Deck Review Workspace
      deckQueue: [], // Active filtered and ordered songs
      deckIndex: 0,
      deckFilter: 'undecided', // 'all' | 'undecided' | 'skipped' | 'rules' | 'vip' | 'unavailable' | 'short' | 'long'
      deckOrder: 'recent',     // 'recent' | 'oldest' | 'title' | 'duration'
      deckHistory: [],         // Local undo history for batch actions
      keptIds: new Set(),      // Set of kept song IDs for the active review session
      skippedIds: new Set(),   // Set of skipped song IDs

      // Settings
      instantModeEnabled: true, // Default to instant mode (3s soft-commit)
      autoplayEnabled: getStorageItem('pruner_autoplay') !== 'false',
      dragEnabled: getStorageItem('pruner_drag_enabled') === 'true', // Optional, default off
      catalogView: getStorageItem('pruner_catalog_view') || 'table',  // 'table' | 'cards'

      // Soft-commit queue (Instant Mode)
      pendingCommits: [], // Array of active SoftCommit objects

      // Staged Batch Queue (Batch Mode, Persisted)
      batchQueue: [], // Array of { song, action, playlistId, targetName }

      // Catalog Workspace Shared State
      catalogSearchQuery: '',
      catalogActiveFilter: 'all',
      catalogSelectedPlaylistId: 'all',
      catalogSortColumn: 'title',
      catalogSortDirection: 'asc',
      catalogPage: 1,
      catalogPageSize: 50,
      catalogSelectedIds: new Set(),

      // Rules Workshop
      rules: [],

      // Audio Playback
      currentPlayingSong: null,
      isPlaying: false,
      currentTime: 0,
      duration: 0,
      isChorusActive: false,
      audioSourceType: 'original', // 'original' | 'alternative'
      alternativeInfo: null,
      coverAccent: 'hsl(140, 20%, 54%)',

      // Synchronized Lyrics
      currentLyrics: null,
      isLyricsDrawerOpen: false,

      // Operation History (Last 100 Minimal Records)
      opsHistory: [],

      // Active Workspaces / UI State
      activeTab: 'deck', // 'deck' | 'catalog' | 'cloud'
      isRulesDrawerOpen: false,
      isPendingDrawerOpen: false,

      // Unknown Result / Frozen State
      hasUnknownResult: false,
      destructiveFrozen: false,
      unknownResultDetail: null,
      ignoredUnknownOperations: new Set(),
    };

    // Load initial preferences
    this.loadUserPreferences();

    // Listen to network status
    if (typeof window !== 'undefined') {
      window.addEventListener('online', () => this.set({ isOnline: true }));
      window.addEventListener('offline', () => this.set({ isOnline: false }));
      // Cancel soft-commits on beforeunload to prevent firing writes after page close
      window.addEventListener('beforeunload', () => {
        this.cancelAllCommits();
      });
    }
  }

  getUserId() {
    const uid = this.state.user?.user_id ?? this.state.user?.id;
    return uid ? String(uid) : 'guest';
  }

  loadUserPreferences() {
    const userId = this.getUserId();
    const instantPref = getStorageItem(`pruner_instant_mode_${userId}`);
    const instantMode = instantPref !== null ? instantPref === 'true' : true;
    const batchQueueData = getStorageItem(`pruner_batch_queue_${userId}`);
    let batchQueue = [];
    if (batchQueueData) {
      try {
        batchQueue = JSON.parse(batchQueueData) || [];
      } catch {}
    }

    const opsHistoryData = getStorageItem(`pruner_ops_history_${userId}`);
    let opsHistory = [];
    if (opsHistoryData) {
      try {
        opsHistory = JSON.parse(opsHistoryData) || [];
      } catch {}
    }

    this.set({
      instantModeEnabled: instantMode,
      batchQueue,
      opsHistory: opsHistory.slice(0, 100),
    });
  }

  on(event, callback) {
    if (!this.listeners.has(event)) {
      this.listeners.set(event, new Set());
    }
    this.listeners.get(event).add(callback);

    return () => {
      const handlers = this.listeners.get(event);
      if (handlers) {
        handlers.delete(callback);
      }
    };
  }

  emit(event, data) {
    const handlers = this.listeners.get(event);
    if (handlers) {
      handlers.forEach(fn => {
        try {
          fn(data, this.state);
        } catch (err) {
          console.error(`Error in handler for ${event}:`, err);
        }
      });
    }
    const allHandlers = this.listeners.get('*');
    if (allHandlers) {
      allHandlers.forEach(fn => {
        try {
          fn(event, data, this.state);
        } catch (err) {
          console.error(`Error in wildcard handler:`, err);
        }
      });
    }
  }

  get(key) {
    return key ? this.state[key] : { ...this.state };
  }

  set(partialState) {
    const changedKeys = [];
    for (const [key, value] of Object.entries(partialState)) {
      if (this.state[key] !== value) {
        this.state[key] = value;
        changedKeys.push(key);
      }
    }

    if (changedKeys.length > 0) {
      for (const key of changedKeys) {
        this.emit(`change:${key}`, this.state[key]);
      }
      this.emit('state:change', { keys: changedKeys, state: this.state });
    }
  }

  // --- Canonical Playlist Resolution & Real Kind Action Helper ---

  /**
   * Finds the real Liked Playlist object from loaded playlists.
   * @returns {PlaylistSummary|null}
   */
  getRealLikedPlaylist() {
    const playlists = this.state.playlists || [];
    return playlists.find(p => p.kind === 'liked') || null;
  }

  /**
   * Resolves canonical selected playlist ID without synthetic 'liked' string.
   * @param {Array} [playlists]
   * @returns {number|string|null}
   */
  resolveCanonicalPlaylistId(playlists = null) {
    const list = playlists || this.state.playlists || [];
    const currentSelected = this.state.selectedPlaylistId;

    // Check if current selection is valid in list
    if (currentSelected && currentSelected !== 'liked' && list.some(p => String(p.id) === String(currentSelected))) {
      return currentSelected;
    }

    // Default to real liked playlist ID
    const likedPl = list.find(p => p.kind === 'liked');
    if (likedPl) {
      return likedPl.id;
    }

    // Fallback to first playlist if available
    return list.length > 0 ? list[0].id : null;
  }

  /**
   * Resolves verified destructive action and target from real playlist kind.
   * @param {Song} song
   * @param {number|string|null} [explicitPlaylistId=null]
   * @returns {{ valid: boolean, action: 'unlike'|'remove_tracks'|null, playlistId: number|string|null, targetName: string, reason?: string }}
   */
  resolveDestructiveAction(song, explicitPlaylistId = null) {
    if (!song) return { valid: false, action: null, playlistId: null, targetName: '', reason: '未指定歌曲' };
    const targetPid = explicitPlaylistId ?? song.source_playlist_id ?? song.playlist_id;
    const playlists = this.state.playlists || [];
    const pl = playlists.find(p => String(p.id) === String(targetPid));
    const likedPl = this.getRealLikedPlaylist();
    const likedPlId = likedPl ? String(likedPl.id) : null;

    if (pl) {
      if (pl.kind === 'liked') {
        return {
          valid: true,
          action: 'unlike',
          playlistId: pl.id,
          targetName: pl.name || '喜欢的音乐',
        };
      }
      if (pl.kind === 'owned' || pl.kind === 'self_created') {
        return {
          valid: true,
          action: 'remove_tracks',
          playlistId: pl.id,
          targetName: pl.name || `歌单 #${pl.id}`,
        };
      }
      return {
        valid: false,
        action: null,
        playlistId: pl.id,
        targetName: pl.name,
        reason: '本地导入或无远端目标的歌单无法进入云端移除',
      };
    }

    if (likedPlId && String(targetPid) === likedPlId) {
      return {
        valid: true,
        action: 'unlike',
        playlistId: likedPl.id,
        targetName: likedPl.name || '喜欢的音乐',
      };
    }

    return {
      valid: false,
      action: null,
      playlistId: targetPid,
      targetName: '未知歌单',
      reason: '未找到匹配的有效云端歌单',
    };
  }

  reviewSongKey(song, fallbackPlaylistId = null) {
    if (!song) return null;
    const source = song.source_playlist_id ?? fallbackPlaylistId ?? 'unknown';
    return `${String(source)}:${String(song.id)}`;
  }

  catalogSongKey(song) {
    return this.reviewSongKey(song, 'unknown');
  }

  unknownOperationKey(song, action, playlistId) {
    if (!song || !action) return null;
    return `${String(action)}:${this.reviewSongKey(song, playlistId)}`;
  }

  ignoreUnknownOperation(song, action, playlistId) {
    const key = this.unknownOperationKey(song, action, playlistId);
    if (!key) return false;
    const ignored = new Set(this.state.ignoredUnknownOperations || []);
    ignored.add(key);
    this.set({ ignoredUnknownOperations: ignored });
    return true;
  }

  isUnknownOperationIgnored(song, action, playlistId) {
    const key = this.unknownOperationKey(song, action, playlistId);
    return Boolean(key && (this.state.ignoredUnknownOperations || new Set()).has(key));
  }

  clearIgnoredUnknownOperation(song, action, playlistId) {
    const key = this.unknownOperationKey(song, action, playlistId);
    if (!key) return false;
    const ignored = new Set(this.state.ignoredUnknownOperations || []);
    const changed = ignored.delete(key);
    if (changed) this.set({ ignoredUnknownOperations: ignored });
    return changed;
  }

  clearIgnoredUnknownOperationsForPlaylist(playlistId) {
    const pid = String(playlistId);
    const ignored = new Set(
      [...(this.state.ignoredUnknownOperations || [])].filter(key => (
        !String(key).startsWith(`unlike:${pid}:`)
        && !String(key).startsWith(`remove_tracks:${pid}:`)
      )),
    );
    this.set({ ignoredUnknownOperations: ignored });
  }

  // --- Settings Persistence ---

  setInstantMode(enabled) {
    const val = Boolean(enabled);
    const userId = this.getUserId();
    setStorageItem(`pruner_instant_mode_${userId}`, val ? 'true' : 'false');
    this.set({ instantModeEnabled: val });
  }

  setAutoplay(enabled) {
    const val = Boolean(enabled);
    setStorageItem('pruner_autoplay', val ? 'true' : 'false');
    this.set({ autoplayEnabled: val });
  }

  setDragEnabled(enabled) {
    const val = Boolean(enabled);
    setStorageItem('pruner_drag_enabled', val ? 'true' : 'false');
    this.set({ dragEnabled: val });
  }

  setCatalogView(view) {
    const val = view === 'cards' ? 'cards' : 'table';
    setStorageItem('pruner_catalog_view', val);
    this.set({ catalogView: val });
  }

  // --- Soft-Commit Management (Instant Mode 3s Countdown) ---

  /**
   * Registers a 3-second local soft-commit timer for a single destructive action.
   * @param {Song} song
   * @param {'unlike'|'remove_tracks'} action
   * @param {number|string|null} playlistId
   * @param {string} targetName
   * @param {Function} commitCallback
   * @param {Function} [cancelCallback]
   * @param {number|null} [originalIndex=null]
   * @returns {Object} softCommit
   */
  createSoftCommit(song, action, playlistId, targetName, commitCallback, cancelCallback, originalIndex = null) {
    const commitId = `commit_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
    const expiresAt = Date.now() + 3000;
    const fromIndex = originalIndex !== null ? originalIndex : (this.state.deckIndex || 0);

    const timerId = setTimeout(async () => {
      // Remove from pendingCommits list
      const remaining = (this.state.pendingCommits || []).filter(c => c.id !== commitId);
      this.set({ pendingCommits: remaining });

      if (typeof commitCallback === 'function') {
        try {
          await commitCallback();
        } catch (err) {
          console.error('Commit execution error:', err);
        }
      }
    }, 3000);

    const softCommit = {
      id: commitId,
      song,
      action,
      playlistId,
      targetName: targetName || '喜欢的音乐',
      expiresAt,
      timerId,
      originalIndex: fromIndex,
      cancelCallback,
    };

    const nextPending = [softCommit, ...(this.state.pendingCommits || [])];
    this.set({ pendingCommits: nextPending });
    return softCommit;
  }

  /**
   * Cancels the newest uncommitted soft-commit action (triggered by Z key).
   * @returns {boolean} true if an action was cancelled
   */
  cancelNewestCommit() {
    const pending = this.state.pendingCommits || [];
    if (pending.length === 0) return false;

    const newest = pending[0];
    return this.cancelCommitById(newest.id);
  }

  /**
   * Cancels a specific soft-commit by ID.
   * @param {string} commitId
   * @returns {boolean}
   */
  cancelCommitById(commitId) {
    const pending = this.state.pendingCommits || [];
    const target = pending.find(c => c.id === commitId);
    if (!target) return false;

    clearTimeout(target.timerId);
    const nextPending = pending.filter(c => c.id !== commitId);
    this.set({ pendingCommits: nextPending });

    if (typeof target.cancelCallback === 'function') {
      target.cancelCallback(target);
    }
    return true;
  }

  /**
   * Cancels all pending soft-commit timers (e.g. before window closes).
   */
  cancelAllCommits() {
    const pending = this.state.pendingCommits || [];
    pending.forEach(c => {
      clearTimeout(c.timerId);
      if (typeof c.cancelCallback === 'function') {
        c.cancelCallback(c);
      }
    });
    this.set({ pendingCommits: [] });
  }

  // --- Staged Batch Queue (Batch Mode, Persisted) ---

  stageBatchAction(song, action = 'unlike', playlistId = null, targetName = '') {
    if (!song) return false;
    const resolved = this.resolveDestructiveAction(song, playlistId);
    if (!resolved.valid || resolved.action !== action || String(resolved.playlistId) !== String(playlistId)) {
      return false;
    }
    if (this.isUnknownOperationIgnored(song, action, playlistId)) return false;
    const queue = [...(this.state.batchQueue || [])];
    const exists = queue.some(item => String(item.song.id) === String(song.id) && String(item.playlistId) === String(playlistId) && item.action === action);
    if (!exists) {
      queue.push({
        song,
        action,
        playlistId,
        targetName: targetName || (action === 'unlike' ? '喜欢的音乐' : `歌单 #${playlistId}`),
        stagedAt: Date.now(),
      });
      this.saveBatchQueue(queue);
      return true;
    }
    return false;
  }

  unstageBatchAction(songId, playlistId = null, action = null) {
    const queue = (this.state.batchQueue || []).filter(item => {
      const matchSong = String(item.song.id) === String(songId);
      if (!matchSong) return true;
      if (playlistId !== null && playlistId !== undefined && String(item.playlistId) !== String(playlistId)) {
        return true;
      }
      if (action !== null && action !== undefined && item.action !== action) {
        return true;
      }
      return false;
    });
    this.saveBatchQueue(queue);
  }

  clearBatchQueue() {
    this.saveBatchQueue([]);
  }

  saveBatchQueue(queue) {
    const userId = this.getUserId();
    setStorageItem(`pruner_batch_queue_${userId}`, JSON.stringify(queue));
    this.set({ batchQueue: queue });
  }

  // --- Operation Activity History (Last 100 Minimal Records) ---

  addOperationLog(log) {
    const userId = this.getUserId();
    const history = [
      {
        id: `op_${Date.now()}`,
        timestamp: new Date().toLocaleTimeString(),
        date: new Date().toLocaleDateString(),
        ...log,
      },
      ...(this.state.opsHistory || []),
    ].slice(0, 100);

    setStorageItem(`pruner_ops_history_${userId}`, JSON.stringify(history));
    this.set({ opsHistory: history });
  }

  // --- Catalog Mutation (Confirmed Success Only) ---

  /**
   * Remove confirmed successful songs from the catalog and deck.
   * NEVER call this for failed IDs.
   * @param {Array<number|string>} songIds
   * @param {number|string|null} [playlistId=null]
   * @param {'unlike'|'remove_tracks'|null} [action=null]
   */
  removeConfirmedSongs(songIds, playlistId = null, action = null) {
    const idSet = new Set(songIds.map(String));
    const likedPl = this.getRealLikedPlaylist();
    const likedPlId = likedPl ? String(likedPl.id) : null;
    const targetPid = playlistId !== null && playlistId !== undefined ? String(playlistId) : (action === 'unlike' && likedPlId ? likedPlId : null);

    // 1. Catalog precision filter: only remove the copy in this playlist
    const nextCatalog = this.state.catalog.filter(song => {
      if (!idSet.has(String(song.id))) return true;
      if (targetPid !== null) {
        return String(song.source_playlist_id) !== targetPid;
      }
      if (likedPlId && String(song.source_playlist_id) === likedPlId) {
        return false;
      }
      return true;
    });

    // 2. DeckQueue adjustment: calculate items removed strictly BEFORE current deckIndex
    const currentDeck = this.state.deckQueue || [];
    const currentIndex = this.state.deckIndex || 0;
    const removedBeforeIndex = currentDeck
      .slice(0, currentIndex)
      .filter(song => {
        if (!idSet.has(String(song.id))) return false;
        if (targetPid !== null) return String(song.source_playlist_id) === targetPid;
        return true;
      }).length;

    const nextDeck = currentDeck.filter(song => {
      if (!idSet.has(String(song.id))) return true;
      if (targetPid !== null) {
        return String(song.source_playlist_id) !== targetPid;
      }
      return false;
    });

    let newIndex = Math.max(0, currentIndex - removedBeforeIndex);
    if (newIndex >= nextDeck.length && nextDeck.length > 0) {
      newIndex = nextDeck.length;
    }

    // 3. BatchQueue precision cleanup
    const nextBatch = (this.state.batchQueue || []).filter(item => {
      const matchSong = idSet.has(String(item.song.id));
      if (!matchSong) return true;
      if (targetPid !== null && String(item.playlistId) !== targetPid) {
        return true;
      }
      if (action !== null && item.action !== action) {
        return true;
      }
      return false;
    });

    this.saveBatchQueue(nextBatch);
    this.set({
      catalog: nextCatalog,
      deckQueue: nextDeck,
      deckIndex: newIndex,
    });

    // Save progress with adjusted newIndex
    const currentPid = this.state.selectedPlaylistId;
    const currentFilter = this.state.deckFilter || 'undecided';
    const currentOrder = this.state.deckOrder || 'recent';
    this.saveReviewProgress(currentPid, currentFilter, currentOrder, {
      index: newIndex,
      currentSongKey: this.reviewSongKey(nextDeck[newIndex], currentPid),
      keptIds: Array.from(this.state.keptIds || []),
      skippedIds: Array.from(this.state.skippedIds || []),
      updatedAt: Date.now(),
    });

    this.emit('catalog:updated', nextCatalog);
  }

  // --- Account-Scoped Review Progress ---

  getProgressKey(playlistId, filter = 'all', order = 'recent') {
    const userId = this.getUserId();
    const pid = playlistId || 'liked';
    return `pruner_prog_${userId}_${pid}_${filter}_${order}`;
  }

  saveReviewProgress(playlistId, filter, order, data) {
    const key = this.getProgressKey(playlistId, filter, order);
    setStorageItem(key, JSON.stringify(data));
  }

  loadReviewProgress(playlistId, filter, order) {
    const key = this.getProgressKey(playlistId, filter, order);
    const stored = getStorageItem(key);
    if (stored) {
      try {
        return JSON.parse(stored);
      } catch {}
    }
    return null;
  }

  resetReviewProgress(playlistId, filter, order) {
    const key = this.getProgressKey(playlistId, filter, order);
    try {
      if (typeof localStorage !== 'undefined') {
        localStorage.removeItem(key);
      }
    } catch {}
    this.set({
      deckIndex: 0,
      keptIds: new Set(),
      skippedIds: new Set(),
      deckHistory: [],
    });
  }
}

export const state = new Store();
