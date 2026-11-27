import { initPlaybackSettings } from './playback_settings.js';
/**
 * Pruner Studio - Cloud Operations, Frozen Batch Confirmation, SSE Sync Progress & Settings
 * Handles playlist sync with SSE progress, frozen manifest batch confirmation with session_generation guard,
 * read-only reconciliation recovery panel, backup/import/history, and complete app settings.
 */
import { api } from './api.js';
import { auth } from './auth.js';
import { state } from './state.js';
import { deckManager } from './deck.js';
import { player } from './player.js';
import { toast } from './toast.js';
import { modal } from './modal.js';
import { createElement, formatArtists, formatDuration, deepClone, computeManifestHash, getTrackKey } from './utils.js';
import { createIcon } from './icons.js';
import {
  themeManager,
  THEME_OPTIONS,
  ACCENT_PRESETS,
  DENSITY_OPTIONS,
  FONT_SCALE_OPTIONS,
} from './theme.js';

export const SSE_BACKOFF_DELAYS = [1000, 2000, 5000, 15000];

export const CHANNEL_CATALOG = [
  {
    id: 'kugou_lite',
    name: '酷狗概念版',
    subtitle: '概念版免费/会员无损，支持独立 KuGouMusicApi (platform=lite)',
    hasApiBase: true,
    apiBasePlaceholder: '可选：自建 KuGouMusicApi 地址（例如 http://127.0.0.1:3000）',
    secretFields: [
      { key: 'token', label: 'Token', placeholder: '酷狗概念版 token' },
      { key: 'userid', label: 'User ID', placeholder: '酷狗 userid' },
      { key: 'dfid', label: 'DFID', placeholder: '可选设备 dfid' },
      { key: 'cookie', label: 'Cookie', placeholder: '可选完整 Cookie' },
    ],
  },
  {
    id: 'migu',
    name: '咪咕音乐',
    subtitle: '华语版权补全与 SQ/ZQ 无损通道（严格限定 *.migu.cn）',
    hasApiBase: true,
    apiBasePlaceholder: '可选：自建咪咕解析服务地址',
    secretFields: [
      { key: 'cookie', label: 'Cookie', placeholder: '咪咕会员 Cookie' },
      { key: 'token', label: 'Token', placeholder: '可选访问 Token' },
    ],
  },
  {
    id: 'qq',
    name: 'QQ音乐',
    subtitle: '周杰伦等热门华语无损通道（严格限定 *.qq.com 或自建服务）',
    hasApiBase: true,
    apiBasePlaceholder: '可选：自建 QQ 音乐解析服务地址',
    secretFields: [
      { key: 'cookie', label: 'Cookie', placeholder: 'QQ 音乐 Cookie' },
      { key: 'uin', label: 'UIN', placeholder: '可选 QQ 号 / UIN' },
    ],
  },
  {
    id: 'kuwo',
    name: '酷我音乐',
    subtitle: '高覆盖老歌与无损回退源',
    hasApiBase: true,
    apiBasePlaceholder: '可选：自建酷我解析服务地址',
    secretFields: [
      { key: 'cookie', label: 'Cookie', placeholder: '可选酷我 Cookie' },
    ],
  },
  {
    id: 'bilibili',
    name: 'B站音频',
    subtitle: 'Live 现场 / Hi-Res FLAC DASH (30251/30250)，SESSDATA 仅发送至 *.bilibili.com',
    hasApiBase: false,
    secretFields: [
      { key: 'sessdata', label: 'SESSDATA', placeholder: 'B站大会员 SESSDATA（仅发往 bilibili.com）' },
    ],
  },
  {
    id: 'gdstudio',
    name: 'GDStudio 聚合',
    subtitle: '零 Cookie 公共聚合回退源，不接收任何账号凭证',
    hasApiBase: false,
    zeroCredential: true,
    secretFields: [],
  },
  {
    id: 'custom',
    name: 'HTTP Resolver v2 / 自定义解析服务',
    subtitle: '接入你信任的本机/私有 HTTP Resolver v2 服务（/manifest /search /resolve /lyric，绝不发送网易云 Cookie）',
    hasApiBase: true,
    apiBasePlaceholder: '例如 http://127.0.0.1:8080',
    secretFields: [
      { key: 'api_key', label: 'Token / Key', placeholder: '可选 X-Resolver-Token / API Key' },
    ],
  },
  {
    id: 'opensubsonic',
    name: 'OpenSubsonic / Navidrome',
    subtitle: '直连个人 Navidrome / Gonic / Subsonic 音乐库，支持无损直推与 ReplayGain 响度补偿',
    hasApiBase: true,
    apiBasePlaceholder: '例如 http://192.168.1.10:4533 或 https://music.example.com',
    extraFields: [
      { key: 'username', label: '用户名', placeholder: 'OpenSubsonic 用户名' },
    ],
    secretFields: [
      { key: 'password', label: '密码', placeholder: '密码（使用 MD5 加盐 token 发送）' },
      { key: 'api_key', label: 'API Key', placeholder: '可选 OpenSubsonic API Key' },
    ],
  },
  {
    id: 'webdav',
    name: 'WebDAV 音乐库与加密同步',
    subtitle: '浏览自建 WebDAV 曲库目录，并支持基于 AES-256-GCM 的去中心化端到端加密同步',
    hasApiBase: true,
    apiBasePlaceholder: '例如 https://dav.example.com/dav 或 http://192.168.1.10:5244/dav',
    extraFields: [
      { key: 'username', label: '用户名', placeholder: 'WebDAV 用户名' },
      { key: 'music_path', label: '曲库路径', placeholder: '例如 /Music' },
      { key: 'sync_path', label: '同步目录', placeholder: '默认 /pruner-sync' },
    ],
    secretFields: [
      { key: 'password', label: '密码', placeholder: 'WebDAV 密码（严格限定当前源）' },
    ],
  },
];

class CloudOperationsManager {
  constructor() {
    this.container = null;
    this.activeSubTab = 'settings'; // 'settings' | 'channels' | 'sync'
    this.pendingOperations = new Set();
    this.frozenBatchManifest = null;
    this.frozenManifestHash = null;
    this.frozenSessionGeneration = null;
    this.eventSource = null;
    this.sseReconnectTimer = null;
    this.sseReconnectAttempt = 0;
    this.sseManuallyDisconnected = false;
    this.channelDrafts = new Map();
    this.expandedChannelDetails = new Set();
    this.channelTestResults = new Map(); // channelId -> { ok, latency_ms, status, message, max_quality_hint }

    if (typeof window !== 'undefined') {
      window.addEventListener('pruner:modal-closed', (event) => {
        if (event.detail?.id === 'batch-confirm-modal' && !this.pendingOperations.has('batch_execute')) {
          this.frozenBatchManifest = null;
          this.frozenManifestHash = null;
          this.frozenSessionGeneration = null;
        }
      });
      window.addEventListener('online', () => {
        if (!this.eventSource && !this.sseManuallyDisconnected) {
          this.sseReconnectAttempt = 0;
          this.connectEvents();
        }
      });
    }
  }

  init(containerId) {
    if (typeof document !== 'undefined') {
      this.container = document.getElementById(containerId);
    }
    this.initSubTabs();
    this.initAccordion();
    this.initSettingsControls();
    if (typeof document !== 'undefined') {
      initPlaybackSettings(patch => this.saveSettings(patch));
      document.getElementById('settings-shortcuts-btn')?.addEventListener('click', () => state.emit('ui:open-shortcuts'));
      document.getElementById('cloud-channels-test-all-btn')?.addEventListener('click', () => this.testAllChannels());
      this.initProtocolAndMigrationControls();
    }

    state.on('change:pendingOperations', () => this.renderRecoveryPanel());
    state.on('change:hasUnknownResult', () => this.renderRecoveryPanel());
    const clearAccountProgress = () => {
      state.set({ syncProgress: null });
      const label = document.getElementById('cloud-batch-total-count');
      if (label) label.textContent = '';
    };
    state.on('change:sessionGeneration', clearAccountProgress);
    state.on('change:activeOfflineAccountId', clearAccountProgress);
    state.on('change:user', clearAccountProgress);
    state.on('change:syncProgress', () => this.renderSyncProgressBanner());
    state.on('change:batchQueue', () => this.updateSyncSubTabBadge());
    state.on('change:desktopLyricsOpen', () => this.renderSettings());
    state.on('change:desktopLyricsLocked', () => this.renderSettings());
    state.on('change:audioCacheStats', () => this.renderAudioCacheStats());
    state.on('change:channels', () => this.renderChannelsPanel());
    state.on('change:user', () => { this.channelDrafts.clear(); this.renderChannelsPanel(); });
    this.connectEvents();
  }

  initSubTabs() {
    if (typeof document === 'undefined') return;
    document.querySelectorAll('[data-cloud-subtab]').forEach(btn => {
      btn.addEventListener('click', () => {
        this.setSubTab(btn.dataset.cloudSubtab || 'settings');
      });
    });
    this.setSubTab(this.activeSubTab);
  }

  setSubTab(subTab = 'settings') {
    const allowed = ['settings', 'channels', 'sync'];
    const resolved = allowed.includes(subTab) ? subTab : 'settings';
    this.activeSubTab = resolved;
    if (typeof document === 'undefined') return;

    document.querySelectorAll('[data-cloud-subtab]').forEach(btn => {
      const isMatch = btn.dataset.cloudSubtab === resolved;
      btn.classList.toggle('active', isMatch);
      btn.setAttribute('aria-selected', isMatch ? 'true' : 'false');
    });

    const settingsPanel = document.getElementById('cloud-panel-settings');
    const channelsPanel = document.getElementById('cloud-panel-channels');
    const syncPanel = document.getElementById('cloud-panel-sync');
    if (settingsPanel) settingsPanel.classList.toggle('active', resolved === 'settings');
    if (channelsPanel) channelsPanel.classList.toggle('active', resolved === 'channels');
    if (syncPanel) syncPanel.classList.toggle('active', resolved === 'sync');
    this.updateSyncSubTabBadge();

    if (resolved === 'channels') {
      this.loadChannels().catch(() => {});
    } else if (resolved === 'settings') {
      this.loadAudioCacheStats().catch(() => {});
    }
  }

  updateSyncSubTabBadge() {
    if (typeof document === 'undefined') return;
    const badge = document.getElementById('cloud-subtab-sync-badge');
    if (!badge) return;
    const count = (state.get('batchQueue') || []).length;
    badge.textContent = String(count);
    badge.classList.toggle('hidden', count === 0);
  }

  initAccordion() {
    if (typeof document === 'undefined') return;
    const toggleBtn = document.getElementById('cloud-more-tools-toggle');
    const contentPanel = document.getElementById('cloud-more-tools-panel');

    if (toggleBtn && contentPanel) {
      toggleBtn.addEventListener('click', () => {
        const isOpen = contentPanel.classList.toggle('open');
        toggleBtn.classList.toggle('expanded', isOpen);
      });
    }
  }

  // --- SSE Real-Time Sync Progress & Global Hotkeys (/api/events) ---

  getSseBackoffDelay(attempt = this.sseReconnectAttempt) {
    const idx = Math.max(0, Math.min(Number(attempt) || 0, SSE_BACKOFF_DELAYS.length - 1));
    return SSE_BACKOFF_DELAYS[idx];
  }

