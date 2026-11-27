import { historyManager } from './history.js';
import { lanSyncManager } from './lan_sync.js';
/**
 * Pruner Studio - Main Application Bootstrap
 * Orchestrates 4 destinations (发现 / 音乐库 / 歌单清理 / 设置与同步),
 * collapsible left rail & narrow-viewport slide-over drawer, global persistent bottom player,
 * play queue drawer, on-demand immersive stage, Ctrl+K command palette, offline read-only library,
 * startup hydration race protection, and pending actions drawer.
 */
import './theme.js';
import { api } from './api.js';
import { state } from './state.js';
import { auth } from './auth.js';
import { player } from './player.js';
import { stageManager } from './stage.js';
import { discoverManager } from './discover.js';
import { dustyView } from './dusty_view.js';
import { deckManager } from './deck.js';
import { catalogManager } from './catalog.js';
import { rulesManager } from './rules.js';
import { cloudManager } from './cloud.js';
import { importManager } from './import.js';
import { searchPalette } from './search_palette.js';
import { searchView } from './search_view.js';
import { initSongContextMenu, openSongContextMenu, closeSongContextMenu } from './context_menu.js';
import { modal } from './modal.js';
import { toast } from './toast.js';
import { lyricsManager } from './lyrics.js';
import { writableNeteaseTrack, getTrackKey, createElement, formatArtists, formatDuration, trackDurationSummary, getSizedCoverUrl, bindVolumeWheel, bindTimelineTooltip } from './utils.js';
import { createIcon, hydrateStaticIcons } from './icons.js';

export class App {
  constructor() {
    this.currentTab = 'discover';
    this.createPlaylistPending = false;
    this.autoSyncGeneration = null;
    this.initialDataGeneration = 0;
    this.pendingDrawerRefreshTimer = null;
    this.pendingAddToPlaylistSong = null;
  }

  async init() {
    // 1. Hydrate static SVG icon slots
    hydrateStaticIcons();
    state.on('ui:open-shortcuts', () => this.openShortcutsHelpModal());

    const addToPlaylistHandler = (song) => this.openAddToPlaylistModal(song);
    const notInterestedHandler = (song, ctx) => discoverManager.markNotInterested(song, ctx);

    // 2. Initialize view managers & unified context menu
    initSongContextMenu({
      onAddToPlaylist: addToPlaylistHandler,
      onNotInterested: notInterestedHandler,
    });
    discoverManager.init('view-discover', { onAddToPlaylist: addToPlaylistHandler });
    dustyView.init({ onAddToPlaylist: addToPlaylistHandler });
    deckManager.init('deck-workspace-container');
    catalogManager.init('catalog-view-panel', { onAddToPlaylist: addToPlaylistHandler });
    rulesManager.init('rules-list-container');
    cloudManager.init('cloud-view-panel');
    lanSyncManager.init();
    importManager.init('import-dropzone', 'import-file-input');
    lyricsManager.init('lyrics-drawer-scroller', (seconds) => player.seek(seconds));
    stageManager.init();
    searchPalette.init({ onAddToPlaylist: addToPlaylistHandler });
    searchView.init({ onAddToPlaylist: addToPlaylistHandler });

    // 3. Setup navigation, collapsible rail, drawers, global player, and workspace controls
    this.initNavigation();
    historyManager.init();
    document.getElementById('shell-guest-local')?.addEventListener('click', () => auth.enterGuestLocalMode());
    document.getElementById('shell-start-protocol')?.addEventListener('click', () => auth.enterGuestLocalMode({ openChannels: true }));
    document.getElementById('shell-start-resolver')?.addEventListener('click', () => auth.enterGuestLocalMode({ openChannels: true }));
    document.getElementById('discover-all-history')?.addEventListener('click', () => this.switchTab('history'));
    this.initPendingDrawer();
    this.initPlayQueueDrawer();
    this.initGlobalPlayerControls();
    this.initDiscoverControls();
    state.on('deck:retry', () => state.get('offlineMode') ? this.loadOfflineCatalog(state.get('activeOfflineAccountId')) : this.loadInitialData());
    state.on('local-library:scanned', async () => { await catalogManager.loadLocalSongs(true); catalogManager.render(); });
    this.initDeckControls();
    this.initCatalogControls();
    this.initCloudControls();
    this.initModalsAndForms();

    // 4. Listen to state changes
    state.on('catalog:loaded', (catalog) => {
      state.rebuildLikedSongIds(catalog);
      this.onCatalogUpdated(catalog, true);
    });

    state.on('catalog:updated', (catalog) => {
      this.onCatalogUpdated(catalog, false);
    });

    state.on('auth:success', async () => {
      discoverManager.resetDiscovery();
      dustyView.reset();
      this.invalidateInitialDataLoads();
      deckManager.resetSessionSnapshot();
      await Promise.all([player.restorePlaybackSession(), this.loadInitialData()]);
      cloudManager.loadSettings().catch(() => {});
      cloudManager.connectEvents();
      this.switchTab('discover');
      discoverManager.ensureLoaded(true).catch(() => {});
    });

    state.on('auth:offline', async ({ accountId, openChannels } = {}) => {
      discoverManager.resetDiscovery();
      dustyView.reset();
      this.invalidateInitialDataLoads();
      const generation = this.initialDataGeneration;
      state.set({catalog: [], playlists: [], cloudSongs: [], catalogSelectedIds: new Set(), likedSongIds: new Set(), likedTrackKeys: new Set()});
      await this.loadOfflineCatalog(accountId);
      if (!state.get('offlineMode') || state.get('activeOfflineAccountId') !== String(accountId)) return;
      await player.restorePlaybackSession();
      if (generation !== this.initialDataGeneration) return;
      if (state.get('guestLocalMode')) await catalogManager.selectSection('local');
      if (generation !== this.initialDataGeneration) return;
      cloudManager.loadSettings().catch(() => {});
      cloudManager.connectEvents();
      this.switchTab(openChannels ? 'cloud' : 'catalog');
      if (openChannels) cloudManager.setSubTab('channels');
    });

    state.on('auth:logout', () => {
      discoverManager.resetDiscovery();
      dustyView.reset();
      this.invalidateInitialDataLoads();
      state.cancelAllCommits();
      player.stopAndReset();
      historyManager.invalidate();
      deckManager.resetSessionSnapshot();
      state.set({
        catalog: [],
        cloudSongs: [],
        dailyRecommend: [],
        personalFmQueue: [],
        deckQueue: [],
        playlists: [],
        selectedPlaylistId: null,
        hasUnknownResult: false,
        destructiveFrozen: false,
        unknownResultDetail: null,
        pendingOperations: [],
        likedSongIds: new Set(),
        likedTrackKeys: new Set(),
      });
      discoverManager.loadedForUserId = null;
      deckManager.render();
      catalogManager.render();
      cloudManager.renderPlaylists([]);
      cloudManager.renderRecoveryPanel();
    });

    state.on('change:pendingCommits', () => {
      this.updatePendingHeaderBadge();
    });

    state.on('change:batchQueue', () => {
      this.updatePendingHeaderBadge();
    });

    state.on('change:playQueue', () => {
      if (state.get('isPlayQueueOpen')) this.renderPlayQueueDrawer();
      this.updateQueueBadge();
    });

    state.on('change:playQueueIndex', () => {
      if (state.get('isPlayQueueOpen')) this.renderPlayQueueDrawer();
    });

    window.addEventListener('switch-tab', (e) => {
      const detail = e.detail;
      if (detail && typeof detail === 'object') {
        if (detail.tab === 'cloud' && state.get('isStageOpen')) stageManager.close();
        this.switchTab(detail.tab, detail);
        if (detail.tab === 'catalog' && detail.query !== undefined) {
          const input = document.getElementById('catalog-search-input');
          if (input) input.value = detail.query;
          state.set({catalogSearchQuery: detail.query.toLowerCase(), catalogActiveFilter: 'all'});
          catalogManager.selectSection('all');
        }
        if (detail.tab === 'search') {
          searchView.open(detail.query || '', detail.channel ? { channel: detail.channel } : {});
        } else if (detail.tab === 'cloud' && detail.subTab) {
          cloudManager.setSubTab(detail.subTab);
        }
      } else {
        this.switchTab(detail);
      }
    });

    // 5. Load settings early so visual mode applies immediately
    state.applyVisualMode();
    const startupSettings = cloudManager.loadSettings().catch(() => {});

    // 6. Check session and load initial data if authenticated
    const [isAuth] = await Promise.all([auth.checkSession(), startupSettings]);
    if (isAuth) {
      await Promise.all([player.restorePlaybackSession(), this.loadInitialData()]);
      cloudManager.connectEvents();
      this.switchTab('discover');
      discoverManager.ensureLoaded(false).catch(() => {});
    } else if (!state.get('offlineMode')) {
      state.set({ catalogLoading: false, catalog: [], playlists: [] });
      deckManager.render();
      catalogManager.render();
      cloudManager.renderPlaylists([]);
    }
  }

