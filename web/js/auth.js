import { player } from './player.js';
/**
 * Pruner Studio - Auth & Session Manager
 * Handles QR login lifecycle, immersive login shell, offline cached library entry,
 * session_generation tracking, status polling, and account switching.
 */
import { api } from './api.js';
import { state } from './state.js';
import { modal } from './modal.js';
import { toast } from './toast.js';
import { isSafeUrl, getSizedCoverUrl, createElement } from './utils.js';

export function connectionBadge(status) {
  return ({configured: '已配置 · 未探测', reachable: '搜索可用',
    authenticated: '已验身份', writable: '可写歌单'})[status] || '未配置';
}

const AUTH_POPUP_CHANNELS = new Set(['qq', 'kugou_lite', 'migu', 'kuwo']);
const AUTH_CHANNEL_ORDER = ['bilibili', 'qq', 'kugou_lite', 'migu', 'kuwo'];
const PUBLIC_CHANNEL_IDS = new Set(['gdstudio', 'public']);
const ADVANCED_CHANNEL_IDS = new Set(['custom']);

class AuthManager {
  constructor() {
    this.pollInterval = null;
    this.currentQrKey = null;
    this.qrGeneration = 0;
    this.qrCreatePending = null;
    this.biliPollTimer = null;
    this.biliQrKey = null;
    this.biliQrGeneration = 0;
    this.hubProbeResults = new Map();
    if (typeof window !== 'undefined') {
      window.addEventListener('pruner:modal-closed', (event) => {
        if (event.detail?.id === 'login-modal') this.stopPolling();
        if (event.detail?.id === 'bilibili-qr-modal') this.stopBilibiliQrPolling();
      });
    }
    state.on?.('channels:updated', (payload) => {
      if (payload?.results && typeof payload.results === 'object') {
        for (const [chId, res] of Object.entries(payload.results)) {
          this.hubProbeResults.set(chId, res);
        }
      }
      if (typeof document === 'undefined') return;
      const panel = document.getElementById('account-hub');
      if (panel && panel.isConnected && panel.matches?.(':popover-open')) {
        this.renderHub(panel).catch(() => {});
      }
    });
  }

  /**
   * Initializes session status by calling /api/status.
   * Captures session_generation and cached_accounts for offline read-only library entry.
   */
  async checkSession() {
    if (typeof document !== 'undefined' && !document.getElementById('account-hub')) this.updateUserUI(state.get('user'));
    try {
      const res = await api.getStatus();
      if (res.ok && res.data) {
        const cachedAccounts = Array.isArray(res.data.cached_accounts) ? res.data.cached_accounts : [];
        const sessionGen = Number(res.data.session_generation) || (state.get('sessionGeneration') + 1);

        if (res.data.authenticated) {
          state.set({
            authenticated: true,
            offlineMode: false, guestLocalMode: false,
            activeOfflineAccountId: null,
            user: res.data.user || null,
            sessionGeneration: sessionGen,
            cachedAccounts,
          });
          state.loadUserPreferences();
          state.restorePendingOperations(res.data.pending_operations || []);
          this.updateUserUI(res.data.user);
          this.updateLoginShell(true);
          return true;
        }

        if (res.data.offline && res.data.active_account_id) {
          const offlineId = String(res.data.active_account_id);
          const matched = cachedAccounts.find(
            acc => String(acc?.account_id ?? acc?.user_id ?? acc?.id ?? '') === offlineId,
          );
          const offlineUser = matched ? {
            user_id: offlineId,
            display_name: matched.display_name || (offlineId === 'local' ? '本地音乐' : `缓存账号 ${offlineId}`),
            avatar_url: matched.avatar_url || '',
            vip_type: matched.vip_type || 0,
          } : {
            user_id: offlineId,
            display_name: offlineId === 'local' ? '本地音乐' : '离线曲库',
            avatar_url: '',
          };
          state.set({
            authenticated: false,
            offlineMode: true,
            guestLocalMode: offlineId === 'local',
            activeOfflineAccountId: offlineId,
            user: offlineUser,
            sessionGeneration: sessionGen,
            cachedAccounts,
          });
          state.loadUserPreferences();
          this.updateUserUI(offlineUser);
          this.updateLoginShell(true);
          state.emit('auth:offline', { accountId: offlineId, restored: true });
          return false;
        }

        state.set({
          authenticated: false,
          offlineMode: false,
          guestLocalMode: false,
          activeOfflineAccountId: null,
          user: null,
          sessionGeneration: sessionGen,
          cachedAccounts,
        });
        this.renderOfflineEntryPanel(cachedAccounts);
        this.updateUserUI(null);
        this.updateLoginShell(false);
        return false;
      }
      state.set({ authenticated: false, user: null });
      this.renderOfflineEntryPanel(state.get('cachedAccounts') || []);
      this.updateUserUI(null);
      this.updateLoginShell(false);
      return false;
    } catch (err) {
      console.warn('Could not check session:', err);
      state.set({ authenticated: false, user: null });
      this.renderOfflineEntryPanel(state.get('cachedAccounts') || []);
      this.updateUserUI(null);
      this.updateLoginShell(false);
      return false;
    }
  }

