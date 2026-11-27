/**
 * Pruner Studio - Playlist Cleanup Engine (歌单清理)
 * 42/58 Workspace layout, strict keyboard shortcuts (ignoring e.repeat & IME e.isComposing),
 * persistent 'L' relisten-later middle state, 3-second soft commit cancellable through backup wait
 * with session_generation verification, skip-to-end, account-scoped progress persistence,
 * and 8+ distinct state representations.
 */
import { api } from './api.js';
import { auth } from './auth.js';
import { state } from './state.js';
import { player } from './player.js';
import { stageManager } from './stage.js';
import { toast } from './toast.js';
import { modal } from './modal.js';
import { RuleEngine } from './rules.js';
import { lyricsManager, LyricsEngine } from './lyrics.js';
import { createIcon } from './icons.js';
import {
  createElement,
  formatDuration,
  formatArtists,
  getSizedCoverUrl,
  isPositiveSongId,
  normalizeTimeToSeconds,
  estimateRemainingTime,
} from './utils.js';

function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

class DeckReviewEngine {
  constructor() {
    this.container = null;
    this.dragThreshold = 90;
    this.sessionSnapshotReady = false;
    this.sessionSnapshotPromise = null;
    this.deckLyricsCache = new Map();
    this.deckLyricsRequestSeq = 0;
    this._playingListenerBound = false;
    this.randomSeed = (Date.now() ^ 0x9e3779b9) >>> 0;
    this.hydratedAttempted = new Set();
    this.unavailableMetadataIds = new Set();
  }

  init(containerId) {
    this.container = document.getElementById(containerId);
    this.initKeyboardControls();
    if (!this._playingListenerBound) {
      this._playingListenerBound = true;
      state.on('change:currentPlayingSong', () => {
        if (state.get('activeTab') === 'deck') {
          const song = this.getCurrentSong();
          if (song) this.syncDeckLyricsForSong(song);
        }
      });
    }
  }

  /**
   * Evaluates whether a KeyboardEvent is eligible to trigger Deck shortcuts.
   * Exposed as a method for deterministic unit testing of repeat/IME/tab/focus guards.
   * @param {KeyboardEvent} e
   * @returns {boolean}
   */
  shouldHandleDeckKeyEvent(e) {
    if (!e || e.defaultPrevented) return false;
    // Strictly ignore auto-repeat key holds and IME composition
    if (e.repeat) return false;
    if (e.isComposing || e.keyCode === 229) return false;
    if (e.ctrlKey || e.metaKey || e.altKey) return false;

    const target = e.target;
    if (target) {
      const tag = String(target.tagName || '').toUpperCase();
      if (['INPUT', 'SELECT', 'TEXTAREA'].includes(tag)) return false;
      if (target.isContentEditable) return false;
      if (typeof target.closest === 'function' && target.closest(
        '.modal-backdrop, .context-menu, #context-menu-host, #pending-actions-drawer, #play-queue-drawer, #lyrics-drawer-panel, #rules-drawer-panel, #player-more-popover, #stage-customize-popover, #app-left-rail',
      )) {
        return false;
      }
    }

    if (typeof document !== 'undefined') {
      const blockingSelector = [
        '.modal-backdrop.open',
        '#context-menu-host .context-menu',
        '#pending-actions-drawer.open',
        '#play-queue-drawer.open',
        '#lyrics-drawer-panel.open',
        '#rules-drawer-panel.open',
        '#immersive-stage-overlay.open',
        '#player-more-popover:not(.hidden)',
        '#stage-customize-popover:not(.hidden)',
        '#stage-more-popover:not(.hidden)',
        '.app-shell.mobile-drawer-open',
      ].join(', ');
      if (document.querySelector?.(blockingSelector)) return false;
      const deckPanel = document.getElementById('view-deck');
      if (!deckPanel || !deckPanel.classList.contains('active')) return false;
    }

    if (
      state.get('isStageOpen')
      || state.get('isPlayQueueOpen')
      || state.get('isLyricsDrawerOpen')
      || state.get('isPendingDrawerOpen')
      || state.get('mobileDrawerOpen')
    ) {
      return false;
    }
    if (state.get('activeTab') && state.get('activeTab') !== 'deck') {
      // Allow tests that only toggle #view-deck without updating state.activeTab
      const deckPanel = typeof document !== 'undefined' ? document.getElementById('view-deck') : null;
      if (!deckPanel || !deckPanel.classList.contains('active')) return false;
    }
    return true;
  }

  handleKeydown(e) {
    if (!this.shouldHandleDeckKeyEvent(e)) return false;

    switch (e.key) {
      case 'ArrowLeft':
      case 'a':
      case 'A':
        e.preventDefault?.();
        this.handleDestructiveAction();
        return true;
      case 'ArrowRight':
      case 'd':
      case 'D':
        e.preventDefault?.();
        this.handleKeepAction();
        return true;
      case ' ':
        e.preventDefault?.();
        this.handleSkipToEndAction();
        return true;
      case 'p':
      case 'P':
        e.preventDefault?.();
        this.toggleDeckPreviewPlayback();
        return true;
      case 'ArrowUp':
      case 'w':
      case 'W':
      case 'c':
      case 'C':
        e.preventDefault?.();
        this.jumpToChorus();
        return true;
      case 'z':
      case 'Z':
        e.preventDefault?.();
        this.handleUndoAction();
        return true;
      case 'l':
      case 'L':
        e.preventDefault?.();
        this.handleRelistenLaterAction();
        return true;
      default:
        return false;
    }
  }

  initKeyboardControls() {
    if (typeof document === 'undefined') return;
    document.addEventListener('keydown', (e) => {
      this.handleKeydown(e);
    });
  }