  /**
   * Loads cached read-only library in offline mode without requiring active NetEase auth.
   */
  async loadOfflineCatalog(accountId = null) {
    const generation = this.initialDataGeneration;
    state.set({ catalogLoading: true, catalogError: null });
    try {
      const params = accountId ? { account_id: accountId } : null;
      const catRes = await api.getCatalog(params);
      if (generation !== this.initialDataGeneration) return;
      if (accountId != null && (catRes.data?.account_id == null || String(catRes.data.account_id) !== String(accountId))) {
        state.set({catalogLoading: false, catalogError: '曲库账号已变更，请重新选择。'});
        return;
      }
      if (catRes.ok && catRes.data) {
        const tracks = Array.isArray(catRes.data.tracks)
          ? catRes.data.tracks
          : (Array.isArray(catRes.data) ? catRes.data : []);
        const playlists = Array.isArray(catRes.data.playlists) ? catRes.data.playlists : [];
        const canonicalId = state.resolveCanonicalPlaylistId(playlists);
        state.set({
          catalog: tracks,
          playlists,
          selectedPlaylistId: canonicalId,
          catalogLoading: false,
          catalogError: null,
        });
        state.rebuildLikedSongIds(tracks);
        this.hydrateCanonicalFavorites();
        cloudManager.renderPlaylists(playlists);
        cloudManager.populatePlaylistDropdowns(playlists, canonicalId);
        this.onCatalogUpdated(tracks, true);
      } else {
        state.set({
          catalogLoading: false,
          catalogError: catRes.error?.message || '离线曲库读取失败。',
        });
      }
    } catch (err) {
      if (generation !== this.initialDataGeneration) return;
      state.set({ catalogLoading: false, catalogError: err.message });
    }
  }

