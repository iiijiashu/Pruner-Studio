import { getTrackKey } from './utils.js';
/**
 * Pruner Studio - Global Ctrl+K Command & Online-First Multi-Channel Search Palette
 * Opens on 'all' (全渠道聚合) by default and remembers last channel in localStorage (`pruner_search_channel`).
 * When query is non-empty, displays a compact `曲库中` strip (max 5 local matches) ABOVE online results
 * from `api.searchMulti(q, channels, 30, 0)`, plus a footer action to open the full `#view-search` page.
 */
import { api } from './api.js';
import { state } from './state.js';
import { player } from './player.js';
import { modal } from './modal.js';
import { openSongContextMenu } from './context_menu.js';
import { createElement, formatArtists, formatDuration, getSizedCoverUrl, debounce } from './utils.js';
import { createIcon } from './icons.js';

export const SEARCH_CHANNEL_STORAGE_KEY = 'pruner_search_channel';

export const SEARCH_CHANNEL_CHIPS = [
  { id: 'all', label: '全渠道聚合', domId: 'search-palette-tab-online' },
  { id: 'local', label: '本地', domId: 'search-palette-tab-local' },
  { id: 'netease', label: '网易云' },
  { id: 'kugou_lite', label: '酷狗概念版' },
  { id: 'migu', label: '咪咕' },
  { id: 'qq', label: 'QQ音乐' },
  { id: 'kuwo', label: '酷我' },
  { id: 'bilibili', label: 'B站' },
];

export const CHANNEL_BADGE_LABELS = {
  netease: '网易云',
  kugou_lite: '酷狗概念版',
  kugou: '酷狗',
  migu: '咪咕',
  qq: 'QQ音乐',
  kuwo: '酷我',
  bilibili: 'B站',
  gdstudio: 'GDStudio',
  custom: '自定义',
  local: '本地',
  cloud: '云盘',
};

function readStoredSearchChannel() {
  try {
    if (typeof localStorage === 'undefined') return 'all';
    const raw = localStorage.getItem(SEARCH_CHANNEL_STORAGE_KEY);
    if (raw && SEARCH_CHANNEL_CHIPS.some(c => c.id === raw)) return raw;
  } catch {}
  return 'all';
}

function writeStoredSearchChannel(channelId) {
  try {
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(SEARCH_CHANNEL_STORAGE_KEY, String(channelId));
    }
  } catch {}
}

export function normalizeMultiTrack(item) {
  if (!item || typeof item !== 'object') return null;
  const provider = String(item.provider || item.source_provider || item.channel || '').toLowerCase();
  const numId = Number(item.song_id ?? item.id);
  const hasNeteaseId = Number.isInteger(numId) && numId > 0 && (provider === '' || provider === 'netease' || item.netease_id);
  const resolvedId = hasNeteaseId
    ? (Number(item.netease_id) || numId)
    : (item.id || `${provider || 'ext'}:${item.external_id || item.title || 'track'}`);
  const artists = Array.isArray(item.artists)
    ? item.artists
    : (item.artist ? [item.artist] : ['未知歌手']);
  const available = Array.isArray(item.available_channels) && item.available_channels.length > 0
    ? item.available_channels
    : (provider ? [provider] : []);

  const song = {
    ...item,
    id: hasNeteaseId ? resolvedId : 0,
    external_id: item.external_id || (!hasNeteaseId ? String(item.id || '') : ''),
    provider: provider || (hasNeteaseId ? 'netease' : ''),
    title: item.title || '未知曲目',
    artists,
    album: item.album || '',
    duration: item.duration ?? item.duration_ms ?? 0,
    cover_url: item.cover_url || '',
    available_channels: available,
    stream_url_hint: item.stream_url_hint || item.url || '',
  };
  return { ...song, track_key: getTrackKey(song) };
}

