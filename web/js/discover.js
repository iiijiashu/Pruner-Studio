/**
 * Pruner Studio - Discover & Native/Local-First Recommendations (发现)
 * Default online landing featuring:
 * - 2-Column Listening Hero: Personal FM (私人漫游) + True Heartbeat Smart Radio (心动电台 with 20%-80% explore slider)
 * - Compact Local Listening Insights bar (本地听歌洞察 via GET /api/insights)
 * - Dusty Gems strip (沉睡宝藏 via GET /api/recommend/dusty-gems)
 * - Daily Recommend (每日推荐 30 tracks) with Play All & explicit "不再推荐"
 * - Recent Played (最近播放)
 */
import { api } from './api.js';
import { state } from './state.js';
import { player } from './player.js';
import { toast } from './toast.js';
import { getTrackKey, createElement, formatArtists, formatDuration, formatSeconds, getSizedCoverUrl, createPlayingEqBars, syncPlayingRowIndicators } from './utils.js';
import { createIcon } from './icons.js';
import { openSongContextMenu, quickNextButton } from './context_menu.js';

class DiscoverManager {
  constructor() {
    this.container = null;
    this.loadingDaily = false;
    this.loadingFm = false;
    this.loadingSmartRadio = false;
    this.loadingDustyGems = false;
    this.loadedForUserId = null;
    this.smartRadioBreakdown = null;
    this.onAddToPlaylistCallback = null;
    this.exploreSaveTimer = null;
    this.settingsWrite = Promise.resolve();
    this.epoch = 0;
    this.dustySeen = new Map();
    this.exploreSeen = new Map();

    // Braun / Dieter Rams Analog Radio Tuner Dial state (pure SVG, zero canvas GPU burn)
    this.orbCanvas = null;
    this.orbCurrentRatio = 0.4;
    this.orbTargetRatio = 0.4;
    this.orbInView = true;
    this.orbWindowBlurred = false;
    this.orbMotionQuery = null;
    this.orbObserver = null;
    this.tunerTickElements = [];
    this.dustySpotlightTimer = null;
  }

  init(containerId, { onAddToPlaylist } = {}) {
    if (typeof document !== 'undefined') {
      this.container = document.getElementById(containerId);
    }
    this.onAddToPlaylistCallback = onAddToPlaylist || null;
    this.bindSmartRadioControls();
    for (const key of ['isPlaying', 'personalFmQueue']) state.on('change:' + key, () => {
      this.renderFmCard();
      this.syncTunerPlayingState();
    });
    state.on('change:radioExhausted', () => this.renderHeroCards());
    state.on('change:settings', () => {
      const toggle = typeof document !== 'undefined' ? document.getElementById('settings-fm-sync') : null;
      if (toggle) toggle.checked = Boolean(state.get('settings')?.sync_fm_trash_to_netease);
      this.renderFmCard();
    });
    document.getElementById('settings-fm-sync')?.addEventListener('change', e => this.saveFmSync(e.target));
    document.getElementById('discover-shuffle')?.addEventListener('click', () => {
      const songs = state.get('dailyRecommend') || [];
      if (songs.length) player.playQueue(songs, Math.floor(Math.random() * songs.length), 'shuffle', '发现 · 每日推荐');
    });
    document.getElementById('discover-history')?.addEventListener('click', () => this.loadPanel('history'));
    document.getElementById('discover-explore')?.addEventListener('click', () => this.loadPanel('explore'));
    const resume = document.getElementById('discover-resume');
    resume?.addEventListener('click', () => player.togglePlay(true));
    state.on('change:resumableSession', session => {
      if (!resume) return;
      resume.hidden = !session;
      const trackTitle = session?.current_track?.title || '上次曲目';
      resume.textContent = session ? `继续上次播放 · ${trackTitle}  ${formatSeconds(session.position_sec).padStart(5, '0')}` : '';
    });

    state.on('change:recentPlayed', () => {
      if (state.get('activeTab') === 'discover') {
        this.renderRecentPlayed();
      }
    });

    state.on('change:likedSongIds', () => {
      if (state.get('activeTab') === 'discover') {
        this.renderDailyList();
      }
    });

    state.on('change:currentPlayingSong', () => {
      if (state.get('activeTab') === 'discover') {
        this.renderFmCard();
        this.syncTunerPlayingState();
      }
    });

    state.on('change:playMode', () => {
      if (state.get('activeTab') === 'discover') {
        this.renderFmCard();
        this.renderHeroCards();
      }
      this.syncTunerPlayingState();
    });

    state.on('change:settings', () => {
      this.syncExploreSliderUI();
      this.syncOrbLifecycle();
    });
    state.on('change:activeTab', () => this.syncOrbLifecycle());
    state.on('change:isStageOpen', () => this.syncOrbLifecycle());
    state.on('change:windowVisible', () => this.syncOrbLifecycle());
    state.on('change:windowMinimized', () => this.syncOrbLifecycle());
    this.initAcousticOrb();
  }

  bindSmartRadioControls() {
    if (typeof document === 'undefined') return;
    const slider = document.getElementById('discover-explore-slider');
    const playAllDustyBtn = document.getElementById('discover-dusty-play-all');
    const refreshDustyBtn = document.getElementById('discover-dusty-refresh');

    const parseSliderPct = (rawVal) => {
      const parsed = Number(rawVal);
      return Math.max(0, Math.min(100, Math.round(Number.isFinite(parsed) ? parsed : 40)));
    };

    const applyExplorePct = (rawVal, { commit = false } = {}) => {
      const pct = parseSliderPct(rawVal);
      const ratio = pct / 100;
      this.orbTargetRatio = ratio;
      this.orbCurrentRatio = ratio;
      state.updateSettings({ smart_radio_explore_ratio: ratio });
      this.syncExploreSliderUI();
      if (commit) {
        if (this.exploreSaveTimer) clearTimeout(this.exploreSaveTimer);
        this.exploreSaveTimer = setTimeout(() => {
          this.exploreSaveTimer = null;
          this.settingsWrite = this.settingsWrite.then(() => api.saveSettings({ smart_radio_explore_ratio: ratio }))
            .then(res => { if (!res.ok) toast.warning('探索度保存失败', res.error?.message); }).catch(() => {});
        }, 300);
      }
    };
    this._applyExplorePct = applyExplorePct;

    if (slider) {
      slider.addEventListener('input', (e) => applyExplorePct(e.target.value, { commit: false }));
      slider.addEventListener('change', (e) => applyExplorePct(e.target.value, { commit: true }));
    }

    if (playAllDustyBtn) {
      playAllDustyBtn.addEventListener('click', () => {
        const gems = state.get('dustyGems') || [];
        if (gems.length > 0) {
          player.playQueue(gems, 0, 'shuffle', '发现 · 沉睡宝藏');
        }
      });
    }

    if (refreshDustyBtn) {
      refreshDustyBtn.addEventListener('click', () => {
        this.loadDustyGems();
        this.loadListeningInsights();
      });
    }

    this.bindDustyCarouselControls();
    this.syncExploreSliderUI();
  }

