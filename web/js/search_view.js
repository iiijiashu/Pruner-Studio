import { getTrackKey } from './utils.js';
/**
 * Pruner Studio - Full-Page Multi-Channel Search View (`#view-search`)
 * Paginates online results at 100 tracks per page (`offset` & `has_more` with `加载更多`),
 * supports channel filter chips, relevance vs quality-first sorting, double-click queue play,
 * inline controls (play, play next, add to playlist, NetEase-only like), and right-click context menu.
 */
import { api } from './api.js';
import { state } from './state.js';
import { player } from './player.js';
import { openSongContextMenu } from './context_menu.js';
import { CHANNEL_BADGE_LABELS, normalizeMultiTrack } from './search_palette.js';
import { createElement, formatArtists, formatDuration, getSizedCoverUrl, createPlayingEqBars, syncPlayingRowIndicators } from './utils.js';
import { createIcon } from './icons.js';

export const SEARCH_VIEW_PAGE_SIZE = 100;

export const SEARCH_VIEW_CHANNELS = [
  { id: 'all', label: '全渠道聚合' },
  { id: 'netease', label: '网易云' },
  { id: 'kugou_lite', label: '酷狗概念版' },
  { id: 'migu', label: '咪咕' },
  { id: 'qq', label: 'QQ音乐' },
  { id: 'kuwo', label: '酷我' },
  { id: 'bilibili', label: 'B站' },
  { id: 'gdstudio', label: 'GDStudio' },
];

const LEVEL_WEIGHT = {
  jymaster: 700,
  sky: 650,
  hires: 600,
  lossless: 500,
  exhigh: 320,
  higher: 220,
  standard: 128,
};

export function computeTrackQualityScore(song) {
  if (!song || typeof song !== 'object') return 0;
  let score = 0;
  const level = String(song.level || song.max_br_level || '').trim().toLowerCase();
  const fmt = String(song.format || '').trim().toLowerCase();
  const bitrate = Number(song.bitrate) || 0;
  const sampleRate = Number(song.sample_rate) || 0;

  if (LEVEL_WEIGHT[level]) {
    score += LEVEL_WEIGHT[level];
  } else if (fmt === 'flac' || fmt === 'wav' || fmt === 'alac' || fmt === 'ape') {
    score += 500;
  } else if (fmt === 'aac' || fmt === 'ogg') {
    score += 220;
  } else if (fmt === 'mp3') {
    score += 128;
  }

  if (bitrate >= 1000) {
    score += Math.min(250, Math.round(bitrate / 4000));
  } else if (bitrate > 0 && bitrate < 1000) {
    score += Math.min(250, Math.round(bitrate / 4));
  }

  if (sampleRate >= 96000) score += 80;
  else if (sampleRate >= 48000) score += 30;

  const chCount = Array.isArray(song.available_channels) ? song.available_channels.length : 0;
  score += Math.min(10, chCount);
  return score;
}

export function formatTrackQualityHint(song) {
  if (!song || typeof song !== 'object') return '';
  const level = String(song.level || song.max_br_level || '').trim().toLowerCase();
  const fmt = String(song.format || '').trim().toUpperCase();
  const bitrate = Number(song.bitrate) || 0;
  const sampleRate = Number(song.sample_rate) || 0;

  const parts = [];
  if (level === 'jymaster') parts.push('超清母带');
  else if (level === 'sky') parts.push('沉浸声');
  else if (level === 'hires') parts.push('Hi-Res');
  else if (level === 'lossless') parts.push('无损');
  else if (level === 'exhigh') parts.push('极高');

  if (fmt && !parts.includes(fmt)) parts.push(fmt);

  if (sampleRate >= 48000) {
    const khz = sampleRate % 1000 === 0 ? `${sampleRate / 1000}kHz` : `${(sampleRate / 1000).toFixed(1)}kHz`;
    parts.push(khz);
  } else if (bitrate >= 1000) {
    parts.push(`${Math.round(bitrate / 1000)}kbps`);
  }

  return parts.join(' · ');
}

