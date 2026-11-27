/**
 * Pruner Studio - Dedicated Dusty Gems Explorer View (`#view-dusty`)
 * Editorial "Library Time Archaeology" workspace: filter by listening memory kind
 * (all / forgotten / unplayed), source playlist, debounced keyword search,
 * balanced / oldest / most_played sorting, grid & list layouts, and deterministic pagination.
 */
import { api } from './api.js';
import { state } from './state.js';
import { player } from './player.js';
import { toast } from './toast.js';
import { openSongContextMenu, quickNextButton } from './context_menu.js';
import {
  createElement,
  createPlayingEqBars,
  debounce,
  formatArtists,
  formatDuration,
  getSizedCoverUrl,
  getTrackKey,
  syncPlayingRowIndicators,
} from './utils.js';
import { createIcon } from './icons.js';

export const DUSTY_PAGE_SIZE = 48;
const VIEW_MODE_STORAGE_KEY = 'pruner_dusty_view_mode';

const KIND_OPTIONS = [
  { id: 'all', label: '全部' },
  { id: 'forgotten', label: '曾经听过' },
  { id: 'unplayed', label: '从未听过' },
];

const SORT_OPTIONS = [
  { id: 'balanced', label: '均衡推荐' },
  { id: 'oldest', label: '沉睡最久' },
  { id: 'most_played', label: '曾经最爱' },
];

function readStoredViewMode() {
  try {
    if (typeof localStorage === 'undefined') return 'grid';
    const raw = localStorage.getItem(VIEW_MODE_STORAGE_KEY);
    return raw === 'list' ? 'list' : 'grid';
  } catch {
    return 'grid';
  }
}

function writeStoredViewMode(mode) {
  try {
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(VIEW_MODE_STORAGE_KEY, mode === 'list' ? 'list' : 'grid');
    }
  } catch {}
}

function generateSeed() {
  return Math.floor(Math.random() * 2147483646) + 1;
}

function formatNumber(n) {
  const num = Number(n);
  if (!Number.isFinite(num) || num < 0) return '0';
  return Math.floor(num).toLocaleString('en-US');
}

function normalizeKind(kind) {
  if (kind === 'forgotten' || kind === 'unplayed') return kind;
  return 'all';
}

function normalizeSort(sort) {
  if (sort === 'oldest' || sort === 'most_played') return sort;
  return 'balanced';
}

function getSongDustyKind(song) {
  if (song?.dusty_kind === 'forgotten' || song?.dusty_kind === 'unplayed') {
    return song.dusty_kind;
  }
  if (song?.days_since_played !== null && song?.days_since_played !== undefined) {
    return 'forgotten';
  }
  if ((Number(song?.play_count) || 0) > 0 || song?.last_played_at) {
    return 'forgotten';
  }
  return 'unplayed';
}

function getSourcePlaylistLabel(song) {
  const explicit = String(song?.source_playlist_name || '').trim();
  if (explicit) return explicit;
  const prov = String(song?.provider || '').trim().toLowerCase();
  if (prov === 'local' || song?.local_path) return '本地曲库';
  if (song?.is_cloud) return '网易云云盘';
  return '';
}

function getMemoryReason(song) {
  const raw = String(song?.recommend_reason || '').trim();
  if (raw) return raw;
  const kind = getSongDustyKind(song);
  const days = Number(song?.days_since_played);
  const plays = Number(song?.play_count) || 0;
  const playlist = getSourcePlaylistLabel(song);
  if (kind === 'forgotten') {
    if (song?.days_since_played !== null && song?.days_since_played !== undefined && Number.isFinite(days) && days >= 0) {
      return plays > 0 ? `${Math.floor(days)} 天未听 · 曾播 ${plays} 次` : `${Math.floor(days)} 天未听`;
    }
    return '超过 30 天未听';
  }
  return playlist ? `尚未播放 · 来自「${playlist}」` : '收藏后尚未播放';
}

export class DustyGemsView {
  constructor() {
    this.container = null;
    this.initialized = false;
    this.shellMounted = false;
    this.onAddToPlaylistCallback = null;

    // Query & filter state
    this.kind = 'all';
    this.playlist = '';
    this.query = '';
    this.sort = 'balanced';
    this.viewMode = readStoredViewMode();
    this.seed = null;

    // Data & pagination state
    this.songs = [];
    this.offset = 0;
    this.limit = DUSTY_PAGE_SIZE;
    this.totalCandidates = 0;
    this.filteredTotal = 0;
    this.remainingCount = 0;
    this.hasMore = false;
    this.exhausted = false;
    this.counts = { all: 0, forgotten: 0, unplayed: 0 };
    this.playlists = [];

    // Async lifecycle state
    this.hasLoaded = false;
    this.loading = false;
    this.loadingMore = false;
    this.errorMessage = '';
    this.loadMoreError = '';
    this.requestSequence = 0;

    // Persistent DOM refs
    this.refs = {};

    this._debouncedSearch = debounce((nextQuery) => {
      if (this.query === nextQuery) return;
      this.query = nextQuery;
      this.syncControlUI();
      this.load({ reset: true });
    }, 220);
  }

  init(options = {}) {
    if (options && typeof options === 'object' && typeof options.onAddToPlaylist === 'function') {
      this.onAddToPlaylistCallback = options.onAddToPlaylist;
    }
    if (typeof document !== 'undefined') {
      this.container = document.getElementById('view-dusty');
    }
    if (this.initialized) return;
    this.initialized = true;

    if (state && typeof state.on === 'function') {
      state.on('change:currentPlayingSong', () => this.syncPlayingState());
      state.on('change:isPlaying', () => this.syncPlayingState());
      state.on('change:likedSongIds', () => this.syncLikedState());
      state.on('change:likedTrackKeys', () => this.syncLikedState());
      state.on('auth:logout', () => this.reset());
    }
  }

