/**
 * Pruner Studio - Searchable Music Library (音乐库)
 * Library sub-navigation (Liked / Self-Created / Subscribed / Cloud Disk / Local),
 * O(1) incremental checkbox selection (zero whole-catalog re-filter on checkbox click),
 * discrete ?param=96y96 / 192y192 cover resizing, Play All, Heart/Like, Add to Playlist,
 * direct "进入歌单清理" CTA on writable playlists, and 50 items/page shared Table/Card views.
 */
import { api } from './api.js';
import { state } from './state.js';
import { player } from './player.js';
import { toast } from './toast.js';
import { modal } from './modal.js';
import { RuleEngine } from './rules.js';
import { writableNeteaseTrack, getTrackKey, createElement, formatArtists, formatDuration, trackDurationSummary, getSizedCoverUrl, debounce, createPlayingEqBars, syncPlayingRowIndicators } from './utils.js';
import { createIcon } from './icons.js';
import { openSongContextMenu, quickNextButton } from './context_menu.js';

function artistNames(song) {
  const artists = Array.isArray(song?.artists) ? song.artists : [song?.artists];
  const seen = new Set();
  const names = [];
  for (const entry of artists) {
    const raw = typeof entry === 'string' ? entry : entry?.name;
    if (!raw || typeof raw !== 'string') continue;
    for (const part of raw.split(/\s*(?:\/|、|，|;|；|\|)\s*/)) {
      const clean = part.trim();
      if (clean && !seen.has(clean)) {
        seen.add(clean);
        names.push(clean);
      }
    }
  }
  return names;
}

class CatalogManager {
  constructor() {
    this.container = null;
    this.pageSize = 50;
    this.drillStack = [];
    this._cachedFilteredSongs = [];
    this._cachedSortedSongs = [];
    this._cachedPageKeys = [];
    this.cloudLoading = false;
    this.onAddToPlaylistCallback = null;
  }

  init(containerId, { onAddToPlaylist } = {}) {
    this.container = document.getElementById(containerId);
    this.onAddToPlaylistCallback = onAddToPlaylist || null;
    state.on('change:user', () => { this.drillStack = []; });
    state.on('change:activeOfflineAccountId', () => { this.drillStack = []; });
    this.initSectionNav();
    this.initFilterEvents();
    this.initViewSwitcher();

    for (const key of ['audioMeta', 'currentPlayingSong']) state.on('change:' + key, () => { if (state.get('activeTab') === 'catalog') this.render(); });
    for (const key of ['likedSongIds', 'likedTrackKeys']) {
      state.on('change:' + key, () => {
        if (state.get('activeTab') === 'catalog') {
          this.refreshVisibleHeartIcons();
        }
      });
    }
  }

  initSectionNav() {
    if (typeof document === 'undefined') return;
    const sectionBtns = document.querySelectorAll('.catalog-section-tab');
    sectionBtns.forEach(btn => {
      btn.addEventListener('click', async () => {
        const section = btn.dataset.section || 'all';
        await this.selectSection(section);
      });
    });
  }