  /**
   * Renders offline read-only library entry options inside the login shell when cached accounts exist.
   */
  renderOfflineEntryPanel(cachedAccounts = []) {
    if (typeof document === 'undefined') return;
    const panel = document.getElementById('shell-offline-entry');
    const listEl = document.getElementById('shell-offline-accounts-list');
    if (!panel || !listEl) return;

    listEl.textContent = '';
    const accounts = Array.isArray(cachedAccounts) ? cachedAccounts.filter(Boolean) : [];
    if (accounts.length === 0) {
      panel.classList.add('hidden');
      return;
    }

    panel.classList.remove('hidden');
    accounts.forEach(acc => {
      const accId = acc.account_id ?? acc.user_id ?? acc.id ?? 'local';
      const label = acc.display_name || (accId === 'local' ? '本地曲库' : `缓存账号 ${accId}`);
      const countText = typeof acc.track_count === 'number' ? ` (${acc.track_count} 首)` : '';
      const btn = createElement('button', {
        className: 'btn btn-secondary btn-sm offline-account-btn',
        text: `离线浏览 · ${label}${countText}`,
        events: {
          click: () => this.enterOfflineMode(acc),
        },
      });
      listEl.appendChild(btn);
    });
  }

  /**
   * Enter read-only offline catalog mode for a cached account without being trapped at login wall.
   */
  enterGuestLocalMode(options) {
    return this.enterOfflineMode({ account_id: 'local', display_name: '本地音乐' }, options);
  }

  async enterOfflineMode(account = null, {openChannels = false} = {}) {
    this.stopPolling();
    const accId = account?.account_id ?? account?.user_id ?? account?.id ?? 'local';
    const previous = this.offlineSelection;
    const selection = this.offlineSelection = (async () => {
      if (previous) await previous.catch(() => {});
      return api.selectOfflineAccount?.(String(accId));
    })();
    let binding;
    try {
      binding = await selection;
      if (selection !== this.offlineSelection) return;
    } catch {
      if (selection === this.offlineSelection) toast.warning('离线账号切换失败', '请重试');
      return;
    } finally {
      if (selection === this.offlineSelection) this.offlineSelection = null;
    }
    if (binding && !binding.ok) {
      toast.warning('离线账号切换失败', binding.error?.message || '请重试');
      return;
    }
    if (binding?.data?.session_generation != null) state.set({sessionGeneration: binding.data.session_generation});
    state.set({
      authenticated: false,
      offlineMode: true, guestLocalMode: String(accId) === 'local',
      activeOfflineAccountId: accId ? String(accId) : null,
      user: account ? {
        user_id: accId,
        display_name: account.display_name || '离线曲库',
        avatar_url: account.avatar_url || '',
      } : null,
    });
    state.loadUserPreferences();
    this.updateUserUI(state.get('user'));
    this.updateLoginShell(true);
    if (String(accId) === 'local') {
      toast.info('已进入本地音乐空间', '可扫描本地目录或搜索已启用的音乐渠道。');
    } else {
      toast.info('已进入离线只读曲库', '可浏览已缓存曲目和本地音频，云端写入与同步需扫码登录。');
    }
    state.emit('auth:offline', { accountId: accId, openChannels });
  }

