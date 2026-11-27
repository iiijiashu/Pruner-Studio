import { writableNeteaseTrack, getTrackKey, serializeTrackForStorage } from './utils.js';
/**
 * Pruner Studio - Application State Store
 * Central reactive store with pub/sub event emitters, account-scoped caching,
 * soft-commit countdown management (cancellable through backup wait),
 * global playback queue, persistent relisten-later state, and canonical playlist resolution.
 */

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

const DEFAULT_SETTINGS = {
  quality_preference: 'jymaster',
  quality_arbitration: 'best_quality_race',
  enable_external_sources: true,
  external_providers: ['kugou_lite', 'migu', 'qq', 'kuwo', 'bilibili', 'gdstudio'],
  custom_external_api: '',
  local_music_dirs: [],
  visual_mode: 'soft_dynamic',
  gpu_performance_mode: 'balanced',
  game_mode: false,
  stage_theme: 'fluid',
  stage_lyric_size: 24,
  stage_lyric_align: 'left',
  visualizer_mode: 'bars',
  enable_3d_effects: true,
  minimize_to_tray: true,
  loudness_normalization: true,
  crossfade_ms: 150,
  audio_cache_limit_mb: 512,
  lyrics_blur_depth: false,
  audio_reactive_glow: true,
  enable_amll_ttml_fallback: true,
  global_hotkeys_enabled: true,
  smart_radio_explore_ratio: 0.4,
};