  async open(options = {}) {
    if (typeof document !== 'undefined' && !this.container) {
      this.container = document.getElementById('view-dusty');
    }

    let filtersChanged = false;
    if (options && typeof options === 'object') {
      if (typeof options.onAddToPlaylist === 'function') {
        this.onAddToPlaylistCallback = options.onAddToPlaylist;
      }
      if (options.kind !== undefined) {
        const nextKind = normalizeKind(options.kind);
        if (nextKind !== this.kind) {
          this.kind = nextKind;
          filtersChanged = true;
        }
      }
      if (options.playlist !== undefined) {
        const nextPlaylist = String(options.playlist || '').trim();
        if (nextPlaylist !== this.playlist) {
          this.playlist = nextPlaylist;
          filtersChanged = true;
        }
      }
      if (options.q !== undefined) {
        const nextQuery = String(options.q || '').trim();
        if (nextQuery !== this.query) {
          this.query = nextQuery;
          filtersChanged = true;
        }
      }
      if (options.sort !== undefined) {
        const nextSort = normalizeSort(options.sort);
        if (nextSort !== this.sort) {
          this.sort = nextSort;
          filtersChanged = true;
        }
      }
    }

    if (this.sort === 'balanced' && this.seed === null) {
      this.seed = generateSeed();
    }

    this.ensureShell();
    this.syncHeaderUI();
    this.syncControlUI();

    if (!this.hasLoaded || filtersChanged || this.errorMessage || (this.songs.length === 0 && !this.hasActiveFilters())) {
      await this.load({ reset: true });
    } else {
      this.renderResults();
      this.renderFooter();
      this.syncPlayingState();
    }
  }

  async refresh() {
    if (this.sort === 'balanced') {
      this.seed = generateSeed();
    }
    this.ensureShell();
    await this.load({ reset: true });
  }

  reset() {
    this.requestSequence++;
    this.kind = 'all';
    this.playlist = '';
    this.query = '';
    this.sort = 'balanced';
    this.seed = null;
    this.songs = [];
    this.offset = 0;
    this.totalCandidates = 0;
    this.filteredTotal = 0;
    this.remainingCount = 0;
    this.hasMore = false;
    this.exhausted = false;
    this.counts = { all: 0, forgotten: 0, unplayed: 0 };
    this.playlists = [];
    this.hasLoaded = false;
    this.loading = false;
    this.loadingMore = false;
    this.errorMessage = '';
    this.loadMoreError = '';

    if (this.refs.searchInput) {
      this.refs.searchInput.value = '';
    }
    if (this.container) {
      this.container.textContent = '';
    }
    this.shellMounted = false;
    this.refs = {};
  }

  hasActiveFilters() {
    return this.kind !== 'all' || Boolean(this.playlist) || Boolean(this.query);
  }

  clearFilters() {
    const changed = this.hasActiveFilters();
    this.kind = 'all';
    this.playlist = '';
    this.query = '';
    if (this.refs.searchInput) {
      this.refs.searchInput.value = '';
    }
    this.syncControlUI();
    if (changed) {
      this.load({ reset: true });
    }
  }

  setKind(nextKind) {
    const resolved = normalizeKind(nextKind);
    if (resolved === this.kind) return;
    this.kind = resolved;
    this.syncControlUI();
    this.load({ reset: true });
  }

  setPlaylist(nextPlaylist) {
    const resolved = String(nextPlaylist || '').trim();
    if (resolved === this.playlist) return;
    this.playlist = resolved;
    this.syncControlUI();
    this.load({ reset: true });
  }

  setSort(nextSort) {
    const resolved = normalizeSort(nextSort);
    if (resolved === this.sort) return;
    this.sort = resolved;
    if (resolved === 'balanced' && this.seed === null) {
      this.seed = generateSeed();
    }
    this.syncHeaderUI();
    this.syncControlUI();
    this.load({ reset: true });
  }

  setViewMode(mode) {
    const resolved = mode === 'list' ? 'list' : 'grid';
    if (resolved === this.viewMode) return;
    this.viewMode = resolved;
    writeStoredViewMode(resolved);
    this.syncControlUI();
    this.renderResults();
    this.syncPlayingState();
  }

  async loadMore() {
    if (this.loading || this.loadingMore || !this.hasMore) return;
    await this.load({ reset: false });
  }

  async load({ reset = true } = {}) {
    if (!reset && (this.loading || this.loadingMore || !this.hasMore)) return;

    this.ensureShell();
    const seq = ++this.requestSequence;
    const ownerId = typeof state.getUserId === 'function' ? state.getUserId() : null;

    if (this.sort === 'balanced' && this.seed === null) {
      this.seed = generateSeed();
    }

    const requestOffset = reset ? 0 : this.offset;
    if (reset) {
      this.loading = true;
      this.loadingMore = false;
      this.errorMessage = '';
      this.loadMoreError = '';
    } else {
      this.loadingMore = true;
      this.loadMoreError = '';
    }

    this.syncHeaderUI();
    this.renderResults();
    this.renderFooter();

    const queryParams = {
      limit: this.limit,
      offset: requestOffset,
      kind: this.kind,
      playlist: this.playlist,
      q: this.query,
      sort: this.sort,
    };
    if (this.sort === 'balanced' && this.seed !== null) {
      queryParams.seed = this.seed;
    }

    let res;
    try {
      res = await api.getDustyGems(queryParams);
    } catch (err) {
      res = { ok: false, error: { message: err?.message || '网络请求失败' } };
    }

    const currentOwnerId = typeof state.getUserId === 'function' ? state.getUserId() : null;
    if (seq !== this.requestSequence || ownerId !== currentOwnerId) return;

    this.loading = false;
    this.loadingMore = false;

    if (!res || res.ok !== true) {
      const msg = res?.error?.message || '沉睡宝藏加载失败，请稍后重试。';
      if (reset) {
        this.errorMessage = msg;
      } else {
        this.loadMoreError = msg;
      }
      this.syncHeaderUI();
      this.syncControlUI();
      this.renderResults();
      this.renderFooter();
      return;
    }

    const data = res.data || {};
    const incomingSongs = Array.isArray(data.songs) ? data.songs : [];

    if (reset) {
      this.songs = this._dedupeSongs(incomingSongs);
      this.offset = incomingSongs.length;
    } else {
      this.songs = this._dedupeSongs([...this.songs, ...incomingSongs]);
      this.offset = requestOffset + incomingSongs.length;
    }

    const rawCounts = data.counts && typeof data.counts === 'object' ? data.counts : null;
    const fallbackTotal = Number.isFinite(Number(data.total_candidates))
      ? Number(data.total_candidates)
      : this.songs.length;

    if (rawCounts) {
      this.counts = {
        all: Math.max(0, Number(rawCounts.all) || 0),
        forgotten: Math.max(0, Number(rawCounts.forgotten) || 0),
        unplayed: Math.max(0, Number(rawCounts.unplayed) || 0),
      };
    } else if (reset && !this.hasActiveFilters()) {
      let forgotten = 0;
      let unplayed = 0;
      for (const song of this.songs) {
        if (getSongDustyKind(song) === 'forgotten') forgotten++;
        else unplayed++;
      }
      this.counts = {
        all: Math.max(fallbackTotal, this.songs.length),
        forgotten,
        unplayed,
      };
    }

    this.totalCandidates = Number.isFinite(Number(data.total_candidates))
      ? Math.max(0, Number(data.total_candidates))
      : this.counts.all;

    this.filteredTotal = Number.isFinite(Number(data.filtered_total))
      ? Math.max(0, Number(data.filtered_total))
      : (this.hasActiveFilters() ? Math.max(this.songs.length, Number(data.remaining_count || 0) + this.songs.length) : this.totalCandidates);

    this.remainingCount = Number.isFinite(Number(data.remaining_count))
      ? Math.max(0, Number(data.remaining_count))
      : Math.max(0, this.filteredTotal - this.offset);

    this.hasMore = typeof data.has_more === 'boolean'
      ? data.has_more
      : this.remainingCount > 0;

    this.exhausted = typeof data.exhausted === 'boolean'
      ? data.exhausted
      : !this.hasMore;

    if (Array.isArray(data.playlists)) {
      this.playlists = data.playlists
        .filter((item) => item && typeof item.name === 'string' && item.name.trim())
        .map((item) => ({ name: item.name.trim(), count: Math.max(0, Number(item.count) || 0) }));
    }

    this.hasLoaded = true;
    this.errorMessage = '';
    this.loadMoreError = '';

    this.syncHeaderUI();
    this.syncControlUI();
    this.renderResults();
    this.renderFooter();
    this.syncPlayingState();
  }