  /**
   * Update header user badge UI safely without innerHTML
   * @param {Object|null} user
   */
  updateUserUI(user) {
    if (typeof document === 'undefined') return;
    const userContainer = document.getElementById('user-status-container');
    if (!userContainer) return;

    userContainer.textContent = '';
    const trigger = createElement('button', {
      className: 'btn btn-ghost hub-trigger',
      attributes: {type: 'button', popovertarget: 'account-hub', 'aria-label': '多账号与多平台整合中心'},
    });
    if (user?.avatar_url) {
      trigger.append(createElement('img', {className: 'user-avatar', attributes: {
        src: getSizedCoverUrl(user.avatar_url, 64, '人'), alt: '',
      }}));
    }
    trigger.append(createElement('span', {text: user?.display_name || '账号与平台'}));
    trigger.append(createElement('span', {className: 'hub-chevron', attributes: {'aria-hidden': 'true'}}));
    document.getElementById("account-hub")?.remove();
    const panel = createElement('section', {className: 'account-hub', attributes: {
      id: 'account-hub', popover: 'auto', 'aria-label': '多账号与多平台整合中心',
    }});
    panel.addEventListener('toggle', e => {
      if (e.newState === 'open') {
        const rect = trigger.getBoundingClientRect?.();
        if (rect && typeof window !== 'undefined' && window.innerWidth > 0) {
          const rightPx = Math.max(16, Math.round(window.innerWidth - rect.right));
          panel.style.right = `${rightPx}px`;
        }
        this.renderHub(panel);
      }
    });
    userContainer.append(trigger);
    document.body.append(panel);
  }

  async prepareAccountChange() {
    state.cancelAllCommits();
    player.pauseAllAudio();
    player.accumulateListenTime();
    await Promise.all([player.scheduleSavePlaybackSession(true), player.maybeRecordGenuineListen(false, true)]);
  }

  async switchSavedAccount(id) {
    await this.prepareAccountChange();
    const result = await api.switchAccount(id);
    if (!result.ok) return toast.error('切换失败', result.error?.message);
    this.stopPolling();
    state.emit('auth:logout');
    if (await this.checkSession()) state.emit('auth:success', state.get('user'));
  }

  async openBilibiliQrModal() {
    modal.open('#bilibili-qr-modal');
    await this.generateBilibiliQr();
  }

  async generateBilibiliQr() {
    this.stopBilibiliQrPolling();
    const generation = ++this.biliQrGeneration;
    const qrBox = document.getElementById('bilibili-qr-image-container');
    const statusEl = document.getElementById('bilibili-qr-status-message');

    if (qrBox) {
      qrBox.textContent = '';
      const spinner = document.createElement('div');
      spinner.className = 'spinner';
      qrBox.appendChild(spinner);
    }
    if (statusEl) statusEl.textContent = '正在获取哔哩哔哩登录二维码...';

    const res = await api.createBilibiliQr();
    if (generation !== this.biliQrGeneration) return;
    if (!res.ok || !res.data?.qrcode_key) {
      if (qrBox) qrBox.textContent = '';
      if (statusEl) statusEl.textContent = res.error?.message || '获取 B站 二维码失败，请点击重新生成。';
      return;
    }

    this.biliQrKey = res.data.qrcode_key;
    const qrImg = res.data.qr_image;
    if (qrBox) {
      qrBox.textContent = '';
      if (qrImg && isSafeUrl(qrImg)) {
        const img = document.createElement('img');
        img.src = qrImg;
        img.alt = '哔哩哔哩扫码登录';
        img.className = 'qr-code-img';
        qrBox.appendChild(img);
      }
    }
    if (statusEl) statusEl.textContent = '请使用哔哩哔哩手机 App 扫码确认';
    this.startBilibiliQrPolling(this.biliQrKey);
  }