  connectEvents() {
    if (typeof EventSource === 'undefined') return;

    if (this.eventSource) return;

    this.sseManuallyDisconnected = false;
    if (this.sseReconnectTimer) {
      clearTimeout(this.sseReconnectTimer);
      this.sseReconnectTimer = null;
    }

    try {
      const es = new EventSource('/api/events');
      this.eventSource = es;

      const resetBackoff = () => {
        if (this.eventSource === es) {
          this.sseReconnectAttempt = 0;
        }
      };

      es.onopen = resetBackoff;
      es.addEventListener?.('connected', resetBackoff);

      const currentProgress = payload => {
        const scope = state.writeScope();
        return payload?.session_generation === scope.session_generation &&
          String(payload.account_id) === scope.expected_account_id;
      };
      es.onmessage = (event) => {
        resetBackoff();
        try {
          const payload = JSON.parse(event.data);
          if (currentProgress(payload)) {
            state.set({ syncProgress: payload });
          }
        } catch {}
      };
      es.addEventListener('sync', (event) => {
        resetBackoff();
        try {
          const payload = JSON.parse(event.data);
          if (currentProgress(payload)) state.set({ syncProgress: payload });
        } catch {}
      });
      for (const name of ['lan_sync_status', 'lan_handoff', 'history_updated']) {
        es.addEventListener(name, event => {
          try { state.emit(name === 'history_updated' ? 'history:updated' : name, JSON.parse(event.data)); } catch {}
        });
      }
      es.addEventListener('window_fullscreen', event => {
        try { state.emit('window:fullscreen', JSON.parse(event.data).fullscreen); } catch {}
      });
      es.addEventListener('window_visibility', event => {
        try {
          const { visible, minimized } = JSON.parse(event.data);
          state.set({ windowVisible: Boolean(visible), windowMinimized: Boolean(minimized) });
        } catch {}
      });
      es.addEventListener('sync_progress', event => {
        try {
          const payload = JSON.parse(event.data);
          if (currentProgress(payload)) state.set({ syncProgress: payload });
        } catch {}
      });
      es.addEventListener('operation_progress', event => {
        try {
          const progress = JSON.parse(event.data);
          if (!currentProgress(progress)) return;
          const label = document.getElementById('cloud-batch-total-count');
          if (label) label.textContent = `已处理 ${progress.processed}/${progress.total} 首，已确认 ${progress.succeeded} 首`;
          if (progress.cooldown_sec > 0) {
            toast.warning('网易云限流', `写入已暂停，请在 ${Math.ceil(progress.cooldown_sec)} 秒后再试。`);
          }
        } catch {}
      });
      es.addEventListener('hotkey', (event) => {
        resetBackoff();
        try {
          const payload = JSON.parse(event.data);
          if (payload && payload.action) {
            player.handleGlobalHotkeyAction(payload.action, payload);
          }
        } catch {}
      });
      es.addEventListener('desktop_lyrics', (event) => {
        resetBackoff();
        try {
          const payload = JSON.parse(event.data);
          this.handleDesktopLyricsSseEvent(payload);
        } catch {}
      });
      es.addEventListener('desktop_lyrics_style', (event) => {
        resetBackoff();
        try {
          const payload = JSON.parse(event.data);
          this.handleDesktopLyricsStyleSseEvent(payload);
        } catch {}
      });
      es.addEventListener('channels', (event) => {
        resetBackoff();
        try {
          const payload = JSON.parse(event.data);
          this.handleChannelsSseEvent(payload);
        } catch {}
      });
      es.onerror = () => {
        if (this.eventSource !== es) return;
        this.closeEventSourceInstance(es);
        this.eventSource = null;
        if (!this.sseManuallyDisconnected) {
          this.scheduleEventsReconnect();
        }
      };
    } catch {
      this.eventSource = null;
      if (!this.sseManuallyDisconnected) {
        this.scheduleEventsReconnect();
      }
    }
  }

  closeEventSourceInstance(es) {
    if (!es) return;
    try {
      es.onopen = null;
      es.onmessage = null;
      es.onerror = null;
      es.close();
    } catch {}
  }

  scheduleEventsReconnect() {
    if (this.sseReconnectTimer || this.sseManuallyDisconnected) return null;


    const delay = this.getSseBackoffDelay(this.sseReconnectAttempt);
    this.sseReconnectAttempt += 1;
    this.sseReconnectTimer = setTimeout(() => {
      this.sseReconnectTimer = null;
      if (this.sseManuallyDisconnected) return;

      this.connectEvents();
    }, delay);
    return delay;
  }

  handleDesktopLyricsSseEvent(payload) {
    if (!payload || typeof payload !== 'object') return;
    const win = payload.window && typeof payload.window === 'object' ? payload.window : null;
    if (!win) return;
    const prevOpen = Boolean(state.get('desktopLyricsOpen'));
    const patch = {};
    const before = state.get('lyricsPreviewSubscribers') || 0;
    if (Number.isInteger(payload.preview_subscribers)) patch.lyricsPreviewSubscribers = payload.preview_subscribers;
    if (typeof win.open === 'boolean') patch.desktopLyricsOpen = win.open;
    if (typeof win.locked === 'boolean') patch.desktopLyricsLocked = win.locked;
    if (Object.keys(patch).length > 0) {
      state.set(patch);
      if ((patch.desktopLyricsOpen === true && !prevOpen) || patch.lyricsPreviewSubscribers > before) {
        player.pushDesktopLyricsState(true);
      }
    }
  }

  handleDesktopLyricsStyleSseEvent(payload) {
    if (!payload || typeof payload !== 'object') return;
    const style = payload.style && typeof payload.style === 'object' ? payload.style : payload;
    if (!style || typeof style !== 'object') return;
    const current = state.get('settings') || {};
    const patch = {
      desktop_lyrics_style: {
        ...(current.desktop_lyrics_style || {}),
        ...style,
      },
    };
    if (typeof payload.game_mode === 'boolean') {
      patch.game_mode = payload.game_mode;
    }
    state.updateSettings(patch);
  }

  disconnectEvents() {
    this.sseManuallyDisconnected = true;
    this.sseReconnectAttempt = 0;
    if (this.sseReconnectTimer) {
      clearTimeout(this.sseReconnectTimer);
      this.sseReconnectTimer = null;
    }
    if (this.eventSource) {
      const es = this.eventSource;
      this.eventSource = null;
      this.closeEventSourceInstance(es);
    }
    state.set({ syncProgress: null });
  }

  renderSyncProgressBanner() {
    if (typeof document === 'undefined') return;
    const banner = document.getElementById('cloud-sync-progress-banner');
    if (!banner) return;
    const progress = state.get('syncProgress');
    if (!progress || progress.status === 'done' || progress.status === 'idle' || progress.stage === 'done' || progress.stage === 'complete' || progress.stage === 'idle') {
      banner.classList.add('hidden');
      banner.textContent = '';
      return;
    }
    banner.classList.remove('hidden');
    let pct = 0;
    if (typeof progress.progress === 'number' && Number.isFinite(progress.progress)) {
      pct = progress.progress <= 1 ? Math.round(progress.progress * 100) : Math.round(progress.progress);
      pct = Math.max(0, Math.min(100, pct));
    } else {
      const loaded = Number(progress.loaded ?? progress.current ?? 0);
      const total = Number(progress.total ?? 0);
      pct = total > 0 ? Math.min(100, Math.round((loaded / total) * 100)) : 0;
    }
    const nameLabel = progress.playlist_name ? `「${progress.playlist_name}」` : '';
    const countLabel = typeof progress.count === 'number' ? ` · ${progress.count} 首` : '';
    banner.textContent = '';
    banner.appendChild(createElement('div', {
      className: 'sync-progress-text',
      text: progress.message || `正在同步歌单${nameLabel}${countLabel}${pct > 0 ? ` (${pct}%)` : '...'}`,
    }));
    if (pct > 0) {
      banner.appendChild(createElement('div', {
        className: 'sync-progress-track',
        children: [createElement('div', { className: 'sync-progress-fill', style: `width: ${pct}%;` })],
      }));
    }
  }

  // --- Read-Only Recovery Panel ---

  renderRecoveryPanel() {
    if (typeof document === 'undefined') return;
    const card = document.getElementById('cloud-recovery-card');
    const list = document.getElementById('cloud-recovery-list');
    if (!card || !list) return;

    const ops = state.get('pendingOperations') || [];
    const frozen = Boolean(state.get('hasUnknownResult') || state.get('destructiveFrozen'));
    if (!frozen && ops.length === 0) {
      card.classList.add('hidden');
      list.textContent = '';
      return;
    }

    card.classList.remove('hidden');
    list.textContent = '';

    if (ops.length === 0) {
      list.appendChild(createElement('div', {
        className: 'drawer-item-row',
        children: [
          createElement('span', { text: '存在待确认的云端操作，破坏性写入已临时冻结。' }),
          createElement('button', {
            className: 'btn btn-primary btn-sm',
            text: '刷新恢复状态',
            events: { click: () => deckManager.refreshRecoveredOperations() },
          }),
        ],
      }));
      return;
    }

    ops.forEach(op => {
      const songCount = Array.isArray(op.target?.song_ids) ? op.target.song_ids.length : 0;
      const actionLabel = { like: '喜欢', unlike: '取消喜欢', remove_tracks: '移出歌单',
        add_tracks: '添加曲目', create_playlist: '创建歌单' }[op.action] || op.action;
      const row = createElement('div', {
        className: 'drawer-item-row',
        children: [
          createElement('div', {
            children: [
              createElement('div', { className: 'drawer-item-title', text: `${actionLabel} (${songCount} 首)` }),
              createElement('div', { className: 'drawer-item-meta', text: `状态: ${op.state || 'sent_unknown'} · ID ${String(op.operation_id).slice(0, 8)}` }),
            ],
          }),
          createElement('div', {
            style: 'display: flex; gap: 6px;',
            children: [
              createElement('button', {
                className: 'btn btn-primary btn-sm',
                text: '只读核对',
                events: { click: () => deckManager.reconcileRecoveredOperation(op) },
              }),
              createElement('button', {
                className: 'btn btn-ghost btn-sm',
                text: '忽略',
                events: { click: () => deckManager.ignoreRecoveredOperation(op) },
              }),
            ],
          }),
        ],
      });
      list.appendChild(row);
    });
  }

  // --- Settings Panel & Multi-Channel Hub ---