  _dedupeSongs(list) {
    const seen = new Set();
    const out = [];
    for (const song of list || []) {
      if (!song || typeof song !== 'object') continue;
      const key = getTrackKey(song);
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(song);
    }
    return out;
  }

  async markNotInterested(song) {
    if (!song || typeof song !== 'object') return;
    const key = getTrackKey(song);
    const kind = getSongDustyKind(song);
    const playlistName = getSourcePlaylistLabel(song);
    const prevLength = this.songs.length;

    this.songs = this.songs.filter((item) => getTrackKey(item) !== key);
    const removed = this.songs.length < prevLength;

    if (removed) {
      this.offset = Math.max(0, this.offset - 1);
      this.filteredTotal = Math.max(0, this.filteredTotal - 1);
      this.totalCandidates = Math.max(0, this.totalCandidates - 1);
      this.counts = {
        all: Math.max(0, (this.counts.all || 0) - 1),
        forgotten: kind === 'forgotten' ? Math.max(0, (this.counts.forgotten || 0) - 1) : (this.counts.forgotten || 0),
        unplayed: kind === 'unplayed' ? Math.max(0, (this.counts.unplayed || 0) - 1) : (this.counts.unplayed || 0),
      };
      if (playlistName && this.playlists.length > 0) {
        this.playlists = this.playlists
          .map((p) => (p.name === playlistName ? { ...p, count: Math.max(0, p.count - 1) } : p))
          .filter((p) => p.count > 0 || p.name === this.playlist);
      }

      // Keep Discover preview list synchronized as well
      const discoverDusty = state.get('dustyGems');
      if (Array.isArray(discoverDusty) && discoverDusty.length > 0) {
        state.set({ dustyGems: discoverDusty.filter((item) => getTrackKey(item) !== key) });
      }

      this.syncHeaderUI();
      this.syncControlUI();
      this.renderResults();
      this.renderFooter();
      this.syncPlayingState();
    }

    const scope = typeof state.writeScope === 'function' ? state.writeScope() : {};
    const songId = key.startsWith('netease:') ? Number(song.id) : 0;
    try {
      const result = await api.submitFeedback({
        ...scope,
        song_id: songId,
        track_key: key,
        action: 'not_interested',
        context: 'dusty_gems',
      });
      if (result && result.ok) {
        if (typeof toast.quiet === 'function') toast.quiet('已加入本地不想听列表');
        else toast.info('已移出沉睡宝藏', `"${song.title || '曲目'}" 已加入不想听列表。`);
      } else {
        toast.warning('本地屏蔽未保存', result?.error?.message || '请稍后重试。');
      }
    } catch (err) {
      toast.warning('本地屏蔽未保存', err?.message || '请稍后重试。');
    }
  }

  _dispatchBackToDiscover() {
    if (typeof window === 'undefined' || typeof window.dispatchEvent !== 'function') return;
    if (typeof CustomEvent === 'function') {
      window.dispatchEvent(new CustomEvent('switch-tab', { detail: 'discover' }));
    }
  }

  _playFiltered(mode = 'shuffle', startIndex = 0) {
    if (!this.songs.length) return;
    const idx = Math.max(0, Math.min(this.songs.length - 1, Number(startIndex) || 0));
    player.playQueue(this.songs, idx, mode, '沉睡宝藏');
  }

  _openContextMenu(event, song, idx) {
    openSongContextMenu(event, song, {
      source: 'discover',
      origin: '沉睡宝藏',
      queue: this.songs,
      index: idx,
      onAddToPlaylist: this.onAddToPlaylistCallback,
      onNotInterested: (target) => this.markNotInterested(target),
    });
  }