  startBilibiliQrPolling(qrcodeKey) {
    if (this.biliPollTimer) {
      clearTimeout(this.biliPollTimer);
      this.biliPollTimer = null;
    }
    const poll = async () => {
      if (!this.biliQrKey || this.biliQrKey !== qrcodeKey) {
        this.stopBilibiliQrPolling();
        return;
      }
      const res = await api.pollBilibiliQr(qrcodeKey);
      if (!this.biliQrKey || this.biliQrKey !== qrcodeKey) return;
      const statusEl = document.getElementById('bilibili-qr-status-message');
      if (!res.ok || !res.data) {
        this.biliPollTimer = setTimeout(poll, 2200);
        return;
      }
      const { state: qrState, message, channels, probe } = res.data;
      if (qrState === 'waiting') {
        if (statusEl) statusEl.textContent = message || '等待扫码...';
      } else if (qrState === 'scanned') {
        if (statusEl) statusEl.textContent = message || '已扫码，请在手机上确认登录';
      } else if (qrState === 'expired') {
        if (statusEl) statusEl.textContent = message || '二维码已过期，请点击重新生成。';
        this.stopBilibiliQrPolling();
        return;
      } else if (qrState === 'confirmed') {
        this.stopBilibiliQrPolling();
        if (statusEl) statusEl.textContent = '登录成功，已加密保存凭据。';
        if (probe) this.hubProbeResults.set('bilibili', probe);
        if (Array.isArray(channels)) state.set({ channels });
        state.emit('channels:updated', { channel: 'bilibili', status: 'authorized', channels });
        toast.success('B站 授权成功', '会话凭据已通过本机加密保存。');
        modal.close('#bilibili-qr-modal');
        return;
      }
      if (this.biliQrKey === qrcodeKey) {
        this.biliPollTimer = setTimeout(poll, 2000);
      }
    };
    this.biliPollTimer = setTimeout(poll, 1200);
  }

  stopBilibiliQrPolling() {
    this.biliQrGeneration += 1;
    this.biliQrKey = null;
    if (this.biliPollTimer) {
      clearTimeout(this.biliPollTimer);
      this.biliPollTimer = null;
    }
  }

  async openChannelAuth(channelId, channelLabel = channelId) {
    if (channelId === 'bilibili') {
      return this.openBilibiliQrModal();
    }
    const res = await api.openChannelAuthWindow(channelId);
    if (!res.ok) {
      toast.error(`${channelLabel} 快捷登录失败`, res.error?.message || '未能打开授权窗口。');
      return res;
    }
    if (res.data?.supported === false) {
      toast.info('当前为浏览器模式', res.data.message || `请在桌面端使用一键登录，或在「设置 → 多渠道与会员」中配置 ${channelLabel}。`);
      return res;
    }
    toast.info(`已打开 ${channelLabel} 登录窗口`, res.data?.message || '完成登录后将自动加密保存并关闭窗口。');
    return res;
  }