  /**
   * Load initial data coordinating playlists and catalog regardless of arrival order.
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
      const [playlistsRes, _rulesRes, catalogRes] = await Promise.allSettled([
        api.getPlaylists({ include_subscribed: true }),
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
        const catPlaylists = Array.isArray(catalogRes.value.data.playlists) ? catalogRes.value.data.playlists : [];
        const hybridPlaylists = catPlaylists.filter(p => p && p.kind === 'hybrid');
        if (hybridPlaylists.length > 0) {
          const existingIds = new Set(playlists.map(p => String(p.id)));
          const merged = [...playlists, ...hybridPlaylists.filter(p => !existingIds.has(String(p.id)))];
          playlists = merged;
          const canonicalId = state.resolveCanonicalPlaylistId(merged);
          state.set({ playlists: merged, selectedPlaylistId: canonicalId });
          cloudManager.renderPlaylists(merged);
          cloudManager.populatePlaylistDropdowns(merged, canonicalId);
        }
        state.set({ catalog: tracks });
        state.rebuildLikedSongIds(tracks);
        this.hydrateCanonicalFavorites();
      } else if (catalogRes.status === 'fulfilled') {
        catalogLoadError = catalogRes.value?.error?.message || '曲库读取失败。';
      } else {
        catalogLoadError = catalogRes.reason?.message || '曲库读取失败。';
      }

      const initialLoadError = catalogLoadError || playlistLoadError;
      state.set({ catalogLoading: false, catalogError: initialLoadError });

      // 3. Auto-sync real liked playlist if uncached
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
              state.rebuildLikedSongIds(freshTracks);
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

  async hydrateCanonicalFavorites() {
    try {
      const res = await api.getFavorites?.();
      if (!res?.ok || !Array.isArray(res.data?.tracks)) return;
      const nextKeys = new Set(state.get('likedTrackKeys') || []);
      const nextIds = new Set(state.get('likedSongIds') || []);
      for (const item of res.data.tracks) {
        const tk = item?.track_key || getTrackKey(item);
        if (tk) nextKeys.add(tk);
        if (item?.provider === 'netease' && item?.external_id) nextIds.add(String(item.external_id));
      }
      state.set({ likedTrackKeys: nextKeys, likedSongIds: nextIds });
      if (player.currentSong) player.updateHeartUI(player.currentSong);
    } catch {}
  }

  onCatalogUpdated(catalog, reloadDeck = false) {
    if (reloadDeck || (state.get('deckQueue') || []).length === 0) {
      deckManager.reloadDeck();
    }
    catalogManager.render();
    rulesManager.render();
    cloudManager.renderGroupedBatchQueue();
    cloudManager.renderRecoveryPanel();
    discoverManager.renderHeroCards();
    this.updatePendingHeaderBadge();
  }

  initNavigation() {
    const shellRoot = document.getElementById('app-shell-root');
    const collapseBtn = document.getElementById('rail-collapse-btn');
    const mobileMenuBtn = document.getElementById('mobile-menu-btn');
    const mobileBackdrop = document.getElementById('mobile-rail-backdrop');

    if (shellRoot && state.get('railCollapsed')) {
      shellRoot.classList.add('rail-collapsed');
    }

    if (collapseBtn && shellRoot) {
      collapseBtn.addEventListener('click', () => {
        const collapsed = shellRoot.classList.toggle('rail-collapsed');
        state.setRailCollapsed(collapsed);
        collapseBtn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      });
    }

    const setMobileDrawer = (open) => {
      if (!shellRoot) return;
      shellRoot.classList.toggle('mobile-drawer-open', open);
      state.set({ mobileDrawerOpen: open });
      if (mobileMenuBtn) mobileMenuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    };

    if (mobileMenuBtn) {
      mobileMenuBtn.addEventListener('click', () => {
        setMobileDrawer(!state.get('mobileDrawerOpen'));
      });
    }
    if (mobileBackdrop) {
      mobileBackdrop.addEventListener('click', () => setMobileDrawer(false));
    }

    const navButtons = document.querySelectorAll('.nav-tab-btn');
    navButtons.forEach(btn => {
      btn.addEventListener('click', () => {
        const tab = btn.dataset.tab;
        if (tab) {
          this.switchTab(tab);
          setMobileDrawer(false);
        }
      });
    });

    this.initRailPlaylists(setMobileDrawer);
  }

  initRailPlaylists(setMobileDrawer = () => {}) {
    const section = document.getElementById('rail-playlists-section');
    const toggle = document.getElementById('rail-playlists-toggle');
    if (toggle && section) {
      toggle.addEventListener('click', () => {
        const collapsed = section.classList.toggle('is-collapsed');
        toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      });
    }
    this._closeMobileDrawerForRail = () => setMobileDrawer(false);
    state.on('change:playlists', () => this.renderRailPlaylists());
    state.on('change:catalogSelectedPlaylistId', () => this.renderRailPlaylists());
    state.on('change:activeTab', () => this.renderRailPlaylists());
    state.on('auth:logout', () => this.renderRailPlaylists());
    this.renderRailPlaylists();
  }

  renderRailPlaylists() {
    if (typeof document === 'undefined') return;
    const listEl = document.getElementById('rail-playlists-list');
    if (!listEl) return;
    listEl.textContent = '';

    const playlists = Array.isArray(state.get('playlists')) ? state.get('playlists') : [];
    if (playlists.length === 0) {
      listEl.appendChild(createElement('div', {
        className: 'rail-playlists-empty',
        text: state.get('authenticated') ? '同步后显示歌单' : '登录后显示歌单',
      }));
      return;
    }

    const activeTab = state.get('activeTab');
    const selectedPlId = String(state.get('catalogSelectedPlaylistId') || 'all');
    playlists.slice(0, 30).forEach((pl) => {
      const pid = String(pl.id);
      const isActive = activeTab === 'catalog' && selectedPlId === pid;
      const item = createElement('button', {
        className: `rail-playlist-item ${isActive ? 'active' : ''}`,
        attributes: {
          type: 'button',
          title: pl.name || '歌单',
          'aria-current': isActive ? 'true' : null,
        },
        events: {
          click: () => {
            this.switchTab('catalog');
            state.set({
              catalogSection: 'all',
              catalogSelectedPlaylistId: pid,
              catalogPage: 1,
            });
            document.querySelectorAll('.catalog-section-tab').forEach(btn => {
              const match = btn.dataset.section === 'all';
              btn.classList.toggle('active', match);
              btn.setAttribute('aria-pressed', match ? 'true' : 'false');
            });
            const sel = document.getElementById('catalog-playlist-filter');
            if (sel) {
              sel.disabled = false;
              sel.value = pid;
            }
            catalogManager.onFilterCriteriaChanged();
            this.renderRailPlaylists();
            this._closeMobileDrawerForRail?.();
          },
        },
        children: [
          createElement('span', { className: 'rail-playlist-name', text: pl.name || '未命名歌单' }),
          createElement('span', { className: 'rail-playlist-count', text: String(pl.track_count ?? 0) }),
        ],
      });
      listEl.appendChild(item);
    });
  }

  switchTab(tabName, options = null) {
    if (this.currentTab === 'deck' && tabName !== 'deck') deckManager.detachDrag?.();
    const validTabs = ['discover', 'dusty', 'catalog', 'history', 'deck', 'cloud', 'search'];
    const target = validTabs.includes(tabName) ? tabName : 'discover';
    this.currentTab = target;
    state.set({ activeTab: target });

    document.querySelectorAll('.nav-tab-btn').forEach(btn => {
      const isMatch = btn.dataset.tab === target || (target === 'dusty' && btn.dataset.tab === 'discover');
      btn.classList.toggle('active', isMatch);
      btn.setAttribute('aria-selected', isMatch ? 'true' : 'false');
    });

    document.querySelectorAll('.view-panel').forEach(panel => {
      panel.classList.toggle('active', panel.id === `view-${target}`);
    });

    // Global bottom player stays persistently visible across ALL tabs when shell is active
    const playerBar = document.getElementById('catalog-player-bar');
    if (playerBar && (state.get('authenticated') || state.get('offlineMode'))) {
      playerBar.classList.add('active');
    }

    if (target === 'discover') {
      discoverManager.ensureLoaded(false).catch(() => {});
      discoverManager.render();
    } else if (target === 'dusty') {
      dustyView.open(options && typeof options === 'object' ? options : {});
    } else if (target === 'history') {
      historyManager.load();
    } else if (target === 'deck') {
      deckManager.render();
      lyricsManager.renderVisible();
    } else if (target === 'catalog') {
      catalogManager.render();
    } else if (target === 'cloud') {
      cloudManager.renderPlaylists(state.get('playlists') || []);
      cloudManager.renderGroupedBatchQueue();
      cloudManager.renderRecoveryPanel();
      cloudManager.renderLogs();
      cloudManager.renderSettings();
      cloudManager.renderChannelsPanel();
      cloudManager.loadChannels().catch(() => {});
      cloudManager.loadAudioCacheStats().catch(() => {});
      cloudManager.updateSyncSubTabBadge();
    }
  }

  // --- Discover Controls ---

  initDiscoverControls() {
    const refreshDailyBtn = document.getElementById('discover-daily-refresh');
    const playAllDailyBtn = document.getElementById('discover-daily-play-all');
    const heartbeatBtn = document.getElementById('discover-heartbeat-btn');

    if (refreshDailyBtn) {
      refreshDailyBtn.addEventListener('click', () => discoverManager.loadDailyRecommend(true));
    }
    if (playAllDailyBtn) {
      playAllDailyBtn.addEventListener('click', () => {
        const songs = state.get('dailyRecommend') || [];
        if (songs.length > 0) player.playQueue(songs, 0, 'order', '发现 · 每日推荐');
      });
    }
    if (heartbeatBtn) {
      heartbeatBtn.addEventListener('click', () => discoverManager.handleSmartRadioPrimaryAction());
    }
  }

  // --- Pending Actions Drawer ---

  initPendingDrawer() {
    const triggerBtn = document.getElementById('header-pending-btn');
    const drawer = document.getElementById('pending-actions-drawer');
    const closeBtn = document.getElementById('pending-drawer-close-btn');
    const confirmBtn = document.getElementById('drawer-batch-confirm-btn');

    const setPendingOpen = (open, trigger = triggerBtn) => {
      if (!drawer) return;
      drawer.classList.toggle('open', open);
      state.set({ isPendingDrawerOpen: open });
      this.updateDrawerStack?.('pending-actions-drawer', open);
      if (open) {
        this.pendingTrigger = trigger || triggerBtn;
        this.renderPendingDrawer();
        this.startPendingDrawerRefresh();
        closeBtn?.focus?.();
      } else {
        this.stopPendingDrawerRefresh();
        (this.pendingTrigger || triggerBtn)?.focus?.();
      }
    };

    if (triggerBtn && drawer) {
      triggerBtn.addEventListener('click', () => {
        setPendingOpen(!drawer.classList.contains('open'), triggerBtn);
      });
    }

    if (closeBtn && drawer) {
      closeBtn.addEventListener('click', () => setPendingOpen(false));
    }

    if (confirmBtn) {
      confirmBtn.addEventListener('click', () => {
        if (drawer) {
          drawer.classList.remove('open');
          state.set({ isPendingDrawerOpen: false });
          this.updateDrawerStack?.('pending-actions-drawer', false);
          this.stopPendingDrawerRefresh();
        }
        cloudManager.openBatchConfirmationModal(this.pendingTrigger || triggerBtn);
      });
    }
  }

  updateDrawerStack(drawerId, isOpen) {
    this.drawerStack ||= [];
    this.drawerStack = this.drawerStack.filter(id => id !== drawerId);
    if (isOpen) this.drawerStack.push(drawerId);
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

    if (commitsContainer) {
      commitsContainer.textContent = '';
      if (pendingCommits.length === 0) {
        commitsContainer.appendChild(createElement('div', {
          className: 'drawer-empty-hint',
          text: '暂无正在倒计时的即时同步项',
        }));
      } else {
        pendingCommits.forEach(c => {
          const remainingSecs = Math.max(0, ((c.expiresAt - Date.now()) / 1000)).toFixed(1);
          const cancelBtn = createElement('button', {
            className: 'btn btn-ghost btn-sm text-danger',
            text: '取消 [Z]',
            events: { click: () => state.cancelCommitById(c.id) },
          });

          const row = createElement('div', {
            className: 'drawer-item-row countdown-row',
            children: [
              createElement('div', {
                children: [
                  createElement('div', { className: 'drawer-item-title', text: c.song.title || '未知歌曲' }),
                  createElement('div', { className: 'drawer-item-meta', text: `${c.targetName} · ${c.action === 'unlike' ? '取消喜欢' : '移出歌单'}` }),
                ],
              }),
              createElement('div', {
                style: 'display: flex; align-items: center; gap: 8px;',
                children: [
                  createElement('span', { className: 'countdown-pill', text: `${remainingSecs}s` }),
                  cancelBtn,
                ],
              }),
            ],
          });
          commitsContainer.appendChild(row);
        });
      }
    }

    if (batchContainer) {
      batchContainer.textContent = '';
      if (batchQueue.length === 0) {
        batchContainer.appendChild(createElement('div', {
          className: 'drawer-empty-hint',
          text: '暂无待确认的批量操作',
        }));
      } else {
        batchQueue.slice(0, 50).forEach(item => {
          const removeBtn = createElement('button', {
            className: 'btn btn-ghost btn-icon btn-sm',
            attributes: { title: '移出', 'aria-label': '移出待处理队列' },
            events: {
              click: () => {
                state.unstageBatchAction(item.song.id, item.playlistId);
                this.renderPendingDrawer();
              },
            },
            children: [createIcon('close', { size: 13 })],
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
                ],
              }),
              removeBtn,
            ],
          });
          batchContainer.appendChild(row);
        });
      }
    }
  }

  // --- Play Queue Drawer ---

  initPlayQueueDrawer() {
    for (const button of document.querySelectorAll('[data-queue-tab]')) {
      button.addEventListener('click', () => {
        this.queueTab = button.dataset.queueTab;
        this.renderPlayQueueDrawer();
      });
    }
    state.on('change:recentPlayed', () => {
      if (state.get('isPlayQueueOpen') && this.queueTab === 'history') this.renderPlayQueueDrawer();
    });
    const toggleBtn = document.getElementById('player-queue-btn');
    const drawer = document.getElementById('play-queue-drawer');
    const closeBtn = document.getElementById('play-queue-close-btn');
    const clearBtn = document.getElementById('play-queue-clear-btn');

    document.getElementById('play-queue-locate')?.addEventListener('click', () => {
      const currentIdx = state.get('playQueueIndex');
      const row = drawer?.querySelector(`[data-queue-index="${currentIdx}"]`);
      if (!row) return;
      row.scrollIntoView({ behavior: 'smooth', block: 'center' });
      row.classList.add('queue-located');
      setTimeout(() => row.classList.remove('queue-located'), 1100);
    });
    const stageBtn = document.getElementById('stage-queue-btn');
    const setQueueOpen = (open, trigger) => {
      drawer.classList.toggle('open', open);
      state.set({ isPlayQueueOpen: open });
      this.updateDrawerStack?.('play-queue-drawer', open);
      for (const btn of [toggleBtn, stageBtn]) btn?.setAttribute('aria-expanded', String(open));
      if (open) {
        this.queueTrigger = trigger;
        this.renderPlayQueueDrawer();
        closeBtn?.focus();
      } else (this.queueTrigger || toggleBtn)?.focus?.();
    };
    if (drawer) {
      for (const btn of [toggleBtn, stageBtn]) {
        btn?.addEventListener('click', () => setQueueOpen(!drawer.classList.contains('open'), btn));
      }
      closeBtn?.addEventListener('click', () => setQueueOpen(false));
    }
    if (clearBtn) {
      clearBtn.addEventListener('click', () => {
        player.clearQueue();
        this.renderPlayQueueDrawer();
      });
    }
  }

  updateQueueBadge() {
    const countEl = document.getElementById('player-queue-count');
    if (countEl) {
      const len = (state.get('playQueue') || []).length;
      countEl.textContent = len > 0 ? String(len) : '';
      countEl.classList.toggle('hidden', len === 0);
    }
  }

  renderPlayQueueDrawer() {
    const listEl = document.getElementById('play-queue-list');
    const countMeta = document.getElementById('play-queue-meta');
    if (!listEl) return;
    const history = this.queueTab === 'history';
    document.querySelectorAll('[data-queue-tab]').forEach(btn =>
      btn.setAttribute('aria-pressed', String((this.queueTab || 'next') === btn.dataset.queueTab)));
    const clear = document.getElementById('play-queue-clear-btn');
    if (clear) clear.hidden = history;
    const queue = state.get('playQueue') || [];
    const currentIdx = state.get('playQueueIndex') ?? -1;
    const locate = document.getElementById('play-queue-locate');
    if (locate) locate.hidden = history || !queue[currentIdx];
    const trackCells = song => [
      createElement('img', { className: 'queue-item-cover', attributes: {
        src: getSizedCoverUrl(song.cover_url, 64, '音'), alt: '', loading: 'lazy',
      } }),
      createElement('span', { className: 'queue-item-text', children: [
        createElement('span', { className: 'drawer-item-title', text: song.title || '未知曲目' }),
        createElement('span', { className: 'drawer-item-meta', text: `${formatArtists(song.artists)} · ${formatDuration(song)}` }),
      ] }),
    ];
    if (history) {
      listEl.textContent = '';
      const songs = state.get('recentPlayed') || [];
      if (countMeta) countMeta.textContent = `共 ${songs.length} 首`;
      if (!songs.length) listEl.textContent = '还没有播放记录';
      songs.forEach(song => listEl.appendChild(createElement('button', {
        className: 'drawer-item-row queue-item-row', children: trackCells(song),
        events: { click: () => player.playSong(song, true, { origin: '播放历史' }) },
      })));
      return;
    }

    if (countMeta) {
      countMeta.textContent = `共 ${queue.length} 首`;
    }

    listEl.textContent = '';
    if (queue.length === 0) {
      listEl.appendChild(createElement('div', {
        className: 'drawer-empty-hint',
        text: '播放队列为空，点击任意歌曲开始播放。',
      }));
      return;
    }

    const heading = text => createElement('h3', { className: 'queue-heading', text });
    const renderRow = (song, idx) => {
      const isCurrent = idx === currentIdx;
      const row = createElement('div', {
        attributes: { draggable: 'true' },
        dataset: { queueIndex: idx },
        className: `drawer-item-row queue-item-row ${isCurrent ? 'is-current' : ''}`,
        events: {
          dragstart: e => e.dataTransfer.setData('application/x-pruner-index', String(idx)),
          dragover: e => e.preventDefault(),
          drop: e => {
            e.preventDefault();
            const raw = e.dataTransfer.getData('application/x-pruner-index');
            if (raw !== '') player.reorderQueue(Number(raw), idx);
          },
          click: () => {
            const isRestoredCurrent = isCurrent && !state.get('isPlaying') && !player.activeSrc && (player.pendingResumeSeekSec || 0) > 0;
            state.set({ playQueueIndex: idx });
            player.playSong(song, true, {
              preserveQueue: true,
              ...(isRestoredCurrent ? { resumeSeekSec: player.pendingResumeSeekSec } : {}),
            });
          },
          contextmenu: (e) => openSongContextMenu(e, song, {
            source: 'queue',
            queue,
            index: idx,
            onAddToPlaylist: (s) => this.openAddToPlaylistModal(s),
          }),
        },
        children: [
          createElement('span', { className: 'queue-drag-handle', text: '⋮⋮', attributes: { 'aria-hidden': 'true' } }),
          createElement('button', {
            className: 'queue-track-button', attributes: { 'aria-label': `播放 ${song.title || '曲目'}` },
            children: trackCells(song),
          }),
          ...[-1, 1].map(step => {
            const label = step < 0 ? '上移' : '下移';
            const move = createElement('button', {
              className: 'btn btn-ghost btn-sm', text: label,
              attributes: { 'aria-label': `${label} ${song.title}` },
              events: { click: e => { e.stopPropagation(); player.reorderQueue(idx, idx + step); } },
            });
            move.disabled = idx + step < 0 || idx + step >= queue.length;
            return move;
          }),
          createElement('button', {
            className: 'btn btn-ghost btn-icon btn-sm',
            attributes: { title: '从队列移除', 'aria-label': '从队列移除' },
            events: {
              click: (e) => {
                e.stopPropagation();
                player.removeFromQueue(idx);
              },
            },
            children: [createIcon('close', { size: 13 })],
          }),
        ],
      });
      return row;
    };
    if (queue[currentIdx]) {
      listEl.appendChild(heading('正在播放'));
      listEl.appendChild(renderRow(queue[currentIdx], currentIdx));
    }
    const next = queue.slice(currentIdx + 1);
    listEl.appendChild(heading(`接下来 · ${trackDurationSummary(next)}`));
    next.forEach((song, i) => listEl.appendChild(renderRow(song, currentIdx + 1 + i)));
    if (currentIdx > 0) {
      const prior = createElement('details', { children: [
        createElement('summary', { className: 'queue-heading', text: `之前播放 · ${currentIdx} 首` }),
        ...queue.slice(0, currentIdx).map(renderRow),
      ] });
      prior.open = Boolean(this.previousQueueOpen);
      prior.addEventListener('toggle', () => { this.previousQueueOpen = prior.open; });
      listEl.appendChild(prior);
    }
  }

  // --- Global Bottom Player Controls ---

  handleSurfaceEscape(e) {
    if (e.key !== 'Escape' || e.isComposing || e.repeat) return;
    const byId = id => document.getElementById(id);
    const dialogs = document.querySelectorAll('.modal-backdrop.open');
    const stageOpen = Boolean(state.get('isStageOpen') || byId('immersive-stage-overlay')?.classList?.contains('open'));
    const stageCustomizeOpen = stageOpen && byId('stage-customize-popover') && !byId('stage-customize-popover').classList.contains('hidden');
    const stageMoreOpen = stageOpen && byId('stage-more-popover') && !byId('stage-more-popover').classList.contains('hidden');
    const playerMoreOpen = byId('player-more-popover') && !byId('player-more-popover').classList.contains('hidden');
    const drawerOrder = [...(this.drawerStack || [])].reverse();
    for (const fallbackId of ['lyrics-drawer-panel', 'play-queue-drawer', 'pending-actions-drawer', 'rules-drawer-panel']) {
      if (!drawerOrder.includes(fallbackId)) drawerOrder.push(fallbackId);
    }
    const openDrawerId = drawerOrder.find(id => byId(id)?.classList?.contains('open'));

    if (document.querySelector('#context-menu-host .context-menu')) {
      closeSongContextMenu({ restoreFocus: true });
    } else if (dialogs.length) {
      modal.close(modal.activeModal || dialogs[dialogs.length - 1]);
    } else if (openDrawerId) {
      if (openDrawerId === 'lyrics-drawer-panel') {
        const panel = byId('lyrics-drawer-panel');
        const closeEl = byId('lyrics-drawer-close-btn');
        if (closeEl?.click) closeEl.click();
        panel?.classList?.remove('open');
        state.set({ isLyricsDrawerOpen: false });
        lyricsManager.detachDrawerFollow?.();
        this.updateDrawerStack?.('lyrics-drawer-panel', false);
        (this.lyricsTrigger || byId('player-more-btn'))?.focus?.();
      } else if (openDrawerId === 'play-queue-drawer') {
        const panel = byId('play-queue-drawer');
        const closeEl = byId('play-queue-close-btn');
        if (closeEl?.click) closeEl.click();
        panel?.classList?.remove('open');
        state.set({ isPlayQueueOpen: false });
        this.updateDrawerStack?.('play-queue-drawer', false);
        for (const btn of [byId('player-queue-btn'), byId('stage-queue-btn')]) btn?.setAttribute?.('aria-expanded', 'false');
        (this.queueTrigger || byId('player-queue-btn'))?.focus?.();
      } else if (openDrawerId === 'pending-actions-drawer') {
        const panel = byId('pending-actions-drawer');
        const closeEl = byId('pending-drawer-close-btn');
        if (closeEl?.click) closeEl.click();
        panel?.classList?.remove('open');
        state.set({ isPendingDrawerOpen: false });
        this.stopPendingDrawerRefresh?.();
        this.updateDrawerStack?.('pending-actions-drawer', false);
        (this.pendingTrigger || byId('header-pending-btn'))?.focus?.();
      } else if (openDrawerId === 'rules-drawer-panel') {
        const panel = byId('rules-drawer-panel');
        const closeEl = byId('rules-drawer-close-btn');
        if (closeEl?.click) closeEl.click();
        panel?.classList?.remove('open');
        state.set({ isRulesDrawerOpen: false });
        this.updateDrawerStack?.('rules-drawer-panel', false);
        byId('catalog-open-rules-btn')?.focus?.();
      }
    } else if (stageMoreOpen) {
      byId('stage-more-popover').classList.add('hidden');
      const trigger = byId('stage-more-btn');
      trigger?.setAttribute('aria-expanded', 'false');
      trigger?.focus?.();
    } else if (stageCustomizeOpen) {
      byId('stage-customize-popover').classList.add('hidden');
      const trigger = byId('stage-customize-btn');
      trigger?.setAttribute('aria-expanded', 'false');
      trigger?.focus?.();
    } else if (stageOpen) {
      stageManager.close();
    } else if (state.get('mobileDrawerOpen') || document.querySelector?.('.app-shell.mobile-drawer-open')) {
      byId('mobile-rail-backdrop')?.click?.();
      byId('mobile-menu-btn')?.focus?.();
    } else if (playerMoreOpen) {
      byId('player-more-popover').classList.add('hidden');
      const trigger = byId('player-more-btn');
      trigger?.setAttribute('aria-expanded', 'false');
      trigger?.focus?.();
    } else {
      return;
    }
    e.preventDefault();
    e.stopImmediatePropagation();
  }

  initGlobalPlayerControls() {
    const playBtn = document.getElementById('player-play-btn');
    const prevTrackBtn = document.getElementById('player-prev-track-btn');
    const nextTrackBtn = document.getElementById('player-next-track-btn');
    const prevSecBtn = document.getElementById('player-prev-btn');
    const nextSecBtn = document.getElementById('player-next-btn');
    const chorusBtn = document.getElementById('player-chorus-btn');
    const modeBtn = document.getElementById('player-mode-btn');
    const heartBtn = document.getElementById('player-heart-btn');
    const similarBtn = document.getElementById('player-similar-btn');
    const candidatesBtn = document.getElementById('player-candidates-btn');
    const sourceBadge = document.getElementById('player-source-badge');
    const coverTrigger = document.getElementById('player-cover-stage-trigger');
    const stageBtn = document.getElementById('player-stage-btn');
    const desktopLyricsBtn = document.getElementById('player-desktop-lyrics-btn');
    const lyricsToggleBtn = document.getElementById('player-lyrics-toggle-btn');
    const lyricsCloseBtn = document.getElementById('lyrics-drawer-close-btn');
    const muteBtn = document.getElementById('player-mute-btn');
    const volumeSlider = document.getElementById('player-volume-slider');
    const timelineSlider = document.getElementById('player-timeline-slider');
    const moreBtn = document.getElementById('player-more-btn');
    const morePopover = document.getElementById('player-more-popover');

    const closeMorePopover = () => {
      if (!morePopover) return;
      morePopover.classList.add('hidden');
      if (moreBtn) moreBtn.setAttribute('aria-expanded', 'false');
    };

    for (const id of ['player-eq-btn', 'player-eq-badge']) {
      document.getElementById(id)?.addEventListener('click', (e) => {
        closeMorePopover();
        const returnTo = id === 'player-eq-btn' ? (moreBtn || e.currentTarget) : e.currentTarget;
        returnTo?.focus?.();
        modal.open('#eq-modal', returnTo);
      });
    }
    for (const name of ['rate', 'sleep']) {
      document.getElementById('player-' + name + '-badge')?.addEventListener('click', e => {
        e.stopPropagation();
        morePopover.classList.remove('hidden');
        moreBtn.setAttribute('aria-expanded', 'true');
        document.getElementById('player-' + name)?.focus();
      });
    }

    if (moreBtn && morePopover) {
      moreBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        const willOpen = morePopover.classList.contains('hidden');
        morePopover.classList.toggle('hidden', !willOpen);
        moreBtn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
      });

      if (typeof document !== 'undefined') {
        document.addEventListener('click', (e) => {
          if (morePopover.classList.contains('hidden')) return;
          if (e.target && typeof e.target.closest === 'function' && e.target.closest('.player-more-wrapper')) {
            return;
          }
          closeMorePopover();
        });
      }
    }

    if (playBtn) {
      playBtn.addEventListener('click', (e) => {
        player.togglePlay();
        e.currentTarget?.blur?.();
      });
    }
    const stagePlayBtn = document.getElementById('stage-play-btn');
    if (stagePlayBtn) {
      stagePlayBtn.addEventListener('click', (e) => {
        e.currentTarget?.blur?.();
      });
    }
    if (typeof document !== 'undefined') {
      document.addEventListener('keydown', e => this.handleSurfaceEscape(e), true);
      document.addEventListener('keydown', (e) => {
        if (e.key === 'F11') {
          e.preventDefault();
          if (!e.repeat && !e.isComposing) stageManager.toggleFullscreen();
          return;
        }
        if (this.shouldHandleGlobalSpaceKey(e)) {
          e.preventDefault();
          player.togglePlay();
          return;
        }
        this.handleGlobalShortcutKey(e);
      });
    }
    if (prevTrackBtn) prevTrackBtn.addEventListener('click', () => player.playPrev());
    if (nextTrackBtn) nextTrackBtn.addEventListener('click', () => player.playNext(true));
    if (prevSecBtn) prevSecBtn.addEventListener('click', () => player.seekDelta(-5));
    if (nextSecBtn) nextSecBtn.addEventListener('click', () => player.seekDelta(5));
    if (chorusBtn) chorusBtn.addEventListener('click', () => { closeMorePopover(); player.jumpToChorus(); });
    if (modeBtn) modeBtn.addEventListener('click', () => player.cyclePlayMode());
    if (heartBtn) heartBtn.addEventListener('click', () => player.toggleLikeCurrentSong());
    if (similarBtn) similarBtn.addEventListener('click', () => { closeMorePopover(); moreBtn?.focus?.(); player.openSimilarSongsModal(null, moreBtn); });
    if (candidatesBtn) candidatesBtn.addEventListener('click', () => { closeMorePopover(); moreBtn?.focus?.(); player.openAudioCandidatesModal(null, moreBtn); });
    if (sourceBadge) sourceBadge.addEventListener('click', () => player.openAudioCandidatesModal(null, sourceBadge));
    if (coverTrigger) coverTrigger.addEventListener('click', () => stageManager.open(coverTrigger));
    if (stageBtn) stageBtn.addEventListener('click', () => { closeMorePopover(); stageManager.toggle(moreBtn || coverTrigger); });
    if (desktopLyricsBtn) desktopLyricsBtn.addEventListener('click', () => player.toggleDesktopLyricsWindow());
    if (muteBtn) muteBtn.addEventListener('click', () => player.toggleMute());

    if (lyricsToggleBtn) {
      lyricsToggleBtn.addEventListener('click', () => {
        closeMorePopover();
        const panel = document.getElementById('lyrics-drawer-panel');
        if (panel) {
          const isOpen = panel.classList.toggle('open');
          state.set({ isLyricsDrawerOpen: isOpen });
          this.updateDrawerStack?.('lyrics-drawer-panel', isOpen);
          if (isOpen) {
            this.lyricsTrigger = moreBtn;
            lyricsManager.render();
            lyricsCloseBtn?.focus?.();
          } else {
            this.lyricsTrigger?.focus?.();
          }
        }
      });
    }

    if (lyricsCloseBtn) {
      lyricsCloseBtn.addEventListener('click', () => {
        const panel = document.getElementById('lyrics-drawer-panel');
        if (panel) panel.classList.remove('open');
        state.set({ isLyricsDrawerOpen: false });
        this.updateDrawerStack?.('lyrics-drawer-panel', false);
        lyricsManager.detachDrawerFollow();
        (this.lyricsTrigger || moreBtn)?.focus?.();
      });
    }

    state.on('change:currentPlayingSong', () => player.syncPlayingRowUI());
    state.on('change:isPlaying', () => player.syncPlayingRowUI());

    if (volumeSlider) {
      volumeSlider.addEventListener('input', (e) => {
        player.setVolume(Number(e.target.value) / 100);
      });
    }
    bindVolumeWheel(
      document.querySelector('#catalog-player-bar .volume-control-group'),
      (delta) => player.adjustVolume(delta, { showOsd: false }),
    );
    document.getElementById('player-game-mode-btn')?.addEventListener('click', closeMorePopover);

    if (timelineSlider) {
      timelineSlider.addEventListener('input', (e) => {
        const percent = Number(e.target.value);
        const duration = state.get('duration') || 0;
        player.seek((percent / 100) * duration);
      });
      bindTimelineTooltip(
        timelineSlider,
        document.getElementById('player-timeline-tooltip'),
        () => state.get('duration') || 0,
      );
    }

    player.updatePlayModeUI();
    player.updateVolumeIconUI();
  }

  isShortcutBlockedContext(e) {
    if (!e) return true;
    if (e.repeat) return true;
    if (e.isComposing || e.keyCode === 229) return true;

    if (typeof document !== 'undefined') {
      if (document.querySelector?.('.modal-backdrop.open')) return true;
      const palette = document.getElementById?.('search-palette-modal');
      if (palette?.classList?.contains?.('open')) return true;
    }

    const isInteractiveTarget = (node) => {
      if (!node) return false;
      const tag = String(node.tagName || '').toUpperCase();
      if (['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON', 'A'].includes(tag)) return true;
      if (node.isContentEditable) return true;
      if (typeof node.closest === 'function') {
        if (node.closest('input, textarea, select, button, a, [contenteditable="true"], [role="button"], [role="slider"]')) {
          return true;
        }
      }
      return false;
    };

    if (isInteractiveTarget(e.target)) return true;
    if (typeof document !== 'undefined' && isInteractiveTarget(document.activeElement)) return true;
    return false;
  }

  /**
   * Determines whether global Spacebar Play/Pause should trigger.
   * Strictly ignores Deck tab (Deck owns Space for skip-to-end), open modals/search palette,
   * form controls/buttons/editable fields, key repeat, IME composition, and modifier keys.
   */
  shouldHandleGlobalSpaceKey(e) {
    if (!e) return false;
    if (e.key !== ' ' && e.code !== 'Space') return false;
    if (e.ctrlKey || e.metaKey || e.altKey) return false;
    if (state.get('activeTab') === 'deck') return false;
    return !this.isShortcutBlockedContext(e);
  }