  ensureShell() {
    if (typeof document === 'undefined') return;
    if (!this.container) {
      this.container = document.getElementById('view-dusty');
    }
    if (!this.container) return;
    if (this.shellMounted && this.refs.root && this.refs.root.parentNode === this.container) {
      return;
    }

    this.container.textContent = '';

    // 1. Header Bar (flat editorial header, border-top: none on first child)
    const backBtn = createElement('button', {
      className: 'btn btn-ghost btn-sm dusty-back-btn',
      attributes: {
        type: 'button',
        id: 'dusty-back-btn',
        'aria-label': '返回发现',
      },
      events: { click: () => this._dispatchBackToDiscover() },
      children: [
        createElement('span', {
          className: 'icon-flip-x',
          attributes: { 'aria-hidden': 'true' },
          children: [createIcon('arrow-right', { size: 14 })],
        }),
        createElement('span', { className: 'btn-label', text: '返回发现' }),
      ],
    });

    const countBadge = createElement('span', {
      className: 'dusty-header-count numeral-mono',
      attributes: { id: 'dusty-total-count' },
      text: '',
    });

    const playShuffleBtn = createElement('button', {
      className: 'btn btn-primary btn-sm',
      attributes: { type: 'button', id: 'dusty-play-shuffle-btn' },
      events: { click: () => this._playFiltered('shuffle', 0) },
      children: [
        createIcon('shuffle', { size: 14 }),
        createElement('span', { className: 'btn-label', text: '均衡随机播' }),
      ],
    });

    const playOrderBtn = createElement('button', {
      className: 'btn btn-secondary btn-sm',
      attributes: { type: 'button', id: 'dusty-play-order-btn' },
      events: { click: () => this._playFiltered('order', 0) },
      children: [
        createIcon('play', { size: 14 }),
        createElement('span', { className: 'btn-label', text: '顺序播放' }),
      ],
    });

    const reshuffleDivider = createElement('span', {
      className: 'toolbar-divider',
      attributes: { 'aria-hidden': 'true' },
    });

    const reshuffleBtn = createElement('button', {
      className: 'btn btn-ghost btn-sm',
      attributes: {
        type: 'button',
        id: 'dusty-reshuffle-btn',
        title: '以全新随机种子重排均衡推荐序列',
      },
      events: { click: () => this.refresh() },
      children: [
        createIcon('refresh', { size: 14 }),
        createElement('span', { className: 'btn-label', text: '重新洗牌' }),
      ],
    });

    const headerBar = createElement('header', {
      className: 'dusty-view-header',
      attributes: { id: 'dusty-view-header' },
      children: [
        createElement('div', {
          className: 'dusty-header-nav-row',
          children: [backBtn],
        }),
        createElement('div', {
          className: 'dusty-header-main',
          children: [
            createElement('div', {
              className: 'dusty-header-heading',
              children: [
                createElement('div', {
                  className: 'dusty-title-row',
                  children: [
                    createIcon('gem', { size: 20, className: 'dusty-title-icon' }),
                    createElement('h1', { className: 'dusty-view-title', text: '沉睡宝藏' }),
                    countBadge,
                  ],
                }),
                createElement('p', {
                  className: 'dusty-view-subtitle',
                  text: '曲库里被时间轻轻放下的好歌 · 随时一键唤醒',
                }),
              ],
            }),
            createElement('div', {
              className: 'dusty-header-actions',
              children: [playShuffleBtn, playOrderBtn, reshuffleDivider, reshuffleBtn],
            }),
          ],
        }),
      ],
    });

    // 2. Recessed Filter & Control Strip
    const kindButtons = {};
    const kindSegment = createElement('div', {
      className: 'segmented-control dusty-kind-segment',
      attributes: { role: 'group', 'aria-label': '沉睡曲目分类', id: 'dusty-kind-segment' },
      children: KIND_OPTIONS.map((opt) => {
        const countSpan = createElement('span', {
          className: 'dusty-chip-count numeral-mono',
          text: '(0)',
        });
        const btn = createElement('button', {
          className: 'segmented-btn dusty-kind-btn',
          dataset: { dustyKind: opt.id },
          attributes: {
            type: 'button',
            'aria-pressed': opt.id === this.kind ? 'true' : 'false',
          },
          events: { click: () => this.setKind(opt.id) },
          children: [
            createElement('span', { text: opt.label }),
            countSpan,
          ],
        });
        kindButtons[opt.id] = { btn, countSpan };
        return btn;
      }),
    });

    const playlistSelect = createElement('select', {
      className: 'input-select dusty-playlist-select',
      attributes: {
        id: 'dusty-playlist-select',
        'aria-label': '按来源歌单筛选',
      },
      events: {
        change: (e) => this.setPlaylist(e.target.value),
      },
      children: [
        createElement('option', { attributes: { value: '' }, text: '全部歌单' }),
      ],
    });

    const searchInput = createElement('input', {
      className: 'input-text dusty-search-input',
      attributes: {
        type: 'search',
        id: 'dusty-search-input',
        placeholder: '搜索歌名、歌手、专辑或歌单...',
        'aria-label': '搜索沉睡曲目',
        autocomplete: 'off',
        spellcheck: 'false',
      },
      events: {
        input: (e) => {
          const val = String(e.target.value || '').trim();
          this._updateClearBtnVisibility(e.target.value);
          this._debouncedSearch(val);
        },
        keydown: (e) => {
          if (e.key === 'Escape' && searchInput.value) {
            e.preventDefault();
            searchInput.value = '';
            this._updateClearBtnVisibility('');
            if (this.query !== '') {
              this.query = '';
              this.syncControlUI();
              this.load({ reset: true });
            }
          } else if (e.key === 'Enter') {
            e.preventDefault();
            const val = String(searchInput.value || '').trim();
            if (val !== this.query) {
              this.query = val;
              this.syncControlUI();
              this.load({ reset: true });
            }
          }
        },
      },
    });

    const searchClearBtn = createElement('button', {
      className: 'dusty-search-clear hidden',
      attributes: {
        type: 'button',
        id: 'dusty-search-clear-btn',
        title: '清除搜索词',
        'aria-label': '清除搜索词',
      },
      events: {
        click: () => {
          searchInput.value = '';
          this._updateClearBtnVisibility('');
          searchInput.focus?.();
          if (this.query !== '') {
            this.query = '';
            this.syncControlUI();
            this.load({ reset: true });
          }
        },
      },
      children: [createIcon('close', { size: 13 })],
    });

    const searchBox = createElement('div', {
      className: 'dusty-search-box',
      children: [
        createIcon('search', { size: 14, className: 'dusty-search-icon' }),
        searchInput,
        searchClearBtn,
      ],
    });

    const sortButtons = {};
    const sortGroup = createElement('div', {
      className: 'dusty-sort-group',
      attributes: { role: 'group', 'aria-label': '排序方式', id: 'dusty-sort-group' },
      children: [
        createElement('span', { className: 'dusty-control-label', text: '排序' }),
        ...SORT_OPTIONS.map((opt) => {
          const btn = createElement('button', {
            className: 'filter-chip dusty-sort-pill',
            dataset: { dustySort: opt.id },
            attributes: {
              type: 'button',
              'aria-pressed': opt.id === this.sort ? 'true' : 'false',
            },
            text: opt.label,
            events: { click: () => this.setSort(opt.id) },
          });
          sortButtons[opt.id] = btn;
          return btn;
        }),
      ],
    });

    const clearFiltersBtn = createElement('button', {
      className: 'btn btn-ghost btn-sm dusty-clear-filters-btn hidden',
      attributes: { type: 'button', id: 'dusty-clear-filters-btn' },
      events: { click: () => this.clearFilters() },
      children: [
        createIcon('close', { size: 13 }),
        createElement('span', { text: '清除筛选' }),
      ],
    });

    const filteredSummary = createElement('span', {
      className: 'dusty-filter-summary numeral-mono',
      attributes: { id: 'dusty-filter-summary' },
      text: '',
    });

    const viewGridBtn = createElement('button', {
      className: 'view-switch-btn dusty-view-btn',
      dataset: { dustyView: 'grid' },
      attributes: {
        type: 'button',
        id: 'dusty-view-grid-btn',
        'aria-pressed': this.viewMode === 'grid' ? 'true' : 'false',
      },
      events: { click: () => this.setViewMode('grid') },
      children: [
        createIcon('grid', { size: 13 }),
        createElement('span', { text: '网格' }),
      ],
    });

    const viewListBtn = createElement('button', {
      className: 'view-switch-btn dusty-view-btn',
      dataset: { dustyView: 'list' },
      attributes: {
        type: 'button',
        id: 'dusty-view-list-btn',
        'aria-pressed': this.viewMode === 'list' ? 'true' : 'false',
      },
      events: { click: () => this.setViewMode('list') },
      children: [
        createIcon('menu', { size: 13 }),
        createElement('span', { text: '列表' }),
      ],
    });

    const viewToggle = createElement('div', {
      className: 'view-switcher-group dusty-view-toggle',
      attributes: { role: 'group', 'aria-label': '视图模式' },
      children: [viewGridBtn, viewListBtn],
    });

    const controlStrip = createElement('section', {
      className: 'dusty-control-strip',
      attributes: { id: 'dusty-control-strip', 'aria-label': '筛选与排序' },
      children: [
        createElement('div', {
          className: 'dusty-control-row-top',
          children: [
            kindSegment,
            createElement('div', {
              className: 'dusty-filter-inputs',
              children: [playlistSelect, searchBox],
            }),
          ],
        }),
        createElement('div', {
          className: 'dusty-control-row-bottom',
          children: [
            createElement('div', {
              className: 'dusty-control-bottom-left',
              children: [sortGroup, clearFiltersBtn],
            }),
            createElement('div', {
              className: 'dusty-control-bottom-right',
              children: [filteredSummary, viewToggle],
            }),
          ],
        }),
      ],
    });

    // 3. Results Container & Pagination Footer
    const resultsRegion = createElement('div', {
      className: 'dusty-results-region',
      attributes: { id: 'dusty-results-region' },
    });

    const paginationFooter = createElement('footer', {
      className: 'dusty-pagination-footer hidden',
      attributes: { id: 'dusty-pagination-footer' },
    });

    const root = createElement('div', {
      className: 'dusty-view-container',
      children: [headerBar, controlStrip, resultsRegion, paginationFooter],
    });

    this.container.appendChild(root);
    this.shellMounted = true;
    this.refs = {
      root,
      countBadge,
      playShuffleBtn,
      playOrderBtn,
      reshuffleDivider,
      reshuffleBtn,
      kindButtons,
      playlistSelect,
      searchInput,
      searchClearBtn,
      sortButtons,
      clearFiltersBtn,
      filteredSummary,
      viewGridBtn,
      viewListBtn,
      resultsRegion,
      paginationFooter,
    };
  }