  async renderHub(panel) {
    const renderId = this.hubRenderId = (this.hubRenderId || 0) + 1;
    const [accounts, channels] = await Promise.all([api.getAccounts(), api.getChannels()]);
    if (!panel.isConnected || renderId !== this.hubRenderId) return;
    panel.replaceChildren();

    const makeBtn = (text, action, { variant = 'btn-secondary', id = null, title = null } = {}) => createElement('button', {
      className: `btn ${variant} btn-sm`,
      text,
      attributes: { type: 'button', ...(id ? { id } : {}), ...(title ? { title } : {}) },
      events: {
        click: async (event) => {
          const control = event.currentTarget;
          control.disabled = true;
          try { await action(); } finally { control.disabled = false; }
        },
      },
    });

    const dotClassFor = (connectionState, probe) => {
      if (probe) {
        if (probe.ok) return 'is-ok';
        if (probe.status === 'unconfigured') return 'is-muted';
        return 'is-err';
      }
      if (['authenticated', 'writable', 'reachable'].includes(connectionState)) return 'is-ok';
      if (connectionState === 'configured') return 'is-warn';
      return 'is-muted';
    };

    const capabilityHint = (probe) => {
      if (!probe) return '';
      const parts = [];
      if (typeof probe.latency_ms === 'number' && probe.latency_ms >= 0) parts.push(`${probe.latency_ms}ms`);
      const capLabel = {
        public_search: '公开搜索',
        authenticated_search: '授权检索',
        custom_endpoint: '自定义节点',
        unconfigured: '未配置',
      }[probe.capability];
      if (capLabel) parts.push(capLabel);
      else if (probe.message) parts.push(probe.message);
      return parts.join(' · ');
    };

    // 1. NetEase Multi-Account Section
    const accSection = createElement('div', { className: 'hub-section' });
    accSection.append(createElement('div', {
      className: 'hub-section-header',
      children: [createElement('h3', { text: '网易云多账号' })],
    }));

    if (!accounts.ok) {
      const errLine = createElement('div', { className: 'hub-row' });
      errLine.append(createElement('span', { text: accounts.error?.message || '读取账号失败' }));
      accSection.append(errLine);
    }

    for (const account of accounts.data?.saved_accounts || []) {
      const active = account.account_id === accounts.data.active_account_id;
      const line = createElement('div', { className: 'hub-row' });
      line.append(createElement('span', { text: account.display_name + (active ? ' · 当前账号' : '') }));
      if (!active || !state.get('authenticated')) {
        line.append(makeBtn('切换', () => this.switchSavedAccount(account.account_id)));
      }
      line.append(makeBtn('移除', async () => {
        if (active) await this.prepareAccountChange();
        const result = await api.deleteSavedAccount(account.account_id);
        if (!result.ok) return toast.error('移除失败', result.error?.message);
        if (active) {
          state.emit('auth:logout');
          await this.checkSession();
        } else {
          await this.renderHub(panel);
        }
      }, { variant: 'btn-ghost' }));
      accSection.append(line);
    }

    const actions = createElement('div', { className: 'hub-actions' });
    actions.append(makeBtn('扫码添加账号', () => {
      panel.hidePopover?.();
      return this.openLoginModal();
    }, { variant: 'btn-primary' }));
    actions.append(makeBtn('切换免登录本地模式', async () => {
      await this.prepareAccountChange();
      const result = await api.useLocalAccount();
      if (!result.ok) return toast.error('切换失败', result.error?.message);
      state.emit('auth:logout');
      state.set({ sessionGeneration: result.data.session_generation });
      await this.enterGuestLocalMode();
    }));
    if (state.get('authenticated')) {
      actions.append(makeBtn('退出当前账号', () => this.logout(), { variant: 'btn-ghost' }));
    }
    accSection.append(actions);
    panel.append(accSection);

    // 2. Multi-Platform Matrix with SINGLE "一键探测全部" button
    const platformSection = createElement('div', { className: 'hub-section' });
    const probeAllBtn = makeBtn('一键探测全部', async () => {
      const res = await api.testAllChannels();
      if (res.ok && res.data) {
        const results = res.data.results || {};
        for (const [chId, info] of Object.entries(results)) {
          this.hubProbeResults.set(chId, info);
        }
        if (Array.isArray(res.data.channels)) {
          state.set({ channels: res.data.channels });
          state.emit('channels:updated', { channel: 'all', status: 'probed', channels: res.data.channels, results });
        }
        const okCount = Object.values(results).filter(r => r?.ok).length;
        toast.success('全平台连通探测完成', `${okCount} / ${Object.keys(results).length} 个渠道响应正常`);
        await this.renderHub(panel);
      } else {
        toast.error('全平台探测失败', res.error?.message || '请稍后重试。');
      }
    }, { id: 'hub-probe-all-btn', variant: 'btn-secondary' });

    platformSection.append(createElement('div', {
      className: 'hub-section-header',
      children: [createElement('h3', { text: '多平台音源矩阵' }), probeAllBtn],
    }));

    const buildPlatformRow = (name, status, { probe = null, actionBtn = null } = {}) => {
      const line = createElement('div', { className: 'hub-row hub-platform-row' });
      const metaText = capabilityHint(probe);
      const textChildren = [createElement('span', { className: 'hub-platform-name', text: name })];
      if (metaText) textChildren.push(createElement('span', { className: 'hub-platform-meta', text: metaText }));

      const main = createElement('div', {
        className: 'hub-platform-main',
        children: [
          createElement('span', { className: `hub-status-dot ${dotClassFor(status, probe)}`, attributes: { 'aria-hidden': 'true' } }),
          createElement('div', { className: 'hub-platform-text', children: textChildren }),
        ],
      });
      const badgeText = probe?.ok ? '搜索可用' : connectionBadge(status);
      const rightChildren = [createElement('span', { className: 'badge badge-muted', text: badgeText })];
      if (actionBtn) rightChildren.push(actionBtn);
      line.append(main, createElement('div', { className: 'hub-row-right', children: rightChildren }));
      return line;
    };

    const channelList = channels.ok && Array.isArray(channels.data?.channels) ? channels.data.channels : [];
    const authChannels = [];
    const publicChannels = [];
    const advancedChannels = [];
    for (const ch of channelList) {
      const id = String(ch.channel || '');
      if (ADVANCED_CHANNEL_IDS.has(id)) advancedChannels.push(ch);
      else if (PUBLIC_CHANNEL_IDS.has(id) || ch.zero_credential) publicChannels.push(ch);
      else authChannels.push(ch);
    }
    authChannels.sort((a, b) => {
      const ia = AUTH_CHANNEL_ORDER.indexOf(String(a.channel || ''));
      const ib = AUTH_CHANNEL_ORDER.indexOf(String(b.channel || ''));
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    });

    // Group A: 账号授权解锁无损
    platformSection.append(createElement('div', { className: 'hub-group-label', text: '账号授权解锁无损' }));
    const neteaseState = state.get('authenticated')
      ? 'authenticated'
      : (accounts.data?.saved_accounts?.length ? 'configured' : 'unconfigured');
    platformSection.append(buildPlatformRow('网易云音乐', neteaseState, {
      actionBtn: state.get('authenticated')
        ? null
        : makeBtn('扫码登录', () => {
            panel.hidePopover?.();
            return this.openLoginModal();
          }, { variant: 'btn-ghost' }),
    }));

    if (!channels.ok) {
      const errLine = createElement('div', { className: 'hub-row' });
      errLine.append(createElement('span', { text: channels.error?.message || '读取平台配置失败' }));
      platformSection.append(errLine);
    }

    for (const ch of authChannels) {
      const chId = String(ch.channel || '');
      const label = ch.display_name || chId;
      let actionBtn = null;
      if (chId === 'bilibili') {
        actionBtn = makeBtn('扫码登录', () => {
          panel.hidePopover?.();
          return this.openBilibiliQrModal();
        }, { variant: 'btn-ghost' });
      } else if (AUTH_POPUP_CHANNELS.has(chId)) {
        actionBtn = makeBtn('快捷登录', () => {
          panel.hidePopover?.();
          return this.openChannelAuth(chId, label);
        }, { variant: 'btn-ghost' });
      }
      platformSection.append(buildPlatformRow(label, ch.connection_state, {
        probe: this.hubProbeResults.get(chId) || null,
        actionBtn,
      }));
    }

    // Group B: 即开即用公共源
    platformSection.append(createElement('div', { className: 'hub-group-label', text: '即开即用公共源' }));
    for (const ch of publicChannels) {
      const chId = String(ch.channel || '');
      platformSection.append(buildPlatformRow(ch.display_name || chId, ch.connection_state, {
        probe: this.hubProbeResults.get(chId) || null,
      }));
    }
    const localDirs = state.get('settings')?.local_music_dirs;
    platformSection.append(buildPlatformRow('本地曲库', Array.isArray(localDirs) && localDirs.length ? 'configured' : 'unconfigured'));
    panel.append(platformSection);

    // 3. Collapsible Advanced / Manual Credentials Disclosure
    const advBody = createElement('div', { className: 'hub-advanced-body' });
    for (const ch of advancedChannels) {
      const chId = String(ch.channel || '');
      advBody.append(buildPlatformRow(ch.display_name || chId, ch.connection_state, {
        probe: this.hubProbeResults.get(chId) || null,
      }));
    }
    advBody.append(makeBtn('管理凭据', () => {
      panel.hidePopover?.();
      if (!state.get('authenticated') && !state.get('offlineMode')) {
        return this.enterGuestLocalMode({ openChannels: true });
      }
      document.getElementById('tab-cloud')?.click();
      document.getElementById('cloud-subtab-channels')?.click();
    }, { variant: 'btn-secondary' }));

    const advDetails = createElement('details', {
      className: 'hub-advanced-details',
      children: [
        createElement('summary', { text: '高级 · 自定义接口 / 手动凭据' }),
        advBody,
      ],
    });
    panel.append(advDetails);
  }

