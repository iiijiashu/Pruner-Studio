/**
 * Pruner Studio - Main Application Bootstrap
 * Orchestrates views (整理 / 曲库 / 云端), resolves startup hydration races,
 * manages the pending action drawer, and connects UI events.
 */
import { api } from './api.js';
import { state } from './state.js';
import { auth } from './auth.js';
import { player } from './player.js';
import { deckManager } from './deck.js';
import { catalogManager } from './catalog.js';
import { rulesManager } from './rules.js';
import { cloudManager } from './cloud.js';
import { importManager } from './import.js';
import { modal } from './modal.js';
import { toast } from './toast.js';
import { lyricsManager } from './lyrics.js';
import { createElement, formatDuration, formatArtists } from './utils.js';

export class App {
  constructor() {
    this.currentTab = 'deck';
    this.createPlaylistPending = false;
    this.autoSyncGeneration = null;
    this.initialDataGeneration = 0;
    this.pendingDrawerRefreshTimer = null;
  }

  async init() {
    console.log('Initializing Pruner Studio...');

    // 1. Initialize view managers
    deckManager.init('deck-workspace-container');
    catalogManager.init('catalog-view-panel');
    rulesManager.init('rules-list-container');
    cloudManager.init('cloud-view-panel');
    importManager.init('import-dropzone', 'import-file-input');
    lyricsManager.init('lyrics-drawer-scroller', (seconds) => player.seek(seconds));

    // 2. Setup navigation, pending drawer, and UI listeners
    this.initNavigation();
    this.initPendingDrawer();
    this.initDeckControls();
    this.initCatalogControls();
    this.initCloudControls();
    this.initModalsAndForms();

    // 3. Listen to state changes
    state.on('catalog:loaded', (catalog) => {
      this.onCatalogUpdated(catalog, true);
    });

    state.on('catalog:updated', (catalog) => {
      this.onCatalogUpdated(catalog, false);
    });

    state.on('auth:success', async () => {
      this.invalidateInitialDataLoads();
      deckManager.resetSessionSnapshot();
      await this.loadInitialData();
    });

    state.on('auth:logout', () => {
      this.invalidateInitialDataLoads();
      state.cancelAllCommits();
      deckManager.resetSessionSnapshot();
      state.set({
        catalog: [],
        deckQueue: [],
        playlists: [],
        selectedPlaylistId: null,
        hasUnknownResult: false,
        destructiveFrozen: false,
        unknownResultDetail: null,
        pendingOperations: [],
      });
      deckManager.render();
      catalogManager.render();
      cloudManager.renderPlaylists([]);
    });

    state.on('change:pendingCommits', () => {
      this.updatePendingHeaderBadge();
    });

    state.on('change:batchQueue', () => {
      this.updatePendingHeaderBadge();
    });

    window.addEventListener('switch-tab', (e) => {
      this.switchTab(e.detail);
    });

    // 4. Check session and load initial data only if authenticated
    const isAuth = await auth.checkSession();
    if (isAuth) {
      await this.loadInitialData();
    } else {
      state.set({ catalogLoading: false, catalog: [], playlists: [] });
      deckManager.render();
      catalogManager.render();
      cloudManager.renderPlaylists([]);
    }
  }