  async selectSection(section) {
    this.drillStack = [];
    state.set({
      catalogSection: section,
      catalogSelectedPlaylistId: 'all',
      catalogPage: 1,
    });

    if (typeof document !== 'undefined') {
      document.querySelectorAll('.catalog-section-tab').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.section === section);
        btn.setAttribute('aria-pressed', btn.dataset.section === section ? 'true' : 'false');
      });
      const playlistSelect = document.getElementById('catalog-playlist-filter');
      if (playlistSelect) { playlistSelect.value = 'all'; playlistSelect.disabled = ['cloud', 'local'].includes(section); }
    }

    if (section === 'cloud' && (state.get('cloudSongs') || []).length === 0 && state.get('authenticated')) {
      await this.loadCloudSongs();
    }
    if (section === 'local' && (state.get('localSongs') || []).length === 0) {
      await this.loadLocalSongs();
    }

    this.onFilterCriteriaChanged();
  }

  async loadLocalSongs(force = false) {
    if (!force && (state.get('localSongs') || []).length > 0) return;
    try {
      const files = [];
      const token = this.localRequest = Symbol();
      for (;;) {
        const res = await api.getLocalLibrary({q: '', limit: 500, offset: files.length});
        if (token !== this.localRequest || !res.ok || !Array.isArray(res.data?.files)) return;
        files.push(...res.data.files);
        if (!res.data.has_more || !res.data.files.length) break;
      }
      const normalized = files.map((f, idx) => ({
        id: `local_${idx}_${f.path || ''}`,
        track_key: f.path ? `local:${f.path}` : '',
        provider: 'local',
        local_path: f.path || '',
        title: f.title || '本地音频',
        artists: f.artist ? [f.artist] : ['本地歌手'],
        album: f.album || '',
        duration: f.duration_ms || 0,
        duration_ms: f.duration_ms || 0,
        format: f.format || '',
        bitrate: f.bitrate || 0,
        sample_rate: f.sample_rate || 0,
        cover_id: f.cover_id || '',
        cover_url: f.cover_url || (f.cover_id ? `/api/local-cover/${encodeURIComponent(f.cover_id)}` : ''),
        isrc: f.isrc || '',
        mbid: f.mbid || '',
        replaygain_track_gain_db: f.replaygain_track_gain_db ?? null,
        replaygain_track_peak: f.replaygain_track_peak ?? null,
        source: 'local',
        source_playlist_id: 'local',
        source_playlist_name: '本地曲库',
        availability: 'available',
      }));
      state.set({ localSongs: normalized });
    } catch {}
  }

  async loadCloudSongs(force = false) {
    const generation = state.get("sessionGeneration");
    if (this.cloudLoading && this.cloudGeneration === generation) return;
    if (!force && (state.get('cloudSongs') || []).length > 0) return;
    if (!state.get('authenticated') || state.get('offlineMode')) return;

    this.cloudGeneration = generation;
    this.cloudLoading = true;
    this.render();
    try {
      const res = await api.getCloudSongs(200, 0, true);
      if (generation !== state.get("sessionGeneration")) return;
      if (res.ok && res.data) {
        const tracks = Array.isArray(res.data.songs)
          ? res.data.songs
          : (Array.isArray(res.data.tracks) ? res.data.tracks : (Array.isArray(res.data) ? res.data : []));
        const normalized = tracks.map(t => ({
          ...t,
          is_cloud: true,
          source_playlist_id: t.source_playlist_id ?? 'cloud',
          source_playlist_name: t.source_playlist_name || '网易云云盘',
        }));
        state.set({ cloudSongs: normalized });
      }
    } catch (err) {
      console.warn('Failed to fetch cloud disk songs:', err);
    } finally {
      if (this.cloudGeneration === generation) {
        this.cloudLoading = false;
        this.render();
      }
    }
  }

  initFilterEvents() {
    if (typeof document === 'undefined') return;
    const searchInput = document.getElementById('catalog-search-input');
    const clearSearchBtn = document.getElementById('catalog-search-clear');
    const filterChips = document.querySelectorAll('.catalog-filter-chips .filter-chip');
    const playlistSelect = document.getElementById('catalog-playlist-filter');

    if (searchInput) {
      searchInput.addEventListener('input', debounce((e) => {
        state.set({
          catalogSearchQuery: e.target.value.trim().toLowerCase(),
          catalogPage: 1,
        });
        this.onFilterCriteriaChanged();
      }, 200));
    }

    if (clearSearchBtn && searchInput) {
      clearSearchBtn.addEventListener('click', () => {
        if (!searchInput.value && !state.get('catalogSearchQuery')) return;
        searchInput.value = '';
        state.set({
          catalogSearchQuery: '',
          catalogPage: 1,
        });
        this.onFilterCriteriaChanged();
      });
    }

    filterChips.forEach(chip => {
      chip.addEventListener('click', () => {
        if (state.get('catalogActiveFilter') === chip.dataset.filter) return;
        filterChips.forEach(c => c.classList.remove('active'));
        chip.classList.add('active');
        state.set({
          catalogActiveFilter: chip.dataset.filter || 'all',
          catalogPage: 1,
        });
        this.onFilterCriteriaChanged();
      });
    });

    if (playlistSelect) {
      playlistSelect.addEventListener('change', (e) => {
        state.set({
          catalogSelectedPlaylistId: e.target.value,
          catalogPage: 1,
        });
        this.onFilterCriteriaChanged();
      });
    }
  }

  initViewSwitcher() {
    if (typeof document === 'undefined') return;
    const tableBtn = document.getElementById('catalog-view-table-btn');
    const cardsBtn = document.getElementById('catalog-view-cards-btn');

    if (tableBtn && cardsBtn) {
      const currentView = state.get('catalogView') || 'table';
      tableBtn.classList.toggle('active', currentView === 'table');
      cardsBtn.classList.toggle('active', currentView === 'cards');

      tableBtn.addEventListener('click', () => {
        state.setCatalogView('table');
        tableBtn.classList.add('active');
        cardsBtn.classList.remove('active');
        this.render();
      });

      cardsBtn.addEventListener('click', () => {
        state.setCatalogView('cards');
        cardsBtn.classList.add('active');
        tableBtn.classList.remove('active');
        this.render();
      });
    }
  }

  onFilterCriteriaChanged() {
    const selectedIds = state.get('catalogSelectedIds') || new Set();
    if (selectedIds.size > 0) {
      state.set({ catalogSelectedIds: new Set() });
      toast.info('筛选已变更', '已重置选中项。');
    }
    this.render();
  }

  getSourceTracksForSection() {
    const section = state.get('catalogSection') || 'all';
    const allSongs = state.getCatalogRows();
    if (section === 'cloud') {
      return allSongs.filter(s => s.is_cloud || String(s.source_playlist_id) === 'cloud');
    }
    if (section === 'local') {
      return allSongs.filter(s => s.local_path || String(s.source_playlist_id) === 'local' || s.source === 'local');
    }
    if (section === 'all') return allSongs;

    const playlists = state.get('playlists') || [];
    const kindMap = new Map(playlists.map(p => [String(p.id), p.kind]));

    if (section === 'liked') {
      const likedPl = state.getRealLikedPlaylist();
      const likedId = likedPl ? String(likedPl.id) : null;
      return allSongs.filter(s => (likedId && String(s.source_playlist_id) === likedId) || s.liked);
    }
    if (section === 'self_created') {
      return allSongs.filter(s => {
        const kind = kindMap.get(String(s.source_playlist_id));
        return kind === 'self_created' || kind === 'owned';
      });
    }
    if (section === 'subscribed') {
      return allSongs.filter(s => kindMap.get(String(s.source_playlist_id)) === 'subscribed');
    }
    return allSongs;
  }

  isAlternative(song) {
    const current = state.get('currentPlayingSong');
    if (current && getTrackKey(current) === getTrackKey(song)) {
      const meta = state.get('audioMeta');
      return Boolean(meta?.is_alternative || ['alternative', 'external'].includes(meta?.source));
    }
    return Boolean(song.is_alternative || song.source_type === 'alternative');
  }

  ruleMatcher() {
    const rules = state.get('rules') || [];
    if (!rules.length) return () => [];
    const tracks = new Map(state.getRuleEvaluationPool().map(song => [getTrackKey(song), song]));
    const matches = new Map();
    return song => {
      const key = getTrackKey(song);
      if (!matches.has(key)) matches.set(key, RuleEngine.getMatchingRules(tracks.get(key) || song, rules));
      return matches.get(key);
    };
  }

  getFilteredSongs(matchRules) {
    const drill = this.drillStack.at(-1);
    if (drill) {
      return state.getRuleEvaluationPool().filter(song => drill.type === 'artist'
        ? artistNames(song).includes(drill.value) : song.album === drill.value);
    }
    const allSongs = this.getSourceTracksForSection();
    const selectedPlaylistId = state.get('catalogSelectedPlaylistId') || 'all';
    const activeFilter = state.get('catalogActiveFilter') || 'all';
    const searchQuery = state.get('catalogSearchQuery') || '';
    const likedPl = state.getRealLikedPlaylist();
    const likedPlId = likedPl ? String(likedPl.id) : null;

    return allSongs.filter(song => {
      // 1. Playlist Filter
      if (selectedPlaylistId !== 'all') {
        if (String(song.source_playlist_id) !== String(selectedPlaylistId)) {
          return false;
        }
      }

      // 2. Chip Filter
      if (activeFilter === 'liked' && (!likedPlId || String(song.source_playlist_id) !== likedPlId)) return false;
      if (activeFilter === 'unavailable' && song.availability !== 'unavailable') return false;
      if (activeFilter === 'vip' && song.availability !== 'vip') return false;
      if (activeFilter === 'alternative' && !this.isAlternative(song)) return false;
      if (activeFilter === 'relisten' && !state.isRelistenLater(song.id)) return false;
      if (activeFilter === 'rules') {
        matchRules ??= this.ruleMatcher();
        const matched = matchRules(song);
        if (matched.length === 0) return false;
      }

      // 3. Search Query
      if (searchQuery) {
        const titleMatch = (song.title || '').toLowerCase().includes(searchQuery);
        const artistMatch = formatArtists(song.artists).toLowerCase().includes(searchQuery);
        const albumMatch = (song.album || '').toLowerCase().includes(searchQuery);
        if (!titleMatch && !artistMatch && !albumMatch) return false;
      }

      return true;
    });
  }

  getSortedSongs(songs) {
    const sorted = [...songs];
    const sortCol = state.get('catalogSortColumn') || 'title';
    const sortDir = state.get('catalogSortDirection') || 'asc';
    const dir = sortDir === 'asc' ? 1 : -1;

    sorted.sort((a, b) => {
      let valA = a[sortCol];
      let valB = b[sortCol];

      if (sortCol === 'artist') {
        valA = formatArtists(a.artists);
        valB = formatArtists(b.artists);
      }

      if (typeof valA === 'string') {
        return valA.localeCompare(String(valB || '')) * dir;
      }
      return ((Number(valA) || 0) - (Number(valB) || 0)) * dir;
    });

    return sorted;
  }

  getCurrentPageSongs() {
    const filtered = this.getFilteredSongs();
    const sorted = this.getSortedSongs(filtered);
    const currentPage = state.get('catalogPage') || 1;
    const startIndex = (currentPage - 1) * this.pageSize;
    return sorted.slice(startIndex, startIndex + this.pageSize);
  }

  setSort(column) {
    if (!column) return;
    const currentCol = state.get('catalogSortColumn') || 'title';
    const currentDir = state.get('catalogSortDirection') || 'asc';

    if (currentCol === column) {
      state.set({ catalogSortDirection: currentDir === 'asc' ? 'desc' : 'asc' });
    } else {
      state.set({
        catalogSortColumn: column,
        catalogSortDirection: 'asc',
      });
    }
    state.set({ catalogPage: 1 });
    this.render();
  }

  drillInto(type, value) {
    const previous = this.drillStack.at(-1);
    if (previous?.type === type && previous.value === value) return;
    this.drillStack.push({
      type, value, previousLabel: this.queueOrigin(), page: state.get('catalogPage'),
      scroll: document.scrollingElement?.scrollTop || 0,
    });
    state.set({ catalogPage: 1 });
    this.onFilterCriteriaChanged();
    if (document.scrollingElement) document.scrollingElement.scrollTop = 0;
  }

  leaveDrill() {
    const previous = this.drillStack.pop();
    if (!previous) return;
    state.set({ catalogPage: previous.page });
    this.onFilterCriteriaChanged();
    if (document.scrollingElement) document.scrollingElement.scrollTop = previous.scroll;
  }

  drillLink(type, value) {
    return createElement('button', {
      className: 'catalog-drill-link', text: value,
      events: { click: e => { e.stopPropagation(); this.drillInto(type, value); } },
    });
  }

  artistLinks(song) {
    const names = artistNames(song);
    return names.length ? names.flatMap((name, i) => i
      ? [' / ', this.drillLink('artist', name)] : [this.drillLink('artist', name)]) : ['未知歌手'];
  }

  renderBanner() {
    const banner = document.getElementById('catalog-playlist-banner');
    if (!banner) return;
    const drill = this.drillStack.at(-1);
    const pid = String(state.get('catalogSelectedPlaylistId') || 'all');
    const playlist = !['all', 'cloud', 'local'].includes(pid) &&
      (state.get('playlists') || []).find(pl => String(pl.id) === pid);
    this.container?.classList.toggle('catalog-drilled', Boolean(drill));
    this.container?.classList.toggle('catalog-has-banner', Boolean(playlist || drill));
    banner.hidden = !playlist && !drill;
    banner.textContent = '';
    if (banner.hidden) return;
    const tracks = drill ? this._cachedSortedSongs : state.getCatalogRows().filter(s => String(s.source_playlist_id) === pid);
    const action = (text, click) => createElement('button', { className: 'btn btn-secondary btn-sm', text, events: { click } });
    const writable = this.getActiveWritableDeckPlaylist();
    if (drill) banner.appendChild(action(`← 返回${drill.previousLabel}`, () => this.leaveDrill()));
    else banner.appendChild(createElement('img', { className: 'queue-item-cover', attributes: {
      alt: '', src: getSizedCoverUrl(playlist.cover_url || tracks[0]?.cover_url, 96, '音'),
    } }));
    const badge = drill ? '' : playlist.kind === 'liked' ? '我喜欢的音乐' : writable ? '自建歌单' : '收藏歌单 · 只读';
    banner.appendChild(createElement('div', { className: 'library-banner-info', children: [
      createElement('strong', { text: drill ? this.queueOrigin() : playlist.name }),
      createElement('small', { text: [badge, trackDurationSummary(tracks)].filter(Boolean).join(' · ') }),
    ] }));
    for (const [label, mode] of [['播放全部', 'order'], ['均衡随机', 'shuffle']]) {
      const btn = action(label, () => player.playQueue(tracks, 0, mode, this.queueOrigin()));
      btn.disabled = !tracks.length;
      banner.appendChild(btn);
    }
    if (!drill && writable && !state.get('offlineMode')) {
      banner.appendChild(action('进入整理台', () => document.getElementById('catalog-enter-deck-btn')?.click()));
    }
  }

  queueOrigin() {
    const drill = this.drillStack.at(-1);
    if (drill) return `${drill.type === 'artist' ? '歌手' : '专辑'}：${drill.value}`;
    const section = state.get('catalogSection');
    const playlistId = state.get('catalogSelectedPlaylistId');
    const playlist = (state.get('playlists') || []).find(item => String(item.id) === String(playlistId));
    if (playlist?.kind === 'liked') return '我喜欢的音乐';
    if (playlist?.name) return `歌单《${playlist.name}》`;
    return ({ liked: '我喜欢的音乐', self_created: '自建歌单', subscribed: '收藏歌单', cloud: '云盘曲库', local: '本地音乐' })[section] || '音乐库';
  }

  playAllFiltered() {
    const list = this._cachedSortedSongs.length > 0 ? this._cachedSortedSongs : this.getSortedSongs(this.getFilteredSongs());
    if (list.length === 0) {
      toast.warning('无可播放曲目', '当前筛选结果为空。');
      return;
    }
    player.playQueue(list, 0, 'order', this.queueOrigin());
  }

  getActiveWritableDeckPlaylist() {
    const playlists = state.get('playlists') || [];
    const selectedPid = state.get('catalogSelectedPlaylistId') || 'all';
    const section = state.get('catalogSection') || 'all';

    if (selectedPid !== 'all') {
      const pl = playlists.find(p => String(p.id) === String(selectedPid));
      if (pl && (pl.kind === 'liked' || pl.kind === 'self_created' || pl.kind === 'owned')) {
        return pl;
      }
      return null;
    }
    if (section === 'liked') {
      return state.getRealLikedPlaylist();
    }
    if (section === 'self_created') {
      return playlists.find(p => p.kind === 'self_created' || p.kind === 'owned') || null;
    }
    if (section === 'all') {
      return state.getRealLikedPlaylist() || playlists.find(p => p.kind === 'self_created' || p.kind === 'owned') || null;
    }
    return null;
  }

  updateContextActionsBar(totalCount) {
    if (typeof document === 'undefined') return;
    const playAllBtn = document.getElementById('catalog-play-all-btn');
    const enterDeckBtn = document.getElementById('catalog-enter-deck-btn');
    const readonlyBadge = document.getElementById('catalog-readonly-badge');
    const section = state.get('catalogSection') || 'all';

    if (playAllBtn) {
      playAllBtn.disabled = totalCount === 0;
    }

    const writablePl = this.getActiveWritableDeckPlaylist();
    const isReadonlySection = ['subscribed', 'cloud', 'local'].includes(section) || state.get('offlineMode');

    if (readonlyBadge) {
      readonlyBadge.classList.toggle('hidden', !isReadonlySection);
    }

    if (enterDeckBtn) {
      if (writablePl && !state.get('offlineMode')) {
        enterDeckBtn.classList.remove('hidden');
        enterDeckBtn.dataset.playlistId = String(writablePl.id);
        enterDeckBtn.title = `前往歌单清理整理「${writablePl.name}」`;
      } else {
        enterDeckBtn.classList.add('hidden');
      }
    }
  }

  render() {
    if (!this.container) return;

    const matchRules = this.ruleMatcher();
    const filtered = this.getFilteredSongs(matchRules);
    const sorted = this.getSortedSongs(filtered);
    this._cachedFilteredSongs = filtered;
    this._cachedSortedSongs = sorted;

    const totalCount = sorted.length;
    const totalPages = Math.ceil(totalCount / this.pageSize) || 1;
    let currentPage = state.get('catalogPage') || 1;
    currentPage = Math.max(1, Math.min(currentPage, totalPages));
    state.set({ catalogPage: currentPage });

    const startIndex = (currentPage - 1) * this.pageSize;
    const pageItems = sorted.slice(startIndex, startIndex + this.pageSize);
    this._cachedPageKeys = pageItems.map(s => state.catalogSongKey(s));

    this.renderBanner();
    this.updateContextActionsBar(totalCount);
    this.updateSelectionBarIncremental();

    const viewType = state.get('catalogView') || 'table';
    const tableContainer = document.getElementById('catalog-table-wrapper');
    const cardsContainer = document.getElementById('catalog-cards-wrapper');

    if (tableContainer && cardsContainer) {
      if (viewType === 'table') {
        tableContainer.classList.remove('hidden');
        cardsContainer.classList.add('hidden');
        this.renderTable(pageItems, startIndex, matchRules);
      } else {
        cardsContainer.classList.remove('hidden');
        tableContainer.classList.add('hidden');
        this.renderCards(pageItems, startIndex);
      }
    }

    this.updatePaginationUI(totalCount, totalPages, currentPage);
  }

  /**
   * O(1) incremental checkbox toggle handler — never re-filters or re-sorts the whole catalog.
   */
  toggleRowSelection(songKey, checked, domElement) {
    const currentSet = state.get('catalogSelectedIds') || new Set();
    const nextSet = new Set(Array.from(currentSet).map(String));
    if (checked) {
      nextSet.add(songKey);
    } else {
      nextSet.delete(songKey);
    }
    state.set({ catalogSelectedIds: nextSet });
    if (domElement?.classList) {
      domElement.classList.toggle('selected', checked);
    }
    this.updateSelectionBarIncremental();
  }

  /**
   * O(page_size) incremental selection bar update using cached page keys (zero catalog re-scan).
   */
  updateSelectionBarIncremental() {
    if (typeof document === 'undefined') return;
    const bar = document.getElementById('catalog-selection-bar');
    const countEl = document.getElementById('catalog-selection-count');
    const selectAllCheckbox = document.getElementById('catalog-select-all');

    const selectedIds = state.get('catalogSelectedIds') || new Set();
    const count = selectedIds.size;

    if (bar && countEl) {
      if (count > 0) {
        bar.classList.remove('hidden');
        countEl.textContent = `已选择 ${count} 首歌曲`;
      } else {
        bar.classList.add('hidden');
      }
    }

    if (selectAllCheckbox) {
      const keys = this._cachedPageKeys || [];
      let selectedOnPage = 0;
      for (let i = 0; i < keys.length; i++) {
        if (selectedIds.has(keys[i])) selectedOnPage++;
      }
      selectAllCheckbox.checked = keys.length > 0 && selectedOnPage === keys.length;
      selectAllCheckbox.indeterminate = selectedOnPage > 0 && selectedOnPage < keys.length;
    }
  }

  updateSelectionBar() {
    this.updateSelectionBarIncremental();
  }

  refreshVisibleHeartIcons() {
    if (typeof document === 'undefined') return;
    document.querySelectorAll('[data-catalog-heart-id]').forEach(btn => {
      const songId = btn.dataset.catalogHeartId;
      const liked = state.isSongLiked(songId);
      btn.classList.toggle('is-liked', liked);
      btn.textContent = '';
      btn.appendChild(createIcon(liked ? 'heart-filled' : 'heart', { size: 14 }));
    });
  }

  renderTable(pageItems, startIndex = 0, matchRules = this.ruleMatcher()) {
    const tbody = document.getElementById('catalog-table-body');
    if (!tbody) return;
    tbody.textContent = '';

    if (this.cloudLoading && state.get('catalogSection') === 'cloud') {
      tbody.appendChild(createElement('tr', {
        children: [
          createElement('td', {
            attributes: { colspan: '8', style: 'text-align: center; padding: 48px;' },
            children: [createElement('div', { className: 'spinner' })],
          }),
        ],
      }));
      return;
    }

    if (pageItems.length === 0) {
      const section = state.get('catalogSection') || 'all';
      const emptyDesc = section === 'local'
        ? '请前往“设置与同步 - 偏好设置”添加本地音乐目录并扫描本地曲库。'
        : (section === 'cloud' ? '网易云云盘暂无已上传曲目。' : '请尝试修改搜索关键词或重置筛选条件。');
      const emptyRow = createElement('tr', {
        children: [
          createElement('td', {
            attributes: { colspan: '8', style: 'text-align: center; padding: 48px;' },
            children: [
              createElement('div', {
                className: 'empty-state',
                children: [
                  createElement('div', { className: 'empty-state-title', text: '无匹配歌曲' }),
                  createElement('div', { className: 'empty-state-desc', text: emptyDesc }),
                ],
              }),
            ],
          }),
        ],
      });
      tbody.appendChild(emptyRow);
      return;
    }

    const selectedIds = state.get('catalogSelectedIds') || new Set();
    const fragment = document.createDocumentFragment ? document.createDocumentFragment() : null;

    pageItems.forEach((song, idx) => {
      const songKey = state.catalogSongKey(song);
      const isSelected = selectedIds.has(songKey);
      const liked = state.isSongLiked(song);
      const writable = state.resolveDestructiveAction(song, song.source_playlist_id).valid;

      const checkbox = createElement('input', {
        className: 'checkbox-custom',
        attributes: { type: 'checkbox', 'aria-label': `选择 ${song.title || '歌曲'}` },
        events: {
          change: (e) => {
            this.toggleRowSelection(songKey, e.target.checked, row);
          },
        },
      });
      if (isSelected) checkbox.checked = true;

      const coverImg = createElement('img', {
        className: 'table-cover-thumb',
        attributes: {
          src: getSizedCoverUrl(song.cover_url, 96, '音'),
          alt: song.title || '封面',
          loading: 'lazy',
        },
      });

      const titleCol = createElement('div', {
        children: [
          createElement('div', {
            className: 'catalog-table-title-row',
            children: [
              createPlayingEqBars(),
              createElement('div', { className: 'table-song-title', text: song.title || '未知歌曲' }),
            ],
          }),
          createElement('div', {
            className: 'table-song-artist-mobile',
            children: [
              createElement('span', { className: 'table-song-inline-artists', children: this.artistLinks(song) }),
              song.album ? createElement('span', { className: 'table-song-inline-sep', text: ' · ' }) : null,
              song.album ? this.drillLink('album', song.album) : null,
            ],
          }),
        ],
      });

      const statusCol = createElement('div', { style: 'display: flex; gap: 4px; flex-wrap: wrap;' });
      if (song.is_cloud || String(song.source_playlist_id) === 'cloud') {
        statusCol.appendChild(createElement('span', { className: 'badge badge-source-cloud', text: '云盘' }));
      } else if (song.source === 'local' || String(song.source_playlist_id) === 'local') {
        statusCol.appendChild(createElement('span', { className: 'badge badge-source-local', text: '本地' }));
      }
      if (song.availability === 'unavailable') {
        statusCol.appendChild(createElement('span', { className: 'badge badge-danger', text: '无音源' }));
      } else if (song.availability === 'vip') {
        statusCol.appendChild(createElement('span', { className: 'badge badge-warning', text: 'VIP' }));
      }
      if (this.isAlternative(song)) {
        statusCol.appendChild(createElement('span', { className: 'badge badge-alt', text: '替代源' }));
      }
      if (state.isRelistenLater(song.id)) {
        statusCol.appendChild(createElement('span', { className: 'badge badge-muted', text: '稍后再听' }));
      }

      const matchedRules = matchRules(song);
      if (matchedRules.length > 0) {
        statusCol.appendChild(createElement('span', {
          className: 'badge badge-warning',
          text: matchedRules[0].name,
          attributes: { title: matchedRules.map(r => r.name).join(', ') },
        }));
      }

      const playBtn = createElement('button', {
        className: 'table-btn',
        attributes: { title: '播放此歌曲', 'aria-label': `播放 ${song.title || '歌曲'}` },
        events: {
          click: () => player.playQueue(this._cachedSortedSongs, startIndex + idx, 'order', this.queueOrigin()),
        },
        children: [createIcon('play', { size: 14 })],
      });

      const heartBtn = createElement('button', {
        className: `table-btn ${liked ? 'is-liked' : ''}`,
        dataset: { catalogHeartId: getTrackKey(song) || String(song.id || '') },
        attributes: { title: liked ? '取消红心' : '红心喜欢', 'aria-label': '红心' },
        events: { click: () => player.toggleLikeCurrentSong(song) },
        children: [createIcon(liked ? 'heart-filled' : 'heart', { size: 14 })],
      });

      const addPlBtn = createElement('button', {
        className: 'table-btn',
        attributes: { title: '添加到自建歌单', 'aria-label': '添加到歌单' },
        events: {
          click: () => {
            if (this.onAddToPlaylistCallback) this.onAddToPlaylistCallback(song);
          },
        },
        children: [createIcon('plus-list', { size: 14 })],
      });

      const actionChildren = [playBtn, quickNextButton(song), heartBtn, addPlBtn];
      if (writable) {
        const stageBtn = createElement('button', {
          className: 'table-btn table-btn-danger',
          attributes: { title: '加入待处理队列', 'aria-label': '移出' },
          events: {
            click: () => {
              const resolved = state.resolveDestructiveAction(song, song.source_playlist_id);
              if (resolved.valid) {
                if (state.stageBatchAction(song, resolved.action, resolved.playlistId, resolved.targetName)) {
                  toast.info('已加入待处理队列', `"${song.title}" 已加入待提交队列。`);
                } else {
                  toast.warning('未加入队列', '该操作已存在，或因未确定的远端结果被本地禁止重试。');
                }
              } else {
                toast.warning('不可移除', resolved.reason || '该歌曲无法执行云端移除。');
              }
            },
          },
          children: [createIcon('trash', { size: 14 })],
        });
        actionChildren.push(stageBtn);
      }

      const row = createElement('tr', {
        className: isSelected ? 'selected' : '',
        dataset: { trackKey: getTrackKey(song) },
        events: {
          contextmenu: (e) => openSongContextMenu(e, song, {
            source: 'catalog', origin: this.queueOrigin(),
            queue: this._cachedSortedSongs,
            index: startIndex + idx,
            onAddToPlaylist: this.onAddToPlaylistCallback,
          }),
        },
        children: [
          createElement('td', { className: 'col-check' }, [checkbox]),
          createElement('td', { className: 'col-cover' }, [coverImg]),
          createElement('td', { className: 'col-title' }, [titleCol]),
          createElement('td', { className: 'col-artist', children: this.artistLinks(song) }),
          createElement('td', { className: 'col-album', children: [song.album ? this.drillLink('album', song.album) : '-'] }),
          createElement('td', { className: 'col-duration', text: formatDuration(song) }),
          createElement('td', { className: 'col-status' }, [statusCol]),
          createElement('td', { className: 'col-actions' }, actionChildren),
        ],
      });

      if (fragment) fragment.appendChild(row);
      else tbody.appendChild(row);
    });

    if (fragment) tbody.appendChild(fragment);
    syncPlayingRowIndicators(state.get('currentPlayingSong'), Boolean(state.get('isPlaying')), tbody);
  }

  renderCards(pageItems, startIndex = 0) {
    const container = document.getElementById('catalog-cards-grid');
    if (!container) return;
    container.textContent = '';

    if (pageItems.length === 0) {
      container.appendChild(createElement('div', {
        className: 'empty-state',
        style: 'grid-column: 1 / -1;',
        children: [
          createElement('div', { className: 'empty-state-title', text: '无匹配歌曲' }),
          createElement('div', { className: 'empty-state-desc', text: '请尝试修改搜索关键词或重置筛选条件。' }),
        ],
      }));
      return;
    }

    const selectedIds = state.get('catalogSelectedIds') || new Set();
    const fragment = document.createDocumentFragment ? document.createDocumentFragment() : null;

    pageItems.forEach((song, idx) => {
      const songKey = state.catalogSongKey(song);
      const isSelected = selectedIds.has(songKey);

      const checkbox = createElement('input', {
        className: 'checkbox-custom card-corner-checkbox',
        attributes: { type: 'checkbox', 'aria-label': `选择 ${song.title || '歌曲'}` },
        events: {
          change: (e) => {
            this.toggleRowSelection(songKey, e.target.checked, card);
          },
        },
      });
      if (isSelected) checkbox.checked = true;

      const playBtn = createElement('button', {
        className: 'card-play-overlay-btn',
        attributes: { title: '播放', 'aria-label': `播放 ${song.title || '歌曲'}` },
        events: {
          click: (e) => {
            e.stopPropagation();
            player.playQueue(this._cachedSortedSongs, startIndex + idx, 'order', this.queueOrigin());
          },
        },
        children: [createIcon('play', { size: 14 })],
      });

      const coverWrapper = createElement('div', {
        className: 'catalog-card-cover-wrapper',
        children: [
          createElement('img', {
            className: 'catalog-card-cover',
            attributes: {
              src: getSizedCoverUrl(song.cover_url, 192, '音'),
              alt: song.title || '封面',
              loading: 'lazy',
            },
          }),
          playBtn,
          checkbox,
        ],
      });

      const body = createElement('div', {
        className: 'catalog-card-body',
        events: {
          click: () => this.openTrackDetailsModal(song),
        },
        children: [
          createElement('div', { className: 'catalog-card-title', text: song.title || '未知曲目', attributes: { title: song.title } }),
          createElement('div', { className: 'catalog-card-artist', children: this.artistLinks(song) }),
          song.album ? this.drillLink('album', song.album) : null,
          createElement('div', { className: 'catalog-card-meta', text: formatDuration(song) }),
          quickNextButton(song),
        ],
      });

      const card = createElement('div', {
        className: `catalog-song-card ${isSelected ? 'selected' : ''}`,
        dataset: { trackKey: getTrackKey(song) },
        events: {
          contextmenu: (e) => openSongContextMenu(e, song, {
            source: 'catalog', origin: this.queueOrigin(),
            queue: this._cachedSortedSongs,
            index: startIndex + idx,
            onAddToPlaylist: this.onAddToPlaylistCallback,
          }),
        },
        children: [coverWrapper, body],
      });

      if (fragment) fragment.appendChild(card);
      else container.appendChild(card);
    });

    if (fragment) container.appendChild(fragment);
    syncPlayingRowIndicators(state.get('currentPlayingSong'), Boolean(state.get('isPlaying')), container);
  }

  updatePaginationUI(totalCount, totalPages, currentPage) {
    if (typeof document === 'undefined') return;
    const infoEl = document.getElementById('catalog-pagination-info');
    const prevBtn = document.getElementById('catalog-page-prev');
    const nextBtn = document.getElementById('catalog-page-next');
    const currentEl = document.getElementById('catalog-page-current');

    const start = totalCount > 0 ? (currentPage - 1) * this.pageSize + 1 : 0;
    const end = Math.min(currentPage * this.pageSize, totalCount);

    if (infoEl) infoEl.textContent = `显示 ${start} - ${end} 首 / 共 ${totalCount} 首`;
    if (currentEl) currentEl.textContent = `第 ${currentPage} / ${totalPages} 页`;
    if (prevBtn) prevBtn.disabled = currentPage <= 1;
    if (nextBtn) nextBtn.disabled = currentPage >= totalPages;
  }

  playSelected() {
    const songs = this.selectedSongs();
    if (songs.length) player.playQueue(songs, 0, 'order', this.queueOrigin());
  }

  selectAllFiltered() {
    const filtered = this.getFilteredSongs();
    const nextSet = new Set(Array.from(state.get('catalogSelectedIds') || []).map(String));
    filtered.forEach(s => nextSet.add(state.catalogSongKey(s)));
    state.set({ catalogSelectedIds: nextSet });
    this.render();
  }

  selectCurrentPage(checked) {
    const pageSongs = this.getCurrentPageSongs();
    const nextSet = new Set(Array.from(state.get('catalogSelectedIds') || []).map(String));
    pageSongs.forEach(s => {
      const songKey = state.catalogSongKey(s);
      if (checked) nextSet.add(songKey);
      else nextSet.delete(songKey);
    });
    state.set({ catalogSelectedIds: nextSet });
    this.render();
  }

  clearSelection() {
    if (!state.get('catalogSelectedIds')?.size) return;
    state.set({ catalogSelectedIds: new Set() });
    this.render();
  }

  stageSelectedForPruning() {
    const songsToStage = this.selectedSongs();

    if (songsToStage.length === 0) return;

    let stagedCount = 0;
    let skippedCount = 0;

    songsToStage.forEach(song => {
      const resolved = state.resolveDestructiveAction(song, song.source_playlist_id);
      if (resolved.valid) {
        if (state.stageBatchAction(song, resolved.action, resolved.playlistId, resolved.targetName)) stagedCount++;
        else skippedCount++;
      } else {
        skippedCount++;
      }
    });

    this.clearSelection();
    if (skippedCount > 0) {
      toast.warning('部分歌曲已跳过', `已将 ${stagedCount} 首歌曲加入待提交队列，${skippedCount} 首未新增。`);
    } else {
      toast.success('已加入待处理队列', `已将 ${stagedCount} 首歌曲加入待提交队列。`);
    }
  }

  selectedSongs() {
    const keys = state.get('catalogSelectedIds') || new Set();
    return state.getCatalogRows().filter(song => keys.has(state.catalogSongKey(song)));
  }

  writableIds(songs) {
    return [...new Set(songs.filter(writableNeteaseTrack).map(song => Number(song.id)))];
  }

  async createInBatches(name, ids, scope) {
    const confirmed = new Set();
    const allFailed = [];
    let playlistId = null;
    for (let offset = 0; offset < ids.length; offset += 500) {
      const chunk = ids.slice(offset, offset + 500);
      const unsentIds = ids.slice(offset + chunk.length);
      const response = offset === 0 ? await api.createPlaylist(name, chunk, 0, scope)
        : await api.addTracksToPlaylist(playlistId, chunk, scope);
      const data = response?.data && typeof response.data === 'object' ? response.data : {};
      const hasSuccessfulArray = Array.isArray(data.successful_ids);
      const added = new Set(hasSuccessfulArray ? data.successful_ids.map(Number) : []);
      for (const id of chunk) if (added.has(Number(id))) confirmed.add(Number(id));
      const failedItems = Array.isArray(data.failed_items) ? data.failed_items : [];
      const failedIds = new Set(failedItems.map(item => Number(item?.id ?? item)));
      allFailed.push(...failedItems);
      if (offset === 0) playlistId = data.playlist_id ?? data.resource_id ?? null;

      const unaccounted = chunk.filter(id => !confirmed.has(Number(id)) && !failedIds.has(Number(id)));
      const isAmbiguousFailure = !response?.ok && (!response?.status || response.status >= 500 || response.status === 409 ||
        ['NETWORK_ERROR', 'TIMEOUT', 'UPSTREAM_TIMEOUT', 'UPSTREAM_UNAVAILABLE', 'UPSTREAM_HTTP_ERROR', 'OPERATION_PENDING'].includes(response.error?.code));
      const hasPendingOrUnknown = Boolean(data.pending_operation_ids?.length || data.unknown_items?.length);
      const isIncompleteOk = Boolean(response?.ok) && (!playlistId || !hasSuccessfulArray || unaccounted.length > 0);
      const hasCooldown = Boolean(data.cooldown_sec);

      if (isAmbiguousFailure || hasPendingOrUnknown || isIncompleteOk || hasCooldown) {
        const unknownItems = Array.isArray(data.unknown_items) && data.unknown_items.length
          ? data.unknown_items
          : (unaccounted.length ? unaccounted : chunk.filter(id => !confirmed.has(Number(id)))).map(id => ({
            id,
            code: response?.error?.code || (hasCooldown ? 'COOLDOWN' : 'INCOMPLETE_RECEIPT'),
            reason: response?.error?.message || '远端回执不完整或状态待核对',
          }));
        return {
          ok: false,
          ambiguous: true,
          error: response?.error || null,
          confirmedIds: [...confirmed],
          unknownItems,
          unsentIds,
          remainingIds: ids.filter(id => !confirmed.has(Number(id))),
          playlistId,
          data: {
            ...data,
            playlist_id: playlistId,
            successful_ids: [...confirmed],
            failed_items: allFailed,
            unknown_items: unknownItems,
            unsent_ids: unsentIds,
            pending_operation_ids: Array.isArray(data.pending_operation_ids) ? data.pending_operation_ids : [],
          },
        };
      }

      if (!response?.ok) {
        if (offset === 0) return response;
        return {
          ok: false,
          ambiguous: false,
          error: response.error,
          confirmedIds: [...confirmed],
          unsentIds,
          playlistId,
          data: {
            ...data,
            playlist_id: playlistId,
            successful_ids: [...confirmed],
            failed_items: allFailed,
            unsent_ids: unsentIds,
          },
        };
      }

      if (failedItems.length > 0 || offset + chunk.length >= ids.length) {
        return {
          ok: true,
          data: {
            ...data,
            playlist_id: playlistId,
            successful_ids: [...confirmed],
            failed_items: allFailed,
            unsent_ids: unsentIds,
          },
        };
      }
    }
  }

  openPlaylistCreateModal(songs = this.selectedSongs(), name = '') {
    const selectedIds = new Set(this.writableIds(songs));
    this.creatingRulePlaylist = Boolean(name);
    this.createPlaylistIds = [...selectedIds];
    const input = document.getElementById('create-playlist-name-input');
    if (input) input.value = name;
    if (selectedIds.size === 0) {
      toast.warning('没有可添加的云端曲目', `共命中 ${songs.length} 首，其中可写入云端歌单 0 首。`);
      return;
    }

    const countSpan = document.getElementById('create-playlist-song-count');
    if (countSpan) countSpan.textContent = `共命中 ${songs.length} 首（含可写入云端歌单 ${selectedIds.size} 首）` +
      (selectedIds.size > 500 ? '。超过单次上限 500，将自动创建后分批追加，遇风控立即停止。' : '');

    this.createPlaylistScope = state.writeScope();
    modal.open('#create-playlist-modal');
  }

  openTrackDetailsModal(song) {
    const titleEl = document.getElementById('track-modal-title');
    const artistEl = document.getElementById('track-modal-artist');
    const albumEl = document.getElementById('track-modal-album');
    const durationEl = document.getElementById('track-modal-duration');
    const sourceEl = document.getElementById('track-modal-source');
    const coverImg = document.getElementById('track-modal-cover');
    const playBtn = document.getElementById('track-modal-play-btn');
    const stageBtn = document.getElementById('track-modal-stage-btn');
    const addBtn = document.getElementById('track-modal-add-pl-btn');

    if (titleEl) titleEl.textContent = song.title || '未知曲目';
    if (artistEl) artistEl.textContent = formatArtists(song.artists);
    if (albumEl) albumEl.textContent = song.album || '单曲';
    if (durationEl) durationEl.textContent = formatDuration(song);
    if (sourceEl) sourceEl.textContent = song.source_playlist_name || '网易云歌单';

    if (coverImg) {
      coverImg.src = getSizedCoverUrl(song.cover_url, 192, '音');
      coverImg.alt = song.title || '封面';
    }

    if (playBtn) {
      playBtn.onclick = () => {
        player.playSong(song, true, { origin: this.queueOrigin() });
        modal.close('#track-details-modal');
      };
    }

    if (addBtn) {
      addBtn.onclick = () => {
        modal.close('#track-details-modal');
        if (this.onAddToPlaylistCallback) this.onAddToPlaylistCallback(song);
      };
    }

    if (stageBtn) {
      const resolved = state.resolveDestructiveAction(song, song.source_playlist_id);
      stageBtn.disabled = !resolved.valid;
      stageBtn.onclick = () => {
        if (resolved.valid) {
          if (!state.stageBatchAction(song, resolved.action, resolved.playlistId, resolved.targetName)) {
            toast.quiet('未新增待处理项'); return;
          }
          modal.close('#track-details-modal');
          toast.info('已加入待处理队列', `"${song.title}" 已加入待提交队列。`);
        } else {
          toast.warning('不可移除', resolved.reason || '该歌曲无法执行云端移除。');
        }
      };
    }

    modal.open('#track-details-modal');
  }
}

export const catalogManager = new CatalogManager();