  handleGlobalShortcutKey(e) {
    if (!e) return false;
    if (this.isShortcutBlockedContext(e)) return false;

    // Ctrl+ArrowLeft / Ctrl+ArrowRight -> prev / next; Ctrl+ArrowUp / Ctrl+ArrowDown -> volume ±5%
    if (e.ctrlKey && !e.metaKey && !e.altKey) {
      if (e.key === 'ArrowLeft') {
        e.preventDefault?.();
        player.playPrev();
        return true;
      }
      if (e.key === 'ArrowRight') {
        e.preventDefault?.();
        player.playNext(true);
        return true;
      }
      if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
        e.preventDefault?.();
        const delta = e.key === 'ArrowUp' ? 0.05 : -0.05;
        const cur = Number(player.targetVolume ?? 0.8);
        const next = Math.max(0, Math.min(1, Math.round((cur + delta) * 100) / 100));
        player.setVolume(next);
        toast.quiet(`音量 ${Math.round(next * 100)}%`);
        return true;
      }
    }

    // '?' -> Shortcuts help modal
    if (!e.ctrlKey && !e.metaKey && !e.altKey && e.key === '?') {
      e.preventDefault?.();
      this.openShortcutsHelpModal();
      return true;
    }

    return false;
  }

  openShortcutsHelpModal() {
    if (typeof document === 'undefined') return;
    const body = document.getElementById('shortcuts-help-body');
    if (body) {
      body.textContent = '';
      const groups = [
        {
          title: '全局播放与搜索',
          items: [
            ['Space', '播放 / 暂停（歌单清理页除外）'],
            ['Ctrl + ← / →', '上一首 / 下一首'],
            ['Ctrl + ↑ / ↓', '音量增大 / 减小 5%'],
            ['Ctrl + K', '打开全局快捷搜索面板'],
            ['F11', '切换窗口全屏'],
            ['?', '显示快捷键一览'],
            ['Esc', '关闭全屏舞台、抽屉或弹窗'],
          ],
        },
        {
          title: '歌单清理（整理台）',
          items: [
            ['← / A', '取消喜欢或移出当前歌单'],
            ['→ / D', '保留歌曲并进入下一首'],
            ['L', '标记“稍后再听”'],
            ['Space', '跳至本轮末尾'],
            ['P', '试听 / 暂停当前待整理曲目'],
            ['C / ↑', '跳转副歌（或 35%）'],
            ['Z', '撤销倒计时或上一步整理'],
          ],
        },
      ];

      groups.forEach((group) => {
        const section = createElement('div', { className: 'shortcuts-help-section' });
        section.appendChild(createElement('div', { className: 'shortcuts-help-heading', text: group.title }));
        const list = createElement('div', { className: 'shortcuts-help-list' });
        group.items.forEach(([keyLabel, desc]) => {
          list.appendChild(createElement('div', {
            className: 'shortcuts-help-row',
            children: [
              createElement('kbd', { className: 'kbd', text: keyLabel }),
              createElement('span', { className: 'shortcuts-help-desc', text: desc }),
            ],
          }));
        });
        section.appendChild(list);
        body.appendChild(section);
      });
    }
    modal.open('#shortcuts-help-modal');
  }

  // --- Add To Playlist Modal ---

  openAddToPlaylistModal(song) {
    if (!song) return;
    const isNeteaseWritable = writableNeteaseTrack(song) && state.get('authenticated') && !state.get('offlineMode');
    const allPlaylists = state.get('playlists') || [];
    const hybridPlaylists = allPlaylists.filter(p => p.kind === 'hybrid');
    const ownedNetease = isNeteaseWritable
      ? allPlaylists.filter(p => p.kind === 'self_created' || p.kind === 'owned')
      : [];
    const scope = state.writeScope();
    this.pendingAddToPlaylistSong = song;
    const titleEl = document.getElementById('add-to-playlist-song-title');
    const listEl = document.getElementById('add-to-playlist-list');
    if (titleEl) {
      titleEl.textContent = `${song.title || '曲目'} - ${formatArtists(song.artists)}`;
    }
    if (listEl) {
      listEl.textContent = '';
      const appendOption = (pl, isHybrid) => {
        const btn = createElement('button', {
          className: 'candidate-item-row add-playlist-option-btn',
          events: {
            click: async () => {
              if (isHybrid) {
                const res = await api.addTracksToHybridPlaylist(pl.id, [song]);
                if (res.ok) {
                  toast.success('已添加到混合歌单', `已将 "${song.title}" 加入「${pl.name}」。`);
                  modal.close('#add-to-playlist-modal');
                  cloudManager.reloadCatalog?.();
                } else {
                  toast.error('添加失败', res.error?.message || '未能写入跨平台混合歌单。');
                }
                return;
              }
              const res = await api.addTracksToPlaylist(pl.id, [song.id], scope);
              if (res.data?.unknown_items?.length || res.data?.pending_operation_ids?.length) {
                toast.warning('加歌状态待核对', res.data.message);
                const pending = await api.getPendingOperations();
                if (pending.ok && Array.isArray(pending.data?.operations)) {
                  state.restorePendingOperations(pending.data.operations);
                }
                modal.close('#add-to-playlist-modal');
              } else if (res.ok && res.data?.confirmed_ids?.map(String).includes(String(song.id))) {
                toast.success('已添加到歌单', `已将 "${song.title}" 加入「${pl.name}」。`);
                modal.close('#add-to-playlist-modal');
              } else {
                toast.error('添加失败', res.error?.message || res.data?.message || '未能确认歌曲已添加。');
              }
            },
          },
          children: [
            createElement('div', {
              className: 'candidate-item-info',
              children: [
                createElement('div', { className: 'candidate-item-title', text: pl.name }),
                createElement('div', {
                  className: 'candidate-item-meta',
                  text: `${pl.track_count || 0} 首曲目 · ${isHybrid ? '跨平台混合歌单' : '网易云自建歌单'}`,
                }),
              ],
            }),
            createIcon('plus', { size: 15 }),
          ],
        });
        listEl.appendChild(btn);
      };

      hybridPlaylists.forEach(pl => appendOption(pl, true));
      ownedNetease.forEach(pl => appendOption(pl, false));

      if (hybridPlaylists.length === 0 && ownedNetease.length === 0) {
        const createQuickBtn = createElement('button', {
          className: 'btn btn-primary btn-sm',
          text: '新建跨平台混合歌单并加入',
          events: {
            click: async () => {
              const res = await api.createHybridPlaylist('我的跨平台收藏', '本地与多源混合歌单', [song]);
              if (res.ok) {
                toast.success('已创建并加入', `已将 "${song.title}" 加入新建的「我的跨平台收藏」。`);
                modal.close('#add-to-playlist-modal');
                cloudManager.reloadCatalog?.();
              } else {
                toast.error('创建失败', res.error?.message || '无法创建混合歌单。');
              }
            },
          },
        });
        listEl.appendChild(createElement('div', {
          className: 'empty-state',
          children: [
            createElement('div', { className: 'empty-state-title', text: '暂无可写入的歌单' }),
            createElement('div', {
              className: 'empty-state-desc',
              text: writableNeteaseTrack(song)
                ? '可登录网易云写入自建歌单，或直接创建本地跨平台混合歌单。'
                : '本地 / OpenSubsonic / WebDAV / 外源曲目可加入跨平台混合歌单。',
            }),
            createQuickBtn,
          ],
        }));
      }
    }
    modal.open('#add-to-playlist-modal');
  }

  // --- Deck Controls ---

  initDeckControls() {
    const playlistSelect = document.getElementById('deck-playlist-select');
    const filterSelect = document.getElementById('deck-filter-select');
    const orderSelect = document.getElementById('deck-order-select');
    const randomNextBtn = document.getElementById('deck-random-next-btn');
    const modeToggle = document.getElementById('deck-mode-toggle');
    const resetProgressBtn = document.getElementById('deck-reset-progress-btn');

    // Round 2 default: start Pruner Deck in 'random' order unless user has already selected another order
    if (!state.get('deckOrder') || state.get('deckOrder') === 'recent') {
      state.set({ deckOrder: 'random' });
    }
    if (orderSelect) {
      orderSelect.value = state.get('deckOrder') || 'random';
    }

    const dislikeBtn = document.getElementById('deck-dislike-btn');
    const likeBtn = document.getElementById('deck-like-btn');
    const relistenBtn = document.getElementById('deck-relisten-btn');
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

    if (randomNextBtn) {
      randomNextBtn.addEventListener('click', () => {
        deckManager.pickAnotherRandomSong();
      });
    }

    if (modeToggle) {
      const modeButtons = modeToggle.querySelectorAll?.('[data-deck-mode]') || [];
      const updateModeUI = () => {
        const isInstant = Boolean(state.get('instantModeEnabled'));
        modeToggle.classList.toggle('mode-instant', isInstant);
        if (modeButtons.length > 0) {
          modeButtons.forEach((btn) => {
            const active = (btn.dataset.deckMode === 'instant') === isInstant;
            btn.classList.toggle('active', active);
            btn.setAttribute('aria-pressed', active ? 'true' : 'false');
          });
        } else {
          modeToggle.textContent = isInstant ? '立即同步 3s' : '批量确认';
        }
      };
      updateModeUI();

      if (modeButtons.length > 0) {
        modeButtons.forEach((btn) => {
          btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const next = btn.dataset.deckMode === 'instant';
            if (Boolean(state.get('instantModeEnabled')) === next) return;
            state.setInstantMode(next);
            updateModeUI();
            toast.quiet(next ? '已切换至 3 秒即时同步模式' : '已切换至批量确认模式');
          });
        });
      }

      modeToggle.addEventListener('click', (e) => {
        if (modeButtons.length > 0 && e.target !== modeToggle) return;
        const next = !state.get('instantModeEnabled');
        state.setInstantMode(next);
        updateModeUI();
        toast.quiet(next ? '已切换至 3 秒即时同步模式' : '已切换至批量确认模式');
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
    if (relistenBtn) relistenBtn.addEventListener('click', () => deckManager.handleRelistenLaterAction());
    if (skipBtn) skipBtn.addEventListener('click', () => deckManager.handleSkipToEndAction());
    if (playBtn) {
      playBtn.addEventListener('click', (e) => {
        deckManager.toggleDeckPreviewPlayback();
        e.currentTarget?.blur?.();
      });
    }
    if (chorusBtn) chorusBtn.addEventListener('click', () => deckManager.jumpToChorus());
    if (undoBtn) undoBtn.addEventListener('click', () => deckManager.handleUndoAction());
  }

  // --- Catalog Controls ---

  initCatalogControls() {
    const rulesDrawerBtn = document.getElementById('catalog-open-rules-btn');
    const rulesDrawerCloseBtn = document.getElementById('rules-drawer-close-btn');
    if (rulesDrawerBtn) {
      rulesDrawerBtn.addEventListener('click', () => {
        rulesManager.openDrawer();
        this.updateDrawerStack?.('rules-drawer-panel', true);
      });
    }
    if (rulesDrawerCloseBtn) {
      rulesDrawerCloseBtn.addEventListener('click', () => {
        rulesManager.closeDrawer();
        this.updateDrawerStack?.('rules-drawer-panel', false);
        rulesDrawerBtn?.focus?.();
      });
    }

    const playAllBtn = document.getElementById('catalog-play-all-btn');
    const enterDeckBtn = document.getElementById('catalog-enter-deck-btn');
    if (playAllBtn) playAllBtn.addEventListener('click', () => catalogManager.playAllFiltered());
    if (enterDeckBtn) {
      enterDeckBtn.addEventListener('click', () => {
        const pid = enterDeckBtn.dataset.playlistId;
        if (pid) {
          state.set({ selectedPlaylistId: Number(pid) || pid });
          const deckSelect = document.getElementById('deck-playlist-select');
          if (deckSelect) deckSelect.value = String(pid);
          deckManager.reloadDeck();
        }
        this.switchTab('deck');
      });
    }

    const selectAllBtn = document.getElementById('catalog-select-all-btn');
    const clearSelectBtn = document.getElementById('catalog-clear-select-btn');
    const stagePruneBtn = document.getElementById('catalog-stage-prune-btn');
    const createPlaylistBtn = document.getElementById('catalog-create-playlist-btn');

    document.getElementById('catalog-play-selection')?.addEventListener('click', () => catalogManager.playSelected());
    if (selectAllBtn) selectAllBtn.addEventListener('click', () => catalogManager.selectAllFiltered());
    if (clearSelectBtn) clearSelectBtn.addEventListener('click', () => catalogManager.clearSelection());
    if (stagePruneBtn) stagePruneBtn.addEventListener('click', () => catalogManager.stageSelectedForPruning());
    state.on('playlist:create-from-rule', ({songs, name}) => catalogManager.openPlaylistCreateModal(songs, name));
    if (createPlaylistBtn) createPlaylistBtn.addEventListener('click', () => catalogManager.openPlaylistCreateModal());

    const selectPageCheckbox = document.getElementById('catalog-select-all');
    if (selectPageCheckbox) {
      selectPageCheckbox.addEventListener('change', (e) => catalogManager.selectCurrentPage(e.target.checked));
    }

    document.querySelectorAll('.catalog-table th.sortable').forEach(th => {
      th.addEventListener('click', () => catalogManager.setSort(th.dataset.sort));
    });

    const prevBtn = document.getElementById('catalog-page-prev');
    const nextBtn = document.getElementById('catalog-page-next');
    if (prevBtn) {
      prevBtn.addEventListener('click', () => {
        const page = state.get('catalogPage') || 1;
        state.set({ catalogPage: Math.max(1, page - 1) });
        catalogManager.render();
      });
    }
    if (nextBtn) {
      nextBtn.addEventListener('click', () => {
        const page = state.get('catalogPage') || 1;
        state.set({ catalogPage: page + 1 });
        catalogManager.render();
      });
    }
  }

  // --- Cloud Controls ---

  initCloudControls() {
    const openConfirmBtn = document.getElementById('cloud-open-batch-confirm-btn');
    const triggerBackupBtn = document.getElementById('cloud-backup-btn');
    const openImportBtn = document.getElementById('cloud-open-import-btn');

    if (openConfirmBtn) openConfirmBtn.addEventListener('click', () => cloudManager.openBatchConfirmationModal());
    if (triggerBackupBtn) triggerBackupBtn.addEventListener('click', () => cloudManager.triggerBackup());
    if (openImportBtn) openImportBtn.addEventListener('click', () => {
      const toggle = document.getElementById('import-replace');
      if (toggle) toggle.checked = false;
      modal.open('#import-modal');
    });

    const executeBatchBtn = document.getElementById('batch-confirm-execute-btn');
    if (executeBatchBtn) {
      executeBatchBtn.addEventListener('click', () => cloudManager.executeConfirmedBatch());
    }
  }

  // --- Modals & Forms ---

  initModalsAndForms() {
    const addRuleBtn = document.getElementById('rules-add-btn');
    const saveRuleBtn = document.getElementById('rule-editor-save-btn');
    if (addRuleBtn) addRuleBtn.addEventListener('click', () => rulesManager.openRuleEditor(null));
    if (saveRuleBtn) saveRuleBtn.addEventListener('click', () => rulesManager.saveCurrentRule());

    const submitCreatePlaylistBtn = document.getElementById('create-playlist-submit-btn');
    if (submitCreatePlaylistBtn) {
      submitCreatePlaylistBtn.addEventListener('click', async () => {
        if (this.createPlaylistPending) return;
        const nameInput = document.getElementById('create-playlist-name-input');
        const name = nameInput?.value.trim() || '新建云端歌单';
        const selectedKeys = new Set(Array.from(state.get('catalogSelectedIds') || []).map(String));
        const selectedIds = catalogManager.createPlaylistIds ?? catalogManager.writableIds(catalogManager.selectedSongs());

        if (selectedIds.length === 0) {
          toast.warning('请先选择歌曲', '请勾选至少一首曲目。');
          return;
        }

        this.createPlaylistPending = true;
        submitCreatePlaylistBtn.disabled = true;
        try {
          toast.info('正在创建歌单', `创建歌单 "${name}" 中...`);
          const res = await catalogManager.createInBatches(name, selectedIds, catalogManager.createPlaylistScope);
          const hasUnknown = Boolean(
            (Array.isArray(res.data?.unknown_items) && res.data.unknown_items.length > 0)
            || (Array.isArray(res.data?.pending_operation_ids) && res.data.pending_operation_ids.length > 0)
          );
          if (res.ambiguous || hasUnknown) {
            const pending = await api.getPendingOperations();
            if (pending.ok && Array.isArray(pending.data?.operations)) {
              state.restorePendingOperations(pending.data.operations);
            }
            const confirmedCount = Array.isArray(res.data?.successful_ids) ? res.data.successful_ids.length : 0;
            const unknownCount = Array.isArray(res.data?.unknown_items) ? res.data.unknown_items.length : 0;
            const unsentCount = Array.isArray(res.data?.unsent_ids)
              ? res.data.unsent_ids.length
              : Math.max(0, selectedIds.length - confirmedCount - unknownCount);
            const msg = unsentCount > 0
              ? `已确认添加 ${confirmedCount} 首，结果未知 ${unknownCount} 首，尚未发送 ${unsentCount} 首，请先在待处理操作或云端歌单中核对，切勿直接重复创建。`
              : `已确认添加 ${confirmedCount} 首，剩余 ${unknownCount} 首结果未知，请先在待处理操作或云端歌单中核对，切勿直接重复创建。`;
            toast.warning('创建/加歌结果待核对', msg);
            modal.close('#create-playlist-modal');
            await cloudManager.loadPlaylists();
          } else if (res.ok) {
            const count = Array.isArray(res.data?.successful_ids) ? res.data.successful_ids.length : 0;
            const failedItems = Array.isArray(res.data?.failed_items) ? res.data.failed_items : [];
            if (failedItems.length > 0 || count < selectedIds.length) {
              const confirmed = new Set(res.data.successful_ids);
              const retainedKeys = new Set(state.getCatalogRows()
                .filter(song => selectedKeys.has(state.catalogSongKey(song)) && !confirmed.has(Number(song.id)))
                .map(song => state.catalogSongKey(song)));
              if (!catalogManager.creatingRulePlaylist) state.set({ catalogSelectedIds: retainedKeys });
              catalogManager.render();
              toast.warning('歌单已创建，部分歌曲未添加', `已确认添加 ${count} 首，剩余 ${selectedIds.length - count} 首未确认添加。`);
            } else {
              toast.success('歌单已创建', `成功在云端创建 "${name}" (${count} 首)。`);
              if (!catalogManager.creatingRulePlaylist) catalogManager.clearSelection();
            }
            modal.close('#create-playlist-modal');
            await cloudManager.loadPlaylists();
          } else {
            const confirmedCount = Array.isArray(res.data?.successful_ids) ? res.data.successful_ids.length : 0;
            const unsentCount = Array.isArray(res.data?.unsent_ids)
              ? res.data.unsent_ids.length
              : Math.max(0, selectedIds.length - confirmedCount);
            toast.error('创建歌单失败', `${res.error?.message || '请求失败'}。已确认添加 ${confirmedCount} 首，剩余 ${unsentCount} 首未发送。`);
          }
        } finally {
          this.createPlaylistPending = false;
          submitCreatePlaylistBtn.disabled = false;
        }
      });
    }

    const qrReloadBtn = document.getElementById('qr-reload-btn');
    const shellQrReloadBtn = document.getElementById('shell-qr-reload-btn');
    const bilibiliQrReloadBtn = document.getElementById('bilibili-qr-reload-btn');
    if (qrReloadBtn) qrReloadBtn.addEventListener('click', () => auth.generateQr());
    if (shellQrReloadBtn) shellQrReloadBtn.addEventListener('click', () => auth.generateShellQr());
    if (bilibiliQrReloadBtn) bilibiliQrReloadBtn.addEventListener('click', () => auth.generateBilibiliQr());
  }
}

window.addEventListener('DOMContentLoaded', () => {
  const app = new App();
  app.init();
});