export class SearchViewManager {
  constructor() {
    this.container = null;
    this.query = '';
    this.activeChannel = 'all';
    state.on('webdav:scanned', () => { if (this.query) this.search(this.query); });
    this.sortBy = 'relevance'; // 'relevance' | 'quality'
    this.offset = 0;
    this.pageSize = SEARCH_VIEW_PAGE_SIZE;
    this.hasMore = false;
    this.loading = false;
    this.loadingMore = false;
    this.errorMessage = '';
    this.channelErrors = {};
    this.tracks = [];
    this.selectedIndex = -1;
    this.requestSequence = 0;
    this.onAddToPlaylistCallback = null;
  }

  init({ onAddToPlaylist } = {}) {
    if (typeof onAddToPlaylist === 'function') {
      this.onAddToPlaylistCallback = onAddToPlaylist;
    }
    if (typeof document !== 'undefined') {
      this.container = document.getElementById('view-search');
      if (this.container && this.container.children?.length === 0) {
        this.render();
      }
    }

    if (state && typeof state.on === 'function') {
      state.on('change:likedSongIds', () => {
        if (state.get('activeTab') === 'search') {
          this.render();
        }
      });
    }
  }

  async open(query = '', options = {}) {
    if (!this.container && typeof document !== 'undefined') {
      this.container = document.getElementById('view-search');
    }
    if (options && typeof options === 'object') {
      if (typeof options.onAddToPlaylist === 'function') {
        this.onAddToPlaylistCallback = options.onAddToPlaylist;
      }
      if (options.channel && SEARCH_VIEW_CHANNELS.some(c => c.id === options.channel)) {
        this.activeChannel = options.channel;
      }
    }

    const nextQ = String(query ?? this.query).trim();
    this.query = nextQ;
    if (nextQ) {
      await this.search(nextQ, { reset: true });
    } else {
      this.render();
      if (typeof document !== 'undefined') {
        document.getElementById('search-view-input')?.focus?.();
      }
    }
  }

  setChannel(channelId) {
    const resolved = SEARCH_VIEW_CHANNELS.some(c => c.id === channelId) ? channelId : 'all';
    if (resolved === this.activeChannel) return;
    this.activeChannel = resolved;
    if (this.query) {
      this.search(this.query, { reset: true });
    } else {
      this.render();
    }
  }

  setSort(sortKey) {
    const resolved = sortKey === 'quality' ? 'quality' : 'relevance';
    if (resolved === this.sortBy) return;
    this.sortBy = resolved;
    this.render();
  }

  getSortedTracks() {
    const copy = [...this.tracks];
    if (this.sortBy === 'quality') {
      copy.sort((a, b) => {
        const diff = computeTrackQualityScore(b) - computeTrackQualityScore(a);
        if (diff !== 0) return diff;
        return (a._rank ?? 0) - (b._rank ?? 0);
      });
    } else {
      copy.sort((a, b) => (a._rank ?? 0) - (b._rank ?? 0));
    }
    return copy;
  }

  async loadMore() {
    if (!this.query || this.loading || this.loadingMore || !this.hasMore) return;
    return this.search(this.query, { reset: false });
  }