  _updateClearBtnVisibility(rawValue) {
    const clearBtn = this.refs.searchClearBtn;
    if (!clearBtn) return;
    clearBtn.classList.toggle('hidden', !String(rawValue || '').length);
  }

  syncHeaderUI() {
    if (!this.shellMounted) return;
    const { countBadge, playShuffleBtn, playOrderBtn, reshuffleDivider, reshuffleBtn } = this.refs;

    if (countBadge) {
      if (this.hasLoaded) {
        const total = this.counts.all || this.totalCandidates;
        countBadge.textContent = this.hasActiveFilters()
          ? `筛选 ${formatNumber(this.filteredTotal)} / 共 ${formatNumber(total)} 首`
          : `共 ${formatNumber(total)} 首`;
        countBadge.classList.remove('hidden');
      } else if (this.loading) {
        countBadge.textContent = '正在检索...';
        countBadge.classList.remove('hidden');
      } else {
        countBadge.textContent = '';
        countBadge.classList.add('hidden');
      }
    }

    const canPlay = this.songs.length > 0 && !this.loading;
    if (playShuffleBtn) playShuffleBtn.disabled = !canPlay;
    if (playOrderBtn) playOrderBtn.disabled = !canPlay;

    const showReshuffle = this.sort === 'balanced';
    if (reshuffleDivider) reshuffleDivider.classList.toggle('hidden', !showReshuffle);
    if (reshuffleBtn) {
      reshuffleBtn.classList.toggle('hidden', !showReshuffle);
      reshuffleBtn.disabled = this.loading || this.loadingMore;
    }
  }