  initSettingsControls() {
    if (typeof document === 'undefined') return;
    const qualitySelect = document.getElementById('settings-quality-select');
    const externalToggle = document.getElementById('settings-external-toggle');
    const loudnessToggle = document.getElementById('settings-loudness-toggle');
    const crossfadeSelect = document.getElementById('settings-crossfade-select');
    const blurDepthToggle = document.getElementById('settings-lyrics-blur-toggle');
    const bassGlowToggle = document.getElementById('settings-bass-glow-toggle');
    const amllTtmlToggle = document.getElementById('settings-amll-ttml-toggle');
    const hotkeysToggle = document.getElementById('settings-hotkeys-toggle');
    const cacheLimitSelect = document.getElementById('settings-cache-limit-select');
    const clearCacheBtn = document.getElementById('settings-clear-cache-btn');
    const desktopLyricsBtn = document.getElementById('settings-desktop-lyrics-toggle-btn');
    const desktopLyricsLockBtn = document.getElementById('settings-desktop-lyrics-lock-btn');
    const addLocalDirBtn = document.getElementById('settings-add-local-dir-btn');
    const localDirInput = document.getElementById('settings-local-dir-input');
    const scanLocalBtn = document.getElementById('settings-scan-local-btn');
    const clearFeedbackBtn = document.getElementById('settings-clear-feedback-btn');

    if (qualitySelect) {
      qualitySelect.addEventListener('change', (e) => {
        this.saveSettings({ quality_preference: e.target.value }, '音质偏好已保存');
      });
    }

    if (externalToggle) {
      externalToggle.addEventListener('change', (e) => {
        this.saveSettings({ enable_external_sources: Boolean(e.target.checked) }, e.target.checked ? '已开启多渠道外部音源' : '已关闭多渠道外部音源');
      });
    }

    document.querySelectorAll('[data-arbitration-btn]').forEach(btn => {
      btn.addEventListener('click', () => {
        const mode = btn.dataset.arbitrationBtn === 'netease_first' ? 'netease_first' : 'best_quality_race';
        this.saveSettings(
          { quality_arbitration: mode },
          mode === 'best_quality_race' ? '已切换为全渠道最高音质择优' : '已切换为网易云原曲优先',
        );
      });
    });

    if (loudnessToggle) {
      loudnessToggle.addEventListener('change', (e) => {
        this.saveSettings({ loudness_normalization: Boolean(e.target.checked) }, e.target.checked ? '已开启响度均衡' : '已关闭响度均衡');
      });
    }

    if (crossfadeSelect) {
      crossfadeSelect.addEventListener('change', (e) => {
        const ms = Math.max(0, Math.min(500, Number(e.target.value) || 0));
        this.saveSettings({ crossfade_ms: ms }, ms > 0 ? `同源歌曲交叉淡化已设为 ${ms}ms` : '已关闭同源歌曲交叉淡化');
      });
    }

    if (blurDepthToggle) {
      blurDepthToggle.addEventListener('change', (e) => {
        this.saveSettings({ lyrics_blur_depth: Boolean(e.target.checked) }, e.target.checked ? '已开启歌词景深虚化' : '已关闭歌词景深虚化');
      });
    }

    if (bassGlowToggle) {
      bassGlowToggle.addEventListener('change', (e) => {
        this.saveSettings({ audio_reactive_glow: Boolean(e.target.checked) }, e.target.checked ? '已开启低频光晕律动' : '已关闭低频光晕律动');
      });
    }

    if (amllTtmlToggle) {
      amllTtmlToggle.addEventListener('change', (e) => {
        this.saveSettings({ enable_amll_ttml_fallback: Boolean(e.target.checked) }, e.target.checked ? '已开启 AMLL 逐字歌词回退' : '已关闭 AMLL 逐字歌词回退');
      });
    }

    if (hotkeysToggle) {
      hotkeysToggle.addEventListener('change', (e) => {
        this.saveSettings({ global_hotkeys_enabled: Boolean(e.target.checked) }, e.target.checked ? '已开启全局媒体快捷键' : '已关闭全局媒体快捷键');
      });
    }

    const setGameMode = async (enabled) => {
      const next = Boolean(enabled);
      await this.saveSettings(
        { game_mode: next },
        next ? '游戏护航模式已开启：动效已冻结以零竞争运行' : '游戏护航模式已关闭',
      );
      player.pushDesktopLyricsState(true, {
        text: next ? '游戏护航已开启' : '游戏护航已关闭',
        kind: 'mode',
      });
    };

    const gameModeToggle = document.getElementById('settings-game-mode-toggle');
    if (gameModeToggle) {
      gameModeToggle.addEventListener('change', (e) => {
        setGameMode(Boolean(e.target.checked));
      });
    }
    const playerGameModeBtn = document.getElementById('player-game-mode-btn');
    if (playerGameModeBtn) {
      playerGameModeBtn.addEventListener('click', () => {
        setGameMode(!state.get('settings')?.game_mode);
      });
    }
    if (typeof window !== 'undefined') {
      window.addEventListener('pruner:toggle-game-mode', () => {
        setGameMode(!state.get('settings')?.game_mode);
      });
    }

    if (cacheLimitSelect) {
      cacheLimitSelect.addEventListener('change', async (e) => {
        const mb = Math.max(0, Math.min(4096, Number(e.target.value) || 0));
        await this.saveSettings({ audio_cache_limit_mb: mb }, mb > 0 ? `本地音频缓存上限已设为 ${mb} MB` : '已关闭本地音频磁盘缓存');
        this.loadAudioCacheStats().catch(() => {});
      });
    }

    if (clearCacheBtn) {
      clearCacheBtn.addEventListener('click', () => this.triggerClearAudioCache());
    }

    if (desktopLyricsBtn) {
      desktopLyricsBtn.addEventListener('click', () => player.toggleDesktopLyricsWindow());
    }

    if (desktopLyricsLockBtn) {
      desktopLyricsLockBtn.addEventListener('click', () => player.toggleDesktopLyricsLock());
    }

    this.renderAppearanceCard();

    document.querySelectorAll('[data-visual-mode-btn]').forEach(btn => {
      if (btn.dataset.visualBound === 'true') return;
      btn.dataset.visualBound = 'true';
      btn.addEventListener('click', () => {
        const mode = btn.dataset.visualModeBtn || 'soft_dynamic';
        this.saveSettings({ visual_mode: mode }, mode === 'soft_dynamic' ? '已切换为柔和动态模式' : '已切换为静谧省电模式');
      });
    });

    if (addLocalDirBtn && localDirInput) {
      addLocalDirBtn.addEventListener('click', () => {
        const dir = localDirInput.value.trim();
        if (!/^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+|\/(?!\/))/.test(dir) || /[\u0000-\u001f<>"|?*]/.test(dir)) {
          toast.quiet('请输入本地音乐文件夹的完整路径'); return;
        }
        const currentDirs = [...(state.get('settings')?.local_music_dirs || [])];
        if (!currentDirs.includes(dir)) {
          currentDirs.push(dir);
          this.saveSettings({ local_music_dirs: currentDirs }, '本地音乐目录已添加').then(ok => { if (ok && localDirInput.value.trim() === dir) localDirInput.value = ''; });
        }
      });
    }

    if (scanLocalBtn) {
      scanLocalBtn.addEventListener('click', () => this.triggerLocalLibraryScan());
    }

    if (clearFeedbackBtn) {
      clearFeedbackBtn.addEventListener('click', () => this.triggerClearFeedback());
    }
  }

  async updateAppearance(partial, successMsg = '外观设置已保存') {
    const nextAppearance = themeManager.setAppearance(partial);
    const cleanAppearance = {
      theme: nextAppearance.theme,
      accent_mode: nextAppearance.accent_mode,
      accent: nextAppearance.accent,
      density: nextAppearance.density,
      font_scale: nextAppearance.font_scale,
    };
    this.syncAppearanceCardUI();
    return this.saveSettings({ appearance: cleanAppearance }, successMsg);
  }

  renderAppearanceCard() {
    if (typeof document === 'undefined') return;
    const host = document.getElementById('settings-appearance-host');
    if (!host) return;
    if (host.querySelector?.('.appearance-card')) {
      this.syncAppearanceCardUI();
      return;
    }

    host.textContent = '';
    const app = themeManager.getAppearance();
    const currentVisual = state.get('settings')?.visual_mode || 'soft_dynamic';

    const themeGrid = createElement('div', {
      className: 'appearance-theme-grid',
      attributes: { role: 'group', 'aria-label': '主题风格' },
      children: THEME_OPTIONS.map(opt => createElement('button', {
        className: `appearance-theme-chip ${app.theme === opt.id ? 'active' : ''}`,
        dataset: { appearanceTheme: opt.id },
        attributes: {
          type: 'button',
          'aria-pressed': app.theme === opt.id ? 'true' : 'false',
        },
        events: {
          click: () => this.updateAppearance({ theme: opt.id }, `已切换至「${opt.label}」主题`),
        },
        children: [
          createElement('span', {
            className: 'theme-swatch-dot',
            style: `background: linear-gradient(135deg, ${opt.bg} 50%, ${opt.surface} 50%);`,
          }),
          createElement('span', { text: opt.label }),
        ],
      })),
    });

    const colorPicker = createElement('input', {
      className: 'accent-color-input',
      attributes: {
        type: 'color',
        id: 'settings-accent-color-picker',
        value: app.accent,
        title: '自定义强调色',
        'aria-label': '自定义强调色',
      },
      events: {
        input: (e) => {
          themeManager.setAppearance({ accent_mode: 'custom', accent: e.target.value });
          this.syncAppearanceCardUI();
        },
        change: (e) => {
          this.updateAppearance({ accent_mode: 'custom', accent: e.target.value }, '自定义强调色已保存');
        },
      },
    });
    colorPicker.value = app.accent;

    const accentRow = createElement('div', {
      className: 'appearance-accent-row',
      attributes: { role: 'group', 'aria-label': '强调色预设' },
      children: [
        ...ACCENT_PRESETS.map(preset => createElement('button', {
          className: `accent-swatch-btn ${app.accent_mode === 'preset' && app.accent === preset.hex ? 'active' : ''}`,
          dataset: { appearanceAccentPreset: preset.hex },
          attributes: {
            type: 'button',
            title: preset.label,
            'aria-label': `强调色 ${preset.label}`,
            'aria-pressed': app.accent_mode === 'preset' && app.accent === preset.hex ? 'true' : 'false',
          },
          events: {
            click: () => this.updateAppearance({ accent_mode: 'preset', accent: preset.hex }, `强调色已设为${preset.label}`),
          },
          children: [
            createElement('span', {
              className: 'accent-swatch-dot',
              style: `background-color: ${preset.hex};`,
            }),
            createElement('span', { className: 'accent-swatch-label', text: preset.label }),
          ],
        })),
        createElement('button', {
          className: `segmented-btn appearance-cover-accent-btn ${app.accent_mode === 'cover' ? 'active' : ''}`,
          text: '跟随封面',
          dataset: { appearanceAccentCover: 'true' },
          attributes: {
            type: 'button',
            'aria-pressed': app.accent_mode === 'cover' ? 'true' : 'false',
          },
          events: {
            click: () => this.updateAppearance({ accent_mode: 'cover' }, '强调色已切换为跟随封面'),
          },
        }),
        createElement('label', {
          className: 'accent-custom-picker-wrap',
          children: [
            colorPicker,
            createElement('span', {
              className: 'accent-hex-label numeral-mono',
              id: 'settings-accent-hex-label',
              text: app.accent.toUpperCase(),
            }),
          ],
        }),
      ],
    });

    const densityControl = createElement('div', {
      className: 'segmented-control',
      attributes: { role: 'group', 'aria-label': '界面密度' },
      children: DENSITY_OPTIONS.map(opt => createElement('button', {
        className: `segmented-btn ${app.density === opt.id ? 'active' : ''}`,
        text: opt.label,
        dataset: { appearanceDensity: opt.id },
        attributes: {
          type: 'button',
          'aria-pressed': app.density === opt.id ? 'true' : 'false',
        },
        events: {
          click: () => this.updateAppearance({ density: opt.id }, `界面密度已设为${opt.label}`),
        },
      })),
    });

    const scaleControl = createElement('div', {
      className: 'segmented-control',
      attributes: { role: 'group', 'aria-label': '字体缩放' },
      children: FONT_SCALE_OPTIONS.map(opt => createElement('button', {
        className: `segmented-btn ${Math.abs(app.font_scale - opt.value) < 0.01 ? 'active' : ''}`,
        text: opt.label,
        dataset: { appearanceScale: String(opt.value) },
        attributes: {
          type: 'button',
          'aria-pressed': Math.abs(app.font_scale - opt.value) < 0.01 ? 'true' : 'false',
        },
        events: {
          click: () => this.updateAppearance({ font_scale: opt.value }, `字体缩放已设为 ${opt.label}`),
        },
      })),
    });

    const visualControl = createElement('div', {
      className: 'segmented-control',
      attributes: { role: 'group', 'aria-label': '动态视觉模式' },
      children: [
        createElement('button', {
          className: `segmented-btn ${currentVisual === 'soft_dynamic' ? 'active' : ''}`,
          text: '柔和动态',
          dataset: { visualModeBtn: 'soft_dynamic', visualBound: 'true' },
          attributes: { type: 'button', 'aria-pressed': currentVisual === 'soft_dynamic' ? 'true' : 'false' },
          events: { click: () => this.saveSettings({ visual_mode: 'soft_dynamic' }, '已切换为柔和动态模式') },
        }),
        createElement('button', {
          className: `segmented-btn ${currentVisual !== 'soft_dynamic' ? 'active' : ''}`,
          text: '静谧省电',
          dataset: { visualModeBtn: 'quiet_eco', visualBound: 'true' },
          attributes: { type: 'button', 'aria-pressed': currentVisual !== 'soft_dynamic' ? 'true' : 'false' },
          events: { click: () => this.saveSettings({ visual_mode: 'quiet_eco' }, '已切换为静谧省电模式') },
        }),
      ],
    });

    const card = createElement('div', {
      className: 'cloud-section-card appearance-card',
      children: [
        createElement('div', {
          className: 'cloud-section-header',
          children: [
            createElement('div', {
              className: 'cloud-section-title',
              children: [
                createIcon('palette', { size: 15 }),
                createElement('span', { text: '外观' }),
              ],
            }),
            createElement('span', { className: 'settings-hint', text: '即时生效并自动同步至本机配置' }),
          ],
        }),
        createElement('div', {
          className: 'settings-grid',
          children: [
            createElement('div', {
              className: 'settings-block settings-block-wide',
              children: [
                createElement('span', { className: 'settings-label', text: '主题配色' }),
                themeGrid,
              ],
            }),
            createElement('div', {
              className: 'settings-block settings-block-wide',
              children: [
                createElement('span', { className: 'settings-label', text: '强调色（Keep 绿 / Prune 红保持恒定）' }),
                accentRow,
              ],
            }),
            createElement('div', {
              className: 'settings-block',
              children: [
                createElement('span', { className: 'settings-label', text: '界面密度' }),
                densityControl,
              ],
            }),
            createElement('div', {
              className: 'settings-block',
              children: [
                createElement('span', { className: 'settings-label', text: '字体缩放' }),
                scaleControl,
              ],
            }),
            createElement('div', {
              className: 'settings-block settings-block-wide settings-row-between',
              children: [
                createElement('div', {
                  children: [
                    createElement('span', { className: 'settings-label', text: '视觉动效档位' }),
                    createElement('p', { className: 'settings-hint', text: '控制全屏舞台流体背景与动画功耗。' }),
                  ],
                }),
                visualControl,
              ],
            }),
          ],
        }),
      ],
    });

    host.appendChild(card);
  }