  bindDustyCarouselControls() {
    if (typeof document === 'undefined') return;
    if (!this._boundDustyNodes) this._boundDustyNodes = new WeakSet();
    const strip = document.getElementById('discover-dusty-grid');
    const prevBtn = document.getElementById('discover-dusty-prev');
    const nextBtn = document.getElementById('discover-dusty-next');
    if (!strip && !prevBtn && !nextBtn) return;

    const scrollStep = (dir) => {
      const target = document.getElementById('discover-dusty-grid');
      if (!target) return;
      const firstCard = target.querySelector?.('.discover-dusty-card');
      const cardWidth = Number(firstCard?.offsetWidth) || 240;
      const delta = dir * (cardWidth * 2 + 20);
      if (typeof target.scrollBy === 'function') {
        target.scrollBy({ left: delta, behavior: 'smooth' });
      } else {
        target.scrollLeft = Math.max(0, (Number(target.scrollLeft) || 0) + delta);
      }
      this.syncDustyCarouselState();
    };

    if (prevBtn && !this._boundDustyNodes.has(prevBtn)) {
      this._boundDustyNodes.add(prevBtn);
      prevBtn.addEventListener?.('click', () => scrollStep(-1));
    }
    if (nextBtn && !this._boundDustyNodes.has(nextBtn)) {
      this._boundDustyNodes.add(nextBtn);
      nextBtn.addEventListener?.('click', () => scrollStep(1));
    }
    if (strip && !this._boundDustyNodes.has(strip)) {
      this._boundDustyNodes.add(strip);
      strip.addEventListener?.('scroll', () => this.syncDustyCarouselState(), { passive: true });
    }
    if (!this._dustyResizeBound && typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
      this._dustyResizeBound = true;
      window.addEventListener('resize', () => this.syncDustyCarouselState(), { passive: true });
    }
    this.syncDustyCarouselState();
  }

  syncDustyCarouselState() {
    if (typeof document === 'undefined') return;
    const strip = document.getElementById('discover-dusty-grid');
    const carousel = document.getElementById('discover-dusty-carousel');
    const prevBtn = document.getElementById('discover-dusty-prev');
    const nextBtn = document.getElementById('discover-dusty-next');
    if (!strip) return;
    const scrollLeft = Number(strip.scrollLeft) || 0;
    const scrollWidth = Number(strip.scrollWidth) || 0;
    const clientWidth = Number(strip.clientWidth) || 0;
    const maxScroll = Math.max(0, scrollWidth - clientWidth);
    const canLeft = scrollLeft > 2;
    const canRight = maxScroll > 2 && scrollLeft < maxScroll - 2;
    if (carousel?.dataset) {
      carousel.dataset.canScrollLeft = String(canLeft);
      carousel.dataset.canScrollRight = String(canRight);
    }
    if (prevBtn) prevBtn.disabled = !canLeft;
    if (nextBtn) nextBtn.disabled = !canRight;
  }

  shouldShowRecommendReason(reason) {
    const text = String(reason || '').trim();
    if (!text) return false;
    return !/^(网易云)?每日推荐$/i.test(text);
  }

  formatExploreLabel(pct) {
    const clamped = Math.max(0, Math.min(100, Math.round(Number.isFinite(Number(pct)) ? Number(pct) : 40)));
    if (clamped === 0) return '0% · 纯熟歌回味';
    if (clamped <= 34) return `${clamped}% · 偏爱熟歌`;
    if (clamped <= 65) return `${clamped}% · 熟歌与新声均衡`;
    if (clamped <= 99) return `${clamped}% · 偏爱新声探索`;
    return '100% · 纯新歌探索';
  }

  computeSliderFillPercent(pct, slider = null) {
    const rawMin = slider?.min !== undefined && slider?.min !== '' ? Number(slider.min) : 0;
    const rawMax = slider?.max !== undefined && slider?.max !== '' ? Number(slider.max) : 100;
    const min = Number.isFinite(rawMin) ? rawMin : 0;
    const max = Number.isFinite(rawMax) ? rawMax : 100;
    const span = Math.max(1, max - min);
    const visual = Math.max(0, Math.min(100, ((Number(pct) - min) / span) * 100));
    return `${Math.round(visual * 100) / 100}%`;
  }

  syncExploreSliderUI() {
    if (typeof document === 'undefined') return;
    const rawRatio = state.get('settings')?.smart_radio_explore_ratio;
    const ratio = Number.isFinite(Number(rawRatio)) ? Number(rawRatio) : 0.4;
    const pct = Math.max(0, Math.min(100, Math.round(ratio * 100)));
    const slider = document.getElementById('discover-explore-slider');
    const label = document.getElementById('discover-explore-ratio-label');
    this.orbTargetRatio = pct / 100;
    this.orbCurrentRatio = pct / 100;
    if (slider) {
      slider.value = String(pct);
      slider.style?.setProperty?.('--fill', this.computeSliderFillPercent(pct, slider));
    }
    if (label) label.textContent = this.formatExploreLabel(pct);
    this.renderAcousticOrbFrame(pct);
  }

  initAcousticOrb() {
    if (typeof document === 'undefined') return;
    const dial = document.getElementById('discover-acoustic-orb');
    if (!dial) return;
    this.orbCanvas = dial;
    this.buildTunerTicks();
    this.bindTunerDialInteraction();

    this.orbWindowBlurred = document.hasFocus?.() === false;
    this.orbMotionQuery = typeof window !== 'undefined' ? window.matchMedia?.('(prefers-reduced-motion: reduce)') : null;
    this.orbMotionQuery?.addEventListener?.('change', () => this.syncOrbLifecycle());

    if (!this._orbListenersBound && typeof window !== 'undefined') {
      this._orbListenersBound = true;
      document.addEventListener?.('visibilitychange', () => this.syncOrbLifecycle());
      window.addEventListener?.('blur', () => {
        this.orbWindowBlurred = true;
        this.syncOrbLifecycle();
      });
      window.addEventListener?.('focus', () => {
        this.orbWindowBlurred = false;
        this.syncOrbLifecycle();
      });
    }

    if (typeof IntersectionObserver !== 'undefined' && !this.orbObserver) {
      this.orbObserver = new IntersectionObserver((entries) => {
        const entry = entries[entries.length - 1];
        this.orbInView = Boolean(entry?.isIntersecting);
        this.syncOrbLifecycle();
      }, { threshold: 0.05 });
      this.orbObserver.observe(dial);
    }

    const rawRatio = state.get('settings')?.smart_radio_explore_ratio;
    const ratio = Number.isFinite(Number(rawRatio)) ? Math.max(0, Math.min(1, Number(rawRatio))) : 0.4;
    this.orbCurrentRatio = ratio;
    this.orbTargetRatio = ratio;
    this.renderAcousticOrbFrame(Math.round(ratio * 100));
    this.syncOrbLifecycle();
    this.syncTunerPlayingState();
  }