  /**
   * Load initial data coordinating playlists and catalog regardless of arrival order
   */
  async loadInitialData() {
    if (!state.get('authenticated')) return;
    const generation = ++this.initialDataGeneration;
    const userId = state.getUserId();
    const isCurrent = () => (
      generation === this.initialDataGeneration
      && state.get('authenticated')
      && state.getUserId() === userId
    );
    state.set({ catalogLoading: true, catalogError: null });

    try {
      // Parallel fetch playlists, rules, catalog
      const [playlistsRes, rulesRes, catalogRes] = await Promise.allSettled([
        api.getPlaylists(),
        rulesManager.loadRules(isCurrent),
        api.getCatalog(),
      ]);
      if (!isCurrent()) return;

      // 1. Process playlists
      let playlists = [];
      let playlistLoadError = null;
      if (playlistsRes.status === 'fulfilled' && playlistsRes.value?.ok && playlistsRes.value?.data) {
        playlists = Array.isArray(playlistsRes.value.data.playlists)
          ? playlistsRes.value.data.playlists
          : (Array.isArray(playlistsRes.value.data) ? playlistsRes.value.data : []);
        const canonicalId = state.resolveCanonicalPlaylistId(playlists);
        state.set({ playlists, selectedPlaylistId: canonicalId });
        cloudManager.renderPlaylists(playlists);
        cloudManager.populatePlaylistDropdowns(playlists, canonicalId);
      } else if (playlistsRes.status === 'fulfilled') {
        playlistLoadError = playlistsRes.value?.error?.message || '歌单读取失败。';
      } else {
        playlistLoadError = playlistsRes.reason?.message || '歌单读取失败。';
      }

      // 2. Process catalog
      let tracks = [];
      let catalogLoadError = null;
      if (catalogRes.status === 'fulfilled' && catalogRes.value?.ok && catalogRes.value?.data) {
        tracks = Array.isArray(catalogRes.value.data.tracks)
          ? catalogRes.value.data.tracks
          : (Array.isArray(catalogRes.value.data) ? catalogRes.value.data : []);
        state.set({ catalog: tracks });
      } else if (catalogRes.status === 'fulfilled') {
        catalogLoadError = catalogRes.value?.error?.message || '曲库读取失败。';
      } else {
        catalogLoadError = catalogRes.reason?.message || '曲库读取失败。';
      }

      const initialLoadError = catalogLoadError || playlistLoadError;
      state.set({ catalogLoading: false, catalogError: initialLoadError });

      // 3. Auto-sync selected liked playlist only when authenticated and uncached:
      // Auto-syncs as long as local catalog has no tracks for the real liked playlist ID,
      // even if catalog already contains other/imported songs.
      const likedPl = state.getRealLikedPlaylist();
      const hasLikedTracksInCatalog = likedPl && tracks.some(s => String(s.source_playlist_id) === String(likedPl.id));

      if (!initialLoadError && state.get('authenticated') && likedPl && !hasLikedTracksInCatalog && this.autoSyncGeneration !== generation) {
        this.autoSyncGeneration = generation;
        toast.info('正在初次同步', '正在拉取我喜欢的音乐曲库...');
        try {
          const syncRes = await api.syncPlaylist(likedPl.id);
          if (!isCurrent()) return;
          if (syncRes.ok) {
            const freshCatRes = await api.getCatalog();
            if (!isCurrent()) return;
            if (freshCatRes.ok && freshCatRes.data) {
              const freshTracks = Array.isArray(freshCatRes.data.tracks) ? freshCatRes.data.tracks : freshCatRes.data;
              state.set({ catalog: freshTracks });
              tracks = freshTracks;
            }
          }
        } finally {
          if (this.autoSyncGeneration === generation) this.autoSyncGeneration = null;
        }
      }

      this.onCatalogUpdated(tracks, true);
    } catch (err) {
      if (!isCurrent()) return;
      console.warn('Initial data load error:', err);
      state.set({ catalogLoading: false, catalogError: err.message });
      deckManager.render();
    }
  }

  invalidateInitialDataLoads() {
    this.initialDataGeneration += 1;
  }

  onCatalogUpdated(catalog, reloadDeck = false) {
    if (reloadDeck || (state.get('deckQueue') || []).length === 0) {
      deckManager.reloadDeck();
    }
    catalogManager.render();
    rulesManager.render();
    cloudManager.renderGroupedBatchQueue();
    this.updatePendingHeaderBadge();
  }

  initNavigation() {
    const navButtons = document.querySelectorAll('.nav-tab-btn');
    navButtons.forEach(btn => {
      btn.addEventListener('click', () => {
        const tab = btn.dataset.tab;
        if (tab) this.switchTab(tab);
      });
    });
  }

  switchTab(tabName) {
    this.currentTab = tabName;
    state.set({ activeTab: tabName });

    // Update nav tab button active states
    document.querySelectorAll('.nav-tab-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.tab === tabName);
      btn.setAttribute('aria-selected', btn.dataset.tab === tabName ? 'true' : 'false');
    });

    // Switch active view panel
    document.querySelectorAll('.view-panel').forEach(panel => {
      panel.classList.toggle('active', panel.id === `view-${tabName}`);
    });

    // Bottom docked player is active only in Catalog workspace
    const playerBar = document.getElementById('catalog-player-bar');
    if (playerBar) {
      playerBar.classList.toggle('active', tabName === 'catalog');
    }