  async search(query, { reset = true } = {}) {
    const q = String(query ?? this.query).trim();
    this.query = q;

    if (!q) {
      this.tracks = [];
      this.offset = 0;
      this.hasMore = false;
      this.errorMessage = '';
      this.channelErrors = {};
      this.loading = false;
      this.loadingMore = false;
      this.render();
      return;
    }

    if (state.get('offlineMode') && !state.get('guestLocalMode')) {
      this.loading = false;
      this.loadingMore = false;
      this.errorMessage = '离线只读模式下无法执行在线多渠道检索，请扫码登录或在「音乐库」浏览本地缓存。';
      this.render();
      return;
    }

    if (reset) {
      this.cursors = null;
      this.legacySearch = false;
    }
    const requestOffset = reset || this.cursors ? 0 : this.nextOffset;
    const seq = ++this.requestSequence;

    if (reset) {
      this.offset = 0;
      this.tracks = [];
      this.hasMore = false;
      this.errorMessage = '';
      this.channelErrors = {};
      this.selectedIndex = -1;
      this.loading = true;
      this.loadingMore = false;
    } else {
      this.loadingMore = true;
      this.errorMessage = '';
    }
    this.render();

    const targetChannels = this.activeChannel === 'all' ? null : [this.activeChannel];
    let res = this.legacySearch ? null : await api.searchMulti(q, targetChannels, this.pageSize, requestOffset, this.cursors);
    if (seq !== this.requestSequence) return;

    // Fallback to NetEase single search when /api/search/multi fails and NetEase is included
    if (this.legacySearch || (reset && !res.ok && state.get('authenticated') &&
        (this.activeChannel === 'all' || this.activeChannel === 'netease'))) {
      const legacy = await api.search(q, Math.min(50, this.pageSize), requestOffset);
      if (seq !== this.requestSequence) return;
      res = legacy;
      if (legacy.ok && legacy.data) {
        this.legacySearch = true;
        const legacySongs = Array.isArray(legacy.data.songs) ? legacy.data.songs : (Array.isArray(legacy.data) ? legacy.data : []);
        res = {
          ok: true,
          data: {
            tracks: legacySongs.map(s => ({ ...s, provider: 'netease', available_channels: ['netease'] })),
            channel_errors: {},
            offset: requestOffset,
            limit: 50,
            has_more: legacySongs.length >= 50,
          },
        };
      }
    }

    this.loading = false;
    this.loadingMore = false;

    if (res.ok && res.data) {
      const rawTracks = Array.isArray(res.data.tracks)
        ? res.data.tracks
        : (Array.isArray(res.data.songs) ? res.data.songs : (Array.isArray(res.data) ? res.data : []));
      const normalized = rawTracks
        .map(item => normalizeMultiTrack(item))
        .filter(Boolean);

      if (reset) {
        this.tracks = normalized.map((t, idx) => ({ ...t, _rank: idx }));
      } else {
        const seen = new Map();
        this.tracks.forEach((existing, idx) => {
          const key = getTrackKey(existing);
          seen.set(key, idx);
        });
        let nextRank = this.tracks.length;
        for (const item of normalized) {
          const key = getTrackKey(item);
          if (seen.has(key)) {
            const existing = this.tracks[seen.get(key)];
            const mergedChannels = new Set([
              ...(existing.available_channels || []),
              ...(item.available_channels || []),
            ]);
            existing.available_channels = Array.from(mergedChannels);
          } else {
            seen.set(key, this.tracks.length);
            this.tracks.push({ ...item, _rank: nextRank++ });
          }
        }
      }

      this.offset = Number.isFinite(Number(res.data.offset)) ? Number(res.data.offset) : requestOffset;
      this.cursors = res.data.next_cursors || null;
      this.nextOffset = this.offset + Number(res.data.limit || rawTracks.length);
      this.hasMore = typeof res.data.has_more === 'boolean'
        ? res.data.has_more
        : rawTracks.length >= this.pageSize;
      this.channelErrors = (res.data.channel_errors && typeof res.data.channel_errors === 'object')
        ? res.data.channel_errors
        : {};
      this.errorMessage = '';
    } else {
      this.errorMessage = res.error?.message || '全网聚合搜索失败，请稍后重试。';
    }

    this.render();
  }