  isStubTrack(song) {
    if (!song || typeof song !== 'object') return false;
    const title = String(song.title || '').trim();
    if (/^歌曲\s*#\d+$/.test(title)) return true;
    const hasCover = Boolean(String(song.cover_url || '').trim());
    const durSec = normalizeTimeToSeconds(song.duration || song.duration_ms);
    const artistText = formatArtists(song.artists);
    return !hasCover && durSec <= 0 && (!title || artistText === '未知歌手');
  }

  seededShuffle(items, seed = this.randomSeed) {
    const arr = [...items];
    const rng = mulberry32(seed);
    for (let i = arr.length - 1; i > 0; i--) {
      const j = Math.floor(rng() * (i + 1));
      [arr[i], arr[j]] = [arr[j], arr[i]];
    }
    return arr;
  }

  /**
   * Filter and order catalog songs for the current review queue.
   * @param {Array} catalog
   * @param {number|string|null} playlistId
   * @param {string} filter
   * @param {string} order
   * @returns {Array}
   */
  filterAndOrderTracks(catalog, playlistId, filter = 'undecided', order = 'random') {
    if (!catalog || catalog.length === 0) return [];
    const rules = state.get('rules') || [];
    const keptIds = state.get('keptIds') || new Set();
    const relistenIds = state.get('relistenIds') || new Set();
    const batchQueue = state.get('batchQueue') || [];
    const stagedKeys = new Set(batchQueue.map(b => state.reviewSongKey(b.song, b.playlistId)));
    const likedPl = state.getRealLikedPlaylist();
    const likedPlId = likedPl ? String(likedPl.id) : null;

    // 1. Playlist match
    let list = catalog.filter(song => {
      if (playlistId === 'all') return true;
      if (playlistId) {
        return String(song.source_playlist_id) === String(playlistId);
      }
      if (likedPlId) {
        return String(song.source_playlist_id) === likedPlId;
      }
      return true;
    });

    // 2. Factual filter
    list = list.filter(song => {
      const songIdStr = String(song.id);
      if (filter === 'undecided') {
        return !keptIds.has(songIdStr)
          && !relistenIds.has(songIdStr)
          && !stagedKeys.has(state.reviewSongKey(song, playlistId));
      }
      if (filter === 'relisten') {
        return relistenIds.has(songIdStr);
      }
      if (filter === 'skipped') {
        const skipped = state.get('skippedIds') || new Set();
        return skipped.has(songIdStr);
      }
      if (filter === 'rules') {
        return RuleEngine.getMatchingRules(song, rules).length > 0;
      }
      if (filter === 'vip') {
        return song.availability === 'vip';
      }
      if (filter === 'unavailable') {
        return song.availability === 'unavailable';
      }
      if (filter === 'short') {
        const sec = normalizeTimeToSeconds(song.duration);
        return sec < 90;
      }
      if (filter === 'long') {
        const sec = normalizeTimeToSeconds(song.duration);
        return sec > 300;
      }
      return true; // 'all'
    });

    // 3. Sorting
    const sorted = [...list];
    if (order === 'random') {
      if (filter === 'unavailable') {
        return this.seededShuffle(sorted, this.randomSeed);
      }
      const healthy = [];
      const stubs = [];
      for (const song of sorted) {
        if (this.isStubTrack(song) || song.availability === 'unavailable') stubs.push(song);
        else healthy.push(song);
      }
      return [
        ...this.seededShuffle(healthy, this.randomSeed),
        ...this.seededShuffle(stubs, (this.randomSeed ^ 0x517cc1b7) >>> 0),
      ];
    } else if (order === 'recent') {
      sorted.reverse();
    } else if (order === 'oldest') {
      // standard order
    } else if (order === 'title') {
      sorted.sort((a, b) => (a.title || '').localeCompare(b.title || ''));
    } else if (order === 'duration') {
      sorted.sort((a, b) => normalizeTimeToSeconds(b.duration) - normalizeTimeToSeconds(a.duration));
    }

    return sorted;
  }

  pickAnotherRandomSong() {
    const queue = [...(state.get('deckQueue') || [])];
    const index = state.get('deckIndex') || 0;
    const remaining = queue.slice(index);
    if (remaining.length <= 1) {
      this.randomSeed = ((this.randomSeed + 0x9e3779b9) ^ Date.now()) >>> 0;
      if (state.get('deckOrder') !== 'random') {
        state.set({ deckOrder: 'random' });
        const orderSelect = typeof document !== 'undefined' ? document.getElementById('deck-order-select') : null;
        if (orderSelect) orderSelect.value = 'random';
      }
      this.reloadDeck();
      return;
    }
    const currentId = String(queue[index]?.id ?? '');
    const healthyCandidates = [];
    const fallbackCandidates = [];
    for (let i = index + 1; i < queue.length; i++) {
      if (String(queue[i]?.id ?? '') === currentId) continue;
      if (!this.isStubTrack(queue[i]) && queue[i]?.availability !== 'unavailable') {
        healthyCandidates.push(i);
      } else {
        fallbackCandidates.push(i);
      }
    }
    const pool = healthyCandidates.length > 0 ? healthyCandidates : fallbackCandidates;
    if (pool.length === 0) return;
    const pickedIdx = pool[Math.floor(Math.random() * pool.length)];
    [queue[index], queue[pickedIdx]] = [queue[pickedIdx], queue[index]];
    state.set({ deckQueue: queue });
    this.saveCurrentProgress();
    this.render();
    const nextSong = this.getCurrentSong();
    if (nextSong && state.get('autoplayEnabled') && state.get('activeTab') === 'deck') {
      player.playSong(nextSong, true, { preserveQueue: true, origin: 'deck' });
    }
  }

  async hydrateStubTrack(song) {
    if (!song || !isPositiveSongId(song.id)) return;
    const idKey = String(song.id);
    if (this.hydratedAttempted.has(idKey)) return;
    this.hydratedAttempted.add(idKey);
    try {
      const res = await api.hydrateSong(song.id);
      if (!res.ok || !res.data) return;
      if (res.data.hydrated && res.data.song) {
        const fresh = res.data.song;
        const patchSong = (item) => (String(item?.id) === idKey ? {
          ...item,
          title: fresh.title || item.title,
          artists: fresh.artists?.length ? fresh.artists : item.artists,
          album: fresh.album ?? item.album,
          duration: fresh.duration ?? item.duration,
          duration_ms: fresh.duration_ms ?? item.duration_ms,
          cover_url: fresh.cover_url || item.cover_url,
          availability: fresh.availability || item.availability,
        } : item);
        const catalog = (state.get('catalog') || []).map(patchSong);
        const deckQueue = (state.get('deckQueue') || []).map(patchSong);
        state.set({ catalog, deckQueue });
        if (String(this.getCurrentSong()?.id) === idKey) {
          this.render();
        }
      } else if (res.data.unavailable_metadata) {
        this.unavailableMetadataIds.add(idKey);
        if (String(this.getCurrentSong()?.id) === idKey) {
          this.render();
        }
      }
    } catch {}
  }

  /**
   * Loads tracks into the review queue with strict progress restoration order.
   */
  async jumpToChorus() {
    const song = (state.get('deckQueue') || [])[state.get('deckIndex') || 0];
    if (!song) return;
    if (String(player.currentSong?.id) !== String(song.id) || !player.activeSrc) {
      await player.playSong(song, true, {preserveQueue: true, origin: 'deck'});
    }
    if (String(player.currentSong?.id) === String(song.id) && player.activeSrc) player.performChorusJump(player.currentSong);
  }

  reloadDeck() {
    const catalog = state.get('catalog') || [];
    const selectedPlaylistId = state.get('selectedPlaylistId');
    const filter = state.get('deckFilter') || 'undecided';
    const order = state.get('deckOrder') || 'random';

    // 1. FIRST: Load saved progress for the current account + playlist + filter + order
    const saved = state.loadReviewProgress(selectedPlaylistId, filter, order);

    // 2. Initialize fresh Sets to prevent leakage across context switches
    const keptIds = new Set(Array.isArray(saved?.keptIds) ? saved.keptIds.map(String) : []);
    const shared = state.loadReviewProgress(selectedPlaylistId, 'skipped', 'shared');
    const skippedIds = new Set((shared?.skippedIds || saved?.skippedIds || []).map(String));
    state.set({ keptIds, skippedIds });

    // 3. Construct filtered tracks using the newly loaded sets
    const tracks = this.filterAndOrderTracks(catalog, selectedPlaylistId, filter, order);

    // 4. Set deckQueue and start index
    let startIndex = 0;
    if (saved?.currentSongKey) {
      const anchoredIndex = tracks.findIndex(song => state.reviewSongKey(song, selectedPlaylistId) === saved.currentSongKey);
      startIndex = anchoredIndex >= 0 ? anchoredIndex : 0;
    } else if (saved && typeof saved.index === 'number') {
      startIndex = Math.max(0, Math.min(saved.index, tracks.length));
    }

    state.set({
      deckQueue: tracks,
      deckIndex: startIndex,
      deckHistory: [],
    });

    this.render();

    // Auto-play first song ONLY when user is actively on the Deck tab
    const current = this.getCurrentSong();
    const isDeckActive = state.get('activeTab') === 'deck';
    if (current && state.get('autoplayEnabled') && isDeckActive) {
      player.playSong(current, true, { preserveQueue: true, origin: 'deck' });
    }
  }

  toggleDeckPreviewPlayback() {
    const deckSong = this.getCurrentSong();
    const playing = state.get('currentPlayingSong');
    if (deckSong && (!playing || String(playing.id) !== String(deckSong.id))) {
      player.playSong(deckSong, true, { preserveQueue: true, origin: 'deck' });
      return;
    }
    player.togglePlay();
  }

  getCurrentSong() {
    const queue = state.get('deckQueue') || [];
    const index = state.get('deckIndex') || 0;
    return queue[index] || null;
  }

  /**
   * Determine the current active UI state among 8 distinct states + unknown_result state.
   * @returns {'loading'|'unauthenticated'|'not_synced'|'true_empty'|'filter_empty'|'complete'|'error'|'unknown_result'|'active'}
   */
  determineWorkspaceState() {
    if (state.get('hasUnknownResult') || state.get('destructiveFrozen')) return 'unknown_result';
    if (state.get('catalogLoading')) return 'loading';
    if (!state.get('authenticated')) return 'unauthenticated';
    if (state.get('catalogError')) return 'error';

    const playlists = state.get('playlists') || [];
    const selectedId = state.get('selectedPlaylistId');
    const currentPl = playlists.find(p => String(p.id) === String(selectedId));

    const catalog = state.get('catalog') || [];
    const queue = state.get('deckQueue') || [];
    const index = state.get('deckIndex') || 0;

    const likedPl = state.getRealLikedPlaylist();
    const targetPlId = selectedId || (likedPl ? likedPl.id : null);
    const plTracksInCatalog = catalog.filter(s => String(s.source_playlist_id) === String(targetPlId));

    if (currentPl && currentPl.track_count > 0 && plTracksInCatalog.length === 0) {
      return 'not_synced';
    }

    if (plTracksInCatalog.length === 0 && (!currentPl || currentPl.track_count === 0)) {
      return 'true_empty';
    }

    if (queue.length === 0 && plTracksInCatalog.length > 0) {
      return 'filter_empty';
    }

    if (index >= queue.length && queue.length > 0) {
      return 'complete';
    }

    return 'active';
  }

  render() {
    this.detachDrag?.();
    player.updateChorusUI(player.currentSong, player.getNormalizedDuration());
    if (!this.container) return;
    this.container.textContent = '';

    const workspaceState = this.determineWorkspaceState();
    this.updateProgressInfo();

    if (workspaceState !== 'active') {
      this.renderStatePlaceholder(workspaceState);
      return;
    }

    const currentSong = this.getCurrentSong();
    if (!currentSong) {
      this.renderStatePlaceholder('complete');
      return;
    }

    const workspaceEl = this.createReviewWorkspaceLayout(currentSong);
    this.container.appendChild(workspaceEl);

    if (this.isStubTrack(currentSong)) {
      this.hydrateStubTrack(currentSong);
    }
    this.syncDeckLyricsForSong(currentSong);
    this.updateDestructiveButtonLabel();
  }

  async syncDeckLyricsForSong(song) {
    if (!song || typeof document === 'undefined') return;
    const statusEl = document.getElementById('deck-lyrics-status-text');
    const previewBtn = document.getElementById('deck-lyrics-preview-btn');
    const container = document.getElementById('deck-lyrics-container');
    if (!container) return;

    const playing = state.get('currentPlayingSong');
    const isMatched = Boolean(playing && String(playing.id) === String(song.id));

    if (isMatched) {
      const reqSeq = ++this.deckLyricsRequestSeq;
      if (statusEl) statusEl.textContent = '同步歌词 · 点击跳转';
      if (previewBtn) previewBtn.classList.add('hidden');
      container.dataset.deckSynced = 'true';
      if ((lyricsManager.lyricsData || []).length === 0 && String(lyricsManager.currentSongId || '') !== String(song.id)) {
        try {
          const res = await api.getLyrics(song.id);
          if (reqSeq !== this.deckLyricsRequestSeq) return;
          if (res?.ok && res.data) {
            const lrc = res.data.lyrics || res.data.lrc || '';
            const tlyric = res.data.tlyrics || res.data.tlyric || '';
            lyricsManager.loadLyrics(lrc, tlyric, {
              yrc: res.data.yrc || '',
              romalrc: res.data.romalrc || '',
              ttmlSource: res.data.ttml_source || '',
              songId: song.id,
            });
          }
        } catch {}
      }
      lyricsManager.renderDeckLyrics();
      if (player && player.audio && !player.audio.paused) {
        lyricsManager.updateTime(player.audio.currentTime);
      }
      return;
    }

    // Mismatched song: show static lyrics for the Deck's own song without hijacking active player lyrics
    if (statusEl) statusEl.textContent = '未在播放 · 按 P 试听';
    if (previewBtn) previewBtn.classList.remove('hidden');

    const cacheKey = String(song.id);
    const reqSeq = ++this.deckLyricsRequestSeq;

    if (this.deckLyricsCache.has(cacheKey)) {
      lyricsManager.renderDeckStaticLyrics(this.deckLyricsCache.get(cacheKey));
      return;
    }

    lyricsManager.renderDeckStaticLyrics([], { emptyText: '正在加载当前曲目歌词...' });

    try {
      const res = await api.getLyrics(song.id);
      if (reqSeq !== this.deckLyricsRequestSeq) return;
      const latestSong = this.getCurrentSong();
      if (!latestSong || String(latestSong.id) !== cacheKey) return;
      const latestPlaying = state.get('currentPlayingSong');
      if (latestPlaying && String(latestPlaying.id) === cacheKey) {
        if (statusEl) statusEl.textContent = '同步歌词 · 点击跳转';
        if (previewBtn) previewBtn.classList.add('hidden');
        lyricsManager.renderDeckLyrics();
        return;
      }

      let parsed = [];
      if (res?.ok && res.data) {
        const yrc = res.data.yrc || '';
        const lrc = res.data.lyrics || res.data.lrc || '';
        const tlyric = res.data.tlyrics || res.data.tlyric || '';
        const romalrc = res.data.romalrc || '';
        if (yrc) parsed = LyricsEngine.parseYrc(yrc, tlyric, romalrc);
        if (parsed.length === 0 && lrc) parsed = LyricsEngine.parseLrc(lrc, tlyric, romalrc);
      }
      if (this.deckLyricsCache.size >= 80) {
        const firstKey = this.deckLyricsCache.keys().next().value;
        if (firstKey !== undefined) this.deckLyricsCache.delete(firstKey);
      }
      this.deckLyricsCache.set(cacheKey, parsed);
      lyricsManager.renderDeckStaticLyrics(parsed, { emptyText: '当前曲目暂无歌词' });
    } catch {
      if (reqSeq !== this.deckLyricsRequestSeq) return;
      lyricsManager.renderDeckStaticLyrics([], { emptyText: '未能加载当前曲目歌词' });
    }
  }

  updateProgressInfo() {
    if (typeof document === 'undefined') return;
    const queue = state.get('deckQueue') || [];
    const index = state.get('deckIndex') || 0;
    const countDisplay = document.getElementById('deck-count-display');
    const remainingDisplay = document.getElementById('deck-remaining-display');
    const progressBar = document.getElementById('deck-progress-bar-fill');

    const total = queue.length;
    const currentNum = total > 0 ? Math.min(index + 1, total) : 0;
    const remaining = Math.max(0, total - index);

    if (countDisplay) {
      countDisplay.textContent = total > 0 ? `${currentNum} / ${total}` : '0 / 0';
    }
    if (remainingDisplay) {
      remainingDisplay.textContent = total > 0 ? `剩余 ${remaining} 首 · ${estimateRemainingTime(remaining)}` : '全部已整理';
    }
    if (progressBar) {
      const pct = total > 0 ? Math.min(100, Math.round((index / total) * 100)) : 0;
      progressBar.style.width = `${pct}%`;
    }
  }

  createReviewWorkspaceLayout(song) {
    const root = createElement('div', { className: 'review-workspace-grid' });

    // LEFT: Cover Area
    const leftPane = createElement('div', { className: 'review-left-pane' });
    const coverContainer = createElement('div', {
      className: 'review-cover-card',
      dataset: { songId: String(song.id) },
      attributes: { role: 'button', tabindex: '0', title: '点击打开全屏沉浸歌词舞台' },
      events: {
        click: () => {
          if (String(state.get('currentPlayingSong')?.id) !== String(song.id)) {
            player.playSong(song, true, { preserveQueue: true, origin: 'deck' });
          }
          stageManager.open(coverContainer);
        },
        keydown: (e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            if (String(state.get('currentPlayingSong')?.id) !== String(song.id)) {
              player.playSong(song, true, { preserveQueue: true, origin: 'deck' });
            }
            stageManager.open(coverContainer);
          }
        },
      },
    });

    const coverImg = createElement('img', {
      className: 'review-cover-img',
      attributes: {
        draggable: 'false',
        src: getSizedCoverUrl(song.cover_url, 384, '音'),
        alt: song.title || '封面',
        loading: 'eager',
      },
    });

    const badgesRow = createElement('div', { className: 'review-cover-badges' });
    if (song.availability === 'unavailable') {
      badgesRow.appendChild(createElement('span', { className: 'badge badge-danger', text: '无音源' }));
    } else if (song.availability === 'vip') {
      badgesRow.appendChild(createElement('span', { className: 'badge badge-warning', text: 'VIP' }));
    }

    if (song.is_cloud) {
      badgesRow.appendChild(createElement('span', { className: 'badge badge-source-cloud', text: '云盘' }));
    }

    if (state.isRelistenLater(song.id)) {
      badgesRow.appendChild(createElement('span', { className: 'badge badge-muted', text: '稍后再听' }));
    }

    if (song.source_type === 'alternative' || song.is_alternative) {
      badgesRow.appendChild(createElement('span', { className: 'badge badge-alt', text: '替代音源' }));
    }

    const rules = state.get('rules') || [];
    const matchedRules = RuleEngine.getMatchingRules(song, rules);
    if (matchedRules.length > 0) {
      badgesRow.appendChild(createElement('span', {
        className: 'badge badge-warning',
        text: `规则: ${matchedRules[0].name}`,
      }));
    }

    const likeIndicator = createElement('div', { className: 'card-action-indicator indicator-like', text: '保留' });
    const dislikeIndicator = createElement('div', { className: 'card-action-indicator indicator-dislike', text: '移除' });

    coverContainer.appendChild(coverImg);
    coverContainer.appendChild(badgesRow);
    coverContainer.appendChild(likeIndicator);
    coverContainer.appendChild(dislikeIndicator);

    if (state.get('dragEnabled')) {
      this.attachDragListeners(coverContainer);
    }

    leftPane.appendChild(coverContainer);

    // RIGHT: Song Details & Generous Zero-Reflow Synchronized Lyrics
    const rightPane = createElement('div', { className: 'review-right-pane' });

    const isDelistedStub = this.unavailableMetadataIds.has(String(song.id))
      || (this.isStubTrack(song) && song.availability === 'unavailable');
    const infoHeaderChildren = [
      createElement('h2', { className: 'review-song-title', text: song.title || '未知曲目' }),
      isDelistedStub
        ? createElement('div', { className: 'review-delisted-notice', text: '曲目已下架 / 仅存 ID' })
        : null,
      createElement('div', { className: 'review-song-artist', text: formatArtists(song.artists) }),
      createElement('div', {
        className: 'review-meta-line',
        children: [
          createElement('span', { text: song.album ? `专辑: ${song.album}` : '单曲' }),
          createElement('span', { text: `· 时长: ${formatDuration(song)}` }),
          createElement('span', { text: `· 来源: ${song.source_playlist_name || '歌单'}` }),
        ],
      }),
    ].filter(Boolean);

    const infoHeader = createElement('div', {
      className: 'review-song-info',
      children: infoHeaderChildren,
    });

    const isPlayingThisSong = String(state.get('currentPlayingSong')?.id) === String(song.id);
    const previewBtn = createElement('button', {
      className: `btn btn-ghost btn-sm deck-lyrics-preview-btn ${isPlayingThisSong ? 'hidden' : ''}`,
      attributes: {
        type: 'button',
        id: 'deck-lyrics-preview-btn',
        title: '试听当前待整理曲目 [P]',
      },
      events: {
        click: (e) => {
          e.stopPropagation();
          player.playSong(song, true, { preserveQueue: true, origin: 'deck' });
        },
      },
      children: [
        createIcon('play', { size: 12 }),
        createElement('span', { text: '试听此曲' }),
      ],
    });

    const lyricsBox = createElement('div', {
      className: 'deck-lyrics-wrapper',
      children: [
        createElement('div', {
          className: 'deck-lyrics-header',
          children: [
            createElement('span', {
              attributes: { id: 'deck-lyrics-status-text' },
              className: 'deck-lyrics-status-text',
              text: isPlayingThisSong ? '同步歌词 · 点击跳转' : '未在播放 · 按 P 试听',
            }),
            previewBtn,
          ],
        }),
        createElement('div', { attributes: { id: 'deck-lyrics-container' }, className: 'deck-lyrics-scroller' }),
      ],
    });

    rightPane.appendChild(infoHeader);
    rightPane.appendChild(lyricsBox);

    root.appendChild(leftPane);
    root.appendChild(rightPane);

    return root;
  }