  /**
   * Update or toggle the immersive unauthenticated login shell vs main app shell.
   * @param {boolean} showMainApp
   */
  updateLoginShell(showMainApp) {
    if (typeof document === 'undefined') return;
    const loginShell = document.getElementById('unauthenticated-shell');
    const mainViewport = document.getElementById('authenticated-main-viewport');
    const appShell = document.getElementById('app-shell-root');
    const playerBar = document.getElementById('catalog-player-bar');

    if (loginShell && mainViewport) {
      if (showMainApp) {
        loginShell.classList.add('hidden');
        mainViewport.classList.remove('hidden');
        if (appShell) appShell.classList.remove('is-unauthenticated');
        if (playerBar) playerBar.classList.add('active');
      } else {
        loginShell.classList.remove('hidden');
        mainViewport.classList.add('hidden');
        if (appShell) appShell.classList.add('is-unauthenticated');
        if (playerBar) playerBar.classList.remove('active');
        this.generateShellQr();
      }
    }
  }

  /**
   * Generate QR for the embedded login shell
   */
  async generateShellQr() {
    this.stopPolling();
    const generation = ++this.qrGeneration;
    const qrContainer = document.getElementById('shell-qr-image-container');
    const qrStatusText = document.getElementById('shell-qr-status-message');

    if (!qrContainer) return;
    qrContainer.textContent = '';
    const spinner = document.createElement('div');
    spinner.className = 'spinner';
    qrContainer.appendChild(spinner);

    if (qrStatusText) qrStatusText.textContent = '正在获取登录二维码...';

    const res = await this.enqueueQrCreation();
    if (generation !== this.qrGeneration) return;
    if (!res.ok || !res.data || !res.data.key) {
      if (qrStatusText) qrStatusText.textContent = res.error?.message || '生成登录二维码失败，请点击重新生成。';
      return;
    }

    this.currentQrKey = res.data.key;
    const qrImage = res.data.qr_image;

    qrContainer.textContent = '';
    if (qrImage && isSafeUrl(qrImage)) {
      const img = document.createElement('img');
      img.src = qrImage;
      img.alt = '网易云音乐扫码登录';
      img.className = 'qr-code-img';
      qrContainer.appendChild(img);
    }

    if (qrStatusText) qrStatusText.textContent = '请使用网易云音乐手机 App 扫码确认';
    this.startPolling(this.currentQrKey);
  }

