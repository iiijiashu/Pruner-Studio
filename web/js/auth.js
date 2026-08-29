/**
 * Pruner Studio - Auth & Session Manager
 * Handles QR login lifecycle, immersive login shell, status polling, and account switching.
 */
import { api } from './api.js';
import { state } from './state.js';
import { modal } from './modal.js';
import { toast } from './toast.js';
import { isSafeUrl, getSafeImageUrl } from './utils.js';

class AuthManager {
  constructor() {
    this.pollInterval = null;
    this.currentQrKey = null;
    this.qrGeneration = 0;
    this.qrCreatePending = null;
    if (typeof window !== 'undefined') {
      window.addEventListener('pruner:modal-closed', (event) => {
        if (event.detail?.id === 'login-modal') this.stopPolling();
      });
    }
  }

  /**
   * Initializes session status by calling /api/status
   */
  async checkSession() {
    try {
      const res = await api.getStatus();
      if (res.ok && res.data) {
        if (res.data.authenticated) {
          state.set({
            authenticated: true,
            user: res.data.user || null,
          });
          state.loadUserPreferences();
          state.restorePendingOperations(res.data.pending_operations || []);
          this.updateUserUI(res.data.user);
          this.updateLoginShell(true);
          return true;
        }
      }
      state.set({ authenticated: false, user: null });
      this.updateUserUI(null);
      this.updateLoginShell(false);
      return false;
    } catch (err) {
      console.warn('Could not check session:', err);
      state.set({ authenticated: false, user: null });
      this.updateUserUI(null);
      this.updateLoginShell(false);
      return false;
    }
  }

  /**
   * Update header user badge UI safely without innerHTML
   * @param {Object|null} user
   */
  updateUserUI(user) {
    const userContainer = document.getElementById('user-status-container');
    if (!userContainer) return;

    userContainer.textContent = ''; // Safe clear

    if (user && state.get('authenticated')) {
      const avatarImg = document.createElement('img');
      avatarImg.className = 'user-avatar';
      avatarImg.alt = user.display_name || '用户头像';
      avatarImg.src = isSafeUrl(user.avatar_url) ? user.avatar_url : getSafeImageUrl('', '人');

      const nameSpan = document.createElement('span');
      nameSpan.className = 'user-name';
      const uid = user.user_id ?? user.id ?? '';
      nameSpan.textContent = user.display_name || `用户 ${uid}`;

      const logoutBtn = document.createElement('button');
      logoutBtn.className = 'btn btn-ghost btn-sm';
      logoutBtn.textContent = '退出';
      logoutBtn.title = '退出当前网易云登录会话';
      logoutBtn.addEventListener('click', () => this.logout());

      userContainer.appendChild(avatarImg);
      userContainer.appendChild(nameSpan);
      userContainer.appendChild(logoutBtn);
    } else {
      const loginBtn = document.createElement('button');
      loginBtn.className = 'btn btn-primary btn-sm';
      loginBtn.textContent = '扫码登录';
      loginBtn.addEventListener('click', () => this.openLoginModal());

      userContainer.appendChild(loginBtn);
    }
  }

  /**
   * Update or toggle the immersive unauthenticated login shell
   * @param {boolean} isAuthenticated
   */
  updateLoginShell(isAuthenticated) {
    const loginShell = document.getElementById('unauthenticated-shell');
    const mainViewport = document.getElementById('authenticated-main-viewport');

    if (loginShell && mainViewport) {
      if (isAuthenticated) {
        loginShell.classList.add('hidden');
        mainViewport.classList.remove('hidden');
      } else {
        loginShell.classList.remove('hidden');
        mainViewport.classList.add('hidden');
        // Auto trigger QR generation inside shell if empty
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
   * Keep QR creation in invocation order. The backend intentionally keeps only
   * one active key, so overlapping requests must not overtake each other.
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
        if (["QR_CANCELLED", "QR_EXPIRED"].includes(res.error?.code)) {
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
        // Expired
        setStatus('二维码已过期，请点击重新生成。');
        this.stopPolling();
      } else if (code === 801) {
        // Waiting for scan
        setStatus('等待扫码...');
      } else if (code === 802) {
        // Scanned, waiting for confirmation
        setStatus('已扫码，请在手机上点击确认登录。');
      } else if (code === 803) {
        // Success
        this.stopPolling();
        setStatus('登录成功！正在进入...');
        toast.success('登录成功', '网易云音乐账号已连接。');
        modal.close('#login-modal');
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
      // Stop delayed writes first. Cancellation callbacks persist the restored
      // review position while the authenticated user's storage scope exists.
      state.cancelAllCommits();
      const result = await api.logout();
      if (!result.ok) {
        toast.error('退出失败', result.error?.message || '无法清除登录会话。');
        return;
      }
      state.set({ authenticated: false, user: null });
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