  updateDestructiveButtonLabel() {
    if (typeof document === 'undefined') return;
    const dislikeBtn = document.getElementById('deck-dislike-btn');
    if (!dislikeBtn) return;

    const currentSong = this.getCurrentSong();
    const selectedId = state.get('selectedPlaylistId');
    const resolved = state.resolveDestructiveAction(currentSong, selectedId);

    const label = resolved.valid ? (resolved.action === 'unlike' ? '取消喜欢' : '移出歌单') : '不可移除';
    dislikeBtn.title = `${label} [← / A]`;
    const labelSpan = dislikeBtn.querySelector('.btn-label');
    if (labelSpan) labelSpan.textContent = label;
  }

  resetSessionSnapshot() {
    this.sessionSnapshotReady = false;
    this.sessionSnapshotPromise = null;
  }

  async ensureSessionSnapshot() {
    if (this.sessionSnapshotReady) return true;
    if (!this.sessionSnapshotPromise) {
      this.sessionSnapshotPromise = api.backup({ reason: 'auto_snapshot_before_first_destructive_write' })
        .then((result) => {
          this.sessionSnapshotReady = Boolean(result?.ok);
          return this.sessionSnapshotReady;
        })
        .catch(() => false)
        .finally(() => {
          this.sessionSnapshotPromise = null;
        });
    }
    return this.sessionSnapshotPromise;
  }

