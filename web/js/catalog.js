/**
 * Pruner Studio - Searchable Catalog Workspace
 * Shared state between dense table and compact 5-column card views, 50 items/page,
 * track details modal, selection clearing on filter change with notice, and rules integration.
 */
import { api } from './api.js';
import { state } from './state.js';
import { player } from './player.js';
import { toast } from './toast.js';
import { modal } from './modal.js';
import { RuleEngine } from './rules.js';
import { createElement, formatArtists, formatDuration, getSafeImageUrl, isSafeUrl, debounce } from './utils.js';

class CatalogManager {
  constructor() {
    this.container = null;
    this.pageSize = 50;
  }

  init(containerId) {
    this.container = document.getElementById(containerId);
    this.initFilterEvents();
    this.initViewSwitcher();
  }

  initFilterEvents() {
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

  getFilteredSongs() {
    const allSongs = state.get('catalog') || [];
    const rules = state.get('rules') || [];
    const selectedPlaylistId = state.get('catalogSelectedPlaylistId') || 'all';
    const activeFilter = state.get('catalogActiveFilter') || 'all';
    const searchQuery = state.get('catalogSearchQuery') || '';

    return allSongs.filter(song => {
      // 1. Playlist Filter
      if (selectedPlaylistId !== 'all') {
        if (String(song.source_playlist_id) !== String(selectedPlaylistId)) {
          return false;
        }
      }

      // 2. Chip Filter
      const likedPl = state.getRealLikedPlaylist();
      const likedPlId = likedPl ? String(likedPl.id) : null;

      if (activeFilter === 'liked' && (!likedPlId || String(song.source_playlist_id) !== likedPlId)) return false;
      if (activeFilter === 'unavailable' && song.availability !== 'unavailable') return false;
      if (activeFilter === 'vip' && song.availability !== 'vip') return false;
      if (activeFilter === 'alternative' && !song.is_alternative && song.source_type !== 'alternative') return false;
      if (activeFilter === 'rules') {
        const matched = RuleEngine.getMatchingRules(song, rules);
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

  render() {
    if (!this.container) return;

    const filtered = this.getFilteredSongs();
    const sorted = this.getSortedSongs(filtered);

    const totalCount = sorted.length;
    const totalPages = Math.ceil(totalCount / this.pageSize) || 1;
    let currentPage = state.get('catalogPage') || 1;
    currentPage = Math.max(1, Math.min(currentPage, totalPages));
    state.set({ catalogPage: currentPage });

    const startIndex = (currentPage - 1) * this.pageSize;
    const pageItems = sorted.slice(startIndex, startIndex + this.pageSize);

    // Update Selection Bar
    this.updateSelectionBar(filtered);

    // Update View Containers
    const viewType = state.get('catalogView') || 'table';
    const tableContainer = document.getElementById('catalog-table-wrapper');
    const cardsContainer = document.getElementById('catalog-cards-wrapper');

    if (tableContainer && cardsContainer) {
      if (viewType === 'table') {
        tableContainer.classList.remove('hidden');
        cardsContainer.classList.add('hidden');
        this.renderTable(pageItems);
      } else {
        cardsContainer.classList.remove('hidden');
        tableContainer.classList.add('hidden');
        this.renderCards(pageItems);
      }
    }

    this.updatePaginationUI(totalCount, totalPages, currentPage);
  }

  renderTable(pageItems) {
    const tbody = document.getElementById('catalog-table-body');
    if (!tbody) return;
    tbody.textContent = ''; // Safe clear

    if (pageItems.length === 0) {
      const emptyRow = createElement('tr', {
        children: [
          createElement('td', {
            attributes: { colspan: '8', style: 'text-align: center; padding: 48px;' },
            children: [
              createElement('div', {
                className: 'empty-state',
                children: [
                  createElement('div', { className: 'empty-state-title', text: '无匹配歌曲' }),
                  createElement('div', { className: 'empty-state-desc', text: '请尝试修改搜索关键词或重置筛选条件。' }),
                ]
              })
            ]
          })
        ]
      });
      tbody.appendChild(emptyRow);
      return;
    }

    const selectedIds = state.get('catalogSelectedIds') || new Set();
    const rules = state.get('rules') || [];

    pageItems.forEach(song => {
      const songKey = state.catalogSongKey(song);
      const isSelected = selectedIds.has(songKey);

      // Checkbox
      const checkbox = createElement('input', {
        className: 'checkbox-custom',
        attributes: { type: 'checkbox', 'aria-label': `选择 ${song.title || '歌曲'}` },
        events: {
          change: (e) => {
            const nextSet = new Set(Array.from(state.get('catalogSelectedIds') || []).map(String));
            if (e.target.checked) {
              nextSet.add(songKey);
            } else {
              nextSet.delete(songKey);
            }
            state.set({ catalogSelectedIds: nextSet });
            this.updateSelectionBar(this.getFilteredSongs());
            row.classList.toggle('selected', e.target.checked);
          }
        }
      });
      if (isSelected) checkbox.checked = true;

      // Cover thumbnail
      const coverImg = createElement('img', {
        className: 'table-cover-thumb',
        attributes: {
          src: isSafeUrl(song.cover_url) ? song.cover_url : getSafeImageUrl('', '音'),
          alt: song.title || '封面',
          loading: 'lazy',
        }
      });

      // Title & Artists
      const titleCol = createElement('div', {
        children: [
          createElement('div', { className: 'table-song-title', text: song.title || '未知歌曲' }),
          createElement('div', { className: 'table-song-artist-mobile', text: formatArtists(song.artists) }),
        ]
      });

      // Status Badges
      const statusCol = createElement('div', { style: 'display: flex; gap: 4px; flex-wrap: wrap;' });
      if (song.availability === 'unavailable') {
        statusCol.appendChild(createElement('span', { className: 'badge badge-danger', text: '无音源' }));
      } else if (song.availability === 'vip') {
        statusCol.appendChild(createElement('span', { className: 'badge badge-warning', text: 'VIP' }));
      }

      if (song.is_alternative || song.source_type === 'alternative') {
        statusCol.appendChild(createElement('span', { className: 'badge badge-alt', text: '替代源' }));
      }

      const matchedRules = RuleEngine.getMatchingRules(song, rules);
      if (matchedRules.length > 0) {
        statusCol.appendChild(createElement('span', {
          className: 'badge badge-warning',
          text: matchedRules[0].name,
          attributes: { title: matchedRules.map(r => r.name).join(', ') }
        }));
      }

      // Actions
      const playBtn = createElement('button', {
        className: 'table-btn',
        text: '▶',
        attributes: { title: '播放此歌曲' },
        events: { click: () => player.playSong(song, true) }
      });

      const stageBtn = createElement('button', {
        className: 'table-btn table-btn-danger',
        text: '移除',
        attributes: { title: '加入待处理队列' },
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
          }
        }
      });

      const row = createElement('tr', {
        className: isSelected ? 'selected' : '',
        children: [
          createElement('td', { className: 'col-check' }, [checkbox]),
          createElement('td', { className: 'col-cover' }, [coverImg]),
          createElement('td', { className: 'col-title' }, [titleCol]),
          createElement('td', { className: 'col-artist', text: formatArtists(song.artists) }),
          createElement('td', { className: 'col-album', text: song.album || '-' }),
          createElement('td', { className: 'col-duration', text: formatDuration(song.duration) }),
          createElement('td', { className: 'col-status' }, [statusCol]),
          createElement('td', { className: 'col-actions' }, [playBtn, stageBtn]),
        ]
      });

      tbody.appendChild(row);
    });
  }

  renderCards(pageItems) {
    const container = document.getElementById('catalog-cards-grid');
    if (!container) return;
    container.textContent = ''; // Safe clear

    if (pageItems.length === 0) {
      container.appendChild(createElement('div', {
        className: 'empty-state',
        style: 'grid-column: 1 / -1;',
        children: [
          createElement('div', { className: 'empty-state-title', text: '无匹配歌曲' }),
          createElement('div', { className: 'empty-state-desc', text: '请尝试修改搜索关键词或重置筛选条件。' }),
        ]
      }));
      return;
    }

    const selectedIds = state.get('catalogSelectedIds') || new Set();

    pageItems.forEach(song => {
      const songKey = state.catalogSongKey(song);
      const isSelected = selectedIds.has(songKey);

      // Checkbox in card corner
      const checkbox = createElement('input', {
        className: 'checkbox-custom card-corner-checkbox',
        attributes: { type: 'checkbox', 'aria-label': `选择 ${song.title || '歌曲'}` },
        events: {
          change: (e) => {
            const nextSet = new Set(Array.from(state.get('catalogSelectedIds') || []).map(String));
            if (e.target.checked) {
              nextSet.add(songKey);
            } else {
              nextSet.delete(songKey);
            }
            state.set({ catalogSelectedIds: nextSet });
            this.updateSelectionBar(this.getFilteredSongs());
            card.classList.toggle('selected', e.target.checked);
          }
        }
      });
      if (isSelected) checkbox.checked = true;

      // Play Overlay Button on cover
      const playBtn = createElement('button', {
        className: 'card-play-overlay-btn',
        text: '▶',
        attributes: { title: '播放' },
        events: {
          click: (e) => {
            e.stopPropagation();
            player.playSong(song, true);
          }
        }
      });

      // Cover
      const coverWrapper = createElement('div', {
        className: 'catalog-card-cover-wrapper',
        children: [
          createElement('img', {
            className: 'catalog-card-cover',
            attributes: {
              src: isSafeUrl(song.cover_url) ? song.cover_url : getSafeImageUrl('', '音'),
              alt: song.title || '封面',
              loading: 'lazy',
            }
          }),
          playBtn,
          checkbox,
        ]
      });

      // Card Meta / Title (opens details modal)
      const body = createElement('div', {
        className: 'catalog-card-body',
        events: {
          click: () => this.openTrackDetailsModal(song)
        },
        children: [
          createElement('div', { className: 'catalog-card-title', text: song.title || '未知曲目', attributes: { title: song.title } }),
          createElement('div', { className: 'catalog-card-artist', text: formatArtists(song.artists), attributes: { title: formatArtists(song.artists) } }),
          createElement('div', { className: 'catalog-card-meta', text: formatDuration(song.duration) }),
        ]
      });

      const card = createElement('div', {
        className: `catalog-song-card ${isSelected ? 'selected' : ''}`,
        children: [coverWrapper, body]
      });

      container.appendChild(card);
    });
  }

  updateSelectionBar(filteredSongs) {
    const bar = document.getElementById('catalog-selection-bar');
    const countEl = document.getElementById('catalog-selection-count');
    const selectAllCheckbox = document.getElementById('catalog-select-all');

    const selectedIds = state.get('catalogSelectedIds') || new Set();
    const count = selectedIds.size;

    if (!bar || !countEl) return;

    if (count > 0) {
      bar.classList.remove('hidden');
      countEl.textContent = `已选择 ${count} 首歌曲`;
    } else {
      bar.classList.add('hidden');
    }

    if (selectAllCheckbox) {
      const pageSongs = this.getCurrentPageSongs();
      selectAllCheckbox.checked = pageSongs.length > 0 && pageSongs.every(s => selectedIds.has(state.catalogSongKey(s)));
      selectAllCheckbox.indeterminate = pageSongs.some(s => selectedIds.has(state.catalogSongKey(s))) && !selectAllCheckbox.checked;
    }
  }

  updatePaginationUI(totalCount, totalPages, currentPage) {
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
    state.set({ catalogSelectedIds: new Set() });
    this.render();
  }

  stageSelectedForPruning() {
    const rawSelected = state.get('catalogSelectedIds') || new Set();
    const selectionKeys = new Set(Array.from(rawSelected).map(String));
    const allSongs = state.get('catalog') || [];
    const songsToStage = allSongs.filter(s => selectionKeys.has(state.catalogSongKey(s)));

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
      toast.warning('部分歌曲已跳过', `已将 ${stagedCount} 首歌曲加入待提交队列，${skippedCount} 首本地导入或无远端目标的歌曲被跳过。`);
    } else {
      toast.success('已加入待处理队列', `已将 ${stagedCount} 首歌曲加入待提交队列。`);
    }
  }

  openPlaylistCreateModal() {
    const selectedIds = state.get('catalogSelectedIds') || new Set();
    if (selectedIds.size === 0) {
      toast.warning('请先选择歌曲', '请勾选至少一首歌曲。');
      return;
    }

    const countSpan = document.getElementById('create-playlist-song-count');
    if (countSpan) countSpan.textContent = String(selectedIds.size);

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

    if (titleEl) titleEl.textContent = song.title || '未知曲目';
    if (artistEl) artistEl.textContent = formatArtists(song.artists);
    if (albumEl) albumEl.textContent = song.album || '单曲';
    if (durationEl) durationEl.textContent = formatDuration(song.duration);
    if (sourceEl) sourceEl.textContent = song.source_playlist_name || '网易云歌单';

    if (coverImg) {
      coverImg.src = isSafeUrl(song.cover_url) ? song.cover_url : getSafeImageUrl('', '音');
      coverImg.alt = song.title || '封面';
    }

    if (playBtn) {
      playBtn.onclick = () => {
        player.playSong(song, true);
        modal.close('#track-details-modal');
      };
    }

    if (stageBtn) {
      stageBtn.onclick = () => {
        const resolved = state.resolveDestructiveAction(song, song.source_playlist_id);
        if (resolved.valid) {
          state.stageBatchAction(song, resolved.action, resolved.playlistId, resolved.targetName);
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