  syncControlUI() {
    if (!this.shellMounted) return;
    const {
      kindButtons,
      playlistSelect,
      searchInput,
      sortButtons,
      clearFiltersBtn,
      filteredSummary,
      viewGridBtn,
      viewListBtn,
    } = this.refs;

    // Kind segmented chips
    for (const opt of KIND_OPTIONS) {
      const entry = kindButtons[opt.id];
      if (!entry) continue;
      const active = this.kind === opt.id;
      entry.btn.classList.toggle('active', active);
      entry.btn.setAttribute('aria-pressed', active ? 'true' : 'false');
      const countVal = this.counts[opt.id] ?? 0;
      entry.countSpan.textContent = `(${formatNumber(countVal)})`;
    }

    // Playlist select options (rebuild only when playlist list or selected value changes)
    if (playlistSelect) {
      const sig = `${this.playlist}::${this.playlists.map((p) => `${p.name}:${p.count}`).join('|')}`;
      if (playlistSelect.dataset.optionsSig !== sig) {
        playlistSelect.textContent = '';
        playlistSelect.appendChild(createElement('option', {
          attributes: { value: '' },
          text: '全部歌单',
        }));
        let hasSelected = this.playlist === '';
        for (const pl of this.playlists) {
          if (pl.name === this.playlist) hasSelected = true;
          playlistSelect.appendChild(createElement('option', {
            attributes: { value: pl.name },
            text: `${pl.name} (${formatNumber(pl.count)})`,
          }));
        }
        if (this.playlist && !hasSelected) {
          playlistSelect.appendChild(createElement('option', {
            attributes: { value: this.playlist },
            text: this.playlist,
          }));
        }
        playlistSelect.dataset.optionsSig = sig;
      }
      if (playlistSelect.value !== this.playlist) {
        playlistSelect.value = this.playlist;
      }
    }

    // Search input sync (never overwrite while user is actively typing inside it)
    if (searchInput && document.activeElement !== searchInput && searchInput.value !== this.query) {
      searchInput.value = this.query;
    }
    this._updateClearBtnVisibility(searchInput ? searchInput.value : this.query);

    // Sort pills
    for (const opt of SORT_OPTIONS) {
      const btn = sortButtons[opt.id];
      if (!btn) continue;
      const active = this.sort === opt.id;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    }

    // Clear filters button
    if (clearFiltersBtn) {
      clearFiltersBtn.classList.toggle('hidden', !this.hasActiveFilters());
    }

    // Live count summary on strip
    if (filteredSummary) {
      if (this.loading && !this.songs.length) {
        filteredSummary.textContent = '加载中...';
      } else if (this.hasLoaded) {
        filteredSummary.textContent = this.songs.length > 0
          ? `已展示 ${formatNumber(this.songs.length)} / ${formatNumber(this.filteredTotal)} 首`
          : `匹配 ${formatNumber(this.filteredTotal)} 首`;
      } else {
        filteredSummary.textContent = '';
      }
    }

    // View mode toggle
    if (viewGridBtn) {
      const isGrid = this.viewMode === 'grid';
      viewGridBtn.classList.toggle('active', isGrid);
      viewGridBtn.setAttribute('aria-pressed', isGrid ? 'true' : 'false');
    }
    if (viewListBtn) {
      const isList = this.viewMode === 'list';
      viewListBtn.classList.toggle('active', isList);
      viewListBtn.setAttribute('aria-pressed', isList ? 'true' : 'false');
    }
  }

  renderResults() {
    if (!this.shellMounted || !this.refs.resultsRegion) return;
    const region = this.refs.resultsRegion;
    region.textContent = '';

    // Initial loading skeleton
    if (this.loading) {
      region.appendChild(this._buildLoadingState());
      return;
    }

    // Error state (page 1 failed)
    if (this.errorMessage && this.songs.length === 0) {
      region.appendChild(createElement('div', {
        className: 'empty-state dusty-empty-state',
        attributes: { role: 'alert' },
        children: [
          createIcon('alert-circle', { size: 28, className: 'dusty-empty-icon is-warning' }),
          createElement('div', { className: 'empty-state-title', text: '沉睡宝藏加载失败' }),
          createElement('div', { className: 'empty-state-desc', text: this.errorMessage }),
          createElement('button', {
            className: 'btn btn-secondary btn-sm',
            text: '重试',
            attributes: { type: 'button' },
            events: { click: () => this.load({ reset: true }) },
          }),
        ],
      }));
      return;
    }

    // Empty states
    if (this.songs.length === 0) {
      if (this.hasActiveFilters()) {
        const activeHints = [];
        if (this.kind === 'forgotten') activeHints.push('分类：曾经听过');
        else if (this.kind === 'unplayed') activeHints.push('分类：从未听过');
        if (this.playlist) activeHints.push(`歌单：${this.playlist}`);
        if (this.query) activeHints.push(`关键词：「${this.query}」`);

        region.appendChild(createElement('div', {
          className: 'empty-state dusty-empty-state',
          attributes: { role: 'status' },
          children: [
            createIcon('search', { size: 28, className: 'dusty-empty-icon' }),
            createElement('div', { className: 'empty-state-title', text: '没有找到符合筛选条件的沉睡曲目' }),
            createElement('div', {
              className: 'empty-state-desc',
              text: `${activeHints.join(' · ')} 暂无匹配结果，可尝试更换关键词或清除筛选。`,
            }),
            createElement('button', {
              className: 'btn btn-secondary btn-sm',
              text: '清除筛选',
              attributes: { type: 'button', id: 'dusty-empty-clear-btn' },
              events: { click: () => this.clearFilters() },
            }),
          ],
        }));
      } else {
        region.appendChild(createElement('div', {
          className: 'empty-state dusty-empty-state',
          attributes: { role: 'status' },
          children: [
            createIcon('gem', { size: 28, className: 'dusty-empty-icon' }),
            createElement('div', { className: 'empty-state-title', text: '曲库里还没有沉睡曲目' }),
            createElement('div', {
              className: 'empty-state-desc',
              text: '同步歌单并听歌一段时间后，超过 30 天未听或收藏后尚未播放的好歌会自动沉淀到这里，等待被再次唤醒。',
            }),
            createElement('div', {
              className: 'dusty-empty-actions',
              children: [
                createElement('button', {
                  className: 'btn btn-secondary btn-sm',
                  text: '返回发现',
                  attributes: { type: 'button' },
                  events: { click: () => this._dispatchBackToDiscover() },
                }),
                createElement('button', {
                  className: 'btn btn-ghost btn-sm',
                  text: '重新检查',
                  attributes: { type: 'button' },
                  events: { click: () => this.refresh() },
                }),
              ],
            }),
          ],
        }));
      }
      return;
    }

    if (this.viewMode === 'list') {
      region.appendChild(this._buildListView());
    } else {
      region.appendChild(this._buildGridView());
    }
  }