    // Tab-specific render
    if (tabName === 'deck') {
      deckManager.render();
    } else if (tabName === 'catalog') {
      catalogManager.render();
    } else if (tabName === 'cloud') {
      cloudManager.renderPlaylists(state.get('playlists') || []);
      cloudManager.renderGroupedBatchQueue();
      cloudManager.renderLogs();
    }
  }

  initPendingDrawer() {
    const triggerBtn = document.getElementById('header-pending-btn');
    const drawer = document.getElementById('pending-actions-drawer');
    const closeBtn = document.getElementById('pending-drawer-close-btn');
    const confirmBtn = document.getElementById('drawer-batch-confirm-btn');

    if (triggerBtn && drawer) {
      triggerBtn.addEventListener('click', () => {
        const isOpen = drawer.classList.toggle('open');
        state.set({ isPendingDrawerOpen: isOpen });
        if (isOpen) {
          this.renderPendingDrawer();
          this.startPendingDrawerRefresh();
        } else {
          this.stopPendingDrawerRefresh();
        }
      });
    }

    if (closeBtn && drawer) {
      closeBtn.addEventListener('click', () => {
        drawer.classList.remove('open');
        state.set({ isPendingDrawerOpen: false });
        this.stopPendingDrawerRefresh();
      });
    }

    if (confirmBtn) {
      confirmBtn.addEventListener('click', () => {
        if (drawer) drawer.classList.remove('open');
        cloudManager.openBatchConfirmationModal();
      });
    }
  }

  updatePendingHeaderBadge() {
    const badge = document.getElementById('header-pending-count');
    const btn = document.getElementById('header-pending-btn');
    const pendingCommits = state.get('pendingCommits') || [];
    const batchQueue = state.get('batchQueue') || [];
    const total = pendingCommits.length + batchQueue.length;

    if (badge) badge.textContent = String(total);
    if (btn) btn.classList.toggle('has-pending', total > 0);

    const drawer = document.getElementById('pending-actions-drawer');
    if (drawer && drawer.classList.contains('open')) {
      this.renderPendingDrawer();
      if (pendingCommits.length > 0) this.startPendingDrawerRefresh();
      else this.stopPendingDrawerRefresh();
    }
  }

  startPendingDrawerRefresh() {
    if (this.pendingDrawerRefreshTimer || (state.get('pendingCommits') || []).length === 0) return;
    this.pendingDrawerRefreshTimer = setInterval(() => {
      const drawer = document.getElementById('pending-actions-drawer');
      if (!drawer?.classList.contains('open') || (state.get('pendingCommits') || []).length === 0) {
        this.stopPendingDrawerRefresh();
        return;
      }
      this.renderPendingDrawer();
    }, 250);
  }

  stopPendingDrawerRefresh() {
    if (!this.pendingDrawerRefreshTimer) return;
    clearInterval(this.pendingDrawerRefreshTimer);
    this.pendingDrawerRefreshTimer = null;
  }

  renderPendingDrawer() {
    const commitsContainer = document.getElementById('drawer-soft-commits-list');
    const batchContainer = document.getElementById('drawer-batch-list');
    const confirmBtn = document.getElementById('drawer-batch-confirm-btn');

    const pendingCommits = state.get('pendingCommits') || [];
    const batchQueue = state.get('batchQueue') || [];

    if (confirmBtn) {
      confirmBtn.disabled = batchQueue.length === 0;
    }

    // Render soft commits countdowns
    if (commitsContainer) {
      commitsContainer.textContent = '';
      if (pendingCommits.length === 0) {
        commitsContainer.appendChild(createElement('div', {
          className: 'drawer-empty-hint',
          text: '暂无正在倒计时的即时同步项'
        }));
      } else {
        pendingCommits.forEach(c => {
          const remainingSecs = Math.max(0, ((c.expiresAt - Date.now()) / 1000)).toFixed(1);
          const cancelBtn = createElement('button', {
            className: 'btn btn-ghost btn-sm text-danger',
            text: '取消 [Z]',
            events: {
              click: () => state.cancelCommitById(c.id)
            }
          });

          const row = createElement('div', {
            className: 'drawer-item-row countdown-row',
            children: [
              createElement('div', {
                children: [
                  createElement('div', { className: 'drawer-item-title', text: c.song.title || '未知歌曲' }),
                  createElement('div', { className: 'drawer-item-meta', text: `${c.targetName} · ${c.action === 'unlike' ? '取消喜欢' : '移出歌单'}` }),
                ]
              }),
              createElement('div', {
                style: 'display: flex; align-items: center; gap: 8px;',
                children: [
                  createElement('span', { className: 'countdown-pill', text: `${remainingSecs}s` }),
                  cancelBtn
                ]
              })
            ]
          });
          commitsContainer.appendChild(row);
        });
      }
    }

    // Render staged batch actions
    if (batchContainer) {
      batchContainer.textContent = '';
      if (batchQueue.length === 0) {
        batchContainer.appendChild(createElement('div', {
          className: 'drawer-empty-hint',
          text: '暂无待确认的批量操作'
        }));
      } else {
        batchQueue.slice(0, 50).forEach(item => {
          const removeBtn = createElement('button', {
            className: 'btn btn-ghost btn-sm',
            text: '×',
            events: {
              click: () => {
                state.unstageBatchAction(item.song.id, item.playlistId);
                this.renderPendingDrawer();
              }
            }
          });

          const row = createElement('div', {
            className: 'drawer-item-row',
            children: [
              createElement('div', {
                children: [
                  createElement('div', { className: 'drawer-item-title', text: item.song.title || '未知歌曲' }),
                  createElement('div', {
                    className: 'drawer-item-meta',
                    text: `${item.targetName} · ${item.status === 'unknown' ? '待只读核对' : (item.action === 'unlike' ? '取消喜欢' : '移出歌单')}`,
                  }),
                ]
              }),
              removeBtn
            ]
          });
          batchContainer.appendChild(row);
        });
      }
    }
  }

  initDeckControls() {
    const playlistSelect = document.getElementById('deck-playlist-select');
    const filterSelect = document.getElementById('deck-filter-select');
    const orderSelect = document.getElementById('deck-order-select');
    const modeToggle = document.getElementById('deck-mode-toggle');
    const resetProgressBtn = document.getElementById('deck-reset-progress-btn');

    // Deck action buttons
    const dislikeBtn = document.getElementById('deck-dislike-btn');
    const likeBtn = document.getElementById('deck-like-btn');
    const skipBtn = document.getElementById('deck-skip-btn');
    const playBtn = document.getElementById('deck-play-btn');
    const chorusBtn = document.getElementById('deck-chorus-jump-btn');
    const undoBtn = document.getElementById('deck-undo-btn');

    if (playlistSelect) {
      playlistSelect.addEventListener('change', (e) => {
        state.set({ selectedPlaylistId: e.target.value });
        deckManager.reloadDeck();
      });
    }

    if (filterSelect) {
      filterSelect.addEventListener('change', (e) => {
        state.set({ deckFilter: e.target.value });
        deckManager.reloadDeck();
      });
    }

    if (orderSelect) {
      orderSelect.addEventListener('change', (e) => {
        state.set({ deckOrder: e.target.value });
        deckManager.reloadDeck();
      });
    }

    if (modeToggle) {
      const updateModeBtn = () => {
        const isInstant = state.get('instantModeEnabled');
        modeToggle.textContent = isInstant ? '模式: 立即同步 (3秒)' : '模式: 稍后确认 (批处理)';
        modeToggle.classList.toggle('mode-instant', isInstant);
      };
      updateModeBtn();

      modeToggle.addEventListener('click', () => {
        const next = !state.get('instantModeEnabled');
        state.setInstantMode(next);
        updateModeBtn();
        toast.info('整理模式切换', next ? '已切换至 3 秒本地倒计时即时同步模式。' : '已切换至稍后统一确认批处理模式。');
      });
    }

    if (resetProgressBtn) {
      resetProgressBtn.addEventListener('click', () => {
        const pid = state.get('selectedPlaylistId');
        const filter = state.get('deckFilter');
        const order = state.get('deckOrder');
        state.resetReviewProgress(pid, filter, order);
        deckManager.reloadDeck();
        toast.info('已重置进度', '当前歌单与筛选条件的整理进度已重置。');
      });
    }

    if (dislikeBtn) dislikeBtn.addEventListener('click', () => deckManager.handleDestructiveAction());
    if (likeBtn) likeBtn.addEventListener('click', () => deckManager.handleKeepAction());
    if (skipBtn) skipBtn.addEventListener('click', () => deckManager.handleSkipToEndAction());
    if (playBtn) playBtn.addEventListener('click', () => player.togglePlay());
    if (chorusBtn) chorusBtn.addEventListener('click', () => player.jumpToChorus());
    if (undoBtn) undoBtn.addEventListener('click', () => deckManager.handleUndoAction());
  }

  initCatalogControls() {
    // Open Rules Drawer Button
    const rulesDrawerBtn = document.getElementById('catalog-open-rules-btn');
    const rulesDrawerCloseBtn = document.getElementById('rules-drawer-close-btn');
    if (rulesDrawerBtn) {
      rulesDrawerBtn.addEventListener('click', () => rulesManager.openDrawer());
    }
    if (rulesDrawerCloseBtn) {
      rulesDrawerCloseBtn.addEventListener('click', () => rulesManager.closeDrawer());
    }

    // Catalog Bulk Actions
    const selectAllBtn = document.getElementById('catalog-select-all-btn');
    const clearSelectBtn = document.getElementById('catalog-clear-select-btn');
    const stagePruneBtn = document.getElementById('catalog-stage-prune-btn');
    const createPlaylistBtn = document.getElementById('catalog-create-playlist-btn');

    if (selectAllBtn) selectAllBtn.addEventListener('click', () => catalogManager.selectAllFiltered());
    if (clearSelectBtn) clearSelectBtn.addEventListener('click', () => catalogManager.clearSelection());
    if (stagePruneBtn) stagePruneBtn.addEventListener('click', () => catalogManager.stageSelectedForPruning());
    if (createPlaylistBtn) createPlaylistBtn.addEventListener('click', () => catalogManager.openPlaylistCreateModal());

    const selectPageCheckbox = document.getElementById('catalog-select-all');
    if (selectPageCheckbox) {
      selectPageCheckbox.addEventListener('change', (e) => catalogManager.selectCurrentPage(e.target.checked));
    }

    document.querySelectorAll('.catalog-table th.sortable').forEach(th => {
      th.addEventListener('click', () => catalogManager.setSort(th.dataset.sort));
    });

    // Pagination buttons
    const prevBtn = document.getElementById('catalog-page-prev');
    const nextBtn = document.getElementById('catalog-page-next');
    if (prevBtn) prevBtn.addEventListener('click', () => {
      const page = state.get('catalogPage') || 1;
      state.set({ catalogPage: Math.max(1, page - 1) });
      catalogManager.render();
    });
    if (nextBtn) nextBtn.addEventListener('click', () => {
      const page = state.get('catalogPage') || 1;
      state.set({ catalogPage: page + 1 });
      catalogManager.render();
    });

    // Compact bottom player controls
    const playBtn = document.getElementById('player-play-btn');
    const chorusBtn = document.getElementById('player-chorus-btn');
    const prevSecBtn = document.getElementById('player-prev-btn');
    const nextSecBtn = document.getElementById('player-next-btn');
    const lyricsToggleBtn = document.getElementById('player-lyrics-toggle-btn');
    const lyricsCloseBtn = document.getElementById('lyrics-drawer-close-btn');
    const volumeSlider = document.getElementById('player-volume-slider');
    const timelineSlider = document.getElementById('player-timeline-slider');

    if (playBtn) playBtn.addEventListener('click', () => player.togglePlay());
    if (chorusBtn) chorusBtn.addEventListener('click', () => player.jumpToChorus());
    if (prevSecBtn) prevSecBtn.addEventListener('click', () => player.seekDelta(-5));
    if (nextSecBtn) nextSecBtn.addEventListener('click', () => player.seekDelta(5));

    if (lyricsToggleBtn) {
      lyricsToggleBtn.addEventListener('click', () => {
        const panel = document.getElementById('lyrics-drawer-panel');
        if (panel) panel.classList.toggle('open');
      });
    }

    if (lyricsCloseBtn) {
      lyricsCloseBtn.addEventListener('click', () => {
        const panel = document.getElementById('lyrics-drawer-panel');
        if (panel) panel.classList.remove('open');
      });
    }

    if (volumeSlider) {
      volumeSlider.addEventListener('input', (e) => {
        player.setVolume(Number(e.target.value) / 100);
      });
    }

    if (timelineSlider) {
      timelineSlider.addEventListener('input', (e) => {
        const percent = Number(e.target.value);
        const duration = state.get('duration') || 0;
        player.seek((percent / 100) * duration);
      });
    }
  }

  initCloudControls() {
    const openConfirmBtn = document.getElementById('cloud-open-batch-confirm-btn');
    const triggerBackupBtn = document.getElementById('cloud-backup-btn');
    const openImportBtn = document.getElementById('cloud-open-import-btn');

    if (openConfirmBtn) openConfirmBtn.addEventListener('click', () => cloudManager.openBatchConfirmationModal());
    if (triggerBackupBtn) triggerBackupBtn.addEventListener('click', () => cloudManager.triggerBackup());
    if (openImportBtn) openImportBtn.addEventListener('click', () => modal.open('#import-modal'));

    // Batch Confirm Modal Execute Button
    const executeBatchBtn = document.getElementById('batch-confirm-execute-btn');
    if (executeBatchBtn) {
      executeBatchBtn.addEventListener('click', () => cloudManager.executeConfirmedBatch());
    }
  }

  initModalsAndForms() {
    // Add Rule
    const addRuleBtn = document.getElementById('rules-add-btn');
    const saveRuleBtn = document.getElementById('rule-editor-save-btn');
    if (addRuleBtn) addRuleBtn.addEventListener('click', () => rulesManager.openRuleEditor(null));
    if (saveRuleBtn) saveRuleBtn.addEventListener('click', () => rulesManager.saveCurrentRule());

    // Create Playlist submit
    const submitCreatePlaylistBtn = document.getElementById('create-playlist-submit-btn');
    if (submitCreatePlaylistBtn) {
      submitCreatePlaylistBtn.addEventListener('click', async () => {
        if (this.createPlaylistPending) return;
        const nameInput = document.getElementById('create-playlist-name-input');
        const name = nameInput?.value.trim() || '新建云端歌单';
        const selectedKeys = new Set(Array.from(state.get('catalogSelectedIds') || []).map(String));
        const selectedIds = [...new Set((state.get('catalog') || [])
          .filter(song => selectedKeys.has(state.catalogSongKey(song)))
          .map(song => song.id))];

        if (selectedIds.length === 0) {
          toast.warning('请先选择歌曲', '请勾选至少一首曲目。');
          return;
        }

        this.createPlaylistPending = true;
        submitCreatePlaylistBtn.disabled = true;
        try {
          toast.info('正在创建歌单', `创建歌单 "${name}" 中...`);
          const res = await api.createPlaylist(name, selectedIds);
          const hasUnknown = res.ok && (
            (Array.isArray(res.data?.unknown_items) && res.data.unknown_items.length > 0)
            || (Array.isArray(res.data?.pending_operation_ids) && res.data.pending_operation_ids.length > 0)
          );
          if (hasUnknown) {
            const pending = await api.getPendingOperations();
            if (pending.ok && Array.isArray(pending.data?.operations)) {
              state.restorePendingOperations(pending.data.operations);
            }
            toast.warning('创建结果待核对', res.data?.message || '未确认是否已创建或完成加歌，已冻结新的云端写入。');
            modal.close('#create-playlist-modal');
          } else if (res.ok) {
            const count = Array.isArray(res.data?.successful_ids) ? res.data.successful_ids.length : 0;
            const failedItems = Array.isArray(res.data?.failed_items) ? res.data.failed_items : [];
            if (failedItems.length > 0) {
              const failedIds = new Set(failedItems.filter(item => item?.id !== null && item?.id !== undefined).map(item => String(item.id)));
              const retainedKeys = failedIds.size > 0
                ? new Set((state.get('catalog') || [])
                  .filter(song => selectedKeys.has(state.catalogSongKey(song)) && failedIds.has(String(song.id)))
                  .map(song => state.catalogSongKey(song)))
                : selectedKeys;
              state.set({ catalogSelectedIds: retainedKeys });
              catalogManager.render();
              toast.warning('歌单已创建，部分歌曲未添加', `已确认添加 ${count} 首，${failedItems.length} 首仍保持选中。`);
            } else {
              toast.success('歌单已创建', `成功在云端创建 "${name}" (${count} 首)。`);
              catalogManager.clearSelection();
            }
            modal.close('#create-playlist-modal');
            await cloudManager.loadPlaylists();
          } else {
            toast.error('创建歌单失败', res.error?.message || '服务器拒绝了请求。');
          }
        } finally {
          this.createPlaylistPending = false;
          submitCreatePlaylistBtn.disabled = false;
        }
      });
    }

    // QR Reload
    const qrReloadBtn = document.getElementById('qr-reload-btn');
    const shellQrReloadBtn = document.getElementById('shell-qr-reload-btn');
    if (qrReloadBtn) qrReloadBtn.addEventListener('click', () => auth.generateQr());
    if (shellQrReloadBtn) shellQrReloadBtn.addEventListener('click', () => auth.generateShellQr());
  }
}

// DOM Ready
window.addEventListener('DOMContentLoaded', () => {
  const app = new App();
  app.init();
});