class SearchPaletteManager {
  constructor() {
    this.activeChannel = readStoredSearchChannel();
    this.mode = this.activeChannel === 'local' ? 'local' : 'online';
    this.query = '';
    this.localResults = [];
    this.results = [];
    this.channelErrors = {};
    this.activeIndex = -1;
    this.onlineLoading = false;
    this.onlineSequence = 0;
    this.onAddToPlaylistCallback = null;
    this.debouncedOnlineSearch = debounce((q) => { if (q === this.query && this.activeChannel !== 'local') this.runOnlineSearch(q); }, 260);
  }

  init({ onAddToPlaylist } = {}) {
    if (typeof document === 'undefined') return;
    this.onAddToPlaylistCallback = onAddToPlaylist || null;

    const triggerBtn = document.getElementById('header-search-trigger');
    const input = document.getElementById('search-palette-input');

    if (triggerBtn) {
      triggerBtn.addEventListener('click', () => this.open());
    }

    document.getElementById('search-palette-clear-btn')?.addEventListener('click', () => this.setQuery(''));
    const resetForAccount = () => {
      this.onlineSequence++;
      this.query = '';
      this.activeIndex = -1;
      this.results = [];
      this.localResults = [];
      this.channelErrors = {};
      this.onlineLoading = false;
      if (input) input.value = '';
      if (globalThis.document?.getElementById('search-palette-modal')?.classList.contains('open')) this.renderResults();
    };
    state.on('change:user', resetForAccount);
    state.on('change:activeOfflineAccountId', resetForAccount);
    state.on('webdav:scanned', () => {
      const open = document.getElementById('search-palette-modal')?.classList.contains('open');
      if (open && this.query && this.activeChannel !== 'local') this.runOnlineSearch(this.query);
    });
    this.renderChannelChips();
    this.renderFooter();

    if (input) {
      input.addEventListener('input', (e) => {
        this.onlineSequence++;
        this.query = e.target.value.trim();
        this.activeIndex = -1;
        if (this.activeChannel === 'local') {
          this.runLocalSearch();
        } else if (!this.query) {
          this.runOnlineSearch('');
        } else {
          this.results = [];
          this.localResults = this.query ? this.findLocalMatches(this.query, 5) : [];
          this.renderResults();
          this.debouncedOnlineSearch(this.query);
        }
      });

      input.addEventListener('keydown', (e) => {
        if (e.isComposing || e.keyCode === 229) return;
        if (e.altKey && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
          e.preventDefault();
          const curIdx = SEARCH_CHANNEL_CHIPS.findIndex(c => c.id === this.activeChannel);
          const step = e.key === 'ArrowRight' ? 1 : -1;
          const nextIdx = (curIdx + step + SEARCH_CHANNEL_CHIPS.length) % SEARCH_CHANNEL_CHIPS.length;
          this.setChannel(SEARCH_CHANNEL_CHIPS[nextIdx].id);
          return;
        }
        const navigable = this.getNavigableResults();
        if (e.key === 'ArrowDown') {
          e.preventDefault();
          if (navigable.length > 0) {
            this.activeIndex = this.activeIndex < 0 ? 0 : (this.activeIndex + 1) % navigable.length;
            this.highlightActiveItem();
          }
          return;
        }
        if (e.key === 'ArrowUp') {
          e.preventDefault();
          if (navigable.length > 0) {
            this.activeIndex = this.activeIndex <= 0 ? navigable.length - 1 : this.activeIndex - 1;
            this.highlightActiveItem();
          }
          return;
        }
        if (e.key === 'Enter') {
          e.preventDefault();
          if (e.shiftKey) {
            this.openFullSearch(this.query);
            return;
          }
          const chosen = this.activeIndex >= 0 ? navigable[this.activeIndex] : null;
          if (chosen) {
            this.playResult(chosen);
          } else if (this.query) {
            this.openFullSearch(this.query);
          } else if (navigable.length > 0) {
            this.playResult(navigable[0]);
          }
        }
      });
    }

    document.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
        if (e.isComposing || e.keyCode === 229) return;
        e.preventDefault();
        const paletteModal = document.getElementById('search-palette-modal');
        if (paletteModal?.classList.contains('open')) {
          modal.close('#search-palette-modal');
        } else {
          this.open();
        }
      }
    });
  }

  getNavigableResults() {
    if (this.activeChannel === 'local') {
      return this.results;
    }
    return [...this.localResults, ...this.results];
  }

  renderChannelChips() {
    if (typeof document === 'undefined') return;
    const host = document.getElementById('search-palette-channel-bar');
    if (!host) return;
    host.textContent = '';

    SEARCH_CHANNEL_CHIPS.forEach(chip => {
      const btn = createElement('button', {
        className: `palette-channel-chip ${this.activeChannel === chip.id ? 'active' : ''}`,
        text: chip.label,
        dataset: { searchChannel: chip.id },
        attributes: {
          type: 'button',
          id: chip.domId || null,
          'aria-pressed': this.activeChannel === chip.id ? 'true' : 'false',
        },
        events: {
          click: () => this.setChannel(chip.id),
        },
      });
      host.appendChild(btn);
    });
  }

  renderFooter() {
    if (typeof document === 'undefined') return;
    const modalEl = document.getElementById('search-palette-modal');
    const footer = modalEl?.querySelector?.('.search-palette-footer');
    if (!footer) return;

    footer.textContent = '';

    const hints = createElement('div', {
      className: 'search-palette-footer-hints',
      children: [
        createElement('span', { children: [createElement('kbd', { className: 'kbd', text: '↑' }), ' ', createElement('kbd', { className: 'kbd', text: '↓' }), ' 选择'] }),
        createElement('span', { children: [createElement('kbd', { className: 'kbd', text: 'Enter' }), ' 播放 / 全部结果'] }),
        createElement('span', { children: [createElement('kbd', { className: 'kbd', text: 'Alt ←/→' }), ' 切换渠道'] }),
      ],
    });

    const viewAllBtn = createElement('button', {
      className: 'btn btn-ghost btn-sm search-palette-view-all-btn',
      text: '查看全部结果 (空选 Enter / Shift+Enter)',
      attributes: {
        type: 'button',
        id: 'search-palette-view-all-btn',
      },
      events: {
        click: () => this.openFullSearch(this.query),
      },
    });

    footer.appendChild(hints);
    footer.appendChild(viewAllBtn);
    footer.appendChild(createElement('button', {
      className: 'btn btn-ghost btn-sm', text: '快捷键一览 [?]',
      events: { click: () => { modal.close('#search-palette-modal'); state.emit('ui:open-shortcuts'); } },
    }));
  }

  open(initialChannel = null) {
    this.onlineSequence++;
    const raw = initialChannel === null || initialChannel === undefined
      ? readStoredSearchChannel()
      : (initialChannel === 'online' ? 'all' : initialChannel);
    this.activeChannel = SEARCH_CHANNEL_CHIPS.some(c => c.id === raw) ? raw : 'all';
    this.mode = this.activeChannel === 'local' ? 'local' : 'online';
    this.activeIndex = -1;
    this.updateTabButtons();
    this.renderFooter();
    modal.open('#search-palette-modal');
    const input = typeof document !== 'undefined' ? document.getElementById('search-palette-input') : null;
    if (input) {
      this.syncInputPlaceholder(input);
      input.focus?.();
      input.select?.();
      this.query = (input.value || '').trim();
    }
    if (this.activeChannel === 'local') {
      this.runLocalSearch();
    } else if (this.query) {
      this.runOnlineSearch(this.query);
    } else {
      this.localResults = [];
      this.results = [];
      this.renderResults();
    }
  }

  setQuery(query) {
    const input = document.getElementById('search-palette-input');
    if (!input) return;
    input.value = query;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.focus();
  }

  recentSearches() {
    try {
      const saved = JSON.parse(localStorage.getItem(`pruner_recent_searches_${state.getUserId()}`) || '[]');
      return saved.filter(q => typeof q === 'string' && q.trim()).slice(0, 10);
    } catch { return []; }
  }

  writeRecent(queries) {
    try { localStorage.setItem(`pruner_recent_searches_${state.getUserId()}`, JSON.stringify(queries.slice(0, 10))); } catch {}
  }

  recordRecent(query) {
    const q = query.trim();
    if (q) this.writeRecent([q, ...this.recentSearches().filter(item => item !== q)]);
  }

  renderRecent(container) {
    const queries = this.recentSearches();
    if (!queries.length) return;
    const remove = query => {
      this.writeRecent(query ? this.recentSearches().filter(item => item !== query) : []);
      this.renderResults();
      document.getElementById('search-palette-input')?.focus();
    };
    container.appendChild(createElement('div', { className: 'palette-section-header', children: [
      createElement('strong', { text: '最近搜索' }),
      createElement('button', { className: 'btn btn-ghost btn-sm', text: '清空', events: { click: () => remove(null) } }),
    ] }));
    container.appendChild(createElement('div', { className: 'recent-chips', children: queries.map(q =>
      createElement('span', { className: 'recent-chip', children: [
        createElement('button', { className: 'btn btn-ghost btn-sm', text: q, events: { click: () => this.setQuery(q) } }),
        createElement('button', { className: 'btn btn-ghost btn-sm', text: '×', attributes: { 'aria-label': `移除搜索 ${q}` }, events: { click: () => remove(q) } }),
      ] })) }));
  }

  openFullSearch(query = this.query) {
    this.onlineSequence++;
    const q = String(query || '').trim();
    this.recordRecent(q);
    modal.close('#search-palette-modal');
    if (typeof window !== 'undefined' && typeof window.dispatchEvent === 'function' && typeof CustomEvent !== 'undefined') {
      window.dispatchEvent(new CustomEvent('switch-tab', {
        detail: {
          tab: this.activeChannel === 'local' ? 'catalog' : 'search',
          query: q,
          channel: this.activeChannel !== 'local' ? this.activeChannel : 'all',
        },
      }));
    }
  }

  syncInputPlaceholder(input) {
    if (!input) return;
    const chip = SEARCH_CHANNEL_CHIPS.find(c => c.id === this.activeChannel);
    input.placeholder = this.activeChannel === 'local'
      ? '搜索本地曲库（歌名、歌手、专辑）... 按 Alt + ←/→ 切换渠道'
      : `在「${chip?.label || '全渠道聚合'}」中搜歌... 按 Alt + ←/→ 切换渠道`;
  }

  setMode(mode) {
    this.setChannel(mode === 'online' ? 'all' : 'local');
  }

  setChannel(channelId) {
    const resolved = SEARCH_CHANNEL_CHIPS.some(c => c.id === channelId) ? channelId : 'all';
    this.activeChannel = resolved;
    this.onlineSequence++;
    this.mode = resolved === 'local' ? 'local' : 'online';
    this.activeIndex = -1;
    this.channelErrors = {};
    writeStoredSearchChannel(resolved);
    this.updateTabButtons();

    const input = typeof document !== 'undefined' ? document.getElementById('search-palette-input') : null;
    if (input) {
      this.syncInputPlaceholder(input);
      input.focus?.();
    }
    if (resolved === 'local') {
      this.localResults = [];
      this.runLocalSearch();
    } else {
      this.runOnlineSearch(this.query);
    }
  }

  updateTabButtons() {
    if (typeof document === 'undefined') return;
    document.querySelectorAll('[data-search-channel]').forEach(btn => {
      const isMatch = btn.dataset.searchChannel === this.activeChannel;
      btn.classList.toggle('active', isMatch);
      btn.setAttribute('aria-pressed', isMatch ? 'true' : 'false');
    });
  }

  findLocalMatches(query, maxCount = 40) {
    const q = String(query || '').trim().toLowerCase();
    const catalog = [
      ...(state.get('catalog') || []),
      ...(state.get('cloudSongs') || []),
      ...(state.get('localSongs') || []),
    ];

    const seen = new Set();
    const matched = [];
    for (const song of catalog) {
      if (!song) continue;
      const key = `${song.source_playlist_id ?? 'local'}:${song.id ?? song.local_path ?? song.title}`;
      if (seen.has(key)) continue;
      if (!q) {
        seen.add(key);
        matched.push(song);
        if (matched.length >= Math.min(20, maxCount)) break;
        continue;
      }
      const title = (song.title || '').toLowerCase();
      const artists = formatArtists(song.artists).toLowerCase();
      const album = (song.album || '').toLowerCase();
      if (title.includes(q) || artists.includes(q) || album.includes(q)) {
        seen.add(key);
        matched.push(song);
        if (matched.length >= maxCount) break;
      }
    }
    return matched;
  }

  runLocalSearch() {
    this.localResults = [];
    this.results = this.findLocalMatches(this.query, 40);
    this.channelErrors = {};
    this.onlineLoading = false;
    this.renderResults();
  }

  normalizeMultiTrack(item) {
    return normalizeMultiTrack(item);
  }

  async runOnlineSearch(query) {
    const q = String(query || '').trim();
    if (!q) {
      this.localResults = [];
      this.results = [];
      this.channelErrors = {};
      this.onlineLoading = false;
      this.renderResults();
      return;
    }

    this.localResults = this.findLocalMatches(q, 5);

    if (state.get('offlineMode') && !state.get('guestLocalMode')) {
      this.results = [];
      this.channelErrors = {};
      this.onlineLoading = false;
      this.renderResults('离线只读模式下无法执行在线全渠道搜索。');
      return;
    }

    const seq = ++this.onlineSequence;
    this.onlineLoading = true;
    this.channelErrors = {};
    this.renderResults();

    const targetChannels = this.activeChannel === 'all' ? null : [this.activeChannel];
    let res = await api.searchMulti(q, targetChannels, 30, 0);
    if (seq !== this.onlineSequence) return;

    // Graceful fallback to legacy /api/search if /api/search/multi failed and netease is included
    if (!res.ok && state.get('authenticated') && (this.activeChannel === 'all' || this.activeChannel === 'netease')) {
      const legacy = await api.search(q, 30, 0);
      if (seq !== this.onlineSequence) return;
      if (legacy.ok && legacy.data) {
        const legacySongs = Array.isArray(legacy.data.songs) ? legacy.data.songs : (Array.isArray(legacy.data) ? legacy.data : []);
        res = {
          ok: true,
          data: {
            tracks: legacySongs.map(s => ({ ...s, provider: 'netease', available_channels: ['netease'] })),
            channel_errors: {},
          },
        };
      }
    }

    this.onlineLoading = false;

    if (res.ok && res.data) {
      const rawTracks = Array.isArray(res.data.tracks)
        ? res.data.tracks
        : (Array.isArray(res.data.songs) ? res.data.songs : (Array.isArray(res.data) ? res.data : []));
      this.results = rawTracks.map(item => this.normalizeMultiTrack(item)).filter(Boolean);
      this.channelErrors = (res.data.channel_errors && typeof res.data.channel_errors === 'object')
        ? res.data.channel_errors
        : {};
      this.activeIndex = -1;
      this.renderResults();
    } else {
      this.results = [];
      this.channelErrors = {};
      this.renderResults(res.error?.message || '全渠道搜索失败，请稍后重试。');
    }
  }

  playResult(song) {
    this.recordRecent(this.query);
    modal.close('#search-palette-modal');
    const queue = this.getNavigableResults();
    const idx = queue.indexOf(song);
    player.playQueue(queue.length > 0 ? queue : [song], Math.max(0, idx), 'order', '搜索结果');
  }

  highlightActiveItem() {
    if (typeof document === 'undefined') return;
    const container = document.getElementById('search-palette-results');
    if (!container) return;
    const rows = container.querySelectorAll('.palette-result-row');
    rows.forEach((row, idx) => {
      row.classList.toggle('active', idx === this.activeIndex);
      if (idx === this.activeIndex && typeof row.scrollIntoView === 'function') {
        row.scrollIntoView({ block: 'nearest' });
      }
    });
  }

  buildResultRow(song, globalIndex, { isLocalSection = false } = {}) {
    const canFavoriteOrPlaylist = Boolean(song && (getTrackKey(song) || song.id));
    const liked = canFavoriteOrPlaylist ? state.isSongLiked(song) : false;

    const channelBadges = [];
    const channels = isLocalSection
      ? [song.is_cloud ? 'cloud' : (song.local_path ? 'local' : 'netease')]
      : (Array.isArray(song.available_channels) && song.available_channels.length > 0
        ? song.available_channels
        : (song.local_path ? ['local'] : (song.provider ? [song.provider] : [])));
    channels.slice(0, 4).forEach(ch => {
      const key = String(ch || '').toLowerCase();
      channelBadges.push(createElement('span', {
        className: `palette-channel-pill channel-pill-${key}`,
        text: CHANNEL_BADGE_LABELS[key] || ch,
      }));
    });

    const titleChildren = [
      createElement('span', { className: 'palette-result-title-text', text: song.title || '未知曲目' }),
    ];
    if (channelBadges.length > 0) {
      titleChildren.push(createElement('span', { className: 'palette-channel-badges' }, channelBadges));
    }

    const actionChildren = [
      createElement('span', { className: 'numeral-mono palette-result-dur', text: formatDuration(song) }),
    ];

    if (canFavoriteOrPlaylist) {
      actionChildren.push(
        createElement('button', {
          className: `btn btn-ghost btn-icon btn-sm ${liked ? 'is-liked' : ''}`,
          attributes: { title: liked ? '取消喜欢' : '红心喜欢', 'aria-label': '喜欢' },
          events: {
            click: (e) => {
              e.stopPropagation();
              player.toggleLikeCurrentSong(song);
            },
          },
          children: [createIcon(liked ? 'heart-filled' : 'heart', { size: 14 })],
        }),
        createElement('button', {
          className: 'btn btn-ghost btn-icon btn-sm',
          attributes: { title: '添加到歌单', 'aria-label': '添加到歌单' },
          events: {
            click: (e) => {
              e.stopPropagation();
              modal.close('#search-palette-modal');
              if (this.onAddToPlaylistCallback) this.onAddToPlaylistCallback(song);
            },
          },
          children: [createIcon('plus-list', { size: 14 })],
        }),
      );
    }

    actionChildren.push(
      createElement('button', {
        className: 'btn btn-ghost btn-icon btn-sm',
        attributes: { title: '加入下一首播放', 'aria-label': '下一首播放' },
        events: {
          click: (e) => {
            e.stopPropagation();
            player.enqueueNext(song);
          },
        },
        children: [createIcon('queue', { size: 14 })],
      }),
    );

    return createElement('div', {
      className: `palette-result-row ${globalIndex === this.activeIndex ? 'active' : ''}`,
      events: {
        mouseenter: () => {
          this.activeIndex = globalIndex;
          this.highlightActiveItem();
        },
        click: () => this.playResult(song),
        contextmenu: (e) => openSongContextMenu(e, song, {
          source: 'search',
          onAddToPlaylist: this.onAddToPlaylistCallback,
        }),
      },
      children: [
        createElement('img', {
          className: 'palette-result-cover',
          attributes: {
            src: getSizedCoverUrl(song.cover_url, 96, '音'),
            alt: song.title || '封面',
            loading: 'lazy',
          },
        }),
        createElement('div', {
          className: 'palette-result-info',
          children: [
            createElement('div', { className: 'palette-result-title' }, titleChildren),
            createElement('div', {
              className: 'palette-result-sub',
              text: `${formatArtists(song.artists)}${song.album ? ` · ${song.album}` : ''}`,
            }),
          ],
        }),
        createElement('div', { className: 'palette-result-actions' }, actionChildren),
      ],
    });
  }

  renderResults(errorHint = '') {
    if (typeof document === 'undefined') return;
    const container = document.getElementById('search-palette-results');
    if (!container) return;
    container.textContent = '';

    const clear = document.getElementById('search-palette-clear-btn');
    if (clear) clear.hidden = !this.query;
    if (!this.query) this.renderRecent(container);

    const chipLabel = SEARCH_CHANNEL_CHIPS.find(c => c.id === this.activeChannel)?.label || '全渠道聚合';
    let globalIdx = 0;

    // Online-first mode: render compact local matches ("曲库中", max 5) ABOVE online results whenever query is non-empty
    if (this.activeChannel !== 'local' && this.query && this.localResults.length > 0) {
      container.appendChild(createElement('div', {
        className: 'palette-section-header',
        dataset: { paletteSection: 'local' },
        children: [
          createElement('span', { text: '曲库中' }),
          createElement('span', { className: 'numeral-mono', text: `${this.localResults.length} 首` }),
        ],
      }));
      this.localResults.slice(0, 5).forEach((song) => {
        container.appendChild(this.buildResultRow(song, globalIdx++, { isLocalSection: true }));
      });
    }

    if (this.onlineLoading) {
      container.appendChild(createElement('div', {
        className: 'empty-state',
        children: [
          createElement('div', { className: 'spinner' }),
          createElement('div', { className: 'empty-state-desc', text: `正在「${chipLabel}」检索 "${this.query}"...` }),
        ],
      }));
      return;
    }

    if (errorHint) {
      container.appendChild(createElement('div', {
        className: 'empty-state',
        children: [createElement('div', { className: 'empty-state-desc', text: errorHint })],
      }));
      return;
    }

    const errKeys = Object.keys(this.channelErrors || {});
    if (errKeys.length > 0 && this.activeChannel !== 'local') {
      const summary = errKeys
        .map(k => `${CHANNEL_BADGE_LABELS[k] || k}`)
        .join(' / ');
      container.appendChild(createElement('div', {
        className: 'palette-error-bar',
        text: `部分渠道暂未响应（${summary}），已展示其他可用渠道结果。`,
      }));
    }

    if (this.results.length === 0) {
      if (this.activeChannel !== 'local' && this.localResults.length > 0) {
        container.appendChild(createElement('div', {
          className: 'palette-section-header',
          text: `「${chipLabel}」暂无更多在线匹配，按 Enter 查看完整搜索页`,
        }));
        return;
      }
      container.appendChild(createElement('div', {
        className: 'empty-state',
        children: [
          createElement('div', {
            className: 'empty-state-title',
            text: this.query ? '未找到匹配曲目' : (this.activeChannel !== 'local' ? `输入关键词在「${chipLabel}」搜歌` : '本地曲库为空'),
          }),
          createElement('div', {
            className: 'empty-state-desc',
            text: this.activeChannel === 'local'
              ? '按 Tab 键可快速切换至全渠道聚合搜索（网易云 / 酷狗概念版 / 咪咕 / QQ音乐 / 酷我 / B站）。'
              : '支持全渠道并发检索，点击播放或按 Enter 查看完整搜索结果页。',
          }),
        ],
      }));
      return;
    }

    if (this.activeChannel !== 'local' && this.query && this.localResults.length > 0) {
      container.appendChild(createElement('div', {
        className: 'palette-section-header',
        dataset: { paletteSection: 'online' },
        children: [
          createElement('span', { text: `${chipLabel}结果` }),
          createElement('span', { className: 'numeral-mono', text: `${this.results.length} 首` }),
        ],
      }));
    }

    this.results.forEach((song) => {
      container.appendChild(this.buildResultRow(song, globalIdx++, { isLocalSection: false }));
    });
  }
}

export const searchPalette = new SearchPaletteManager();