  _buildLoadingState() {
    const skeletonCount = this.viewMode === 'list' ? 8 : 8;
    const statusLine = createElement('div', {
      className: 'dusty-loading-banner',
      attributes: { role: 'status' },
      children: [
        createElement('span', { className: 'spinner' }),
        createElement('span', { text: '正在翻阅曲库时光档案...' }),
      ],
    });

    if (this.viewMode === 'list') {
      const rows = Array.from({ length: skeletonCount }, () => createElement('div', {
        className: 'dusty-skeleton-row',
        children: [
          createElement('div', { className: 'dusty-skeleton-thumb-sm' }),
          createElement('div', {
            className: 'dusty-skeleton-lines',
            children: [
              createElement('div', { className: 'dusty-skeleton-line is-title' }),
              createElement('div', { className: 'dusty-skeleton-line is-sub' }),
            ],
          }),
          createElement('div', { className: 'dusty-skeleton-pill' }),
        ],
      }));
      return createElement('div', {
        className: 'dusty-loading-wrap',
        children: [statusLine, createElement('div', { className: 'dusty-skeleton-list' }, rows)],
      });
    }

    const cards = Array.from({ length: skeletonCount }, () => createElement('div', {
      className: 'dusty-skeleton-card',
      children: [
        createElement('div', {
          className: 'dusty-skeleton-card-top',
          children: [
            createElement('div', { className: 'dusty-skeleton-thumb' }),
            createElement('div', {
              className: 'dusty-skeleton-lines',
              children: [
                createElement('div', { className: 'dusty-skeleton-line is-title' }),
                createElement('div', { className: 'dusty-skeleton-line is-sub' }),
              ],
            }),
          ],
        }),
        createElement('div', { className: 'dusty-skeleton-pill' }),
      ],
    }));

    return createElement('div', {
      className: 'dusty-loading-wrap',
      children: [statusLine, createElement('div', { className: 'dusty-grid dusty-skeleton-grid' }, cards)],
    });
  }

  _buildGridView() {
    const grid = createElement('div', {
      className: 'dusty-grid',
      attributes: { role: 'list', id: 'dusty-results-grid' },
    });

    this.songs.forEach((song, idx) => {
      const trackKey = getTrackKey(song);
      const kind = getSongDustyKind(song);
      const reason = getMemoryReason(song);
      const playlistLabel = getSourcePlaylistLabel(song);
      const artistText = formatArtists(song.artists);
      const durationText = formatDuration(song);
      const showSeparatePlaylistTag = Boolean(playlistLabel) && !reason.includes(playlistLabel);

      const coverWrap = createElement('div', {
        className: 'dusty-card-cover-wrap',
        children: [
          createElement('img', {
            className: 'dusty-card-cover',
            attributes: {
              src: getSizedCoverUrl(song.cover_url, 96, '藏'),
              alt: song.title || '封面',
              loading: 'lazy',
            },
          }),
          createElement('span', {
            className: 'dusty-card-play-badge',
            attributes: { 'aria-hidden': 'true' },
            children: [createIcon('play', { size: 14 })],
          }),
          createElement('span', {
            className: 'dusty-card-eq-badge',
            attributes: { 'aria-hidden': 'true' },
            children: [createPlayingEqBars()],
          }),
        ],
      });

      const thirdLine = showSeparatePlaylistTag
        ? createElement('div', {
            className: 'dusty-card-source',
            attributes: {
              title: song.album ? `来自「${playlistLabel}」 · ${song.album}` : `来自「${playlistLabel}」`,
            },
            children: [
              createIcon('folder', { size: 12, className: 'dusty-card-source-icon' }),
              createElement('span', { className: 'dusty-card-source-text', text: playlistLabel }),
            ],
          })
        : (song.album
            ? createElement('div', {
                className: 'dusty-card-album',
                text: song.album,
                attributes: { title: song.album },
              })
            : createElement('div', {
                className: 'dusty-card-album numeral-mono',
                text: durationText !== '0:00' ? durationText : '',
              }));

      const metaBlock = createElement('div', {
        className: 'dusty-card-meta',
        children: [
          createElement('div', {
            className: 'dusty-card-title',
            text: song.title || '未知曲目',
            attributes: { title: song.title || '未知曲目' },
          }),
          createElement('div', {
            className: 'dusty-card-artist',
            text: artistText,
            attributes: { title: song.album ? `${artistText} · ${song.album}` : artistText },
          }),
          thirdLine,
        ],
      });

      const memoryPill = createElement('span', {
        className: `dusty-memory-pill ${kind === 'forgotten' ? 'is-forgotten' : 'is-unplayed'}`,
        attributes: { title: reason },
        children: [
          createIcon(kind === 'forgotten' ? 'clock' : 'disc', { size: 12 }),
          createElement('span', { className: 'dusty-memory-pill-text', text: reason }),
        ],
      });

      const footerChildren = [memoryPill];

      const hoverActions = createElement('div', {
        className: 'dusty-card-hover-actions',
        children: [quickNextButton(song)],
      });

      const card = createElement('article', {
        className: 'dusty-card',
        dataset: { trackKey, dustyKind: kind },
        attributes: {
          role: 'listitem',
          tabindex: '0',
          'aria-label': `播放沉睡宝藏 ${song.title || '曲目'} - ${artistText}`,
        },
        events: {
          click: () => this._playFiltered('order', idx),
          keydown: (e) => {
            if ((e.key === 'Enter' || e.key === ' ') && e.target === e.currentTarget) {
              e.preventDefault();
              this._playFiltered('order', idx);
            }
          },
          contextmenu: (e) => this._openContextMenu(e, song, idx),
        },
        children: [
          createElement('div', {
            className: 'dusty-card-top',
            children: [coverWrap, metaBlock],
          }),
          createElement('div', {
            className: 'dusty-card-footer',
            children: footerChildren,
          }),
          hoverActions,
        ],
      });

      grid.appendChild(card);
    });

    return grid;
  }