  syncAppearanceCardUI() {
    if (typeof document === 'undefined') return;
    const host = document.getElementById('settings-appearance-host');
    if (!host) return;
    const app = themeManager.getAppearance();

    host.querySelectorAll('[data-appearance-theme]').forEach(btn => {
      const match = btn.dataset.appearanceTheme === app.theme;
      btn.classList.toggle('active', match);
      btn.setAttribute('aria-pressed', match ? 'true' : 'false');
    });

    host.querySelectorAll('[data-appearance-accent-preset]').forEach(btn => {
      const match = app.accent_mode === 'preset' && btn.dataset.appearanceAccentPreset === app.accent;
      btn.classList.toggle('active', match);
      btn.setAttribute('aria-pressed', match ? 'true' : 'false');
    });

    const coverBtn = host.querySelector('[data-appearance-accent-cover]');
    if (coverBtn) {
      const match = app.accent_mode === 'cover';
      coverBtn.classList.toggle('active', match);
      coverBtn.setAttribute('aria-pressed', match ? 'true' : 'false');
    }

    const picker = document.getElementById('settings-accent-color-picker');
    if (picker && picker.value.toLowerCase() !== app.accent) {
      picker.value = app.accent;
    }
    const hexLabel = document.getElementById('settings-accent-hex-label');
    if (hexLabel) {
      hexLabel.textContent = app.accent_mode === 'cover' ? '封面色' : app.accent.toUpperCase();
    }

    host.querySelectorAll('[data-appearance-density]').forEach(btn => {
      const match = btn.dataset.appearanceDensity === app.density;
      btn.classList.toggle('active', match);
      btn.setAttribute('aria-pressed', match ? 'true' : 'false');
    });

    host.querySelectorAll('[data-appearance-scale]').forEach(btn => {
      const match = Math.abs(Number(btn.dataset.appearanceScale) - app.font_scale) < 0.01;
      btn.classList.toggle('active', match);
      btn.setAttribute('aria-pressed', match ? 'true' : 'false');
    });
  }

  async loadSettings() {
    try {
      const res = await api.getSettings();
      if (res.ok && res.data) {
        const payload = res.data.settings || res.data;
        state.updateSettings(payload);
        if (payload.appearance && typeof payload.appearance === 'object') {
          themeManager.syncFromBackendSettings(payload.appearance);
        }
      } else {
        state.applyVisualMode();
      }
    } catch {
      state.applyVisualMode();
    }
    this.renderSettings();
    this.loadAudioCacheStats().catch(() => {});
  }

  async loadAudioCacheStats() {
    try {
      const res = await api.getAudioCacheStats();
      if (res.ok && res.data) {
        state.set({ audioCacheStats: res.data });
      }
    } catch {}
    this.renderAudioCacheStats();
  }

  renderAudioCacheStats() {
    if (typeof document === 'undefined') return;
    const el = document.getElementById('settings-cache-stats-text');
    if (!el) return;
    const stats = state.get('audioCacheStats');
    if (!stats) {
      el.textContent = '缓存统计加载中...';
      return;
    }
    const entries = Number(stats.entry_count) || 0;
    const totalMb = Number(stats.total_mb ?? ((Number(stats.total_bytes) || 0) / (1024 * 1024))).toFixed(1);
    const limitMb = Number(stats.limit_mb) || 0;
    el.textContent = stats.enabled === false || limitMb === 0
      ? '本地磁盘缓存已关闭'
      : `当前已缓存 ${entries} 首音频 · 已用 ${totalMb} MB / 上限 ${limitMb} MB`;
  }

  async triggerClearAudioCache() {
    const res = await api.clearAudioCache();
    if (res.ok) {
      const freedMb = ((Number(res.data?.freed_bytes) || 0) / (1024 * 1024)).toFixed(1);
      toast.success('音频缓存已清空', `共清理 ${res.data?.cleared_entries ?? 0} 个音频文件，释放 ${freedMb} MB。`);
      this.loadAudioCacheStats().catch(() => {});
    } else {
      toast.error('清理缓存失败', res.error?.message || '未能清空音频磁盘缓存。');
    }
  }

  // --- Multi-Channel & VIP Credential Hub (DPAPI Encrypted, Zero Raw Secret Display) ---

  handleChannelsSseEvent(payload) {
    if (!payload || typeof payload !== 'object') return;
    if (payload.results && typeof payload.results === 'object') {
      const summaryById = new Map((Array.isArray(payload.channels) ? payload.channels : []).map(ch => [String(ch.channel), ch]));
      for (const [chId, info] of Object.entries(payload.results)) {
        const rev = summaryById.get(chId)?.updated_at;
        this.channelTestResults.set(chId, { ...info, revision: rev });
      }
    }
    if (Array.isArray(payload.channels)) {
      state.set({ channels: payload.channels });
    } else {
      this.renderChannelsPanel();
    }
    state.emit('channels:updated', payload);
    if (payload.status === 'authorized') {
      const def = CHANNEL_CATALOG.find(item => item.id === payload.channel);
      toast.success(`${def?.name || payload.channel} 授权已保存`, payload.message || '凭据已通过本机 DPAPI 加密保存。');
    } else if (payload.status === 'timeout') {
      toast.warning('授权窗口超时', payload.message || '未检测到登录完成，可重试快捷登录。');
    }
  }