  /**
   * Action 1: Destructive Action (Unlike / Remove from Playlist)
   * Instant Mode: 3s soft-commit countdown, cancellable even through backup wait,
   * verifies session_generation and global freeze before emitting cloud write.
   * Batch Mode: stages for later frozen-manifest confirmation.
   */
  handleDestructiveAction() {
    if (state.get('destructiveFrozen') || state.get('hasUnknownResult')) {
      toast.warning('破坏性操作已冻结', '存在未确定的云端变更，请先完成只读核对。');
      return;
    }

    const song = this.getCurrentSong();
    if (!song) return;

    const selectedId = state.get('selectedPlaylistId');
    const resolved = state.resolveDestructiveAction(song, selectedId);

    if (!resolved.valid) {
      toast.warning('不可移除', resolved.reason || '该歌曲属于本地导入或只读歌单，无法执行云端移除。');
      return;
    }

    this.checkFirstUseExplanation();

    const isInstant = state.get('instantModeEnabled');

    if (isInstant) {
      const currentIndex = state.get('deckIndex') || 0;
      const scope = state.writeScope();
      const capturedSessionGen = scope.session_generation || 0;
      state.set({ deckIndex: currentIndex + 1 });
      this.saveCurrentProgress();

      state.createSoftCommit(
        song,
        resolved.action,
        resolved.playlistId,
        resolved.targetName,
        async (softCommit) => {
          try {
            // Re-check cancellation, freeze, and session generation BEFORE backup
            if (softCommit?.cancelled) return;
            if ((state.get('sessionGeneration') || 0) !== capturedSessionGen) return;
            if (state.get('destructiveFrozen') || state.get('hasUnknownResult')) {
              this.restoreSongToDeck(song, currentIndex);
              toast.warning('操作已冻结', '检测到未确定的云端结果，本次没有发送写请求。');
              return;
            }

            if (!(await this.ensureSessionSnapshot())) {
              if (softCommit?.cancelled) return;
              this.restoreSongToDeck(song, currentIndex);
              toast.error('自动快照失败', '本次云端操作未执行。请检查本地数据目录后重试。');
              return;
            }

            // Re-check cancellation, freeze, and session generation AFTER backup wait!
            if (softCommit?.cancelled) return;
            if ((state.get('sessionGeneration') || 0) !== capturedSessionGen) return;
            if (state.get('destructiveFrozen') || state.get('hasUnknownResult')) {
              this.restoreSongToDeck(song, currentIndex);
              toast.warning('操作已冻结', '备份期间触发写入冻结，本次没有发送写请求。');
              return;
            }

            if (softCommit) softCommit.status = 'writing';

            const extra = scope;
            let res;
            if (resolved.action === 'remove_hybrid_tracks') {
              const tk = resolved.trackKey || song.track_key || String(song.id);
              res = await api.removeTracksFromHybridPlaylist(resolved.playlistId, [tk]);
              if (softCommit) softCommit.status = 'committed';
              if (res.ok) {
                state.removeConfirmedSongs([tk], resolved.playlistId, resolved.action);
                toast.success('已从混合歌单移出', `"${song.title}" 已从 ${resolved.targetName} 移出。`);
              } else {
                this.restoreSongToDeck(song, currentIndex);
                toast.error('移出混合歌单失败', res.error?.message || '请稍后重试。');
              }
              return;
            }
            if (resolved.action === 'unlike') {
              res = await api.unlike([song.id], resolved.playlistId, extra);
            } else {
              res = await api.removeTracks(resolved.playlistId, [song.id], resolved.playlistId, extra);
            }

            if (softCommit) softCommit.status = 'committed';

            const isSuccess = res.ok && res.data && Array.isArray(res.data.successful_ids) && res.data.successful_ids.map(String).includes(String(song.id));
            const unknownItems = res.ok && res.data && Array.isArray(res.data.unknown_items)
              ? res.data.unknown_items
              : [];
            const legacyUnknownItems = res.ok && res.data && Array.isArray(res.data.failed_items)
              ? res.data.failed_items.filter(item => String(item.code || '').endsWith('_RESULT_UNKNOWN'))
              : [];
            const isUnknown = [...unknownItems, ...legacyUnknownItems].some(item => String(item.id) === String(song.id));

            if (isSuccess) {
              state.unmarkRelistenLater(song.id);
              state.removeConfirmedSongs([song.id], resolved.playlistId, resolved.action);
              state.addOperationLog({
                type: resolved.action === 'unlike' ? '取消喜欢' : '移出歌单',
                requested: 1,
                successful: 1,
                failed: 0,
                targetName: resolved.targetName,
                message: `成功${resolved.action === 'unlike' ? '取消喜欢' : '移出'}: "${song.title}"`,
              });
              toast.success('已同步至云端', `"${song.title}" 已从${resolved.targetName}同步处理。`);
            } else if (isUnknown) {
              await this.handleCommitFailureWithReconcile(
                song,
                resolved.action,
                resolved.playlistId,
                resolved.targetName,
                currentIndex,
                res.data?.pending_operation_ids?.[0] || null,
              );
            } else if (res.ok && res.data && Array.isArray(res.data.failed_items) && res.data.failed_items.some(f => String(f.id) === String(song.id))) {
              const failReason = res.data.failed_items.find(f => String(f.id) === String(song.id))?.reason || '远程操作被拒绝';
              toast.error('云端同步失败', `未能处理 "${song.title}": ${failReason}`);
              this.restoreSongToDeck(song, currentIndex);
            } else {
              await this.handleCommitFailureWithReconcile(
                song,
                resolved.action,
                resolved.playlistId,
                resolved.targetName,
                currentIndex,
                res.data?.pending_operation_ids?.[0] || null,
              );
            }
          } catch {
            if (softCommit?.cancelled) return;
            await this.handleCommitFailureWithReconcile(song, resolved.action, resolved.playlistId, resolved.targetName, currentIndex);
          }
        },
        (softCommit) => {
          this.restoreSongToDeck(song, softCommit?.originalIndex ?? currentIndex);
          toast.info('已取消操作', `已撤销针对 "${song.title}" 的同步。`);
        },
        currentIndex,
      );

      this.advanceToNextSong();
    } else {
      if (!state.stageBatchAction(song, resolved.action, resolved.playlistId, resolved.targetName)) {
        toast.quiet('未新增待处理项'); return;
      }
      const history = [...(state.get('deckHistory') || []), { song, action: resolved.action, playlistId: resolved.playlistId }];
      const currentIndex = state.get('deckIndex') || 0;

      state.set({
        deckHistory: history,
        deckIndex: currentIndex + 1,
      });
      this.saveCurrentProgress();

      toast.info('已暂存至待执行队列', `"${song.title}" 已加入待提交队列。`);
      this.advanceToNextSong();
    }
  }