  buildTunerTicks() {
    if (typeof document === 'undefined' || typeof document.createElementNS !== 'function') return;
    const group = document.getElementById('discover-tuner-ticks');
    if (!group || group.childNodes?.length > 0) return;
    const svgNs = 'http://www.w3.org/2000/svg';
    const totalTicks = 40; // 0..40 => every 2.5% along the 270 deg analog sweep
    const ticks = [];
    for (let i = 0; i <= totalTicks; i++) {
      const frac = i / totalTicks;
      const deg = -135 + frac * 270;
      const rad = ((deg - 90) * Math.PI) / 180;
      const isMajor = i % 10 === 0;
      const isMedium = !isMajor && i % 4 === 0;
      const rOuter = 67;
      const rInner = isMajor ? 59 : isMedium ? 61.5 : 63.5;
      const x1 = (80 + Math.cos(rad) * rInner).toFixed(2);
      const y1 = (80 + Math.sin(rad) * rInner).toFixed(2);
      const x2 = (80 + Math.cos(rad) * rOuter).toFixed(2);
      const y2 = (80 + Math.sin(rad) * rOuter).toFixed(2);
      const line = document.createElementNS(svgNs, 'line');
      line.setAttribute('x1', x1);
      line.setAttribute('y1', y1);
      line.setAttribute('x2', x2);
      line.setAttribute('y2', y2);
      line.setAttribute('stroke-width', isMajor ? '1.6' : isMedium ? '1.15' : '0.85');
      line.setAttribute('class', isMajor ? 'discover-tuner-tick is-major' : 'discover-tuner-tick');
      line.dataset.tickPct = String(Math.round(frac * 100));
      group.appendChild(line);
      ticks.push({ el: line, pct: Math.round(frac * 100) });
    }
    this.tunerTickElements = ticks;
  }