  _buildListView() {
    const header = createElement('div', {
      className: 'dusty-list-header',
      attributes: { 'aria-hidden': 'true' },
      children: [
        createElement('span', { className: 'dusty-col-index', text: '#' }),
        createElement('span', { className: 'dusty-col-track', text: '曲目' }),
        createElement('span', { className: 'dusty-col-playlist', text: '来源歌单' }),
        createElement('span', { className: 'dusty-col-memory', text: '时光印记' }),
        createElement('span', { className: 'dusty-col-duration', text: '时长' }),
        createElement('span', { className: 'dusty-col-actions', text: '' }),
      ],
    });

    const listEl = createElement('div', {
      className: 'dusty-list-body',
      attributes: { role: 'list', id: 'dusty-results-list' },
    });

    this.songs.forEach((song, idx) => {
      const trackKey = getTrackKey(song);
      const kind = getSongDustyKind(song);
      const reason = getMemoryReason(song);
      const playlistLabel = getSourcePlaylistLabel(song) || '个人曲库';
      const artistText = formatArtists(song.artists);
      const durationText = formatDuration(song);

      const nextBtn = createElement('button', {
        className: 'table-btn dusty-row-action-btn',
        attributes: {
          type: 'button',
          title: '下一首播放',
          'aria-label': `下一首：${song.title || '曲目'}`,
        },
        events: {
          click: (e) => {
            e.stopPropagation();
            player.enqueueNext(song);
          },
        },
        children: [createIcon('skip-next', { size: 14 })],
      });

      const notInterestedBtn = createElement('button', {
        className: 'table-btn dusty-row-action-btn is-danger',
        attributes: {
          type: 'button',
          title: '不想听',
          'aria-label': `不想听：${song.title || '曲目'}`,
        },
        events: {
          click: (e) => {
            e.stopPropagation();
            this.markNotInterested(song);
          },
        },
        children: [createIcon('ban', { size: 14 })],
      });

      const row = createElement('div', {
        className: 'dusty-list-row',
        dataset: { trackKey, dustyKind: kind },
        attributes: {
          role: 'listitem',
          tabindex: '0',
          'aria-label': `播放 ${song.title || '曲目'} - ${artistText}`,
        },
        events: {
          click: () => this._playFiltered('order', idx),
          keydown: (e) => {
            if ((e.key === 'Enter' || e.key === ' ') && e.target === e.currentTarget) {
              e.preventDefault();
              this._playFiltered('order', idx);
            }
          },
          contextmenu: (e) => this._openContextMenu(e, song, idx),
        },
        children: [
          createElement('span', {
            className: 'dusty-row-index numeral-mono',
            children: [
              createElement('span', { className: 'dusty-index-num', text: String(idx + 1).padStart(2, '0') }),
              createPlayingEqBars(),
            ],
          }),
          createElement('div', {
            className: 'dusty-row-track',
            children: [
              createElement('img', {
                className: 'dusty-row-cover',
                attributes: {
                  src: getSizedCoverUrl(song.cover_url, 64, '藏'),
                  alt: song.title || '封面',
                  loading: 'lazy',
                },
              }),
              createElement('div', {
                className: 'dusty-row-track-text',
                children: [
                  createElement('span', {
                    className: 'dusty-row-title',
                    text: song.title || '未知曲目',
                    attributes: { title: song.title || '未知曲目' },
                  }),
                  createElement('span', {
                    className: 'dusty-row-artist',
                    text: song.album ? `${artistText} · ${song.album}` : artistText,
                    attributes: { title: song.album ? `${artistText} · ${song.album}` : artistText },
                  }),
                ],
              }),
            ],
          }),
          createElement('div', {
            className: 'dusty-row-playlist',
            children: [
              createElement('span', {
                className: 'dusty-source-tag',
                text: playlistLabel,
                attributes: { title: playlistLabel },
              }),
            ],
          }),
          createElement('div', {
            className: 'dusty-row-memory',
            children: [
              createElement('span', {
                className: `dusty-memory-pill ${kind === 'forgotten' ? 'is-forgotten' : 'is-unplayed'}`,
                attributes: { title: reason },
                children: [
                  createIcon(kind === 'forgotten' ? 'clock' : 'disc', { size: 12 }),
                  createElement('span', { className: 'dusty-memory-pill-text', text: reason }),
                ],
              }),
            ],
          }),
          createElement('span', {
            className: 'dusty-row-duration numeral-mono',
            text: durationText,
          }),
          createElement('div', {
            className: 'dusty-row-actions',
            children: [nextBtn, notInterestedBtn],
          }),
        ],
      });

      listEl.appendChild(row);
    });

    return createElement('div', {
      className: 'dusty-list-shell',
      children: [header, listEl],
    });
  }

  renderFooter() {
    if (!this.shellMounted || !this.refs.paginationFooter) return;
    const footer = this.refs.paginationFooter;
    footer.textContent = '';

    if (this.loading || this.songs.length === 0) {
      footer.classList.add('hidden');
      return;
    }

    footer.classList.remove('hidden');

    const totalDisplay = Math.max(this.filteredTotal, this.songs.length);
    const statusText = createElement('span', {
      className: 'dusty-pagination-status numeral-mono',
      text: `已展示 ${formatNumber(this.songs.length)} / ${formatNumber(totalDisplay)} 首沉睡曲目`,
    });

    const rightControls = createElement('div', { className: 'dusty-pagination-actions' });

    if (this.loadMoreError) {
      rightControls.appendChild(createElement('span', {
        className: 'dusty-load-more-error',
        attributes: { role: 'alert' },
        text: this.loadMoreError,
      }));
    }

    if (this.hasMore || this.loadingMore) {
      const loadMoreBtn = createElement('button', {
        className: 'btn btn-secondary btn-sm',
        attributes: {
          type: 'button',
          id: 'dusty-load-more-btn',
          disabled: this.loadingMore ? 'disabled' : null,
        },
        events: { click: () => this.loadMore() },
        children: this.loadingMore
          ? [
              createElement('span', { className: 'spinner dusty-btn-spinner' }),
              createElement('span', { text: '正在加载更多...' }),
            ]
          : [
              createElement('span', {
                text: this.remainingCount > 0
                  ? `加载更多（剩余 ${formatNumber(this.remainingCount)} 首）`
                  : '加载更多',
              }),
            ],
      });
      rightControls.appendChild(loadMoreBtn);
    } else {
      rightControls.appendChild(createElement('span', {
        className: 'dusty-pagination-done',
        text: '已展示全部符合条件的曲目',
      }));
    }

    footer.appendChild(statusText);
    footer.appendChild(rightControls);
  }

  syncPlayingState() {
    if (!this.shellMounted || !this.refs.resultsRegion) return;
    syncPlayingRowIndicators(
      state.get('currentPlayingSong'),
      Boolean(state.get('isPlaying')),
      this.refs.resultsRegion,
    );
  }

  syncLikedState() {
    // Context menu reads live liked state dynamically; no disruptive full re-render needed.
    this.syncPlayingState();
  }
}

export const dustyView = new DustyGemsView();
export default dustyView;