class Store {
  constructor() {
    this.listeners = new Map();
    this.inFlightCommits = [];

    // Initial State
    this.state = {
      // User / Session
      authenticated: false,
      user: null, // AccountSummary: { user_id, display_name, avatar_url, vip_type }
      sessionGeneration: 0,
      cachedAccounts: [],
      offlineMode: false,
      guestLocalMode: false,
      windowVisible: true,
      windowMinimized: false,
      resumableSession: null,
      activeOfflineAccountId: null,
      isOnline: typeof navigator !== 'undefined' ? Boolean(navigator.onLine) : true,

      // Playlists & Catalog
      playlists: [],
      selectedPlaylistId: null, // Canonical real playlist ID (never synthetic 'liked')
      catalog: [], // Full Song[]
      cloudSongs: [],
      localSongs: [],
      catalogLoading: false,
      catalogError: null,
      playlistSyncing: false,
      syncProgress: null,

      // Discovery / Recommendations
      dailyRecommend: [],
      dailyRecommendMeta: null,
      personalFmQueue: [],
      smartRadioSongs: [],
      radioExhausted: false,
      radioBreakdown: null,
      lyricsPreviewSubscribers: 0,
      dustyGems: [],
      listeningInsights: null,
      recentPlayed: [],

      // Multi-Channel Hub & Disk Cache
      channels: [],
      audioCacheStats: null,
      desktopLyricsOpen: false,
      desktopLyricsLocked: false,

      // Deck Review Workspace
      deckQueue: [], // Active filtered and ordered songs
      deckIndex: 0,
      deckFilter: 'undecided', // 'all' | 'undecided' | 'relisten' | 'skipped' | 'rules' | 'vip' | 'unavailable' | 'short' | 'long'
      deckOrder: 'recent',     // 'recent' | 'oldest' | 'title' | 'duration'
      deckHistory: [],         // Local undo history for review actions
      keptIds: new Set(),      // Set of kept song IDs for the active review session
      skippedIds: new Set(),   // Set of skipped song IDs
      relistenIds: new Set(),  // Set of persistent relisten-later song IDs ('L' key)

      // Settings & Preferences
      instantModeEnabled: true, // Default to instant mode (3s soft-commit)
      autoplayEnabled: getStorageItem('pruner_autoplay') !== 'false',
      dragEnabled: getStorageItem('pruner_drag_enabled') === 'true',
      catalogView: getStorageItem('pruner_catalog_view') || 'table', // 'table' | 'cards'
      railCollapsed: getStorageItem('pruner_rail_collapsed') === 'true',
      mobileDrawerOpen: false,
      settings: { ...DEFAULT_SETTINGS },

      // Soft-commit queue (Instant Mode)
      pendingCommits: [], // Array of active SoftCommit objects in countdown

      // Staged Batch Queue (Batch Mode, Persisted)
      batchQueue: [], // Array of { song, action, playlistId, targetName }

      // Catalog Workspace Shared State
      catalogSection: 'all', // 'all' | 'liked' | 'self_created' | 'subscribed' | 'cloud' | 'local'
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

      // Global Audio Playback & Queue
      queueVersion: 0,
      playQueueOrigin: '',
      playQueue: [],
      playQueueIndex: -1,
      playHistoryStack: [],
      playMode: getStorageItem('pruner_play_mode') || 'order', // 'order' | 'loop' | 'single' | 'shuffle' | 'fm' | 'intelligence' | 'smart_radio'
      currentPlayingSong: null,
      isPlaying: false,
      currentTime: 0,
      duration: 0,
      isChorusActive: false,
      audioSourceType: 'original', // 'original' | 'alternative' | 'cloud' | 'local' | 'external'
      alternativeInfo: null,
      audioMeta: null,
      playbackError: null,
      likedSongIds: new Set(),
      coverAccent: 'hsl(140, 20%, 54%)',
      coverAccent2: 'hsl(182, 22%, 48%)',
      coverPaletteColors: null,

      // Synchronized Lyrics & Stage
      currentLyrics: null,
      isLyricsDrawerOpen: false,
      isStageOpen: false,
      isPlayQueueOpen: false,

      // Operation History (Last 100 Minimal Records)
      opsHistory: [],

      // Active Workspaces / UI State
      activeTab: 'discover', // 'discover' | 'catalog' | 'deck' | 'cloud'
      isRulesDrawerOpen: false,
      isPendingDrawerOpen: false,

      // Unknown Result / Frozen State
      hasUnknownResult: false,
      destructiveFrozen: false,
      unknownResultDetail: null,
      pendingOperations: [],
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
    const uid = this.state.user?.user_id ?? this.state.user?.id ?? this.state.activeOfflineAccountId;
    return uid ? String(uid) : 'guest';
  }

  writeScope() {
    const owner = this.getUserId();
    return { session_generation: this.get('sessionGeneration'), expected_account_id: owner === 'guest' ? 'local' : owner };
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

    const relistenData = getStorageItem(`pruner_relisten_${userId}`);
    let relistenIds = new Set();
    if (relistenData) {
      try {
        const parsed = JSON.parse(relistenData);
        if (Array.isArray(parsed)) {
          relistenIds = new Set(parsed.map(String));
        }
      } catch {}
    }

    const recentData = getStorageItem(`pruner_recent_played_${userId}`);
    let recentPlayed = [];
    if (recentData) {
      try {
        const parsed = JSON.parse(recentData);
        if (Array.isArray(parsed)) {
          recentPlayed = parsed.slice(0, 50);
        }
      } catch {}
    }

    this.set({
      instantModeEnabled: instantMode,
      batchQueue,
      opsHistory: opsHistory.slice(0, 100),
      relistenIds,
      recentPlayed,
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
    const queueKeys = ['playQueue', 'playQueueIndex', 'playMode', 'sessionGeneration', 'user'];
    if (queueKeys.some(key => key in partialState && partialState[key] !== this.state[key])) {
      this.state.queueVersion++;
      changedKeys.push('queueVersion');
    }
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

  // --- Liked Set Sync & Recent Played ---

  rebuildLikedSongIds(catalog = this.state.catalog) {
    const likedPl = this.getRealLikedPlaylist();
    const likedPlId = likedPl ? String(likedPl.id) : null;
    const nextLiked = new Set();
    const nextKeys = new Set(this.state.likedTrackKeys || []);
    (catalog || []).forEach(song => {
      if (song?.liked || (likedPlId && String(song.source_playlist_id) === likedPlId)) {
        if (song.id) nextLiked.add(String(song.id));
        const tk = getTrackKey(song);
        if (tk) nextKeys.add(tk);
      }
    });
    this.set({ likedSongIds: nextLiked, likedTrackKeys: nextKeys });
  }

  setSongLiked(songOrId, isLiked) {
    const next = new Set(this.state.likedSongIds || []);
    const nextKeys = new Set(this.state.likedTrackKeys || []);
    if (songOrId && typeof songOrId === 'object') {
      const tk = getTrackKey(songOrId);
      if (isLiked) {
        if (songOrId.id) next.add(String(songOrId.id));
        if (tk) nextKeys.add(tk);
      } else {
        if (songOrId.id) next.delete(String(songOrId.id));
        if (tk) nextKeys.delete(tk);
      }
    } else if (songOrId !== null && songOrId !== undefined) {
      const str = String(songOrId);
      if (isLiked) {
        next.add(str);
        if (str.includes(':')) nextKeys.add(str);
      } else {
        next.delete(str);
        if (str.includes(':')) nextKeys.delete(str);
      }
    }
    this.set({ likedSongIds: next, likedTrackKeys: nextKeys });
  }

  isSongLiked(songOrId) {
    if (songOrId === null || songOrId === undefined) return false;
    const ids = this.state.likedSongIds || new Set();
    const keys = this.state.likedTrackKeys || new Set();
    if (typeof songOrId === 'object') {
      const tk = getTrackKey(songOrId);
      if (tk && keys.has(tk)) return true;
      return Boolean(songOrId.id && ids.has(String(songOrId.id)));
    }
    const str = String(songOrId);
    if (ids.has(str) || keys.has(str)) return true;
    if (str.startsWith('netease:')) return ids.has(str.slice(8));
    return keys.has(`netease:${str}`);
  }

  recordRecentPlayed(song) {
    if (!song) return;
    const slim = { ...serializeTrackForStorage(song), playedAt: Date.now() };
    const filtered = this.state.recentPlayed.filter(item => getTrackKey(item) !== slim.track_key);
    this.replaceRecentPlayed([slim, ...filtered].slice(0, 50));
  }

  replaceRecentPlayed(next) {
    setStorageItem(`pruner_recent_played_${this.getUserId()}`, JSON.stringify(next));
    this.set({ recentPlayed: next });
  }

  // --- Relisten-Later Persistent Middle State ('L' Key) ---

  markRelistenLater(songId) {
    if (songId === null || songId === undefined) return;
    const userId = this.getUserId();
    const next = new Set(this.state.relistenIds || []);
    next.add(String(songId));
    setStorageItem(`pruner_relisten_${userId}`, JSON.stringify(Array.from(next)));
    this.set({ relistenIds: next });
  }

  unmarkRelistenLater(songId) {
    if (songId === null || songId === undefined) return;
    const userId = this.getUserId();
    const next = new Set(this.state.relistenIds || []);
    next.delete(String(songId));
    setStorageItem(`pruner_relisten_${userId}`, JSON.stringify(Array.from(next)));
    this.set({ relistenIds: next });
  }

  isRelistenLater(songId) {
    if (songId === null || songId === undefined) return false;
    return (this.state.relistenIds || new Set()).has(String(songId));
  }

  // --- Settings & Visual Mode ---

  updateSettings(partial) {
    const next = {
      ...DEFAULT_SETTINGS,
      ...(this.state.settings || {}),
      ...(partial || {}),
    };
    this.set({ settings: next });
    this.applyVisualMode(next.visual_mode);
    return next;
  }

  applyVisualMode(mode = null) {
    const resolved = mode || this.state.settings?.visual_mode || 'soft_dynamic';
    if (typeof document !== 'undefined' && document.documentElement?.dataset) {
      document.documentElement.dataset.visualMode = resolved;
      document.documentElement.dataset.gameMode = String(Boolean(this.state.settings?.game_mode));
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

    // Fallback to first writable playlist, then first playlist
    const ownedPl = list.find(p => p.kind === 'self_created' || p.kind === 'owned');
    if (ownedPl) return ownedPl.id;

    return list.length > 0 ? list[0].id : null;
  }

  /**
   * Resolves verified destructive action and target from real playlist kind.
   * @param {Song} song
   * @param {number|string|null} [explicitPlaylistId=null]
   * @returns {{ valid: boolean, action: 'unlike'|'remove_tracks'|null, playlistId: number|string|null, targetName: string, reason?: string }}
   */
  resolveDestructiveAction(song, explicitPlaylistId = null) {
    if (!song || typeof song !== 'object') return { valid: false, action: null, playlistId: null, targetName: '', reason: '未指定歌曲' };
    const targetPid = explicitPlaylistId ?? song.source_playlist_id ?? song.playlist_id;
    const playlists = this.state.playlists || [];
    const pl = playlists.find(p => String(p.id) === String(targetPid));
    // Unified local hybrid playlists support pruning ANY canonical track (local/OpenSubsonic/WebDAV/external/NetEase) even offline.
    if ((pl && pl.kind === 'hybrid') || String(targetPid || '').startsWith('hybrid:')) {
      const tk = getTrackKey(song);
      if (!tk) return { valid: false, action: null, playlistId: null, targetName: '', reason: '缺少有效曲目标识' };
      return {
        valid: true,
        action: 'remove_hybrid_tracks',
        playlistId: pl ? pl.id : String(targetPid),
        targetName: (pl && pl.name) || '混合歌单',
        trackKey: tk,
      };
    }
    if (!writableNeteaseTrack(song)) return { valid: false, action: null, playlistId: null, targetName: '', reason: '未指定歌曲' };
    if (this.state.offlineMode) {
      return { valid: false, action: null, playlistId: null, targetName: '', reason: '离线只读模式下不可执行云端移除' };
    }
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
      if (pl.kind === 'subscribed') {
        return {
          valid: false,
          action: null,
          playlistId: pl.id,
          targetName: pl.name,
          reason: '收藏歌单仅支持只读播放，无法从云端移除曲目',
        };
      }
      if (pl.kind === 'cloud' || pl.kind === 'local') {
        return {
          valid: false,
          action: null,
          playlistId: pl.id,
          targetName: pl.name,
          reason: '云盘或本地曲库仅支持只读播放，无法执行歌单移除',
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
    if (song?.local_path || (song?.provider && song.provider !== 'netease') ||
        (song?.track_key && !song.track_key.startsWith('netease:'))) return getTrackKey(song);
    return this.reviewSongKey(song, 'unknown');
  }

  getCatalogRows() {
    const unique = new Map();
    for (const source of ['catalog', 'cloudSongs', 'localSongs']) {
      for (const song of this.get(source) || []) {
        const key = this.catalogSongKey(song);
        if (!unique.has(key)) unique.set(key, song);
      }
    }
    return [...unique.values()];
  }

  getRuleEvaluationPool() {
    const unique = new Map();
    const songs = ['catalog', 'cloudSongs', 'localSongs'].flatMap(source => this.get(source) || []);
    for (const song of songs) {
      const key = getTrackKey(song);
      const preferred = unique.get(key);
      if (!preferred) unique.set(key, song);
      else {
        const merged = {...song, ...preferred};
        for (const [field, value] of Object.entries(song)) {
          if (merged[field] == null || merged[field] === '' || (Array.isArray(merged[field]) && !merged[field].length)) {
            merged[field] = value;
          }
        }
        unique.set(key, merged);
      }
    }
    return [...unique.values()];
  }

  restorePendingOperations(operations) {
    const pending = Array.isArray(operations)
      ? operations.filter(item => (
          item
          && typeof item === 'object'
          && typeof item.operation_id === 'string'
          && ['like', 'unlike', 'remove_tracks', 'create_playlist', 'add_tracks'].includes(item.action)
          && item.target
          && typeof item.target === 'object'
        ))
      : [];
    const first = pending[0] || null;
    let detail = null;
    if (first) {
      const songId = Array.isArray(first.target.song_ids) ? first.target.song_ids[0] : null;
      const playlistId = first.target.source_playlist_id ?? first.target.playlist_id ?? null;
      const song = (this.state.catalog || []).find(item => (
        String(item.id) === String(songId)
        && (playlistId === null || String(item.source_playlist_id) === String(playlistId))
      )) || (songId !== null ? { id: songId, title: `歌曲 #${songId}`, source_playlist_id: playlistId } : null);
      detail = {
        pendingOperation: first,
        operationId: first.operation_id,
        song,
        action: first.action,
        playlistId,
        targetName: first.target.name || (playlistId !== null ? `歌单 #${playlistId}` : '云端歌单'),
        originalIndex: null,
      };
    }
    this.set({
      pendingOperations: pending,
      hasUnknownResult: pending.length > 0,
      destructiveFrozen: pending.length > 0,
      unknownResultDetail: detail,
    });
    return pending;
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

  setRailCollapsed(collapsed) {
    const val = Boolean(collapsed);
    setStorageItem('pruner_rail_collapsed', val ? 'true' : 'false');
    this.set({ railCollapsed: val });
  }

  setPlayMode(mode) {
    const allowed = ['order', 'loop', 'single', 'shuffle', 'fm', 'intelligence', 'smart_radio'];
    const resolved = allowed.includes(mode) ? mode : 'order';
    if (['order', 'loop', 'single', 'shuffle'].includes(resolved)) {
      setStorageItem('pruner_play_mode', resolved);
    }
    this.set({ playMode: resolved });
  }

  // --- Soft-Commit Management (Instant Mode 3s Countdown + Cancellable Through Backup Wait) ---

  /**
   * Registers a 3-second local soft-commit timer for a single destructive action.
   * Remains cancellable even while awaiting pre-write backup before HTTP mutation fires.
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
    const sessionGeneration = this.state.sessionGeneration || 0;

    const softCommit = {
      id: commitId,
      song,
      action,
      playlistId,
      targetName: targetName || '喜欢的音乐',
      expiresAt,
      timerId: null,
      originalIndex: fromIndex,
      sessionGeneration,
      status: 'countdown', // 'countdown' | 'preparing' | 'writing' | 'cancelled' | 'committed'
      cancelled: false,
      cancelCallback,
    };

    softCommit.timerId = setTimeout(async () => {
      if (softCommit.cancelled) return;
      softCommit.status = 'preparing';

      // Move from pendingCommits countdown list to cancellable inFlightCommits during backup wait
      const remaining = (this.state.pendingCommits || []).filter(c => c.id !== commitId);
      this.inFlightCommits = [softCommit, ...this.inFlightCommits.filter(c => c.id !== commitId)];
      this.set({ pendingCommits: remaining });

      if (typeof commitCallback === 'function') {
        try {
          await commitCallback(softCommit);
        } catch (err) {
          console.error('Commit execution error:', err);
        } finally {
          this.inFlightCommits = this.inFlightCommits.filter(c => c.id !== commitId);
        }
      } else {
        this.inFlightCommits = this.inFlightCommits.filter(c => c.id !== commitId);
      }
    }, 3000);

    const nextPending = [softCommit, ...(this.state.pendingCommits || [])];
    this.set({ pendingCommits: nextPending });
    return softCommit;
  }

  /**
   * Cancels the newest uncommitted soft-commit action (triggered by Z key),
   * including commits currently awaiting backup before the cloud write fires.
   * @returns {boolean} true if an action was cancelled
   */
  cancelNewestCommit() {
    const pending = this.state.pendingCommits || [];
    if (pending.length > 0) {
      return this.cancelCommitById(pending[0].id);
    }
    const preparing = this.inFlightCommits.find(c => c.status === 'preparing' && !c.cancelled);
    if (preparing) {
      return this.cancelCommitById(preparing.id);
    }
    return false;
  }

  /**
   * Cancels a specific soft-commit by ID (either during 3s countdown or during backup wait).
   * @param {string} commitId
   * @returns {boolean}
   */
  cancelCommitById(commitId) {
    const pending = this.state.pendingCommits || [];
    const targetPending = pending.find(c => c.id === commitId);
    if (targetPending) {
      clearTimeout(targetPending.timerId);
      targetPending.cancelled = true;
      targetPending.status = 'cancelled';
      const nextPending = pending.filter(c => c.id !== commitId);
      this.set({ pendingCommits: nextPending });
      if (typeof targetPending.cancelCallback === 'function') {
        targetPending.cancelCallback(targetPending);
      }
      return true;
    }

    const targetInFlight = this.inFlightCommits.find(c => c.id === commitId && c.status === 'preparing' && !c.cancelled);
    if (targetInFlight) {
      targetInFlight.cancelled = true;
      targetInFlight.status = 'cancelled';
      this.inFlightCommits = this.inFlightCommits.filter(c => c.id !== commitId);
      if (typeof targetInFlight.cancelCallback === 'function') {
        targetInFlight.cancelCallback(targetInFlight);
      }
      return true;
    }

    return false;
  }

  /**
   * Cancels all pending soft-commit timers and backup-waiting commits (e.g. before logout/close).
   */
  cancelAllCommits() {
    const pending = this.state.pendingCommits || [];
    pending.forEach(c => {
      clearTimeout(c.timerId);
      c.cancelled = true;
      c.status = 'cancelled';
      if (typeof c.cancelCallback === 'function') {
        c.cancelCallback(c);
      }
    });
    const preparing = this.inFlightCommits.filter(c => c.status === 'preparing' && !c.cancelled);
    this.inFlightCommits = this.inFlightCommits.filter(c => c.status !== 'preparing');
    preparing.forEach(c => {
      c.cancelled = true;
      c.status = 'cancelled';
      if (typeof c.cancelCallback === 'function') {
        c.cancelCallback(c);
      }
    });
    this.set({ pendingCommits: [] });
  }

  // --- Staged Batch Queue (Batch Mode, Persisted) ---

  stageBatchAction(song, action = 'unlike', playlistId = null, targetName = '') {
    if (!song || this.state.destructiveFrozen || this.state.hasUnknownResult) return false;
    if (this.state.offlineMode && action !== 'remove_hybrid_tracks') return false;
    const resolved = this.resolveDestructiveAction(song, playlistId);
    if (!resolved.valid || resolved.action !== action || String(resolved.playlistId) !== String(playlistId)) {
      return false;
    }
    const queue = [...(this.state.batchQueue || [])];
    const songIdentity = getTrackKey(song) || String(song.id);
    const exists = queue.some(item => (getTrackKey(item.song) || String(item.song.id)) === songIdentity && String(item.playlistId) === String(playlistId) && item.action === action);
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

  unstageBatchAction(songIdOrKey, playlistId = null, action = null) {
    const targetKey = String(songIdOrKey);
    const queue = (this.state.batchQueue || []).filter(item => {
      const itemTk = getTrackKey(item.song);
      const isHybrid = item.action === 'remove_hybrid_tracks';
      const matchSong = isHybrid
        ? (itemTk === targetKey || (Number(item.song?.id) > 0 && String(item.song.id) === targetKey))
        : (String(item.song?.id) === targetKey || itemTk === targetKey);
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

    const isHybridAction = action === 'remove_hybrid_tracks';
    const matchesSong = (song) => {
      const tk = getTrackKey(song);
      if (tk && idSet.has(tk)) return true;
      if (isHybridAction) return false;
      return Number(song?.id) > 0 && idSet.has(String(song.id));
    };

    // 1. Catalog precision filter: only remove the copy in this playlist
    const nextCatalog = this.state.catalog.filter(song => {
      if (!matchesSong(song)) return true;
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
        if (!matchesSong(song)) return false;
        if (targetPid !== null) return String(song.source_playlist_id) === targetPid;
        return true;
      }).length;

    const nextDeck = currentDeck.filter(song => {
      if (!matchesSong(song)) return true;
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
      const matchSong = matchesSong(item.song);
      if (!matchSong) return true;
      if (targetPid !== null && String(item.playlistId) !== targetPid) {
        return true;
      }
      if (action !== null && item.action !== action) {
        return true;
      }
      return false;
    });

    // Update likedSongIds if unlike
    if (action === 'unlike' || (likedPlId && targetPid === likedPlId)) {
      const nextLiked = new Set(this.state.likedSongIds || []);
      idSet.forEach(id => nextLiked.delete(id));
      this.set({ likedSongIds: nextLiked });
    }

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
      relistenIds: Array.from(this.state.relistenIds || []),
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
    this.saveReviewProgress(playlistId, 'skipped', 'shared', {skippedIds: []});
    this.set({
      deckIndex: 0,
      keptIds: new Set(),
      skippedIds: new Set(),
      deckHistory: [],
    });
  }
}

export const state = new Store();