  /**
   * Open QR Login Modal and start QR generation
   */
  async openLoginModal() {
    if (state.get('authenticated')) await this.prepareAccountChange();
    modal.open('#login-modal');
    await this.generateQr();
  }

  /**
   * Generate new QR login key and start polling
   */
  async generateQr() {
    this.stopPolling();
    const generation = ++this.qrGeneration;

    const qrContainer = document.getElementById('qr-image-container');
    const qrStatusText = document.getElementById('qr-status-message');

    if (qrContainer) {
      qrContainer.textContent = '';
      const spinner = document.createElement('div');
      spinner.className = 'spinner';
      qrContainer.appendChild(spinner);
    }

    if (qrStatusText) {
      qrStatusText.textContent = '正在生成二维码...';
    }

    const res = await this.enqueueQrCreation();
    if (generation !== this.qrGeneration) return;
    if (!res.ok || !res.data || !res.data.key) {
      if (qrStatusText) {
        qrStatusText.textContent = res.error?.message || '生成二维码失败。';
      }
      return;
    }

    this.currentQrKey = res.data.key;
    const qrImage = res.data.qr_image;

    if (qrContainer) {
      qrContainer.textContent = '';
      if (qrImage && isSafeUrl(qrImage)) {
        const img = document.createElement('img');
        img.src = qrImage;
        img.alt = '网易云登录二维码';
        img.className = 'qr-code-img';
        qrContainer.appendChild(img);
      } else {
        const message = document.createElement('p');
        message.className = 'empty-state-desc';
        message.textContent = '未能获取二维码图像，请重新尝试。';
        qrContainer.appendChild(message);
      }
    }

    if (qrStatusText) {
      qrStatusText.textContent = '请使用网易云音乐 App 扫码登录';
    }

    this.startPolling(this.currentQrKey);
  }