  restoreSongToDeck(song, targetIndex = null) {
    const queue = state.get('deckQueue') || [];
    const songKey = state.reviewSongKey(song, state.get('selectedPlaylistId'));
    const songIndex = queue.findIndex(s => state.reviewSongKey(s, state.get('selectedPlaylistId')) === songKey);
    let newIndex = targetIndex !== null ? targetIndex : (state.get('deckIndex') || 0) - 1;
    if (songIndex >= 0) {
      newIndex = songIndex;
    }
    newIndex = Math.max(0, Math.min(newIndex, Math.max(0, queue.length - 1)));

    state.set({ deckIndex: newIndex });
    this.saveCurrentProgress();
    this.render();

    const current = this.getCurrentSong();
    if (current && state.get('autoplayEnabled') && state.get('activeTab') === 'deck') {
      player.playSong(current, true, { preserveQueue: true, origin: 'deck' });
    }
  }

  async handleCommitFailureWithReconcile(song, action, playlistId, targetName, originalIndex = null, operationId = null) {
    toast.info('正在只读核对', `网络未确认 "${song.title}" 状态，正在核对云端结果...`);
    try {
      const recRes = await api.reconcileOperations(action, playlistId, [song.id], playlistId);
      if (recRes.ok && recRes.data && Array.isArray(recRes.data.successful_ids) && recRes.data.successful_ids.map(String).includes(String(song.id))) {
        state.removeConfirmedSongs([song.id], playlistId, action);
        state.set({ hasUnknownResult: false, destructiveFrozen: false, unknownResultDetail: null });
        toast.success('已核对确认', `"${song.title}" 经只读核对已成功在远端处理。`);
        this.render();
        return;
      }
      if (recRes.ok && recRes.data && Array.isArray(recRes.data.failed_items) && recRes.data.failed_items.some(item => String(item.id) === String(song.id))) {
        this.restoreSongToDeck(song, originalIndex);
        state.set({ hasUnknownResult: false, destructiveFrozen: false, unknownResultDetail: null });
        toast.info('云端未发生变更', `"${song.title}" 已保留，可以继续整理。`);
        return;
      }
    } catch (err) {
      console.warn('Reconciliation call failed:', err);
    }

    this.restoreSongToDeck(song, originalIndex);
    let pendingOperation = null;
    try {
      const pending = await api.getPendingOperations();
      if (pending.ok && Array.isArray(pending.data?.operations)) {
        state.restorePendingOperations(pending.data.operations);
        pendingOperation = pending.data.operations.find(item => item.operation_id === operationId)
          || pending.data.operations.find(item => (
            item.action === action
            && String(item.target?.source_playlist_id ?? item.target?.playlist_id) === String(playlistId)
            && (item.target?.song_ids || []).map(String).includes(String(song.id))
          ))
          || null;
      }
    } catch {}
    state.set({
      hasUnknownResult: true,
      destructiveFrozen: true,
      unknownResultDetail: {
        song,
        action,
        playlistId,
        targetName,
        originalIndex,
        operationId: pendingOperation?.operation_id || operationId,
        pendingOperation,
      },
    });
    toast.error('远端状态未确定', `未能确认 "${song.title}" 的远端执行结果，破坏性操作已冻结。禁止盲目重试。`);
    this.render();
  }