  async testAllChannels() {
    const btn = typeof document !== 'undefined' ? document.getElementById('cloud-channels-test-all-btn') : null;
    if (btn) {
      btn.disabled = true;
      btn.textContent = '探测中...';
    }
    try {
      const res = await api.testAllChannels();
      if (res.ok && res.data) {
        const channels = Array.isArray(res.data.channels) ? res.data.channels : (state.get('channels') || []);
        const summaryById = new Map(channels.map(ch => [String(ch.channel), ch]));
        const results = res.data.results || {};
        for (const [chId, info] of Object.entries(results)) {
          this.channelTestResults.set(chId, { ...info, revision: summaryById.get(chId)?.updated_at });
        }
        if (Array.isArray(res.data.channels)) {
          state.set({ channels: res.data.channels });
        }
        state.emit('channels:updated', { channel: 'all', status: 'probed', channels, results });
        const okCount = Object.values(results).filter(r => r?.ok).length;
        toast.success('全渠道探测完成', `${okCount} / ${Object.keys(results).length} 个渠道响应正常`);
      } else {
        toast.error('全渠道探测失败', res.error?.message || '请稍后重试。');
      }
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = '一键探测全部';
      }
      this.renderChannelsPanel();
    }
  }

  async loadChannels() {
    try {
      const res = await api.getChannels();
      if (res.ok && res.data) {
        const list = Array.isArray(res.data.channels) ? res.data.channels : (Array.isArray(res.data) ? res.data : []);
        state.set({ channels: list });
      }
    } catch {}
    this.renderChannelsPanel();
  }

  renderChannelsPanel() {
    if (typeof document === 'undefined') return;
    const grid = document.getElementById('cloud-channels-grid');
    if (!grid) return;
    grid.textContent = '';

    const cfg = state.get('settings') || {};
    const arb = cfg.quality_arbitration || 'best_quality_race';
    document.querySelectorAll('[data-arbitration-btn]').forEach(btn => {
      const match = btn.dataset.arbitrationBtn === arb;
      btn.classList.toggle('active', match);
      btn.setAttribute('aria-pressed', match ? 'true' : 'false');
    });

    const externalToggle = document.getElementById('settings-external-toggle');
    if (externalToggle) {
      externalToggle.checked = Boolean(cfg.enable_external_sources);
    }

    // 1. Primary NetEase Music Account Card
    const user = state.get('user');
    const isAuth = Boolean(state.get('authenticated'));
    const neteaseCard = createElement('div', {
      className: 'channel-card channel-card-primary',
      children: [
        createElement('div', {
          className: 'channel-card-header',
          children: [
            createElement('div', {
              className: 'channel-card-title-group',
              children: [
                createElement('div', { className: 'channel-card-name', text: '网易云音乐（主曲库与歌单）' }),
                createElement('div', {
                  className: 'channel-card-sub',
                  text: isAuth
                    ? `已登录：${user?.display_name || '网易云用户'}${user?.vip_type > 0 ? ' · 黑胶 VIP' : ''} · 凭证仅发往 music.163.com`
                    : '未登录，仅支持本地与离线缓存浏览。',
                }),
              ],
            }),
            createElement('span', {
              className: `badge ${isAuth ? 'badge-success' : 'badge-muted'}`,
              text: isAuth ? '已加密保存' : '未连接',
            }),
          ],
        }),
        createElement('div', {
          className: 'channel-isolation-note',
          children: [
            createIcon('shield', { size: 13 }),
            createElement('span', { text: '严格隔离：网易云 Cookie 与会话密钥绝不会发送给任何外部第三方渠道。' }),
          ],
        }),
      ],
    });
    grid.appendChild(neteaseCard);

    // 2. Multi-Channel Aggregation Cards backed by ChannelCredentialVault.public_summary()
    const summaryMap = new Map();
    (state.get('channels') || []).forEach(ch => {
      if (ch && ch.channel) summaryMap.set(String(ch.channel), ch);
    });

    CHANNEL_CATALOG.forEach(def => {
      const summary = summaryMap.get(def.id) || {
        channel: def.id,
        enabled: true,
        connected: false,
        has_credential: false,
        api_base: '',
        display_name: '',
        vip_label: '',
      };
      const lastProbe = this.channelTestResults.get(def.id);
      const testRes = lastProbe?.revision === summary.updated_at ? lastProbe : null;
      const card = this.buildChannelCard(def, summary, testRes);
      grid.appendChild(card);
    });
  }

  buildChannelCard(def, summary, testRes) {
    const enabledToggle = createElement('input', {
      attributes: {
        type: 'checkbox',
        'aria-label': `启用 ${def.name}`,
      },
      events: {
        change: async (e) => {
          const nextEnabled = Boolean(e.target.checked);
          const res = await api.updateChannel(def.id, { enabled: nextEnabled });
          if (res.ok) {
            toast.success(`${def.name}${nextEnabled ? '已启用' : '已停用'}`, '');
            this.loadChannels().catch(() => {});
          } else {
            e.target.checked = !nextEnabled;
            toast.error('更新渠道状态失败', res.error?.message || '请稍后重试。');
          }
        },
      },
    });
    enabledToggle.checked = Boolean(summary.enabled);

    const statusBadges = [];
    if (def.zeroCredential) {
      statusBadges.push(createElement('span', { className: 'badge badge-muted', text: '零凭证隔离' }));
    } else if (summary.has_credential) {
      statusBadges.push(createElement('span', {
        className: 'badge badge-success channel-dpapi-badge',
        children: [createIcon('shield', { size: 11 }), createElement('span', { text: '已加密保存' })],
      }));
    } else {
      statusBadges.push(createElement('span', { className: 'badge badge-muted', text: '未配置凭据' }));
    }
    if (summary.vip_label) {
      statusBadges.push(createElement('span', { className: 'badge badge-warning', text: summary.vip_label }));
    }

    const statusLineParts = [];
    if (def.zeroCredential) {
      statusLineParts.push(summary.verified ? '搜索可用' : '公开接口 · 未探测');
    } else if (summary.has_credential) {
      statusLineParts.push(summary.display_name ? `已配置 · ${summary.display_name}` : '已配置加密凭据');
      if (summary.vip_label) statusLineParts.push(summary.vip_label);
    } else {
      statusLineParts.push('免登录公开搜索 · 未绑定高级账号凭据');
    }
    if (testRes) {
      statusLineParts.push(
        testRes.ok
          ? `搜索可用 · ${testRes.latency_ms ?? 0}ms`
          : `异常 · ${testRes.message || testRes.status || '未连通'}`,
      );
    }

    const draft = this.channelDrafts.get(def.id) || {};
    const bindDraft = (input, key) => {
      if (Object.hasOwn(draft, key)) input.value = draft[key];
      input.addEventListener('input', () => { draft[key] = input.value; this.channelDrafts.set(def.id, draft); });
    };
    const bodyChildren = [];
    let apiBaseInput = null;
    if (def.hasApiBase) {
      apiBaseInput = createElement('input', {
        className: 'input-text channel-input',
        attributes: {
          type: 'text',
          value: summary.api_base || '',
          placeholder: def.apiBasePlaceholder || '可选：自建解析服务地址',
          'aria-label': `${def.name} API 地址`,
        },
      });
      bindDraft(apiBaseInput, 'api_base');
      bodyChildren.push(createElement('div', {
        className: 'channel-field-row',
        children: [
          createElement('label', { className: 'channel-field-label', text: 'API 地址' }),
          apiBaseInput,
        ],
      }));
    }

    const extraInputs = new Map();
    if (Array.isArray(def.extraFields)) {
      def.extraFields.forEach(field => {
        const inp = createElement('input', {
          className: 'input-text channel-input',
          attributes: {
            type: 'text',
            value: summary[field.key] || '',
            placeholder: field.placeholder || '',
            'aria-label': `${def.name} ${field.label}`,
          },
        });
        bindDraft(inp, field.key);
        extraInputs.set(field.key, inp);
        bodyChildren.push(createElement('div', {
          className: 'channel-field-row',
          children: [
            createElement('label', { className: 'channel-field-label', text: field.label }),
            inp,
          ],
        }));
      });
    }

    // Secret inputs are NEVER pre-populated with raw credentials!
    const secretInputs = new Map();
    def.secretFields.forEach(field => {
      const inp = createElement('input', {
        className: 'input-text channel-input',
        attributes: {
          type: 'password',
          autocomplete: 'off',
          placeholder: summary.has_credential
            ? '已加密保存（留空保持不变，输入新值即覆盖）'
            : field.placeholder,
          'aria-label': `${def.name} ${field.label}`,
        },
      });
      bindDraft(inp, field.key);
      secretInputs.set(field.key, inp);
      bodyChildren.push(createElement('div', {
        className: 'channel-field-row',
        children: [
          createElement('label', { className: 'channel-field-label', text: field.label }),
          inp,
        ],
      }));
    });

    const saveBtn = (def.hasApiBase || def.secretFields.length > 0 || extraInputs.size > 0)
      ? createElement('button', {
          className: 'btn btn-secondary btn-sm',
          text: '加密保存',
          attributes: { type: 'button' },
          events: {
            click: async () => {
              const payload = { enabled: Boolean(enabledToggle.checked) };
              if (apiBaseInput) payload.api_base = apiBaseInput.value.trim();
              for (const [k, inp] of extraInputs.entries()) {
                payload[k] = inp.value.trim();
              }
              for (const [k, inp] of secretInputs.entries()) {
                const v = inp.value.trim();
                if (v) payload[k] = v;
              }
              const res = await api.updateChannel(def.id, payload);
              if (res.ok) {
                this.channelDrafts.delete(def.id);
                secretInputs.forEach(inp => { inp.value = ''; });
                toast.success(`${def.name} 配置已加密保存`, '凭证已安全加密存储。');
                this.loadChannels().catch(() => {});
              } else {
                toast.error('保存失败', res.error?.message || '请检查地址格式或安全边界限制。');
              }
            },
          },
        })
      : null;

    const clearBtn = (!def.zeroCredential && def.secretFields.length > 0)
      ? createElement('button', {
          className: 'btn btn-ghost btn-sm text-danger',
          text: '清除凭证',
          attributes: { type: 'button', disabled: summary.has_credential ? null : 'disabled' },
          events: {
            click: async () => {
              const res = await api.updateChannel(def.id, { clear_credentials: true });
              if (res.ok) {
                this.channelDrafts.delete(def.id);
                toast.info(`${def.name} 凭证已清除`, '本地加密凭证已删除。');
                this.loadChannels().catch(() => {});
              } else {
                toast.error('清除失败', res.error?.message || '未能清除凭证。');
              }
            },
          },
        })
      : null;

    const scanButton = def.id === 'webdav' ? createElement('button', {
      className: 'btn btn-secondary btn-sm', text: '扫描 WebDAV 曲库',
      attributes: { type: 'button' },
      events: { click: async () => {
        scanButton.disabled = true;
        scanButton.textContent = '扫描中…';
        try {
          const result = await api.scanWebDav();
          if (!result.ok) throw new Error(result.error?.message || '扫描失败');
          toast.success('WebDAV 扫描完成', `已索引 ${result.data.scanned_count} 首歌曲`);
          state.emit('webdav:scanned');
        } catch (error) {
          toast.error('WebDAV 扫描失败', error.message);
        } finally {
          scanButton.textContent = '扫描 WebDAV 曲库';
          scanButton.disabled = false;
        }
      } },
    }) : null;

    const testBtn = createElement('button', {
      className: 'btn btn-secondary btn-sm',
      text: '测试连通性',
      attributes: { type: 'button' },
      events: {
        click: async () => {
          testBtn.disabled = true;
          testBtn.textContent = '测试中...';
          try {
            const res = await api.testChannel(def.id);
            if (res.ok && res.data) {
              this.channelTestResults.set(def.id, {...res.data, revision: summary.updated_at});
              if (res.data.ok) {
                toast.success(`${def.name} 搜索可用`, `${res.data.latency_ms ?? '-'}ms · ${res.data.message || '搜索可用'}`);
              } else {
                toast.warning(`${def.name} 连通受限`, res.data.message || res.data.status || '当前渠道响应异常');
              }
            } else {
              this.channelTestResults.set(def.id, { ok: false, revision: summary.updated_at, message: res.error?.message || '请求失败' });
              toast.error(`${def.name} 测试失败`, res.error?.message || '网络请求失败');
            }
          } finally {
            this.renderChannelsPanel();
          }
        },
      },
    });

    const credentialsDisclosure = bodyChildren.length > 0
      ? createElement('details', {
          className: 'channel-credentials-details',
          attributes: this.expandedChannelDetails.has(def.id) ? { open: 'open' } : {},
          events: {
            toggle: (e) => {
              if (e.target.open) this.expandedChannelDetails.add(def.id);
              else this.expandedChannelDetails.delete(def.id);
            },
          },
          children: [
            createElement('summary', {
              className: 'channel-credentials-summary',
              text: '高级凭据',
            }),
            createElement('div', {
              className: 'channel-credentials-body',
              children: [
                createElement('div', { className: 'channel-card-fields' }, bodyChildren),
                createElement('div', {
                  className: 'channel-card-actions',
                  children: [saveBtn, clearBtn].filter(Boolean),
                }),
              ],
            }),
          ],
        })
      : null;

    let quickAuthBtn = null;
    if (def.id === 'bilibili') {
      quickAuthBtn = createElement('button', {
        className: 'btn btn-primary btn-sm',
        text: '扫码登录',
        attributes: { type: 'button' },
        events: { click: () => auth.openBilibiliQrModal() },
      });
    } else if (['qq', 'kugou_lite', 'migu', 'kuwo'].includes(def.id)) {
      quickAuthBtn = createElement('button', {
        className: 'btn btn-primary btn-sm',
        text: '快捷登录',
        attributes: { type: 'button' },
        events: { click: () => auth.openChannelAuth(def.id, def.name) },
      });
    }

    const footerChildren = [
      createElement('div', {
        className: 'channel-card-actions',
        children: [quickAuthBtn, testBtn, scanButton].filter(Boolean),
      }),
    ];

    if (testRes) {
      const diagText = testRes.ok
        ? `搜索可用 · ${testRes.latency_ms ?? 0}ms`
        : `异常 · ${testRes.message || testRes.status || '未连通'}`;
      footerChildren.push(createElement('div', {
        className: `channel-test-status ${testRes.ok ? 'is-ok' : 'is-err'} numeral-mono`,
        text: diagText,
      }));
    }

    return createElement('div', {
      className: `channel-card ${summary.enabled ? '' : 'is-disabled'}`,
      dataset: { channelCard: def.id },
      children: [
        createElement('div', {
          className: 'channel-card-header',
          children: [
            createElement('div', {
              className: 'channel-card-title-group',
              children: [
                createElement('div', {
                  className: 'channel-card-name-row',
                  children: [
                    createElement('span', { className: 'channel-card-name', text: def.name }),
                    ...statusBadges,
                  ],
                }),
                createElement('div', { className: 'channel-card-sub', text: def.subtitle }),
                createElement('div', {
                  className: `channel-status-line ${testRes ? (testRes.ok ? 'is-ok' : 'is-err') : ''}`,
                  text: statusLineParts.join(' · '),
                }),
              ],
            }),
            createElement('label', {
              className: 'switch',
              children: [enabledToggle, createElement('span', { className: 'switch-slider' })],
            }),
          ],
        }),
        createElement('div', { className: 'channel-card-footer' }, footerChildren),
        credentialsDisclosure,
      ].filter(Boolean),
    });
  }

  async saveSettings(partial, successMsg = '设置已保存') {
    const previous = deepClone(state.get('settings') || {});
    state.updateSettings(partial);
    this.renderSettings();
    const write = (this.settingsWrite || Promise.resolve()).then(() => api.saveSettings(partial));
    this.settingsWrite = write;
    const res = await write;
    const current = state.get('settings') || {};
    const currentKeys = Object.keys(partial).filter(key => current[key] === partial[key]);
    if (!res.ok) {
      state.updateSettings(Object.fromEntries(currentKeys.map(key => [key, previous[key]])));
      if (currentKeys.includes('appearance') && previous.appearance) {
        themeManager.syncFromBackendSettings(previous.appearance);
      }
      this.renderSettings();
      toast.error('保存设置失败', res.error?.message || '未能写入本地设置。');
      return false;
    }
    if (res.data && typeof res.data === 'object') {
      const saved = res.data.settings || res.data;
      state.updateSettings(Object.fromEntries(currentKeys.filter(key => key in saved).map(key => [key, saved[key]])));
      if (currentKeys.includes('appearance') && saved.appearance && typeof saved.appearance === 'object') {
        themeManager.syncFromBackendSettings(saved.appearance);
      }
      this.renderSettings();
    }
    toast.success(successMsg, '');
    return true;
  }

  renderSettings() {
    if (typeof document === 'undefined') return;
    const cfg = state.get('settings') || {};
    this.renderAppearanceCard();
    this.syncAppearanceCardUI();

    const qualitySelect = document.getElementById('settings-quality-select');
    if (qualitySelect && cfg.quality_preference) {
      qualitySelect.value = cfg.quality_preference;
    }

    const externalToggle = document.getElementById('settings-external-toggle');
    if (externalToggle) {
      externalToggle.checked = Boolean(cfg.enable_external_sources);
    }

    const loudnessToggle = document.getElementById('settings-loudness-toggle');
    if (loudnessToggle) loudnessToggle.checked = cfg.loudness_normalization !== false;

    const crossfadeSelect = document.getElementById('settings-crossfade-select');
    if (crossfadeSelect) crossfadeSelect.value = String(cfg.crossfade_ms ?? 150);

    const blurDepthToggle = document.getElementById('settings-lyrics-blur-toggle');
    if (blurDepthToggle) blurDepthToggle.checked = Boolean(cfg.lyrics_blur_depth);

    const bassGlowToggle = document.getElementById('settings-bass-glow-toggle');
    if (bassGlowToggle) bassGlowToggle.checked = cfg.audio_reactive_glow !== false;

    const amllTtmlToggle = document.getElementById('settings-amll-ttml-toggle');
    if (amllTtmlToggle) amllTtmlToggle.checked = cfg.enable_amll_ttml_fallback !== false;

    const hotkeysToggle = document.getElementById('settings-hotkeys-toggle');
    const hotkeyStatus = document.getElementById('settings-hotkey-status');
    if (hotkeyStatus) {
      const flags = Object.values(cfg.hotkey_registration || {});
      hotkeyStatus.textContent = !cfg.global_hotkeys_enabled ? '已关闭' : !cfg.hotkeys_supported ? '当前环境不支持全局快捷键' :
        `已注册 ${flags.filter(Boolean).length}/${flags.length} 个快捷键`;
    }
    if (hotkeysToggle) hotkeysToggle.checked = cfg.global_hotkeys_enabled !== false;

    const gameModeToggle = document.getElementById('settings-game-mode-toggle');
    if (gameModeToggle) gameModeToggle.checked = Boolean(cfg.game_mode);
    const playerGameModeBtn = document.getElementById('player-game-mode-btn');
    if (playerGameModeBtn) {
      const gm = Boolean(cfg.game_mode);
      playerGameModeBtn.classList.toggle('active', gm);
      playerGameModeBtn.setAttribute('aria-pressed', String(gm));
    }

    const cacheLimitSelect = document.getElementById('settings-cache-limit-select');
    if (cacheLimitSelect) cacheLimitSelect.value = String(cfg.audio_cache_limit_mb ?? 512);

    const desktopLyricsBtn = document.getElementById('settings-desktop-lyrics-toggle-btn');
    if (desktopLyricsBtn) {
      desktopLyricsBtn.textContent = state.get('desktopLyricsOpen') ? '关闭桌面歌词' : '开启桌面歌词';
    }
    const desktopLyricsLockBtn = document.getElementById('settings-desktop-lyrics-lock-btn');
    if (desktopLyricsLockBtn) {
      desktopLyricsLockBtn.textContent = state.get('desktopLyricsLocked') ? '解锁鼠标穿透' : '锁定鼠标穿透';
    }

    // Local dirs list
    const dirsContainer = document.getElementById('settings-local-dirs-list');
    if (dirsContainer) {
      dirsContainer.textContent = '';
      const dirs = Array.isArray(cfg.local_music_dirs) ? cfg.local_music_dirs : [];
      if (dirs.length === 0) {
        dirsContainer.appendChild(createElement('div', {
          className: 'settings-muted-hint',
          text: '尚未配置本地无损音乐目录（支持 FLAC / MP3 / M4A / WAV / OGG）。',
        }));
      } else {
        dirs.forEach(dir => {
          const row = createElement('div', {
            className: 'local-dir-row',
            children: [
              createElement('span', { className: 'local-dir-path', text: dir }),
              createElement('button', {
                className: 'btn btn-ghost btn-icon btn-sm',
                attributes: { title: '移除目录', 'aria-label': `移除目录 ${dir}` },
                events: {
                  click: () => {
                    const nextDirs = (state.get('settings')?.local_music_dirs || []).filter(d => d !== dir);
                    this.saveSettings({ local_music_dirs: nextDirs }, '已移除本地目录');
                  },
                },
                children: [createIcon('close', { size: 13 })],
              }),
            ],
          });
          dirsContainer.appendChild(row);
        });
      }
    }

    // Visual mode segmented control
    const currentVisual = cfg.visual_mode || 'soft_dynamic';
    document.querySelectorAll('[data-visual-mode-btn]').forEach(btn => {
      const isSelected = btn.dataset.visualModeBtn === currentVisual;
      btn.classList.toggle('active', isSelected);
      btn.setAttribute('aria-pressed', isSelected ? 'true' : 'false');
    });

    // Arbitration segmented control (in channels tab)
    const arb = cfg.quality_arbitration || 'best_quality_race';
    document.querySelectorAll('[data-arbitration-btn]').forEach(btn => {
      const isSelected = btn.dataset.arbitrationBtn === arb;
      btn.classList.toggle('active', isSelected);
      btn.setAttribute('aria-pressed', isSelected ? 'true' : 'false');
    });
  }

  async triggerLocalLibraryScan() {
    if (this.pendingOperations.has('scan_local')) return;
    this.pendingOperations.add('scan_local');
    const statusEl = document.getElementById('settings-local-scan-status');
    if (statusEl) statusEl.textContent = '正在扫描本地曲库...';
    toast.info('正在扫描本地曲库', '正在索引已配置目录中的音频文件...');

    try {
      const dirs = state.get('settings')?.local_music_dirs || [];
      const res = await api.scanLocalLibrary(dirs);
      if (res.ok && res.data) {
        const count = res.data.scanned_count ?? res.data.scanned_files ?? res.data.total_indexed ?? res.data.count ?? (Array.isArray(res.data.files) ? res.data.files.length : 0);
        state.emit('local-library:scanned');
        const labels = {partial_failure: '部分目录扫描失败', unreadable_dirs: '目录无法读取', empty_dirs: '尚未配置目录'};
        const label = labels[res.data.status] || '扫描完成';
        const summary = `${label} · 本次扫描 ${count} 首 · 索引共 ${res.data.total_indexed ?? count} 首`;
        if (statusEl) statusEl.textContent = summary;
        toast.quiet(summary);
      } else {
        if (statusEl) statusEl.textContent = res.error?.message || '扫描失败';
        toast.error('扫描失败', res.error?.message || '请确认目录路径是否存在且可读。');
      }
    } finally {
      this.pendingOperations.delete('scan_local');
    }
  }

  async triggerClearFeedback() {
    const res = await api.clearFeedback();
    if (res.ok) {
      toast.success('推荐反馈已清除', '本地“不再推荐”及显式反馈记录已清空。');
    } else {
      toast.error('清除失败', res.error?.message || '未能清除本地推荐反馈。');
    }
  }

  // --- Playlists & Sync ---

  async loadPlaylists() {
    try {
      const res = await api.getPlaylists({ include_subscribed: true });
      if (res.ok && res.data) {
        const playlists = Array.isArray(res.data.playlists) ? res.data.playlists : (Array.isArray(res.data) ? res.data : []);
        const canonicalId = state.resolveCanonicalPlaylistId(playlists);
        state.set({ playlists, selectedPlaylistId: canonicalId });
        this.renderPlaylists(playlists);
        this.populatePlaylistDropdowns(playlists, canonicalId);
      }
    } catch (err) {
      console.warn('Failed to fetch playlists:', err);
    }
  }

  populatePlaylistDropdowns(playlists, selectedDeckId = state.get('selectedPlaylistId')) {
    const deckSelect = document.getElementById('deck-playlist-select');
    const catalogSelect = document.getElementById('catalog-playlist-filter');

    const updateSelect = (selectEl, includeAll = false, desiredValue = null, writableOnly = false) => {
      if (!selectEl) return;
      selectEl.textContent = '';

      if (includeAll) {
        selectEl.appendChild(createElement('option', { text: '全部歌单', attributes: { value: 'all' } }));
      }

      const list = writableOnly
        ? playlists.filter(pl => ['liked', 'self_created', 'owned', 'imported'].includes(pl.kind) || !pl.kind)
        : playlists;

      list.forEach(pl => {
        const kindLabel = pl.kind === 'liked'
          ? ' (我喜欢的音乐)'
          : (pl.kind === 'subscribed' ? ' (收藏·只读)' : '');
        const opt = createElement('option', {
          text: `${pl.name}${kindLabel} (${pl.track_count || 0})`,
          attributes: { value: String(pl.id) },
        });
        selectEl.appendChild(opt);
      });

      const normalizedDesired = desiredValue !== null && desiredValue !== undefined ? String(desiredValue) : '';
      if (normalizedDesired && Array.from(selectEl.options).some(o => o.value === normalizedDesired)) {
        selectEl.value = normalizedDesired;
      }
    };

    updateSelect(deckSelect, false, selectedDeckId, true);
    updateSelect(catalogSelect, true, state.get('catalogSelectedPlaylistId') || 'all', false);
  }

  renderPlaylists(playlists) {
    const container = document.getElementById('cloud-playlists-grid');
    if (!container) return;
    container.textContent = '';

    if (playlists.length === 0) {
      container.appendChild(createElement('div', {
        className: 'empty-state',
        children: [
          createElement('div', { className: 'empty-state-title', text: '暂无云端歌单' }),
          createElement('div', { className: 'empty-state-desc', text: '登录网易云音乐账号后将自动拉取红心、自建及收藏歌单。' }),
        ],
      }));
      return;
    }

    playlists.forEach(pl => {
      const syncBtn = createElement('button', {
        className: 'btn btn-secondary btn-sm',
        text: '同步',
        attributes: { disabled: state.get('offlineMode') ? 'disabled' : null },
        events: {
          click: () => this.syncPlaylist(pl.id, pl.name),
        },
      });

      const kindText = pl.kind === 'liked'
        ? '喜欢的音乐'
        : ((pl.kind === 'self_created' || pl.kind === 'owned') ? '自建歌单' : (pl.kind === 'subscribed' ? '收藏歌单 · 只读' : '本地导入'));

      const card = createElement('div', {
        className: 'playlist-card-item',
        children: [
          createElement('div', {
            className: 'playlist-info',
            children: [
              createElement('div', { className: 'playlist-name', text: pl.name }),
              createElement('div', {
                className: 'playlist-meta',
                children: [
                  createElement('span', { className: 'numeral-mono', text: `${pl.track_count || 0} 首曲目` }),
                  createElement('span', { className: 'badge badge-muted', text: kindText }),
                ],
              }),
            ],
          }),
          syncBtn,
        ],
      });

      container.appendChild(card);
    });
  }

  async syncPlaylist(playlistId, playlistName = '') {
    if (state.get('offlineMode')) {
      toast.warning('离线模式', '请先扫码登录网易云账号再同步歌单。');
      return;
    }
    const operationKey = `sync:${playlistId}`;
    if (this.pendingOperations.has(operationKey)) return;
    this.pendingOperations.add(operationKey);
    toast.info('正在同步歌单', `正在拉取 "${playlistName || playlistId}" 的最新曲目...`);

    try {
      const res = await api.syncPlaylist(playlistId);
      if (res.ok) {
        toast.success('同步完成', `已成功同步 ${res.data?.synced_count ?? ''} 首曲目。`);
        await this.reloadCatalog();
      } else {
        toast.error('同步失败', res.error?.message || '未能同步歌单。');
      }
    } catch (err) {
      toast.error('网络同步错误', err.message);
    } finally {
      this.pendingOperations.delete(operationKey);
      state.set({ syncProgress: null });
    }
  }

  async reloadCatalog() {
    try {
      const res = await api.getCatalog();
      if (res.ok && res.data) {
        const tracks = Array.isArray(res.data.tracks) ? res.data.tracks : (Array.isArray(res.data) ? res.data : []);
        state.set({ catalog: tracks });
        state.rebuildLikedSongIds(tracks);
        state.emit('catalog:loaded', tracks);
      }
    } catch (err) {
      console.warn('Failed to reload catalog:', err);
    }
  }

  // --- Grouped Pending Batch Queue & Frozen Manifest Confirmation ---

  renderGroupedBatchQueue() {
    const container = document.getElementById('cloud-batch-groups-container');
    const queueCountBadge = document.getElementById('cloud-batch-total-count');
    const executeBtn = document.getElementById('cloud-open-batch-confirm-btn');

    const queue = state.get('batchQueue') || [];

    if (queueCountBadge) {
      queueCountBadge.textContent = `${queue.length} 首待处理`;
    }

    if (executeBtn) {
      executeBtn.disabled = queue.length === 0 || state.get('offlineMode');
    }

    if (!container) return;
    container.textContent = '';

    if (queue.length === 0) {
      container.appendChild(createElement('div', {
        className: 'empty-state',
        children: [
          createElement('div', { className: 'empty-state-title', text: '暂无待执行的云端变更' }),
          createElement('div', { className: 'empty-state-desc', text: '在“歌单清理”或“音乐库”中选择移除的歌曲将暂存在此，经二次确认后批量同步。' }),
        ],
      }));
      return;
    }

    const groups = new Map();
    queue.forEach(item => {
      const key = `${item.action}:${item.playlistId || 'liked'}`;
      if (!groups.has(key)) {
        groups.set(key, {
          action: item.action,
          playlistId: item.playlistId,
          targetName: item.targetName || (item.action === 'unlike' ? '喜欢的音乐' : `歌单 #${item.playlistId}`),
          items: [],
        });
      }
      groups.get(key).items.push(item);
    });

    groups.forEach((group) => {
      const groupHeader = createElement('div', {
        className: 'batch-group-header',
        children: [
          createElement('div', {
            className: 'batch-group-title',
            children: [
              createElement('strong', { text: group.targetName }),
              createElement('span', {
                className: 'badge badge-danger',
                text: `${group.action === 'unlike' ? '取消喜欢' : '从歌单移出'} · ${group.items.length} 首`,
              }),
            ],
          }),
          createElement('button', {
            className: 'btn btn-ghost btn-sm text-danger',
            text: '清空本组',
            events: {
              click: () => {
                group.items.forEach(it => state.unstageBatchAction(it.song.id, it.playlistId));
                this.renderGroupedBatchQueue();
              },
            },
          }),
        ],
      });

      const itemsList = createElement('div', { className: 'batch-group-items-list' });
      group.items.forEach(it => {
        const removeBtn = createElement('button', {
          className: 'btn btn-ghost btn-icon btn-sm',
          attributes: { title: '从待处理队列移出', 'aria-label': '移出待处理队列' },
          events: {
            click: () => {
              const unstageKey = it.action === 'remove_hybrid_tracks'
                ? (getTrackKey(it.song) || it.song.id)
                : it.song.id;
              state.unstageBatchAction(unstageKey, it.playlistId, it.action);
              this.renderGroupedBatchQueue();
            },
          },
          children: [createIcon('close', { size: 13 })],
        });

        const row = createElement('div', {
          className: 'batch-item-row',
          children: [
            createElement('div', {
              children: [
                createElement('span', { className: 'batch-song-title', text: it.song.title || '未知曲目' }),
                createElement('span', { className: 'batch-song-artist', text: ` - ${formatArtists(it.song.artists)}` }),
              ],
            }),
            createElement('div', {
              style: 'display: flex; align-items: center; gap: 8px;',
              children: [
                ...(it.status === 'unknown'
                  ? [createElement('span', { className: 'badge badge-warning', text: '待只读核对' })]
                  : []),
                createElement('span', { className: 'batch-song-duration', text: formatDuration(it.song) }),
                removeBtn,
              ],
            }),
          ],
        });

        itemsList.appendChild(row);
      });

      const groupCard = createElement('div', {
        className: 'batch-group-card',
        children: [groupHeader, itemsList],
      });

      container.appendChild(groupCard);
    });
  }

  /**
   * Freezes the batch confirmation manifest at modal open so execution uses the EXACT items shown.
   */
  openBatchConfirmationModal() {
    const queue = state.get('batchQueue') || [];
    if (queue.length === 0) {
      toast.warning('待处理队列为空', '当前没有需要执行的云端变更。');
      return;
    }

    this.frozenWriteScope = state.writeScope();
    this.frozenBatchManifest = deepClone(queue);
    this.frozenManifestHash = computeManifestHash(this.frozenBatchManifest);
    this.frozenSessionGeneration = state.get('sessionGeneration') || 0;

    const summaryContainer = typeof document !== 'undefined'
      ? document.getElementById('batch-confirm-summary-list')
      : null;
    if (summaryContainer) {
      summaryContainer.textContent = '';

      const groups = new Map();
      this.frozenBatchManifest.forEach(item => {
        const key = `${item.action}:${item.playlistId || 'liked'}`;
        if (!groups.has(key)) {
          groups.set(key, {
            action: item.action,
            targetName: item.targetName || (item.action === 'unlike' ? '喜欢的音乐' : `歌单 #${item.playlistId}`),
            count: 0,
          });
        }
        groups.get(key).count++;
      });

      groups.forEach(g => {
        const actionLabel = g.action === 'unlike' ? '取消喜欢' : `从 ${g.targetName} 移出`;
        const row = createElement('div', {
          className: 'batch-confirm-row',
          children: [
            createElement('span', { text: actionLabel }),
            createElement('strong', { className: 'numeral-mono', text: `${g.count} 首` }),
          ],
        });
        summaryContainer.appendChild(row);
      });

      summaryContainer.appendChild(createElement('div', {
        className: 'batch-manifest-meta numeral-mono',
        text: `已锁定清单摘要: ${this.frozenManifestHash} (共 ${this.frozenBatchManifest.length} 首)`,
      }));
    }

    modal.open('#batch-confirm-modal');
  }

  /**
   * Execute Grouped Batch Actions using the exact frozen manifest (when opened via modal)
   * and verifying session_generation before writing.
   */
  async executeConfirmedBatch() {
    if (this.pendingOperations.has('batch_execute')) return;
    if (state.get('destructiveFrozen') || state.get('hasUnknownResult')) {
      toast.warning('批量操作已冻结', '请先处理当前未确定的云端结果；本次没有发送任何写请求。');
      return;
    }

    const usingFrozen = Array.isArray(this.frozenBatchManifest) && this.frozenBatchManifest.length > 0;
    const capturedGen = usingFrozen ? this.frozenSessionGeneration : (state.get('sessionGeneration') || 0);
    if (usingFrozen && (state.get('sessionGeneration') || 0) !== capturedGen) {
      toast.warning('登录会话已变更', '当前账号会话已变化，已中止批量执行以防误操作。');
      this.frozenBatchManifest = null;
      this.frozenManifestHash = null;
      this.frozenSessionGeneration = null;
      modal.close('#batch-confirm-modal');
      return;
    }

    const queue = usingFrozen ? deepClone(this.frozenBatchManifest) : [...(state.get('batchQueue') || [])];
    const manifestHash = usingFrozen ? this.frozenManifestHash : computeManifestHash(queue);
    this.frozenBatchManifest = null;
    this.frozenManifestHash = null;
    this.frozenSessionGeneration = null;

    if (queue.length === 0) return;

    this.pendingOperations.add('batch_execute');
    try {
      modal.close('#batch-confirm-modal');
      const groups = new Map();
      queue.forEach(item => {
        const key = `${item.action}:${item.playlistId || 'liked'}`;
        if (!groups.has(key)) {
          groups.set(key, {
            action: item.action,
            playlistId: item.playlistId,
            targetName: item.targetName || (item.action === 'unlike' ? '喜欢的音乐' : `歌单 #${item.playlistId}`),
            items: [],
          });
        }
        groups.get(key).items.push(item);
      });

      const hasNewWrites = queue.some(item => item.status !== 'unknown' && item.action !== 'remove_hybrid_tracks');
      if (hasNewWrites) {
        toast.info('自动快照中', '执行批量变更前正在创建本地备份快照...');
        let backupResult;
        try {
          backupResult = await api.backup({ reason: 'auto_snapshot_before_batch_execute', manifest_hash: manifestHash });
        } catch {
          backupResult = { ok: false };
        }
        if (!backupResult.ok) {
          toast.error('自动快照失败', '批量变更未执行，待处理队列保持不变。');
          return;
        }
        // Re-verify session generation after backup wait
        if ((state.get('sessionGeneration') || 0) !== capturedGen) {
          toast.warning('会话已变更', '备份期间登录会话变化，已安全中止云端写入。');
          return;
        }
      }

      let totalSuccess = 0;
      let totalUnresolved = 0;

      let riskPaused = false;
      for (const group of groups.values()) {
        if (riskPaused) {
          totalUnresolved += group.items.length;
          continue;
        }
        const isHybridGroup = group.action === 'remove_hybrid_tracks';
        const batchItemKey = (item) => (
          isHybridGroup
            ? (item.song?.track_key || getTrackKey(item.song) || String(item.song?.id ?? ''))
            : String(item.song.id)
        );
        const writableItems = group.items.filter(item => item.status !== 'unknown');
        const writableIds = writableItems.map(item => item.song.id);
        let unknownIds = group.items.filter(item => item.status === 'unknown').map(item => batchItemKey(item));
        const successful = new Set();
        const definiteFailed = new Set();

        if (writableItems.length > 0) {
          toast.info('正在提交变更', `正在处理 "${group.targetName}" (${writableItems.length} 首)...`);
          const groupManifestHash = computeManifestHash(writableItems);
          const extra = capturedGen > 0 || groupManifestHash
            ? { ...(usingFrozen ? this.frozenWriteScope : state.writeScope()), ...(capturedGen > 0 ? { session_generation: capturedGen } : {}), manifest_hash: groupManifestHash }
            : null;
          let result;
          try {
            if (isHybridGroup) {
              const tkeys = writableItems.map(item => batchItemKey(item)).filter(Boolean);
              result = await api.removeTracksFromHybridPlaylist(group.playlistId, tkeys);
              if (result.ok) {
                writableItems.forEach(item => successful.add(batchItemKey(item)));
              } else {
                writableItems.forEach(item => definiteFailed.add(batchItemKey(item)));
              }
              result = null;
            } else {
              result = group.action === 'unlike'
                ? await api.unlike(writableIds, group.playlistId, extra)
                : await api.removeTracks(group.playlistId, writableIds, group.playlistId, extra);
            }
          } catch {
            if (isHybridGroup) {
              writableItems.forEach(item => definiteFailed.add(batchItemKey(item)));
              result = null;
            } else {
              result = { ok: false, data: null };
            }
          }

          if (result && (result.data?.cooldown_sec > 0 || ['UPSTREAM_RISK_CONTROL', 'RISK_CONTROL_COOLDOWN'].includes(result.error?.code))) {
            riskPaused = true;
            toast.warning('网易云限流', result.error?.message || `写入已暂停，冷却约 ${Math.ceil(result.data.cooldown_sec)} 秒。`);
          }
          if (result && result.ok && result.data) {
            const resultSuccess = Array.isArray(result.data.successful_ids) ? result.data.successful_ids.map(String) : [];
            resultSuccess.forEach(id => successful.add(id));

            const failedItems = Array.isArray(result.data.failed_items) ? result.data.failed_items : [];
            failedItems.forEach(item => {
              const id = String(item.id);
              if (String(item.code || '').endsWith('_RESULT_UNKNOWN')) unknownIds.push(id);
              else definiteFailed.add(id);
            });

            const unknownItems = Array.isArray(result.data.unknown_items) ? result.data.unknown_items : [];
            unknownItems.forEach(item => unknownIds.push(String(item.id)));

            writableIds.map(String).forEach(id => {
              if (!successful.has(id) && !definiteFailed.has(id) && !unknownIds.includes(id)) unknownIds.push(id);
            });
          } else if (result && result.error?.code === 'RISK_CONTROL_COOLDOWN') {
            writableIds.forEach(id => definiteFailed.add(String(id)));
          } else if (result) {
            unknownIds.push(...writableIds.map(String));
          }
        }

        unknownIds = [...new Set(unknownIds)].filter(id => !successful.has(id));
        let stillUnknown = [...unknownIds];
        if (!riskPaused && !isHybridGroup && unknownIds.length > 0) {
          let reconcileResult;
          try {
            reconcileResult = await api.reconcileOperations(
              group.action,
              group.playlistId,
              unknownIds,
              group.playlistId,
            );
          } catch {
            reconcileResult = { ok: false, data: null };
          }
          if (reconcileResult.ok && reconcileResult.data) {
            const reconciledSuccess = Array.isArray(reconcileResult.data.successful_ids)
              ? reconcileResult.data.successful_ids.map(String)
              : [];
            reconciledSuccess.forEach(id => successful.add(id));
            const reconciledFailed = Array.isArray(reconcileResult.data.failed_items)
              ? reconcileResult.data.failed_items.map(item => String(item.id))
              : [];
            reconciledFailed.forEach(id => definiteFailed.add(id));
            const reconciledUnknown = Array.isArray(reconcileResult.data.unknown_items)
              ? reconcileResult.data.unknown_items.map(item => String(item.id))
              : [];
            stillUnknown = unknownIds.filter(id => !successful.has(id) && !definiteFailed.has(id));
            stillUnknown.push(...reconciledUnknown);
            stillUnknown = [...new Set(stillUnknown)];
          }
        }

        const successfulIds = [...successful];
        if (successfulIds.length > 0) {
          state.removeConfirmedSongs(successfulIds, group.playlistId, group.action);
          totalSuccess += successfulIds.length;
        }

        const successfulSet = new Set(successfulIds);
        const unknownSet = new Set(stillUnknown);
        const nextQueue = (state.get('batchQueue') || []).map(item => {
          const sameGroup = item.action === group.action && String(item.playlistId) === String(group.playlistId);
          const id = batchItemKey(item);
          if (!sameGroup || successfulSet.has(id)) return item;
          if (unknownSet.has(id)) return { ...item, status: 'unknown' };
          if (item.status === 'unknown') {
            const { status, ...rest } = item;
            return rest;
          }
          return item;
        });
        state.saveBatchQueue(nextQueue.filter(item => !(
          item.action === group.action
          && String(item.playlistId) === String(group.playlistId)
          && successfulSet.has(batchItemKey(item))
        )));

        const unresolvedCount = Math.max(0, group.items.length - successfulIds.length);
        totalUnresolved += unresolvedCount;
        state.addOperationLog({
          type: group.action === 'unlike' ? '批量取消喜欢' : '批量移出歌单',
          requested: group.items.length,
          successful: successfulIds.length,
          failed: unresolvedCount,
          targetName: group.targetName,
          message: `处理 ${group.targetName}: 成功 ${successfulIds.length}, 保留 ${unresolvedCount}`,
        });
      }

      if (totalUnresolved === 0) {
        toast.success('批量同步完成', `成功处理全部 ${totalSuccess} 首歌曲变更。`);
      } else {
        toast.warning('部分变更完成', `成功同步 ${totalSuccess} 首，${totalUnresolved} 首失败或待核对，已保留在队列中。`);
      }
      const pending = await api.getPendingOperations();
      if (pending.ok && Array.isArray(pending.data?.operations)) {
        state.restorePendingOperations(pending.data.operations);
      }
    } finally {
      this.pendingOperations.delete('batch_execute');
      this.renderGroupedBatchQueue();
      this.renderLogs();
      this.renderRecoveryPanel();
      await this.reloadCatalog();
    }
  }

  renderLogs() {
    const tbody = document.getElementById('cloud-ops-log-body');
    if (!tbody) return;
    tbody.textContent = '';

    const opsHistory = state.get('opsHistory') || [];

    if (opsHistory.length === 0) {
      tbody.appendChild(createElement('tr', {
        children: [createElement('td', { attributes: { colspan: '5', style: 'text-align: center; color: var(--text-muted);' }, text: '暂无操作记录。' })],
      }));
      return;
    }

    opsHistory.slice(0, 100).forEach(log => {
      const row = createElement('tr', {
        children: [
          createElement('td', { className: 'numeral-mono', text: log.timestamp || '-' }),
          createElement('td', { text: log.type || '-' }),
          createElement('td', { text: log.targetName || '默认目标' }),
          createElement('td', { className: 'numeral-mono', text: `${log.successful || 0} / ${log.requested || 0}` }),
          createElement('td', {
            children: [
              createElement('span', {
                className: `badge ${(log.failed || 0) === 0 ? 'badge-success' : 'badge-warning'}`,
                text: (log.failed || 0) === 0 ? '成功' : `部分失败 (${log.failed})`,
              }),
            ],
          }),
        ],
      });
      tbody.appendChild(row);
    });
  }

  async triggerBackup() {
    if (this.pendingOperations.has('backup')) return;
    this.pendingOperations.add('backup');
    toast.info('正在创建备份', '正在生成服务端快照并准备下载本地文件...');

    try {
      const res = await api.backup({ format: 'json' });
      if (!res.ok) {
        toast.error('备份失败', res.error?.message || '服务端创建快照失败。');
        return;
      }

      const blob = await api.downloadBackup(res.data?.backup_id);
      const downloadUrl = URL.createObjectURL(blob);
      try {
        const a = document.createElement('a');
        a.href = downloadUrl;
        a.download = `pruner-studio-backup-${res.data.backup_id}.json`;
        document.body.appendChild(a);
        a.click();
        a.remove();
      } finally {
        setTimeout(() => URL.revokeObjectURL(downloadUrl), 1000);
      }

      toast.success('备份已完成并开始下载', '服务端快照已落盘，已触发本地 JSON 文件下载。');
      state.addOperationLog({
        type: '本地备份',
        requested: 1,
        successful: 1,
        failed: 0,
        targetName: '全曲库快照',
        message: '创建服务端快照并触发客户端 JSON 下载',
      });
      this.renderLogs();
    } catch (err) {
      toast.error('备份异常', err.message);
    } finally {
      this.pendingOperations.delete('backup');
    }
  }

  initProtocolAndMigrationControls() {
    if (typeof document === 'undefined') return;
    document.getElementById('resolver-subscribe-btn')?.addEventListener('click', async () => {
      const input = document.getElementById('resolver-sub-url');
      const url = String(input?.value || '').trim();
      if (!url) { toast.warning('请输入订阅地址', '支持 HTTPS 或本机回环 Resolver v2 订阅 JSON。'); return; }
      const res = await api.subscribeResolvers(url);
      if (res.ok) {
        if (input) input.value = '';
        toast.success('订阅导入成功', `已导入 ${(res.data?.imported || []).length} 个 Resolver v2 节点`);
        this.refreshResolverAndSidecarPanel().catch(() => {});
      } else {
        toast.error('订阅导入失败', res.error?.message || '无法加载订阅清单');
      }
    });
    document.getElementById('sidecar-toggle-btn')?.addEventListener('click', async () => {
      const statusRes = await api.getSidecarStatus();
      const running = Boolean(statusRes.ok && statusRes.data?.running);
      const adapterInput = document.getElementById('sidecar-adapter-path');
      const res = running
        ? await api.stopSidecar()
        : await api.startSidecar(String(adapterInput?.value || '').trim());
      if (res.ok) {
        toast.success(running ? 'Sidecar 已停止' : 'Sidecar 隔离桥接已启动', res.data?.url || '已退出隔离进程');
        this.refreshResolverAndSidecarPanel().catch(() => {});
      } else {
        toast.error('Sidecar 操作失败', res.error?.message || '请检查脚本或适配器路径');
      }
    });
    document.getElementById('webdav-sync-push-btn')?.addEventListener('click', async () => {
      const pass = String(document.getElementById('webdav-sync-passphrase')?.value || '');
      if (pass.length < 6) { toast.warning('口令过短', '端到端加密同步口令至少需要 6 位字符。'); return; }
      const res = await api.pushWebDavSync(pass);
      const statusEl = document.getElementById('webdav-sync-status');
      if (res.ok) {
        const msg = `已加密推送序列 #${res.data?.seq ?? 1}（AES-256-GCM）`;
        if (statusEl) statusEl.textContent = msg;
        toast.success('WebDAV 加密推送成功', msg);
      } else {
        if (statusEl) statusEl.textContent = res.error?.message || '推送失败';
        toast.error('WebDAV 加密推送失败', res.error?.message || '请检查 WebDAV 配置与口令');
      }
    });
    document.getElementById('webdav-sync-pull-btn')?.addEventListener('click', async () => {
      const pass = String(document.getElementById('webdav-sync-passphrase')?.value || '');
      if (pass.length < 6) { toast.warning('口令过短', '请输入至少 6 位端到端解密口令。'); return; }
      const res = await api.pullWebDavSync(pass, 0);
      const statusEl = document.getElementById('webdav-sync-status');
      if (res.ok) {
        const msg = `已解密拉取并合并 ${res.data?.applied_events ?? 0} 个增量事件`;
        if (statusEl) statusEl.textContent = msg;
        toast.success('WebDAV 加密拉取成功', msg);
        this.reloadCatalog().catch(() => {});
      } else {
        if (statusEl) statusEl.textContent = res.error?.message || '拉取失败';
        toast.error('WebDAV 加密拉取失败', res.error?.message || '口令错误或远端数据不可读');
      }
    });
    this._migrationPreviewItems = [];
    document.getElementById('migration-preview-btn')?.addEventListener('click', async () => {
      const text = String(document.getElementById('migration-source-text')?.value || '').trim();
      const fmt = String(document.getElementById('migration-format-select')?.value || 'auto');
      const allowOnline = Boolean(document.getElementById('migration-online-checkbox')?.checked);
      if (!text) { toast.warning('请输入歌单内容', '支持 JSON、M3U/M3U8 或逐行“歌名 - 歌手”文本。'); return; }
      const res = await api.previewPlaylistMigration(text, fmt, allowOnline);
      if (!res.ok || !res.data) {
        toast.error('解析歌单失败', res.error?.message || '格式无效');
        return;
      }
      this._migrationPreviewItems = Array.isArray(res.data.items) ? res.data.items : [];
      const nameInput = document.getElementById('migration-target-name');
      if (nameInput && !nameInput.value.trim()) nameInput.value = res.data.suggested_name || '迁移歌单';
      this.renderMigrationPreview(res.data);
    });
    document.getElementById('migration-commit-btn')?.addEventListener('click', async () => {
      const checkedTracks = [];
      const renderedIndices = new Set();
      document.querySelectorAll('#migration-preview-list input[data-migration-idx]').forEach(cb => {
        const idx = Number(cb.dataset.migrationIdx);
        renderedIndices.add(idx);
        if (!cb.checked) return;
        const item = this._migrationPreviewItems[idx];
        const altSelect = document.querySelector(`#migration-preview-list select[data-migration-alt="${idx}"]`);
        const altIdx = altSelect ? Number(altSelect.value) : -1;
        const chosen = (altIdx >= 0 && Array.isArray(item?.alternatives) && item.alternatives[altIdx])
          ? item.alternatives[altIdx]
          : item?.matched_track;
        if (chosen) checkedTracks.push(chosen);
        else if (item?.source?.title) checkedTracks.push(item.source);
      });
      // Preserve matched items beyond any rendered cap so large playlists (>500 tracks) never drop tracks.
      this._migrationPreviewItems.forEach((item, idx) => {
        if (!renderedIndices.has(idx) && item?.matched_track) {
          checkedTracks.push(item.matched_track);
        }
      });
      if (!checkedTracks.length) { toast.warning('未勾选曲目', '请至少保留一首已匹配或手动确认的曲目。'); return; }
      const name = String(document.getElementById('migration-target-name')?.value || '迁移歌单').trim() || '迁移歌单';
      const res = await api.commitPlaylistMigration({ playlist_name: name, tracks: checkedTracks });
      if (res.ok) {
        toast.success('歌单迁移完成', `已创建混合歌单「${name}」（${checkedTracks.length} 首）`);
        this.reloadCatalog().catch(() => {});
      } else {
        toast.error('写入混合歌单失败', res.error?.message || '请重试');
      }
    });
    this.refreshResolverAndSidecarPanel().catch(() => {});
  }

  async refreshResolverAndSidecarPanel() {
    if (typeof document === 'undefined') return;
    const listEl = document.getElementById('resolver-v2-list');
    const sidecarStatusEl = document.getElementById('sidecar-status-text');
    const sidecarBtn = document.getElementById('sidecar-toggle-btn');
    if (!listEl && !sidecarStatusEl) return;
    const res = await api.getResolvers?.();
    if (!res?.ok || !res.data) return;
    if (sidecarStatusEl) {
      const sc = res.data.sidecar || {};
      sidecarStatusEl.textContent = sc.running
        ? `运行中 · ${sc.url}（零宿主机凭据隔离）`
        : '未启动（可选独立低权回环 Sidecar 桥接）';
      if (sidecarBtn) sidecarBtn.textContent = sc.running ? '停止 Sidecar' : '启动 Sidecar';
    }
    if (listEl) {
      listEl.textContent = '';
      const items = Array.isArray(res.data.resolvers) ? res.data.resolvers : [];
      if (!items.length) {
        listEl.appendChild(createElement('div', { className: 'settings-hint', text: '暂未订阅外部 HTTP Resolver v2 节点。' }));
      } else {
        items.forEach(item => {
          listEl.appendChild(createElement('div', {
            className: 'resolver-v2-row',
            children: [
              createElement('span', { text: `${item.name} (${item.url})` }),
              createElement('button', {
                className: 'btn btn-ghost btn-sm text-danger',
                text: '移除',
                attributes: { type: 'button' },
                events: {
                  click: async () => {
                    await api.deleteResolver(item.id);
                    this.refreshResolverAndSidecarPanel().catch(() => {});
                  },
                },
              }),
            ],
          }));
        });
      }
    }
  }

  renderMigrationPreview(data) {
    if (typeof document === 'undefined') return;
    const summaryEl = document.getElementById('migration-summary-text');
    const listEl = document.getElementById('migration-preview-list');
    const commitBtn = document.getElementById('migration-commit-btn');
    if (summaryEl && data.summary) {
      const s = data.summary;
      summaryEl.textContent = `共 ${s.total} 首：精确 ${s.exact} · 高置信 ${s.high} · 中置信 ${s.medium} · 待人工确认 ${s.unmatched}`;
    }
    if (commitBtn) commitBtn.disabled = !(data.items && data.items.length);
    if (!listEl) return;
    listEl.textContent = '';
    (data.items || []).slice(0, 500).forEach(item => {
      const matched = Boolean(item.matched_track);
      const cb = createElement('input', {
        attributes: { type: 'checkbox', 'data-migration-idx': String(item.index) },
      });
      cb.checked = matched;
      const badgeLabel = ({ exact: '精确匹配', high: '高置信', medium: '候选匹配', unmatched: '未匹配' })[item.confidence] || item.confidence;
      const children = [
        cb,
        createElement('span', { className: 'badge badge-muted', text: badgeLabel }),
        createElement('span', {
          text: matched
            ? `${item.source.title} -> ${item.matched_track.title} [${item.matched_track.provider}]`
            : `${item.source.title} - ${(item.source.artists || []).join(', ')}`,
        }),
      ];
      if (Array.isArray(item.alternatives) && item.alternatives.length > 1) {
        const altSel = createElement('select', {
          className: 'select-input select-sm',
          attributes: { 'data-migration-alt': String(item.index), 'aria-label': '选择替代候选' },
        });
        item.alternatives.forEach((alt, altIdx) => {
          altSel.appendChild(createElement('option', {
            attributes: { value: String(altIdx) },
            text: `${alt.title || ''} [${alt.provider || 'external'}]`,
          }));
        });
        children.push(altSel);
      }
      listEl.appendChild(createElement('label', {
        className: 'migration-preview-row',
        children,
      }));
    });
  }
}

export const cloudManager = new CloudOperationsManager();