  render() {
    if (typeof document === 'undefined') return;
    if (!this.container) {
      this.container = document.getElementById('view-search');
    }
    const root = this.container;
    if (!root) return;
    root.textContent = '';

    const shell = createElement('div', { className: 'search-view-container' });

    // 1. Header & Query Form
    const queryInput = createElement('input', {
      className: 'input-text search-view-input',
      attributes: {
        type: 'text',
        id: 'search-view-input',
        value: this.query,
        placeholder: '输入歌名、歌手或专辑名进行全渠道搜索（每页 100 首）...',
        'aria-label': '全网聚合搜索关键词',
        autocomplete: 'off',
      },
      events: {
        keydown: (e) => {
          if (e.isComposing || e.keyCode === 229) return;
          if (e.key === 'Enter') {
            e.preventDefault();
            this.search(queryInput.value, { reset: true });
          }
        },
      },
    });
    queryInput.value = this.query;

    const searchBtn = createElement('button', {
      className: 'btn btn-primary btn-sm',
      attributes: { type: 'button', id: 'search-view-submit-btn' },
      events: { click: () => this.search(queryInput.value, { reset: true }) },
      children: [createIcon('search', { size: 14 }), createElement('span', { text: '搜索' })],
    });

    const headerCard = createElement('div', {
      className: 'search-view-header-card',
      children: [
        createElement('div', {
          className: 'search-view-title-row',
          children: [
            createElement('div', {
              className: 'search-view-heading',
              children: [
                createIcon('globe', { size: 16 }),
                createElement('h2', { className: 'search-view-title', text: '全网聚合搜索' }),
              ],
            }),
            createElement('span', {
              className: 'search-view-count numeral-mono',
              id: 'search-view-count-label',
              text: this.tracks.length > 0 ? `已加载 ${this.tracks.length} 首结果` : (this.query ? `关键词：${this.query}` : '输入关键词开始检索'),
            }),
          ],
        }),
        createElement('div', {
          className: 'search-view-input-row',
          children: [queryInput, searchBtn],
        }),
        createElement('div', {
          className: 'search-view-controls-row',
          children: [
            createElement('div', {
              className: 'search-view-channel-chips',
              attributes: { role: 'group', 'aria-label': '渠道筛选' },
              children: SEARCH_VIEW_CHANNELS.map(ch => createElement('button', {
                className: `palette-channel-chip ${this.activeChannel === ch.id ? 'active' : ''}`,
                text: ch.label,
                dataset: { searchViewChannel: ch.id },
                attributes: {
                  type: 'button',
                  'aria-pressed': this.activeChannel === ch.id ? 'true' : 'false',
                },
                events: { click: () => this.setChannel(ch.id) },
              })),
            }),
            createElement('div', {
              className: 'segmented-control',
              attributes: { role: 'group', 'aria-label': '排序方式' },
              children: [
                createElement('button', {
                  className: `segmented-btn ${this.sortBy === 'relevance' ? 'active' : ''}`,
                  text: '相关度',
                  dataset: { searchSort: 'relevance' },
                  attributes: { type: 'button', 'aria-pressed': this.sortBy === 'relevance' ? 'true' : 'false' },
                  events: { click: () => this.setSort('relevance') },
                }),
                createElement('button', {
                  className: `segmented-btn ${this.sortBy === 'quality' ? 'active' : ''}`,
                  text: '音质优先',
                  dataset: { searchSort: 'quality' },
                  attributes: { type: 'button', 'aria-pressed': this.sortBy === 'quality' ? 'true' : 'false' },
                  events: { click: () => this.setSort('quality') },
                }),
              ],
            }),
          ],
        }),
      ],
    });
    shell.appendChild(headerCard);

    // 2. Partial channel error banner
    const errKeys = Object.keys(this.channelErrors || {});
    if (errKeys.length > 0 && !this.loading) {
      const summary = errKeys.map(k => CHANNEL_BADGE_LABELS[k] || k).join(' / ');
      shell.appendChild(createElement('div', {
        className: 'search-view-partial-error',
        attributes: { role: 'status' },
        children: [
          createIcon('alert-circle', { size: 14 }),
          createElement('span', { text: `部分渠道暂未响应（${summary}），已展示其余可用渠道结果。` }),
        ],
      }));
    }

    // 3. Body Content (Loading / Error / Empty / Results List)
    const resultsCard = createElement('div', { className: 'search-view-results-card' });

    if (this.loading) {
      resultsCard.appendChild(createElement('div', {
        className: 'empty-state',
        children: [
          createElement('div', { className: 'spinner' }),
          createElement('div', { className: 'empty-state-desc', text: `正在聚合检索 "${this.query}"...` }),
        ],
      }));
      shell.appendChild(resultsCard);
      root.appendChild(shell);
      return;
    }

    if (this.errorMessage && this.tracks.length) {
      resultsCard.appendChild(createElement('div', {className: 'discover-degraded-notice', attributes: {role: 'alert'},
        text: '加载更多失败 · ' + this.errorMessage}));
    }
    if (this.errorMessage && this.tracks.length === 0) {
      resultsCard.appendChild(createElement('div', {
        className: 'empty-state',
        children: [
          createElement('div', { className: 'empty-state-title', text: '检索未完成' }),
          createElement('div', { className: 'empty-state-desc', text: this.errorMessage }),
          (!state.get('offlineMode') || state.get('guestLocalMode')) && this.query
            ? createElement('button', {
                className: 'btn btn-secondary btn-sm',
                text: '重试搜索',
                attributes: { type: 'button' },
                events: { click: () => this.search(this.query, { reset: true }) },
              })
            : null,
        ].filter(Boolean),
      }));
      shell.appendChild(resultsCard);
      root.appendChild(shell);
      return;
    }

    const sortedTracks = this.getSortedTracks();

    if (!this.query) {
      resultsCard.appendChild(createElement('div', {
        className: 'empty-state',
        children: [
          createElement('div', { className: 'empty-state-title', text: '全网多渠道搜歌' }),
          createElement('div', { className: 'empty-state-desc', text: '搜索歌名、歌手或专辑。双击结果即可播放。' }),
        ],
      }));
      shell.appendChild(resultsCard);
      root.appendChild(shell);
      return;
    }

    if (sortedTracks.length === 0) {
      resultsCard.appendChild(createElement('div', {
        className: 'empty-state',
        children: [
          createElement('div', { className: 'empty-state-title', text: `未找到「${this.query}」相关曲目` }),
          createElement('div', { className: 'empty-state-desc', text: '可尝试切换上方渠道筛选或更换关键词。' }),
        ],
      }));
      shell.appendChild(resultsCard);
      root.appendChild(shell);
      return;
    }

    const listEl = createElement('div', { className: 'search-results-list', attributes: { role: 'list' } });

    sortedTracks.forEach((song, idx) => {
      const canFavoriteOrPlaylist = Boolean(song && (getTrackKey(song) || song.id));
      const liked = canFavoriteOrPlaylist ? state.isSongLiked(song) : false;
      const qualityHint = formatTrackQualityHint(song);
      const channels = Array.isArray(song.available_channels) && song.available_channels.length > 0
        ? song.available_channels
        : (song.provider ? [song.provider] : []);

      const badges = channels.slice(0, 5).map(ch => {
        const key = String(ch || '').toLowerCase();
        return createElement('span', {
          className: `palette-channel-pill channel-pill-${key}`,
          text: CHANNEL_BADGE_LABELS[key] || ch,
        });
      });

      if (qualityHint) {
        badges.push(createElement('span', {
          className: 'badge badge-source-highres search-quality-badge',
          text: qualityHint,
        }));
      }

      const rowActions = [
        createElement('span', { className: 'search-row-duration numeral-mono', text: formatDuration(song) }),
        createElement('button', {
          className: 'table-btn',
          attributes: { type: 'button', title: '播放此曲', 'aria-label': `播放 ${song.title || '曲目'}` },
          events: {
            click: (e) => {
              e.stopPropagation();
              player.playQueue(sortedTracks, idx, 'order', '搜索结果');
            },
          },
          children: [createIcon('play', { size: 14 })],
        }),
        createElement('button', {
          className: 'table-btn',
          attributes: { type: 'button', title: '下一首播放', 'aria-label': '下一首播放' },
          events: {
            click: (e) => {
              e.stopPropagation();
              player.enqueueNext(song);
            },
          },
          children: [createIcon('queue', { size: 14 })],
        }),
      ];

      if (canFavoriteOrPlaylist) {
        rowActions.push(
          createElement('button', {
            className: 'table-btn',
            attributes: { type: 'button', title: '加入歌单', 'aria-label': '加入歌单' },
            events: {
              click: (e) => {
                e.stopPropagation();
                if (typeof this.onAddToPlaylistCallback === 'function') {
                  this.onAddToPlaylistCallback(song);
                } else if (typeof window !== 'undefined' && typeof CustomEvent !== 'undefined') {
                  window.dispatchEvent(new CustomEvent('pruner:add-to-playlist', { detail: { song } }));
                }
              },
            },
            children: [createIcon('plus-list', { size: 14 })],
          }),
          createElement('button', {
            className: `table-btn ${liked ? 'is-liked' : ''}`,
            attributes: { type: 'button', title: liked ? '取消喜欢' : '喜欢', 'aria-label': liked ? '取消喜欢' : '喜欢' },
            events: {
              click: (e) => {
                e.stopPropagation();
                player.toggleLikeCurrentSong(song);
              },
            },
            children: [createIcon(liked ? 'heart-filled' : 'heart', { size: 14 })],
          }),
        );
      }

      const row = createElement('div', {
        className: `search-result-row ${idx === this.selectedIndex ? 'is-selected' : ''}`,
        dataset: { trackKey: getTrackKey(song) },
        attributes: {
          role: 'listitem',
          tabindex: '0',
          'aria-label': `${song.title || '曲目'} - ${formatArtists(song.artists)}`,
        },
        events: {
          click: () => {
            this.selectedIndex = idx;
            listEl.querySelectorAll('.search-result-row').forEach((n, nIdx) => n.classList.toggle('is-selected', nIdx === idx));
          },
          dblclick: () => player.playQueue(sortedTracks, idx, 'order', '搜索结果'),
          keydown: (e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              player.playQueue(sortedTracks, idx, 'order', '搜索结果');
            }
          },
          contextmenu: (e) => openSongContextMenu(e, song, {
            source: 'search',
            queue: sortedTracks,
            index: idx,
            onAddToPlaylist: this.onAddToPlaylistCallback,
          }),
        },
        children: [
          createElement('span', {
            className: 'search-row-index numeral-mono',
            children: [
              createElement('span', { className: 'search-index-num', text: String(idx + 1).padStart(2, '0') }),
              createPlayingEqBars(),
            ],
          }),
          createElement('img', {
            className: 'search-row-cover',
            attributes: {
              src: getSizedCoverUrl(song.cover_url, 96, '音'),
              alt: song.title || '封面',
              loading: 'lazy',
            },
          }),
          createElement('div', {
            className: 'search-row-main',
            children: [
              createElement('div', {
                className: 'search-row-title-line',
                children: [
                  createElement('span', { className: 'search-row-title', text: song.title || '未知曲目' }),
                  ...badges,
                ],
              }),
              createElement('div', {
                className: 'search-row-sub',
                text: `${formatArtists(song.artists)}${song.album ? ` · ${song.album}` : ''}`,
              }),
            ],
          }),
          createElement('div', { className: 'search-row-actions' }, rowActions),
        ],
      });

      listEl.appendChild(row);
    });

    syncPlayingRowIndicators(state.get('currentPlayingSong'), Boolean(state.get('isPlaying')), listEl);
    resultsCard.appendChild(listEl);

    // 4. Load More Pagination Footer
    if (this.hasMore || this.loadingMore) {
      resultsCard.appendChild(createElement('div', {
        className: 'search-view-load-more-bar',
        children: [
          createElement('button', {
            className: 'btn btn-secondary btn-sm',
            text: this.loadingMore ? '正在加载更多...' : '加载更多',
            attributes: {
              type: 'button',
              id: 'search-view-load-more-btn',
              disabled: this.loadingMore ? 'disabled' : null,
            },
            events: { click: () => this.loadMore() },
          }),
        ],
      }));
    }

    shell.appendChild(resultsCard);
    root.appendChild(shell);
  }
}

export const searchView = new SearchViewManager();