  async refreshRecoveredOperations() {
    const pending = await api.getPendingOperations();
    if (!pending.ok || !pending.data || !Array.isArray(pending.data.operations)) {
      return false;
    }
    state.restorePendingOperations(pending.data.operations);
    this.render();
    return true;
  }

  async reconcileRecoveredOperation(operation) {
    if (!operation?.operation_id) return false;
    const result = await api.reconcilePendingOperation(operation.operation_id);
    if (!result.ok) {
      toast.error('只读核对失败', result.error?.message || '暂时无法确认远端结果，写操作仍保持冻结。');
      return false;
    }
    if (!(await this.refreshRecoveredOperations())) {
      toast.warning('核对结果待刷新', '远端核对已返回，但恢复日志暂时无法刷新；写操作仍保持冻结。');
      return false;
    }
    if ((state.get('pendingOperations') || []).length === 0) {
      toast.success('恢复核对完成', '未确定操作已得到确认，破坏性操作已解除冻结。');
      state.emit('auth:success', state.get('user'));
    }
    return true;
  }

  async ignoreRecoveredOperation(operation) {
    if (!operation?.operation_id) return false;
    const result = await api.ignorePendingOperation(operation.operation_id);
    if (!result.ok) {
      toast.error('无法忽略操作', result.error?.message || '恢复日志未更新，写操作仍保持冻结。');
      return false;
    }
    if (!(await this.refreshRecoveredOperations())) return false;
    toast.info('已明确忽略', '该语义操作仍被后端标记，后续不会盲目重发。');
    return true;
  }

  async dismissUnknownResultLocally() {
    const detail = state.get('unknownResultDetail');
    const operationId = detail?.pendingOperation?.operation_id || detail?.operationId;
    if (operationId) {
      await this.ignoreRecoveredOperation(detail.pendingOperation || { operation_id: operationId });
      return;
    }
    toast.error('无法忽略操作', '恢复日志标识不可用，写操作仍保持冻结。请重新加载后再试。');
  }

  /**
   * Action 2: Keep Action (Keep Song, Advance)
   */
  handleKeepAction() {
    const song = this.getCurrentSong();
    if (!song) return;

    const keptIds = new Set(state.get('keptIds') || []);
    keptIds.add(String(song.id));
    state.unmarkRelistenLater(song.id);

    const currentIndex = state.get('deckIndex') || 0;
    const history = [...(state.get('deckHistory') || []), { song, action: 'keep' }];

    state.set({
      keptIds,
      deckHistory: history,
      deckIndex: currentIndex + 1,
    });
    this.saveCurrentProgress();

    this.advanceToNextSong();
  }

  /**
   * Action 2.5: Persistent Relisten-Later Middle State ('L' Key)
   */
  handleRelistenLaterAction() {
    const song = this.getCurrentSong();
    if (!song) return;

    state.markRelistenLater(song.id);
    const currentIndex = state.get('deckIndex') || 0;
    const history = [...(state.get('deckHistory') || []), { song, action: 'relisten' }];

    state.set({
      deckHistory: history,
      deckIndex: currentIndex + 1,
    });
    this.saveCurrentProgress();

    toast.info('已标记稍后再听', `"${song.title}" 已加入“稍后再听”筛选，可在筛选器中随时复听。`);
    this.advanceToNextSong();
  }

  /**
   * Action 3: Skip to End of Current Round (Spacebar)
   */
  handleSkipToEndAction() {
    const song = this.getCurrentSong();
    if (!song) return;

    const queue = [...(state.get('deckQueue') || [])];
    const currentIndex = state.get('deckIndex') || 0;

    const skippedSong = queue.splice(currentIndex, 1)[0];
    queue.push(skippedSong);

    const skippedIds = new Set(state.get('skippedIds') || []);
    skippedIds.add(String(song.id));

    state.set({
      deckQueue: queue,
      skippedIds,
    });
    this.saveCurrentProgress();

    toast.info('已跳至本轮末尾', `"${song.title}" 将在最后再次出现。`);
    this.render();

    const nextSong = this.getCurrentSong();
    if (nextSong && state.get('autoplayEnabled')) {
      player.playSong(nextSong, true, { preserveQueue: true, origin: 'deck' });
    }
  }

  /**
   * Action 4: Undo Action (Z Key)
   * Cancels newest uncommitted soft commit (including during backup wait),
   * or reverts previous keep/relisten/batch review action.
   */
  handleUndoAction() {
    const isInstant = state.get('instantModeEnabled');

    if (isInstant) {
      const cancelled = state.cancelNewestCommit();
      if (cancelled) return;
      // Fallback: undo local keep / relisten history if no soft-commit is pending
      const history = state.get('deckHistory') || [];
      if (history.length === 0) {
        toast.info('无可撤销项', '当前没有正在倒计时的未提交操作。');
        return;
      }
      this.undoLastLocalHistoryEntry();
      return;
    }

    const history = state.get('deckHistory') || [];
    if (history.length === 0) {
      toast.info('无可撤销项', '当前轮次没有可撤销的整理操作。');
      return;
    }
    this.undoLastLocalHistoryEntry();
  }