  bindTunerDialInteraction() {
    if (typeof document === 'undefined') return;
    const stage = document.getElementById('discover-tuner-stage');
    if (!stage || this._tunerDialBound) return;
    this._tunerDialBound = true;

    const pctFromPointer = (clientX, clientY) => {
      const rect = stage.getBoundingClientRect?.();
      if (!rect || !rect.width || !rect.height) return null;
      const cx = rect.left + rect.width / 2;
      const cy = rect.top + rect.height / 2;
      let deg = (Math.atan2(clientY - cy, clientX - cx) * 180) / Math.PI + 90;
      if (deg > 180) deg -= 360;
      if (deg < -135) deg = -135;
      if (deg > 135) deg = 135;
      return Math.max(0, Math.min(100, Math.round(((deg + 135) / 270) * 100)));
    };

    let dragging = false;
    let lastPct = null;

    stage.addEventListener?.('pointerdown', (e) => {
      if (e.button !== undefined && e.button !== 0) return;
      dragging = true;
      stage.classList?.add('is-dragging');
      stage.setPointerCapture?.(e.pointerId);
      const pct = pctFromPointer(e.clientX, e.clientY);
      if (pct !== null) {
        lastPct = pct;
        this._applyExplorePct?.(pct, { commit: false });
      }
    });

    stage.addEventListener?.('pointermove', (e) => {
      if (!dragging) return;
      const pct = pctFromPointer(e.clientX, e.clientY);
      if (pct !== null && pct !== lastPct) {
        lastPct = pct;
        this._applyExplorePct?.(pct, { commit: false });
      }
    });

    const endDrag = () => {
      if (!dragging) return;
      dragging = false;
      stage.classList?.remove('is-dragging');
      if (lastPct !== null) {
        this._applyExplorePct?.(lastPct, { commit: true });
        toast.quiet(`心动电台频段已调至 FM · ${lastPct}%`);
      }
      lastPct = null;
    };

    stage.addEventListener?.('pointerup', endDrag);
    stage.addEventListener?.('pointercancel', endDrag);

    stage.addEventListener?.('keydown', (e) => {
      const cur = Math.round((Number(state.get('settings')?.smart_radio_explore_ratio) || 0.4) * 100);
      if (e.key === 'ArrowRight' || e.key === 'ArrowUp') {
        e.preventDefault();
        const next = Math.min(100, cur + 5);
        this._applyExplorePct?.(next, { commit: true });
      } else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') {
        e.preventDefault();
        const next = Math.max(0, cur - 5);
        this._applyExplorePct?.(next, { commit: true });
      }
    });
  }

  shouldPauseOrb() {
    const cfg = state.get('settings') || {};
    if (cfg.visual_mode === 'quiet_eco') return true;
    if (cfg.gpu_performance_mode === 'eco') return true;
    if (cfg.game_mode === true) return true;
    if (state.get('windowVisible') === false || state.get('windowMinimized')) return true;
    if (state.get('activeTab') && state.get('activeTab') !== 'discover') return true;
    if (state.get('isStageOpen')) return true;
    if (!this.orbInView) return true;
    if (this.orbWindowBlurred) return true;
    if (typeof document !== 'undefined' && document.hidden) return true;
    return Boolean(this.orbMotionQuery?.matches);
  }

  syncOrbLifecycle() {
    if (!this.orbCanvas) return;
    const paused = this.shouldPauseOrb();
    if (this.orbCanvas.dataset) {
      this.orbCanvas.dataset.orbPaused = String(paused);
    }
    this.syncTunerPlayingState();
  }

  syncTunerPlayingState() {
    if (typeof document === 'undefined') return;
    const dial = this.orbCanvas || document.getElementById('discover-acoustic-orb');
    if (!dial || !dial.dataset) return;
    const isSmartPlaying = state.get('playMode') === 'smart_radio' && Boolean(state.get('isPlaying'));
    dial.dataset.radioPlaying = String(isSmartPlaying);
  }

  renderAcousticOrbFrame(overridePct = null) {
    if (typeof document === 'undefined') return;
    const pct = overridePct !== null && Number.isFinite(Number(overridePct))
      ? Math.max(0, Math.min(100, Math.round(Number(overridePct))))
      : Math.max(0, Math.min(100, Math.round(this.orbCurrentRatio * 100)));
    const ratio = pct / 100;
    const deg = -135 + ratio * 270;
    const arcLen = 259.18;
    const dashOffset = (arcLen * (1 - ratio)).toFixed(2);

    const stage = document.getElementById('discover-tuner-stage');
    stage?.setAttribute?.('aria-valuenow', String(pct));

    const needle = document.getElementById('discover-tuner-needle');
    if (needle) {
      needle.setAttribute?.('transform', `rotate(${deg.toFixed(1)} 80 80)`);
      if (needle.style) needle.style.transform = `rotate(${deg.toFixed(1)}deg)`;
    }

    const arc = document.getElementById('discover-tuner-arc');
    if (arc) {
      arc.setAttribute?.('stroke-dashoffset', dashOffset);
    }

    const readout = document.getElementById('discover-tuner-value');
    if (readout) {
      readout.textContent = `FM · ${pct}%`;
    }

    if (Array.isArray(this.tunerTickElements) && this.tunerTickElements.length > 0) {
      for (const tick of this.tunerTickElements) {
        tick.el?.classList?.toggle('is-active', tick.pct <= pct);
      }
    }
    this.syncTunerPlayingState();
  }

  async ensureLoaded(force = false) {
    const uid = state.getUserId();
    const isOffline = !state.get('authenticated') || state.get('offlineMode');
    if (isOffline) {
      await Promise.allSettled([
        this.loadDailyRecommend(),
        this.loadDustyGems(),
        this.loadListeningInsights(),
      ]);
      this.render();
      return;
    }
    const recommendDay = new Date(Date.now() + 2 * 3600_000).toISOString().slice(0, 10);
    if (!force && state.get('dailyRecommendMeta')?.recommend_day === recommendDay && this.loadedForUserId === uid && (state.get('dailyRecommend') || []).length > 0) {
      this.render();
      return;
    }
    this.loadedForUserId = uid;
    await Promise.allSettled([
      this.loadDailyRecommend(),
      this.loadFmPreview(),
      this.loadDustyGems(),
      this.loadListeningInsights(),
    ]);
    this.render();
  }

  async loadListeningInsights() {
    try {
      const epoch = this.epoch;
      const res = await api.getInsights();
      if (this.epoch !== epoch) return;
      if (res.ok && res.data) {
        state.set({ listeningInsights: res.data });
      }
    } catch {}
    this.renderInsightsBar();
  }

  async loadDustyGems() {
    if (this.loadingDustyGems) return;
    this.loadingDustyGems = true;
    const epoch = this.epoch;
    try {
      if (this.dustyExhausted) this.dustySeen.clear();
      const res = await api.getDustyGems({limit: 12, excludeKeys: [...this.dustySeen.keys()],
        excludeIds: [...this.dustySeen.values()].filter(Number.isSafeInteger)});
      if (epoch !== this.epoch) return;
      if (res.ok && res.data) {
        const songs = Array.isArray(res.data.songs) ? res.data.songs : [];
        songs.forEach(song => this.dustySeen.set(getTrackKey(song), Number(song.id) || null));
        this.dustyExhausted = !!res.data.exhausted;
        state.set({ dustyGems: songs });
        const btn = globalThis.document?.getElementById('discover-dusty-refresh');
        const btnLabel = btn?.querySelector?.('.btn-label');
        const nextLabel = this.dustyExhausted ? '已看完 · 再看一轮' : '换一批';
        if (btnLabel) btnLabel.textContent = nextLabel;
        else if (btn) btn.textContent = nextLabel;
      }
    } catch {} finally {
      if (this.epoch === epoch) { this.loadingDustyGems = false; this.renderDustyGems(); }
    }
  }

  async loadDailyRecommend(forceCheck = false) {
    if (this.loadingDaily) return;
    const epoch = this.epoch;
    this.loadingDaily = true;
    try {
      const previous = state.get('dailyRecommendMeta') || {};
      const res = await api.getDailyRecommend({ forceCheck, knownRawIds: previous.raw_song_ids || [] });
      if (epoch !== this.epoch) return;
      const data = res.data || {};
      const usable = res.ok && data.songs?.length && (!data.degraded || !state.get('dailyRecommend')?.length);
      const reason = data.reason || res.error?.message || (!usable ? '暂未取得今日推荐' : '');
      if (usable) state.set({ dailyRecommend: data.songs, dailyRecommendMeta: data });
      else state.set({ dailyRecommendMeta: { ...previous, degraded: true, reason } });
      const previousIds = new Set(previous.raw_song_ids || []);
      const latestIds = new Set(data.raw_song_ids || []);
      const removed = [...previousIds].filter(id => !latestIds.has(id)).length;
      if (forceCheck) toast.info('检查更新', reason || (data.changed
        ? `新增或变化 ${(data.added_count || 0) + removed} 首` : '今日歌单暂无更新（每日 06:00 更新）'));
    } finally {
      if (epoch === this.epoch) { this.loadingDaily = false; this.renderDailyList(); }
    }
  }

  async loadPanel(kind, date) {
    const panel = document.getElementById('discover-extra');
    const token = this.panelToken = Symbol();
    panel.textContent = '正在加载…';
    const res = kind === 'history' ? await api.getDailyHistory(date) :
      await api.postExploreBatch({ ...state.writeScope(), mode: 'start', limit: 20,
        exclude_keys: [...this.exploreSeen.keys()], exclude_ids: [...this.exploreSeen.values()].filter(Number.isSafeInteger) });
    if (token !== this.panelToken) return;
    panel.replaceChildren();
    if (date) panel.appendChild(createElement('button', {className: 'btn btn-ghost btn-sm', text: date + ' · 返回日期列表',
      events: {click: () => this.loadPanel('history')} }));
    const data = res.data || {};
    for (const day of data.dates || []) {
      panel.appendChild(createElement('button', { className: 'btn btn-secondary btn-sm', text: day + (state.get('offlineMode') && !data.cached_dates?.includes(day) ? ' · 详情未缓存' : ''),
        events: {click: () => this.loadPanel('history', day)} }));
    }
    const songs = data.songs || [];
    songs.forEach((song, index) => {
      if (kind === 'explore') this.exploreSeen.set(getTrackKey(song), Number(song.id) || null);
      panel.appendChild(createElement('button', {className: 'btn btn-ghost btn-sm', text: song.title,
        events: {click: () => player.playQueue(songs, index, 'order', kind === 'explore' ? '发现 · 探索' : '发现 · 历史推荐')} }));
    });
    if (!songs.length && !data.dates?.length) panel.appendChild(createElement('span', {text: data.reason || res.error?.message || '暂无曲目'}));
  }

  resetDiscovery() {
    this.epoch++;
    this.panelToken = null;
    globalThis.document?.getElementById('discover-extra')?.replaceChildren();
    const dustyButton = globalThis.document?.getElementById('discover-dusty-refresh');
    const dustyLabel = dustyButton?.querySelector?.('.btn-label');
    if (dustyLabel) dustyLabel.textContent = '换一批';
    else if (dustyButton) dustyButton.textContent = '换一批';
    this.dustySeen.clear();
    this.exploreSeen.clear();
    this.dustyExhausted = false;
    this.loadedForUserId = this.smartRadioBreakdown = null;
    this.loadingDaily = this.loadingFm = this.loadingDustyGems = this.loadingSmartRadio = false;
    player.resetRadioSession();
    state.set({ dailyRecommend: [], dailyRecommendMeta: null, personalFmQueue: [], smartRadioSongs: [],
      dustyGems: [], listeningInsights: null, radioBreakdown: null });
  }

  async saveFmSync(toggle) {
    toggle.disabled = true;
    try {
      const res = await api.saveSettings({ sync_fm_trash_to_netease: toggle.checked });
      if (res.ok) state.updateSettings({ sync_fm_trash_to_netease: res.data.sync_fm_trash_to_netease });
      else { toggle.checked = !toggle.checked; toast.error('设置保存失败', res.error?.message); }
    } finally { toggle.disabled = false; }
  }

  async loadFmPreview() {
    if (this.loadingFm) return;
    if ((state.get('personalFmQueue') || []).length > 0) return;
    this.loadingFm = true;
    const epoch = this.epoch;
    try {
      const res = await api.getPersonalFm();
      if (this.epoch !== epoch) return;
      if (res.ok && res.data) {
        const songs = Array.isArray(res.data.songs) ? res.data.songs : [];
        if (songs.length > 0) {
          state.set({ personalFmQueue: songs });
        }
      }
    } catch {} finally {
      if (epoch === this.epoch) { this.loadingFm = false; this.renderFmCard(); }
    }
  }

  async startPersonalFm() {
    if (!state.get('authenticated') || state.get('offlineMode')) {
      toast.warning('需要登录', '私人漫游需要在线登录网易云账号。');
      return;
    }
    if (this.loadingFm) return;
    const epoch = this.epoch;
    this.loadingFm = true;
    this.renderFmCard();
    try {
      let songs = state.get('personalFmQueue') || [];
      if (songs.length === 0) {
        const res = await api.getPersonalFm();
        if (this.epoch !== epoch) return;
        if (res.ok && res.data && Array.isArray(res.data.songs)) {
          songs = res.data.songs;
          state.set({ personalFmQueue: songs });
        } else {
          toast.error('私人漫游启动失败', res.error?.message || '未能获取漫游曲目。');
          return;
        }
      }
      if (songs.length > 0) {
        await player.playQueue(songs, 0, 'fm', '私人漫游');
        toast.info('私人漫游已开启', '播完当前批次将自动续播下一批推荐。');
      }
    } finally {
      if (epoch === this.epoch) { this.loadingFm = false; this.renderFmCard(); }
    }
  }

  /**
   * Start a session or replace only its upcoming tracks.
   */
  async startSmartRadio() {
    if (this.loadingSmartRadio) return;
    const epoch = this.epoch;
    const replay = Boolean(state.get('radioExhausted'));
    this.loadingSmartRadio = true;
    this.renderHeroCards();
    try {
      await player.requestRadio(state.get('playMode') === 'smart_radio' && !replay ? 'rearrange' : 'start', replay);
      this.smartRadioBreakdown = state.get('radioBreakdown');
    } finally {
      if (epoch === this.epoch) { this.loadingSmartRadio = false; this.renderHeroCards(); }
    }
  }

  async startHeartbeatMode() {
    return this.startSmartRadio();
  }

  async markNotInterested(song, context = 'daily_recommend') {
    if (!song || (!song.id && !song.track_key && !song.local_path)) return;
    const epoch = this.epoch;
    const scope = state.writeScope();
    const key = getTrackKey(song);
    const without = tracks => (tracks || []).filter(item => getTrackKey(item) !== key);
    state.set({ dailyRecommend: without(state.get('dailyRecommend')), personalFmQueue: without(state.get('personalFmQueue')) });
    this.renderDailyList();
    this.renderFmCard();
    const saving = api.submitFeedback({...scope, song_id: key.startsWith('netease:') ? Number(song.id) : 0,
      track_key: key, action: 'not_interested', context});
    if (['fm', 'smart_radio'].includes(state.get('playMode')) && player.currentSong && getTrackKey(player.currentSong) === key) {
      const queue = state.get('playQueue') || [];
      const index = state.get('playQueueIndex');
      if (queue[index] && getTrackKey(queue[index]) === key) {
        if (queue.length === 1 && state.get('playMode') === 'fm') await player.refillPersonalFmQueue();
        if (epoch === this.epoch && player.currentSong && getTrackKey(player.currentSong) === key) player.removeFromQueue(index);
      }
    }
    const result = await saving;
    if (epoch !== this.epoch) return;
    if (result.ok) toast.quiet('已加入本地不想听列表');
    else toast.warning('本地屏蔽未保存', result.error?.message);
    if (context === 'personal_fm' && state.get('settings')?.sync_fm_trash_to_netease) {
      const sync = await api.trashPersonalFmTrack({...scope, song_id: Number(song.id)});
      if (epoch === this.epoch && !sync.data?.synced) toast.warning(sync.data?.status === 'auth_required' ? '请重新登录网易云' : '私人漫游同步未完成', sync.data?.reason || sync.error?.message);
    }
    this.renderFmCard();
  }

  render() {
    if (!this.container) return;
    this.renderHeroCards();
    this.renderFmCard();
    this.renderInsightsBar();
    this.renderDustyGems();
    this.renderDailyList();
    this.renderRecentPlayed();
  }

  renderHeroCards() {
    if (typeof document === 'undefined') return;
    this.syncExploreSliderUI();
    const heartbeatBtn = document.getElementById('discover-heartbeat-btn');
    if (heartbeatBtn) {
      heartbeatBtn.disabled = this.loadingSmartRadio;
      const isSmartActive = state.get('playMode') === 'smart_radio';
      const label = heartbeatBtn.querySelector('.btn-label');
      if (label) {
        label.textContent = this.loadingSmartRadio
          ? '编排中...'
          : (state.get('radioExhausted') ? '本轮已听完 · 允许重播' : isSmartActive ? '重新编排心动电台' : '开启心动电台');
      }
    }
    const tagsEl = document.getElementById('discover-smart-radio-tags');
    if (tagsEl) {
      tagsEl.textContent = '';
      const bd = this.smartRadioBreakdown;
      const similarCount = bd ? (bd.multi_seed_similar ?? bd.similar_graph ?? 0) : 0;
      const pills = bd
        ? [
            bd.dusty_gems ? `沉睡宝藏 ${bd.dusty_gems}` : '',
            bd.fresh_rotation ? `近期心头好 ${bd.fresh_rotation}` : '',
            similarCount ? `共同相似 ${similarCount}` : '',
            bd.exploration ? `探索发现 ${bd.exploration}` : '',
          ].filter(Boolean)
        : [];
      pills.forEach(txt => {
        tagsEl.appendChild(createElement('span', { className: 'badge badge-muted discover-radio-tag', text: txt }));
      });
    }
  }

  _dispatchTabNavigation(detail) {
    if (typeof window === 'undefined' || typeof window.dispatchEvent !== 'function') return;
    const EventCtor = typeof CustomEvent === 'function' ? CustomEvent : null;
    if (EventCtor) {
      window.dispatchEvent(new EventCtor('switch-tab', { detail }));
    }
  }

  _spotlightDustySection(dustyCount) {
    const section = typeof document !== 'undefined' ? document.getElementById('discover-dusty-section') : null;
    const gems = state.get('dustyGems') || [];
    if (gems.length === 0 && !this.loadingDustyGems) {
      this.loadDustyGems();
    }
    if (section) {
      section.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
      section.classList?.remove('is-spotlight');
      if (this.dustySpotlightTimer) clearTimeout(this.dustySpotlightTimer);
      section.classList?.add('is-spotlight');
      this.dustySpotlightTimer = setTimeout(() => {
        section.classList?.remove('is-spotlight');
        this.dustySpotlightTimer = null;
      }, 1400);
    }
    toast.info('沉睡宝藏', dustyCount > 0 ? `已定位到 ${dustyCount} 首长期未听好歌，点击卡片或「均衡随机播」即可唤醒` : '正在从本地曲库挖掘沉睡宝藏');
  }

  renderInsightsBar() {
    if (typeof document === 'undefined') return;
    const bar = document.getElementById('discover-insights-bar');
    if (!bar) return;
    bar.textContent = '';

    const data = state.get('listeningInsights');
    const catalogCount = data?.catalog_track_count ?? (state.get('catalog') || []).length;
    const totalPlays = Number(data?.total_plays) || 0;
    const totalCompleted = Number(data?.total_completed) || 0;
    const totalMins = Number(data?.total_listen_minutes) || 0;
    const dustyCount = Number(data?.dusty_gems_count) || (state.get('dustyGems') || []).length;
    const cloudCount = Number(data?.cloud_count) || (state.get('cloudSongs') || []).length;
    const localCount = Number(data?.local_count) || (state.get('localSongs') || []).length;
    const topArtists = Array.isArray(data?.top_artists)
      ? data.top_artists.slice(0, 3).map(a => (typeof a === 'string' ? a : (a?.name || a?.artist || ''))).filter(Boolean)
      : [];

    const insightsValue = totalPlays > 0
      ? `${totalPlays} 次播放 · ${totalCompleted} 次完整收听 · ${totalMins} 分钟`
      : '开始收听即可积累本地画像';
    const assetsValue = `${catalogCount} 歌单曲目 · ${cloudCount} 云盘 · ${localCount} 本地`;

    const buildStandardPill = (item) => createElement('button', {
      className: 'discover-insight-pill is-interactive',
      dataset: { insightAction: item.action },
      attributes: {
        type: 'button',
        title: item.hint || item.value,
        'aria-label': `${item.label}：${item.value}`,
      },
      events: { click: item.onActivate },
      children: [
        createIcon(item.icon, { size: 14, className: 'insight-icon' }),
        createElement('div', {
          className: 'discover-insight-text',
          children: [
            createElement('div', {
              className: 'discover-insight-label-row',
              children: [
                createElement('span', { className: 'discover-insight-label', text: item.label }),
                createIcon('chevron-right', { size: 13, className: 'discover-insight-arrow' }),
              ],
            }),
            createElement('span', { className: 'discover-insight-value', text: item.value, attributes: { title: item.value } }),
          ],
        }),
      ],
    });

    bar.appendChild(buildStandardPill({
      action: 'history',
      label: '本地听歌洞察',
      value: insightsValue,
      icon: 'activity',
      hint: '点击查看完整本地播放历史与收听统计',
      onActivate: () => {
        this._dispatchTabNavigation('history');
        toast.info('本地听歌洞察', totalPlays > 0 ? `已前往播放历史（累计 ${totalPlays} 次播放 · ${totalMins} 分钟）` : '已前往播放历史，收听曲目后自动积累洞察');
      },
    }));

    bar.appendChild(buildStandardPill({
      action: 'dusty',
      label: '沉睡宝藏',
      value: `${dustyCount} 首长期未听`,
      icon: 'gem',
      hint: '点击定位并唤醒长期未听的好歌',
      onActivate: () => this._spotlightDustySection(dustyCount),
    }));

    bar.appendChild(buildStandardPill({
      action: 'catalog',
      label: '本地曲库资产',
      value: assetsValue,
      icon: 'library',
      hint: '点击进入音乐库管理歌单、云盘与本地曲库',
      onActivate: () => {
        this._dispatchTabNavigation('catalog');
        toast.info('本地曲库资产', `已前往音乐库（${assetsValue}）`);
      },
    }));

    if (topArtists.length > 0) {
      const openArtistSearch = (artistName) => {
        if (!artistName) return;
        this._dispatchTabNavigation({ tab: 'search', query: artistName });
        toast.info('常听歌手', `正在检索「${artistName}」相关曲目`);
      };
      const artistChildren = [];
      topArtists.forEach((artistName, idx) => {
        if (idx > 0) {
          artistChildren.push(createElement('span', { className: 'discover-insight-artist-sep', text: '/' }));
        }
        artistChildren.push(createElement('button', {
          className: 'discover-insight-artist-chip',
          text: artistName,
          dataset: { artist: artistName },
          attributes: { type: 'button', title: `点击搜索歌手「${artistName}」` },
          events: {
            click: (e) => {
              e.stopPropagation();
              openArtistSearch(artistName);
            },
          },
        }));
      });

      bar.appendChild(createElement('div', {
        className: 'discover-insight-pill is-interactive discover-insight-artists',
        dataset: { insightAction: 'artists' },
        attributes: {
          role: 'button',
          tabindex: '0',
          title: `点击检索常听歌手：${topArtists.join(' / ')}`,
          'aria-label': `常听歌手：${topArtists.join('、')}`,
        },
        events: {
          click: () => openArtistSearch(topArtists[0]),
          keydown: (e) => {
            if ((e.key === 'Enter' || e.key === ' ') && e.target === e.currentTarget) {
              e.preventDefault();
              openArtistSearch(topArtists[0]);
            }
          },
        },
        children: [
          createIcon('sparkles', { size: 14, className: 'insight-icon' }),
          createElement('div', {
            className: 'discover-insight-text',
            children: [
              createElement('div', {
                className: 'discover-insight-label-row',
                children: [
                  createElement('span', { className: 'discover-insight-label', text: '常听歌手' }),
                  createIcon('chevron-right', { size: 13, className: 'discover-insight-arrow' }),
                ],
              }),
              createElement('div', {
                className: 'discover-insight-artists-list',
                children: artistChildren,
              }),
            ],
          }),
        ],
      }));
    }
  }

  renderDustyGems() {
    if (typeof document === 'undefined') return;
    const grid = document.getElementById('discover-dusty-grid');
    const playAllBtn = document.getElementById('discover-dusty-play-all');
    if (!grid) return;

    const gems = state.get('dustyGems') || [];
    if (playAllBtn) playAllBtn.disabled = gems.length === 0;
    grid.textContent = '';

    if (gems.length === 0) {
      grid.appendChild(createElement('div', {
        className: 'discover-recent-empty',
        text: this.loadingDustyGems ? '正在从本地曲库挖掘沉睡宝藏...' : '同步红心歌单并听歌一段时间后，这里会定期唤醒超过 30 天未听的好歌。',
      }));
      this.syncDustyCarouselState();
      return;
    }

    gems.slice(0, 12).forEach((song, idx) => {
      const card = createElement('div', {
        className: 'discover-recent-card discover-dusty-card',
        attributes: { role: 'button', tabindex: '0', 'aria-label': `播放沉睡宝藏 ${song.title || '曲目'}` },
        events: {
          click: () => player.playQueue(gems, idx, 'order', '发现 · 沉睡宝藏'),
          keydown: (e) => {
            if (e.key === 'Enter' && e.target === e.currentTarget) player.playQueue(gems, idx, 'order', '发现 · 沉睡宝藏');
          },
          contextmenu: (e) => openSongContextMenu(e, song, {
            source: 'discover',
            queue: gems, origin: '发现 · 沉睡宝藏',
            index: idx,
            onAddToPlaylist: this.onAddToPlaylistCallback,
            onNotInterested: (s) => this.markNotInterested(s, 'dusty_gems'),
          }),
        },
        children: [
          createElement('img', {
            className: 'discover-recent-cover',
            attributes: {
              src: getSizedCoverUrl(song.cover_url, 96, '藏'),
              alt: song.title || '封面',
              loading: 'lazy',
            },
          }),
          createElement('div', {
            className: 'discover-recent-meta',
            children: [
              createElement('div', { className: 'discover-recent-title', text: song.title || '未知曲目' }),
              createElement('div', { className: 'discover-recent-artist', text: formatArtists(song.artists) }),
              song.recommend_reason
                ? createElement('div', { className: 'discover-dusty-reason', text: song.recommend_reason })
                : null,
            ],
          }),
        ],
      });
      card.appendChild(quickNextButton(song));
      grid.appendChild(card);
    });
    this.syncDustyCarouselState();
    if (typeof requestAnimationFrame === 'function') {
      requestAnimationFrame(() => this.syncDustyCarouselState());
    }
  }

  renderFmCard() {
    if (typeof document === 'undefined') return;
    const fmContainer = document.getElementById('discover-fm-body');
    if (!fmContainer) return;
    fmContainer.textContent = '';

    const isFmActive = state.get('playMode') === 'fm' && state.get('currentPlayingSong');
    const previewSong = isFmActive
      ? state.get('currentPlayingSong')
      : (state.get('personalFmQueue') || [])[0] || null;

    if (state.get('offlineMode')) {
      fmContainer.appendChild(createElement('div', {
        className: 'discover-fm-placeholder',
        text: '离线只读模式下暂不可用，请扫码登录后开启私人漫游。',
      }));
      return;
    }

    const toggleFm = () => {
      if (isFmActive) player.togglePlay();
      else this.startPersonalFm();
    };

    let coverEl;
    if (previewSong?.cover_url) {
      coverEl = createElement('img', {
        className: 'discover-fm-cover',
        attributes: {
          src: getSizedCoverUrl(previewSong.cover_url, 192),
          alt: previewSong?.title || '私人漫游封面',
          title: isFmActive ? '点击暂停或继续私人漫游' : '点击开启私人漫游',
        },
        events: { click: toggleFm },
      });
    } else {
      coverEl = createElement('div', {
        className: 'discover-fm-cover discover-fm-cover-fallback',
        attributes: { role: 'button', tabindex: '0', title: '点击开启私人漫游' },
        events: {
          click: toggleFm,
          keydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleFm(); } },
        },
        children: [createIcon('radio', { size: 32 })],
      });
    }

    const title = createElement('div', {
      className: 'discover-fm-title',
      text: previewSong ? previewSong.title : (this.loadingFm ? '正在加载私人漫游...' : '探索你的专属电台'),
      attributes: { title: isFmActive ? '点击暂停或继续私人漫游' : '点击开启私人漫游' },
      events: { click: toggleFm },
    });

    const artist = createElement('div', {
      className: 'discover-fm-artist',
      text: previewSong ? formatArtists(previewSong.artists) : '基于你的听歌偏好实时续播',
    });

    const controls = createElement('div', {
      className: 'discover-fm-actions',
      children: [
        createElement('button', {
          className: 'btn btn-primary btn-sm',
          events: {
            click: () => {
              if (isFmActive) player.togglePlay();
              else this.startPersonalFm();
            },
          },
          children: [
            createIcon(isFmActive && state.get('isPlaying') ? 'pause' : 'play', { size: 14 }),
            createElement('span', { className: 'btn-label', text: isFmActive ? (state.get('isPlaying') ? '暂停漫游' : '继续漫游') : '开始漫游' }),
          ],
        }),
        createElement('button', {
          className: 'btn btn-secondary btn-sm',
          attributes: { title: '切到下一首漫游曲目' },
          events: {
            click: async () => {
              if (isFmActive) {
                await player.playNext(true);
              } else {
                state.set({ personalFmQueue: [] });
                await this.startPersonalFm();
              }
            },
          },
          children: [createIcon('skip-next', { size: 14 }), createElement('span', { className: 'btn-label', text: '换一首' })],
        }),
        previewSong ? createElement('button', {
          className: 'btn btn-ghost btn-sm',
          attributes: { title: '永久加入本地不想听列表' },
          events: { click: () => this.markNotInterested(previewSong, 'personal_fm') },
          children: [createIcon('ban', { size: 14 }), createElement('span', { className: 'btn-label', text: '不想听' })],
        }) : null,
      ],
    });

    fmContainer.appendChild(coverEl);
    fmContainer.appendChild(createElement('div', {
      className: 'discover-fm-meta',
      children: [title, artist, controls],
    }));
  }

  renderDailyList() {
    if (typeof document === 'undefined') return;
    const listEl = document.getElementById('discover-daily-list');
    const noticeEl = document.getElementById('discover-daily-notice');
    const playAllBtn = document.getElementById('discover-daily-play-all');
    if (!listEl) return;

    const songs = state.get('dailyRecommend') || [];
    const meta = state.get('dailyRecommendMeta');

    if (playAllBtn) {
      playAllBtn.disabled = songs.length === 0;
    }

    if (noticeEl) {
      if (meta?.degraded && meta?.reason) {
        noticeEl.textContent = meta.reason;
        noticeEl.classList.remove('hidden');
      } else {
        noticeEl.textContent = '';
        noticeEl.classList.add('hidden');
      }
    }

    listEl.textContent = '';

    if (state.get('offlineMode') && !songs.length) {
      listEl.appendChild(createElement('div', {
        className: 'empty-state',
        children: [
          createElement('div', { className: 'empty-state-title', text: '离线只读模式' }),
          createElement('div', { className: 'empty-state-desc', text: '每日推荐需要连接网易云音乐账号；你可前往“音乐库”浏览本地缓存曲目。' }),
        ],
      }));
      return;
    }

    if (this.loadingDaily && !songs.length) {
      listEl.appendChild(createElement('div', {
        className: 'empty-state',
        children: [
          createElement('div', { className: 'spinner' }),
          createElement('div', { className: 'empty-state-desc', text: '正在拉取今日 30 首每日推荐...' }),
        ],
      }));
      return;
    }

    if (songs.length === 0) {
      listEl.appendChild(createElement('div', {
        className: 'empty-state',
        children: [
          createElement('div', { className: 'empty-state-title', text: '暂无每日推荐曲目' }),
          createElement('div', { className: 'empty-state-desc', text: meta?.reason || '点击“检查更新”重新拉取今日推荐。' }),
        ],
      }));
      return;
    }

    const shownSongs = songs.slice(0, 30);
    listEl.style?.setProperty?.('--daily-col-rows', String(Math.max(1, Math.ceil(shownSongs.length / 2))));
    shownSongs.forEach((song, index) => {
      const liked = state.isSongLiked(song.id);

      const cover = createElement('img', {
        className: 'discover-track-cover',
        attributes: {
          src: getSizedCoverUrl(song.cover_url, 96, '音'),
          alt: song.title || '封面',
          loading: 'lazy',
        },
      });

      const titleRowChildren = [
        createElement('span', { className: 'discover-track-title', text: song.title || '未知曲目' }),
      ];
      if (this.shouldShowRecommendReason(song.recommend_reason)) {
        titleRowChildren.push(createElement('span', {
          className: 'badge badge-muted discover-reason-badge',
          text: String(song.recommend_reason).trim(),
        }));
      }

      const info = createElement('div', {
        className: 'discover-track-info',
        children: [
          createElement('div', { className: 'discover-track-title-row' }, titleRowChildren),
          createElement('div', {
            className: 'discover-track-sub',
            text: `${formatArtists(song.artists)}${song.album ? ` · ${song.album}` : ''}`,
          }),
        ],
      });

      const actions = createElement('div', {
        className: 'discover-track-actions',
        children: [
          quickNextButton(song),
          createElement('button', {
            className: `btn btn-ghost btn-icon btn-sm ${liked ? 'is-liked' : 'discover-row-reveal-action'}`,
            attributes: { title: liked ? '取消红心' : '红心喜欢', 'aria-label': liked ? '取消红心' : '红心喜欢' },
            events: {
              click: (e) => {
                e.stopPropagation();
                player.toggleLikeCurrentSong(song);
              },
            },
            children: [createIcon(liked ? 'heart-filled' : 'heart', { size: 15 })],
          }),
          createElement('button', {
            className: 'btn btn-ghost btn-icon btn-sm discover-row-reveal-action',
            attributes: { title: '添加到自建歌单', 'aria-label': '添加到自建歌单' },
            events: {
              click: (e) => {
                e.stopPropagation();
                if (this.onAddToPlaylistCallback) this.onAddToPlaylistCallback(song);
              },
            },
            children: [createIcon('plus-list', { size: 15 })],
          }),
          createElement('button', {
            className: 'btn btn-ghost btn-icon btn-sm discover-not-interested-btn discover-row-reveal-action',
            attributes: { title: '不再推荐此曲', 'aria-label': '不再推荐' },
            events: {
              click: (e) => {
                e.stopPropagation();
                this.markNotInterested(song, 'daily_recommend');
              },
            },
            children: [
              createIcon('ban', { size: 14 }),
              createElement('span', { className: 'sr-only', text: '不再推荐' }),
            ],
          }),
          createElement('span', { className: 'discover-track-duration numeral-mono', text: formatDuration(song) }),
        ],
      });

      const row = createElement('div', {
        className: 'discover-track-row',
        dataset: { trackKey: getTrackKey(song) },
        attributes: { tabindex: '0', role: 'button', 'aria-label': `播放 ${song.title || '曲目'}` },
        events: {
          click: () => player.playQueue(songs, index, 'order', '发现 · 每日推荐'),
          keydown: (e) => {
            if (e.key === 'Enter' && e.target === e.currentTarget) player.playQueue(songs, index, 'order', '发现 · 每日推荐');
          },
          contextmenu: (e) => openSongContextMenu(e, song, {
            source: 'discover',
            queue: songs, origin: '发现 · 每日推荐',
            index,
            onAddToPlaylist: this.onAddToPlaylistCallback,
            onNotInterested: (s) => this.markNotInterested(s, 'daily_recommend'),
          }),
        },
        children: [
          createElement('span', {
            className: 'discover-track-index numeral-mono',
            children: [
              createElement('span', { className: 'track-index-num', text: String(index + 1).padStart(2, '0') }),
              createPlayingEqBars(),
            ],
          }),
          cover,
          info,
          actions,
        ],
      });

      listEl.appendChild(row);
    });
    syncPlayingRowIndicators(state.get('currentPlayingSong'), Boolean(state.get('isPlaying')), listEl);
  }

  renderRecentPlayed() {
    if (typeof document === 'undefined') return;
    const gridEl = document.getElementById('discover-recent-grid');
    if (!gridEl) return;
    gridEl.textContent = '';

    const recent = state.get('recentPlayed') || [];
    const subtitle = document.getElementById('discover-recent-count');
    if (subtitle) subtitle.textContent = `最近播放的 ${Math.min(12, recent.length)} 首曲目`;
    if (recent.length === 0) {
      gridEl.appendChild(createElement('div', {
        className: 'discover-recent-empty',
        text: '还没有最近播放记录，点击任意曲目开始收听。',
      }));
      return;
    }

    recent.slice(0, 12).forEach((song, idx) => {
      const card = createElement('div', {
        className: 'discover-recent-card',
        dataset: { trackKey: getTrackKey(song) },
        attributes: { role: 'button', tabindex: '0', 'aria-label': `播放 ${song.title || '曲目'}` },
        events: {
          click: () => player.playQueue(recent, idx, 'order', '播放历史'),
          keydown: (e) => {
            if (e.key === 'Enter') player.playQueue(recent, idx, 'order', '播放历史');
          },
          contextmenu: (e) => openSongContextMenu(e, song, {
            source: 'discover',
            queue: recent, origin: '播放历史',
            index: idx,
            onAddToPlaylist: this.onAddToPlaylistCallback,
          }),
        },
        children: [
          createElement('img', {
            className: 'discover-recent-cover',
            attributes: {
              src: getSizedCoverUrl(song.cover_url, 96, '音'),
              alt: song.title || '封面',
              loading: 'lazy',
            },
          }),
          createElement('div', {
            className: 'discover-recent-meta',
            children: [
              createElement('div', { className: 'discover-recent-title', text: song.title || '未知曲目' }),
              createElement('div', { className: 'discover-recent-artist', text: formatArtists(song.artists) }),
            ],
          }),
        ],
      });
      gridEl.appendChild(card);
    });
    syncPlayingRowIndicators(state.get('currentPlayingSong'), Boolean(state.get('isPlaying')), gridEl);
  }
}

export const discoverManager = new DiscoverManager();