  /**
   * Keep QR creation in invocation order.
   */
  enqueueQrCreation() {
    const request = this.qrCreatePending
      ? this.qrCreatePending.then(() => api.createQr(), () => api.createQr())
      : Promise.resolve(api.createQr());
    const tail = request.then(() => undefined, () => undefined);
    this.qrCreatePending = tail;
    tail.finally(() => {
      if (this.qrCreatePending === tail) this.qrCreatePending = null;
    });
    return request;
  }

  /**
   * Poll status of QR code every 2 seconds
   * @param {string} key
   */
  startPolling(key) {
    if (this.pollInterval) {
      clearTimeout(this.pollInterval);
      this.pollInterval = null;
    }

    const poll = async () => {
      if (!this.currentQrKey || this.currentQrKey !== key) {
        this.stopPolling();
        return;
      }

      const res = await api.checkQr(key);
      if (!this.currentQrKey || this.currentQrKey !== key) return;

      const modalStatus = document.getElementById('qr-status-message');
      const shellStatus = document.getElementById('shell-qr-status-message');

      const setStatus = (txt) => {
        if (modalStatus) modalStatus.textContent = txt;
        if (shellStatus) shellStatus.textContent = txt;
      };

      if (!res.ok) {
        if (['QR_CANCELLED', 'QR_EXPIRED'].includes(res.error?.code)) {
          setStatus('二维码已取消或过期，请点击重新生成。');
          this.stopPolling();
          return;
        }
        this.pollInterval = setTimeout(poll, 2000);
        return;
      }

      const data = res.data;
      if (!data) {
        this.pollInterval = setTimeout(poll, 2000);
        return;
      }

      const code = data.code;

      if (code === 800) {
        setStatus('二维码已过期，请点击重新生成。');
        this.stopPolling();
      } else if (code === 801) {
        setStatus('等待扫码...');
      } else if (code === 802) {
        setStatus('已扫码，请在手机上点击确认登录。');
      } else if (code === 803) {
        this.stopPolling();
        setStatus('登录成功！正在进入...');
        toast.success('登录成功', '网易云音乐账号已连接。');
        modal.close('#login-modal');
        state.emit('auth:logout');
        await this.checkSession();
        state.emit('auth:success', state.get('user'));
        return;
      }
      if (this.currentQrKey === key) this.pollInterval = setTimeout(poll, 2000);
    };
    this.pollInterval = setTimeout(poll, 0);
  }

  stopPolling() {
    this.qrGeneration += 1;
    this.currentQrKey = null;
    if (this.pollInterval) {
      clearTimeout(this.pollInterval);
      this.pollInterval = null;
    }
  }

  async logout() {
    try {
      await this.prepareAccountChange();
      const result = await api.logout();
      if (!result.ok) {
        toast.error('退出失败', result.error?.message || '无法清除登录会话。');
        return;
      }
      const nextGen = (state.get('sessionGeneration') || 0) + 1;
      state.set({
        authenticated: false,
        offlineMode: false, guestLocalMode: false,
        activeOfflineAccountId: null,
        user: null,
        sessionGeneration: nextGen,
      });
      this.updateUserUI(null);
      this.updateLoginShell(false);
      toast.info('已退出登录', '当前会话已清除。');
      state.emit('auth:logout');
    } catch (err) {
      toast.error('退出登录异常', err.message);
    }
  }
}

export const auth = new AuthManager();