  undoLastLocalHistoryEntry() {
    const history = state.get('deckHistory') || [];
    if (history.length === 0) return;

    const lastAction = history[history.length - 1];
    const currentIndex = state.get('deckIndex') || 0;

    if (currentIndex > 0) {
      const nextIndex = currentIndex - 1;
      const nextHistory = history.slice(0, -1);

      if (lastAction.action === 'relisten') {
        state.unmarkRelistenLater(lastAction.song.id);
      } else if (lastAction.action === 'keep') {
        const keptIds = new Set(state.get('keptIds') || []);
        keptIds.delete(String(lastAction.song.id));
        state.set({ keptIds });
      } else {
        state.unstageBatchAction(lastAction.song.id, lastAction.playlistId, lastAction.action);
      }

      state.set({
        deckIndex: nextIndex,
        deckHistory: nextHistory,
      });
      this.saveCurrentProgress();

      this.render();
      const song = this.getCurrentSong();
      if (song) {
        if (state.get('autoplayEnabled')) player.playSong(song, true, { preserveQueue: true, origin: 'deck' });
        toast.info('已撤销', `已恢复 "${song.title}"。`);
      }
    }
  }

  advanceToNextSong() {
    this.render();
    const nextSong = this.getCurrentSong();
    if (nextSong && state.get('autoplayEnabled')) {
      player.playSong(nextSong, true, { preserveQueue: true, origin: 'deck' });
    }
  }

  saveCurrentProgress() {
    const playlistId = state.get('selectedPlaylistId');
    const filter = state.get('deckFilter') || 'undecided';
    const order = state.get('deckOrder') || 'random';
    const index = state.get('deckIndex') || 0;
    const keptIds = Array.from(state.get('keptIds') || []);
    const skippedIds = Array.from(state.get('skippedIds') || []);

    state.saveReviewProgress(playlistId, 'skipped', 'shared', { skippedIds });
    state.saveReviewProgress(playlistId, filter, order, {
      index,
      currentSongKey: state.reviewSongKey(this.getCurrentSong(), playlistId),
      keptIds,
      skippedIds,
      updatedAt: Date.now(),
    });
  }

  checkFirstUseExplanation() {
    const userId = state.getUserId();
    const key = `pruner_destructive_intro_shown_${userId}`;
    if (localStorage.getItem(key)) return;

    localStorage.setItem(key, 'true');
    toast.info('3秒延迟同步说明', '单曲整理默认在 3 秒本地倒计时后同步至云端。倒计时期间可随时按 Z 键或点击取消撤销该操作。', 6000);
  }

  renderStatePlaceholder(stateType) {
    if (!this.container) return;

    const wrapper = createElement('div', { className: 'deck-state-container' });

    if (stateType === 'unknown_result') {
      const detail = state.get('unknownResultDetail');
      wrapper.appendChild(createElement('div', {
        className: 'empty-state animate-fade',
        children: [
          createElement('div', { className: 'empty-state-title text-danger', text: '远端变更状态未确定' }),
          createElement('div', {
            className: 'empty-state-desc',
            text: `网络异常导致针对 "${detail?.song?.title || '歌曲'}" 的云端操作未收到确定响应。为保护曲库数据，破坏性操作已临时冻结。系统禁止盲目重试。`,
          }),
          createElement('div', {
            style: 'display: flex; gap: var(--space-2); margin-top: var(--space-3);',
            children: [
              createElement('button', {
                className: 'btn btn-primary',
                text: '只读核对状态',
                events: {
                  click: async () => {
                    if (detail?.pendingOperation) {
                      await this.reconcileRecoveredOperation(detail.pendingOperation);
                    } else if (detail) {
                      await this.handleCommitFailureWithReconcile(
                        detail.song,
                        detail.action,
                        detail.playlistId,
                        detail.targetName,
                        detail.originalIndex,
                      );
                    }
                  },
                },
              }),
              createElement('button', {
                className: 'btn btn-secondary',
                text: '保留本地并解除冻结',
                events: {
                  click: () => this.dismissUnknownResultLocally(),
                },
              }),
            ],
          }),
        ],
      }));
    } else if (stateType === 'loading') {
      wrapper.appendChild(createElement('div', {
        className: 'empty-state animate-fade',
        children: [
          createElement('div', { className: 'spinner', style: 'width: 32px; height: 32px;' }),
          createElement('div', { className: 'empty-state-title', text: '正在加载曲库...' }),
          createElement('div', { className: 'empty-state-desc', text: '正在从本地缓存或云端检索歌单曲目。' }),
        ],
      }));
    } else if (stateType === 'unauthenticated') {
      wrapper.appendChild(createElement('div', {
        className: 'empty-state animate-fade',
        children: [
          createElement('div', { className: 'empty-state-title', text: state.get('offlineMode') ? '歌单清理需在线登录' : '请先连接网易云音乐账号' }),
          createElement('div', { className: 'empty-state-desc', text: '扫码登录后即可开始清理个人红心歌单及自建歌单；离线模式下可在“音乐库”浏览本地缓存。' }),
          createElement('button', {
            className: 'btn btn-primary',
            text: '扫码登录',
            events: {
              click: () => auth.openLoginModal(),
            },
          }),
        ],
      }));
    } else if (stateType === 'not_synced') {
      const selectedId = state.get('selectedPlaylistId');
      wrapper.appendChild(createElement('div', {
        className: 'empty-state animate-fade',
        children: [
          createElement('div', { className: 'empty-state-title', text: '当前歌单尚未同步到本地缓存' }),
          createElement('div', { className: 'empty-state-desc', text: '首次整理该歌单需要从网易云拉取一次曲目元数据。' }),
          createElement('button', {
            className: 'btn btn-primary',
            text: '立即同步此歌单',
            events: {
              click: async () => {
                if (selectedId) {
                  toast.info('正在同步', '拉取歌单曲目中...');
                  const res = await api.syncPlaylist(selectedId);
                  if (res.ok) {
                    const catRes = await api.getCatalog();
                    if (catRes.ok && catRes.data) {
                      const tracks = Array.isArray(catRes.data.tracks) ? catRes.data.tracks : catRes.data;
                      state.set({ catalog: tracks });
                      this.reloadDeck();
                    }
                  } else {
                    toast.error('同步失败', res.error?.message || '无法同步歌单。');
                  }
                }
              },
            },
          }),
        ],
      }));
    } else if (stateType === 'true_empty') {
      wrapper.appendChild(createElement('div', {
        className: 'empty-state animate-fade',
        children: [
          createElement('div', { className: 'empty-state-title', text: '歌单暂无歌曲' }),
          createElement('div', { className: 'empty-state-desc', text: '当前所选歌单内没有任何曲目。' }),
        ],
      }));
    } else if (stateType === 'filter_empty') {
      wrapper.appendChild(createElement('div', {
        className: 'empty-state animate-fade',
        children: [
          createElement('div', { className: 'empty-state-title', text: '当前筛选条件下无匹配歌曲' }),
          createElement('div', { className: 'empty-state-desc', text: '可以尝试切换事实筛选器或查看全部歌曲。' }),
          createElement('button', {
            className: 'btn btn-secondary',
            text: '查看全部歌曲',
            events: {
              click: () => {
                state.set({ deckFilter: 'all' });
                const filterSelect = document.getElementById('deck-filter-select');
                if (filterSelect) filterSelect.value = 'all';
                this.reloadDeck();
              },
            },
          }),
        ],
      }));
    } else if (stateType === 'complete') {
      const keptCount = (state.get('keptIds') || new Set()).size;
      const relistenCount = (state.get('relistenIds') || new Set()).size;
      const batchCount = (state.get('batchQueue') || []).length;
      const selectedId = state.get('selectedPlaylistId');
      const filter = state.get('deckFilter') || 'undecided';
      const order = state.get('deckOrder') || 'random';

      wrapper.appendChild(createElement('div', {
        className: 'deck-finished-card animate-fade',
        children: [
          createElement('div', { className: 'finished-title', text: '本轮整理已完成' }),
          createElement('div', { className: 'finished-desc', text: '当前筛选条件下的所有歌曲均已处理完毕。' }),
          createElement('div', {
            className: 'deck-finished-stats',
            children: [
              createElement('div', {
                className: 'finished-stat-box',
                children: [
                  createElement('div', { className: 'finished-stat-val kept', text: String(keptCount) }),
                  createElement('div', { className: 'finished-stat-label', text: '保留歌曲' }),
                ],
              }),
              createElement('div', {
                className: 'finished-stat-box',
                children: [
                  createElement('div', { className: 'finished-stat-val', text: String(relistenCount) }),
                  createElement('div', { className: 'finished-stat-label', text: '稍后再听' }),
                ],
              }),
              createElement('div', {
                className: 'finished-stat-box',
                children: [
                  createElement('div', { className: 'finished-stat-val pruned', text: String(batchCount) }),
                  createElement('div', { className: 'finished-stat-label', text: '待提交批处理' }),
                ],
              }),
            ],
          }),
          createElement('div', {
            style: 'display: flex; gap: var(--space-3); margin-top: var(--space-2); flex-wrap: wrap; justify-content: center;',
            children: [
              createElement('button', {
                className: 'btn btn-secondary',
                text: '重置进度并重新整理',
                events: {
                  click: () => {
                    state.resetReviewProgress(selectedId, filter, order);
                    this.reloadDeck();
                  },
                },
              }),
              relistenCount > 0 ? createElement('button', {
                className: 'btn btn-secondary',
                text: `复听稍后再听 (${relistenCount})`,
                events: {
                  click: () => {
                    state.set({ deckFilter: 'relisten' });
                    const filterSelect = document.getElementById('deck-filter-select');
                    if (filterSelect) filterSelect.value = 'relisten';
                    this.reloadDeck();
                  },
                },
              }) : null,
              createElement('button', {
                className: 'btn btn-primary',
                text: '查看同步与待执行队列',
                events: {
                  click: () => {
                    window.dispatchEvent(new CustomEvent('switch-tab', { detail: { tab: 'cloud', subTab: 'sync' } }));
                  },
                },
              }),
            ],
          }),
        ],
      }));
    } else if (stateType === 'error') {
      wrapper.appendChild(createElement('div', {
        className: 'empty-state animate-fade',
        children: [
          createElement('div', { className: 'empty-state-title text-danger', text: '加载数据失败' }),
          createElement('div', { className: 'empty-state-desc', text: state.get('catalogError') || '无法连接本地服务。' }),
          createElement('button', {
            className: 'btn btn-secondary',
            text: '重新尝试',
            events: {
              click: () => state.emit('deck:retry'),
            },
          }),
        ],
      }));
    }

    this.container.appendChild(wrapper);
  }

  attachDragListeners(card) {
    this.detachDrag?.();
    let startX, startY, currentX, currentY;
    let suppressClick = false;
    let activePointerId = null;
    let dragging = false;
    const like = card.querySelector('.indicator-like');
    const dislike = card.querySelector('.indicator-dislike');
    const matches = e => activePointerId == null || e?.pointerId === activePointerId;
    const resolveCoords = e => {
      const point = e?.changedTouches?.[0] || e?.touches?.[0] || e;
      return {
        x: Number.isFinite(point?.clientX) ? point.clientX : null,
        y: Number.isFinite(point?.clientY) ? point.clientY : null,
      };
    };
    const isVerticalScroll = (pointerType, dx, dy) =>
      (pointerType === 'touch' || pointerType === 'pen') && Math.abs(dy) > Math.abs(dx) && Math.abs(dy) > 14;
    const finishDrag = () => {
      dragging = false;
      activePointerId = null;
      card.classList.remove('is-dragging');
      card.style.transform = 'translate(0, 0) rotate(0deg)';
      if (like) like.style.opacity = '0';
      if (dislike) dislike.style.opacity = '0';
      document.removeEventListener('pointerdown', onAdditionalPointer, true);
      document.removeEventListener('pointermove', onPointerMove);
      document.removeEventListener('pointerup', onPointerUp);
      document.removeEventListener('pointercancel', onPointerCancel);
      window.removeEventListener('blur', onPointerCancel);
    };
    const onPointerCancel = () => {
      suppressClick = true;
      finishDrag();
    };
    const onClick = e => {
      if (!suppressClick || e.detail === 0) return;
      e.preventDefault();
      e.stopImmediatePropagation();
    };
    const onAdditionalPointer = e => {
      if (matches(e)) return;
      onPointerCancel();
      // The second contact may land on this same card.
      if (card.contains(e.target)) e.stopPropagation();
    };
    const onPointerDown = e => {
      if (dragging) {
        if (!matches(e)) onPointerCancel();
        return;
      }
      if (e.button > 0 || e.isPrimary === false || e.target?.closest?.('button, .badge')) return;
      suppressClick = false;
      const coords = resolveCoords(e);
      currentX = 0;
      currentY = 0;
      startX = coords.x ?? 0;
      startY = coords.y ?? 0;
      activePointerId = e.pointerId;
      dragging = true;
      card.classList.add('is-dragging');
      document.addEventListener('pointerdown', onAdditionalPointer, true);
      document.addEventListener('pointermove', onPointerMove);
      document.addEventListener('pointerup', onPointerUp);
      document.addEventListener('pointercancel', onPointerCancel);
      window.addEventListener('blur', onPointerCancel);
    };
    const onPointerMove = e => {
      if (!dragging || !matches(e)) return;
      const coords = resolveCoords(e);
      if (coords.x !== null) currentX = coords.x - startX;
      if (coords.y !== null) currentY = coords.y - startY;
      if (isVerticalScroll(e.pointerType, currentX, currentY)) {
        onPointerCancel();
        return;
      }
      card.style.transform = `translate(${currentX}px, 0) rotate(${currentX / 25}deg)`;
      const opacity = Math.min(1, Math.abs(currentX) / this.dragThreshold);
      if (like) like.style.opacity = String(currentX > 0 ? opacity : 0);
      if (dislike) dislike.style.opacity = String(currentX < 0 ? opacity : 0);
    };
    const onPointerUp = e => {
      if (!dragging || !matches(e)) return;
      const coords = resolveCoords(e);
      if (coords.x !== null) currentX = coords.x - startX;
      if (coords.y !== null) currentY = coords.y - startY;
      if (isVerticalScroll(e.pointerType, currentX, currentY)
          || (Math.abs(currentY) > Math.abs(currentX) && Math.abs(currentY) > 14)) {
        onPointerCancel();
        return;
      }
      suppressClick = Math.abs(currentX) > this.dragThreshold;
      finishDrag();
      if (currentX > this.dragThreshold) this.handleKeepAction();
      else if (currentX < -this.dragThreshold) this.handleDestructiveAction();
    };
    this.detachDrag = () => {
      onPointerCancel();
      card.removeEventListener('click', onClick, true);
      card.removeEventListener('pointerdown', onPointerDown);
      this.detachDrag = null;
    };
    card.addEventListener('click', onClick, true);
    card.addEventListener('pointerdown', onPointerDown);
  }
}

export const deckManager = new DeckReviewEngine();
