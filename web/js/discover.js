/**
 * Pruner Studio - Discover & Native/Local-First Recommendations (发现)
 * Default online landing featuring:
 * - 2-Column Listening Hero: Personal FM (私人漫游) + Smart Radio (心动电台: Vinyl Sleeve & Real Album Cover Stage + Unified Precision Tuner + Live Tuning)
 * - Interactive Local Listening Insights bar (本地听歌洞察 via GET /api/insights)
 * - Dusty Gems 2-row 8-card Showcase (沉睡宝藏 via GET /api/recommend/dusty-gems + dedicated #view-dusty explorer)
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

const RADIO_PRESET_STOPS = [0, 30, 55, 85];
const DUSTY_SHOWCASE_LIMIT = 8;

function formatCount(n) {
  const num = Number(n);
  if (!Number.isFinite(num) || num <= 0) return '0';
  return Math.round(num).toLocaleString('en-US');
}

function getActiveRadioPreset(pct) {
  for (const stop of RADIO_PRESET_STOPS) {
    if (Math.abs(pct - stop) <= 2) return stop;
  }
  return null;
}

function updateActionButton(btn, iconName, labelText) {
  if (!btn) return;
  const label = btn.querySelector?.('.btn-label');
  if (typeof document !== 'undefined' && typeof document.createElementNS === 'function') {
    btn.textContent = '';
    btn.appendChild(createIcon(iconName, { size: 14 }));
    const span = createElement('span', { className: 'btn-label', text: labelText });
    btn.appendChild(span);
    return;
  }
  if (label) label.textContent = labelText;
  else btn.textContent = labelText;
}

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
    this.liveTuneTimer = null;
    this.liveTuneNoticeTimer = null;
    this.liveTunePending = false;
    this.liveTuneFeedbackVisible = false;
    this.liveTuneRecentNotice = false;
    this.settingsWrite = Promise.resolve();
    this.epoch = 0;
    this.dustySeen = new Map();
    this.dustyBatchHistory = [];
    this.dustyForwardHistory = [];
    this.dustyTotal = 0;
    this.dustyExhausted = false;
    this.exploreSeen = new Map();

    // Vinyl Sleeve Stage motion lifecycle state (pauses cleanly in eco/game/hidden/blur)
    this.orbCanvas = null;
    this.orbCurrentRatio = 0.4;
    this.orbTargetRatio = 0.4;
    this.orbInView = true;
    this.orbWindowBlurred = false;
    this.orbMotionQuery = null;
    this.orbObserver = null;
    this._lastRadioStageKey = '';
  }

  init(containerId, { onAddToPlaylist } = {}) {
    if (typeof document !== 'undefined') {
      this.container = document.getElementById(containerId);
    }
    this.onAddToPlaylistCallback = onAddToPlaylist || null;
    this.bindSmartRadioControls();
    for (const key of ['isPlaying', 'personalFmQueue']) state.on('change:' + key, () => {
      this.renderFmCard();
      this.renderHeroCards();
      this.syncTunerPlayingState();
      if (state.get('activeTab') === 'discover') {
        const dustyGrid = typeof document !== 'undefined' ? document.getElementById('discover-dusty-grid') : null;
        if (dustyGrid) syncPlayingRowIndicators(state.get('currentPlayingSong'), Boolean(state.get('isPlaying')), dustyGrid);
      }
    });
    state.on('change:radioExhausted', () => this.renderHeroCards());
    state.on('change:radioBreakdown', (bd) => {
      if (bd) this.smartRadioBreakdown = bd;
      this.renderHeroCards();
    });
    state.on('change:smartRadioSongs', () => this.renderHeroCards());
    state.on('change:playQueue', () => {
      if (state.get('playMode') === 'smart_radio' && state.get('activeTab') === 'discover') {
        this.renderHeroCards();
      }
    });
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
      if (this.liveTuneFeedbackVisible || this.liveTuneRecentNotice) {
        this.liveTuneFeedbackVisible = false;
        this.liveTuneRecentNotice = false;
        if (this.liveTuneNoticeTimer) {
          clearTimeout(this.liveTuneNoticeTimer);
          this.liveTuneNoticeTimer = null;
        }
      }
      if (state.get('activeTab') === 'discover') {
        this.renderFmCard();
        this.renderHeroCards();
        this.syncTunerPlayingState();
        const dustyGrid = typeof document !== 'undefined' ? document.getElementById('discover-dusty-grid') : null;
        if (dustyGrid) syncPlayingRowIndicators(state.get('currentPlayingSong'), Boolean(state.get('isPlaying')), dustyGrid);
      }
    });

    state.on('change:playMode', () => {
      if (state.get('playMode') !== 'smart_radio') {
        this.liveTunePending = false;
        this.liveTuneFeedbackVisible = false;
        this.liveTuneRecentNotice = false;
        if (this.liveTuneTimer) {
          clearTimeout(this.liveTuneTimer);
          this.liveTuneTimer = null;
        }
        if (this.liveTuneNoticeTimer) {
          clearTimeout(this.liveTuneNoticeTimer);
          this.liveTuneNoticeTimer = null;
        }
      }
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
    if (!(this._boundRadioNodes instanceof WeakMap)) {
      this._boundRadioNodes = new WeakMap();
    }
    const bindOnce = (el, eventName, handler, options) => {
      if (!el || typeof el.addEventListener !== 'function') return;
      const key = `${eventName}`;
      let seen = this._boundRadioNodes.get(el);
      if (!seen) {
        seen = new Set();
        this._boundRadioNodes.set(el, seen);
      }
      if (seen.has(key)) return;
      seen.add(key);
      el.addEventListener(eventName, handler, options);
    };

    const slider = document.getElementById('discover-explore-slider');
    const rail = document.getElementById('discover-tuner-rail');
    const stageBtn = document.getElementById('discover-radio-stage');
    const headlineEl = document.getElementById('discover-radio-headline');
    const nextBtn = document.getElementById('discover-radio-next-btn');
    const tryNextBtn = document.getElementById('discover-radio-try-next-btn');
    const queueBtn = document.getElementById('discover-radio-queue-btn');
    const playAllDustyBtn = document.getElementById('discover-dusty-play-all');
    const prevDustyBtn = document.getElementById('discover-dusty-prev');
    const refreshDustyBtn = document.getElementById('discover-dusty-refresh');
    const viewAllDustyBtn = document.getElementById('discover-dusty-view-all');

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

        if (state.get('playMode') === 'smart_radio' && !state.get('radioExhausted')) {
          if (this.liveTuneTimer) clearTimeout(this.liveTuneTimer);
          this.liveTuneFeedbackVisible = false;
          this.liveTuneRecentNotice = false;
          this.liveTuneTimer = setTimeout(() => {
            this.liveTuneTimer = null;
            this.triggerLiveRadioRearrange();
          }, 450);
        }
      }
    };
    this._applyExplorePct = applyExplorePct;

    bindOnce(slider, 'input', (e) => applyExplorePct(e.target.value, { commit: false }));
    bindOnce(slider, 'change', (e) => applyExplorePct(e.target.value, { commit: true }));

    bindOnce(rail, 'wheel', (e) => {
      if (typeof e.preventDefault === 'function') e.preventDefault();
      const rawRatio = state.get('settings')?.smart_radio_explore_ratio;
      const cur = Math.round((Number.isFinite(Number(rawRatio)) ? Number(rawRatio) : 0.4) * 100);
      const step = Number(e.deltaY) < 0 ? 5 : -5;
      applyExplorePct(cur + step, { commit: true });
    }, { passive: false });

    const presets = document.querySelectorAll?.('.discover-tuner-preset') || [];
    presets.forEach((btn) => {
      bindOnce(btn, 'click', () => {
        const val = Number(btn.dataset?.radioPreset);
        if (Number.isFinite(val)) applyExplorePct(val, { commit: true });
      });
    });

    bindOnce(stageBtn, 'click', () => this.handleSmartRadioPrimaryAction());
    bindOnce(headlineEl, 'click', () => {
      if (state.get('playMode') === 'smart_radio' && state.get('currentPlayingSong')) {
        player.togglePlay();
      }
    });
    bindOnce(nextBtn, 'click', () => {
      this.liveTuneFeedbackVisible = false;
      this.liveTuneRecentNotice = false;
      player.playNext(true);
    });
    bindOnce(tryNextBtn, 'click', () => {
      this.liveTuneFeedbackVisible = false;
      this.liveTuneRecentNotice = false;
      this.renderHeroCards();
      player.playNext(true);
    });
    bindOnce(queueBtn, 'click', () => {
      document.getElementById('player-queue-btn')?.click?.();
    });

    bindOnce(playAllDustyBtn, 'click', () => {
      const gems = state.get('dustyGems') || [];
      if (gems.length > 0) {
        player.playQueue(gems, 0, 'shuffle', '发现 · 沉睡宝藏');
      }
    });
    bindOnce(prevDustyBtn, 'click', () => this.showPrevDustyBatch());
    bindOnce(refreshDustyBtn, 'click', () => {
      this.showNextDustyBatch();
      this.loadListeningInsights();
    });
    bindOnce(viewAllDustyBtn, 'click', () => {
      this._dispatchTabNavigation({ tab: 'dusty' });
    });

    this.syncExploreSliderUI();
  }

  async triggerLiveRadioRearrange() {
    if (state.get('playMode') !== 'smart_radio' || state.get('radioExhausted')) return false;
    const epoch = this.epoch;
    this.liveTunePending = true;
    this.renderHeroCards();
    let ok = false;
    try {
      const result = await player.requestRadio('rearrange', false);
      if (epoch !== this.epoch) return false;
      ok = result !== false;
      if (ok && state.get('playMode') === 'smart_radio') {
        this.smartRadioBreakdown = state.get('radioBreakdown') || this.smartRadioBreakdown;
        this.liveTuneFeedbackVisible = true;
        this.liveTuneRecentNotice = true;
        if (this.liveTuneNoticeTimer) clearTimeout(this.liveTuneNoticeTimer);
        this.liveTuneNoticeTimer = setTimeout(() => {
          this.liveTuneRecentNotice = false;
          this.liveTuneNoticeTimer = null;
          this.renderHeroCards();
        }, 10000);
      }
    } finally {
      if (epoch === this.epoch) {
        this.liveTunePending = false;
        this.renderHeroCards();
      }
    }
    return ok;
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

  formatSmartRadioReadout(pct) {
    const clamped = Math.max(0, Math.min(100, Math.round(Number.isFinite(Number(pct)) ? Number(pct) : 40)));
    if (clamped === 0) return '0% 纯熟歌';
    if (clamped === 100) return '100% 纯新声';
    return `${clamped}% 新声`;
  }

  formatSmartRadioMood(pct) {
    const clamped = Math.max(0, Math.min(100, Math.round(Number.isFinite(Number(pct)) ? Number(pct) : 40)));
    const familiarPct = 100 - clamped;
    if (clamped === 0) {
      return { title: '纯粹回味 · 唤醒曲库深处', sub: '100% 来自你的曲库熟歌与沉睡宝藏' };
    }
    if (clamped <= 34) {
      return { title: '偏爱熟歌 · 熟面孔里藏惊喜', sub: `约 ${familiarPct}% 曲库熟歌 · ${clamped}% 风格新声` };
    }
    if (clamped <= 65) {
      return { title: '熟新均衡 · 经典与新声交织', sub: `约 ${familiarPct}% 曲库熟歌 · ${clamped}% 风格新声` };
    }
    if (clamped <= 99) {
      return { title: '探索新声 · 顺着品味向外漫游', sub: `约 ${familiarPct}% 曲库熟歌 · ${clamped}% 风格新声` };
    }
    return { title: '纯新声漫游 · 推开未知声场', sub: '100% 基于你品味的相似与探索新声' };
  }

  formatSmartRadioBreakdownSummary(bd) {
    if (!bd || typeof bd !== 'object') return '';
    const similarCount = bd.multi_seed_similar ?? bd.similar_graph ?? 0;
    const parts = [
      bd.dusty_gems ? `沉睡宝藏 ${bd.dusty_gems}` : '',
      bd.fresh_rotation ? `近期心头好 ${bd.fresh_rotation}` : '',
      similarCount ? `共同相似 ${similarCount}` : '',
      bd.exploration ? `探索发现 ${bd.exploration}` : '',
    ].filter(Boolean);
    return parts.length ? `本轮构成：${parts.join(' · ')}` : '';
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
    const familiarPct = 100 - pct;
    const slider = document.getElementById('discover-explore-slider');
    const label = document.getElementById('discover-explore-ratio-label');
    const familiarSeg = document.getElementById('discover-blend-familiar');
    const exploreSeg = document.getElementById('discover-blend-explore');
    const blendBar = document.getElementById('discover-radio-blend-bar');
    this.orbTargetRatio = pct / 100;
    this.orbCurrentRatio = pct / 100;

    if (slider) {
      slider.value = String(pct);
      slider.style?.setProperty?.('--fill', this.computeSliderFillPercent(pct, slider));
    }
    if (label) {
      label.textContent = this.formatSmartRadioReadout(pct);
      label.setAttribute?.('title', this.formatExploreLabel(pct));
    }
    if (familiarSeg?.style) familiarSeg.style.width = `${familiarPct}%`;
    if (exploreSeg?.style) exploreSeg.style.width = `${pct}%`;

    if (blendBar) {
      const breakdownSummary = this.formatSmartRadioBreakdownSummary(this.smartRadioBreakdown);
      const baseSummary = `熟歌 ${familiarPct}% / 新声 ${pct}%`;
      blendBar.setAttribute?.('title', breakdownSummary ? `${baseSummary} · ${breakdownSummary}` : baseSummary);
      if (blendBar.dataset) {
        blendBar.dataset.breakdownSummary = breakdownSummary;
      }
    }

    const activePreset = getActiveRadioPreset(pct);
    const presets = document.querySelectorAll?.('.discover-tuner-preset') || [];
    presets.forEach((btn) => {
      const presetVal = Number(btn.dataset?.radioPreset);
      const isActive = presetVal === activePreset;
      btn.classList?.toggle('is-active', isActive);
      btn.setAttribute?.('aria-pressed', String(isActive));
    });

    // Update idle headline/subline live while scrubbing
    const isSmartActive = state.get('playMode') === 'smart_radio' && Boolean(state.get('currentPlayingSong'));
    if (!isSmartActive) {
      const mood = this.formatSmartRadioMood(pct);
      const headline = document.getElementById('discover-radio-headline');
      const subline = document.getElementById('discover-radio-subline');
      if (headline) headline.textContent = mood.title;
      if (subline) subline.textContent = mood.sub;
    }
  }

  initAcousticOrb() {
    if (typeof document === 'undefined') return;
    const stage = document.getElementById('discover-radio-stage');
    if (!stage) return;
    this.orbCanvas = stage;

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
      this.orbObserver.observe(stage);
    }

    const rawRatio = state.get('settings')?.smart_radio_explore_ratio;
    const ratio = Number.isFinite(Number(rawRatio)) ? Math.max(0, Math.min(1, Number(rawRatio))) : 0.4;
    this.orbCurrentRatio = ratio;
    this.orbTargetRatio = ratio;
    this.syncOrbLifecycle();
    this.syncTunerPlayingState();
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
    if (typeof document === 'undefined') return;
    const stage = this.orbCanvas || document.getElementById('discover-radio-stage');
    const paused = this.shouldPauseOrb();
    if (stage?.dataset) {
      stage.dataset.orbPaused = String(paused);
    }
    const card = stage?.closest?.('.discover-action-card');
    if (card?.dataset) {
      card.dataset.orbPaused = String(paused);
    }
    this.syncTunerPlayingState();
  }

  syncTunerPlayingState() {
    if (typeof document === 'undefined') return;
    const stage = this.orbCanvas || document.getElementById('discover-radio-stage');
    if (!stage || !stage.dataset) return;
    const isSmartPlaying = state.get('playMode') === 'smart_radio' && Boolean(state.get('isPlaying'));
    stage.dataset.radioPlaying = String(isSmartPlaying);
  }

  async ensureLoaded(force = false) {
    const uid = state.getUserId();
    const isOffline = !state.get('authenticated') || state.get('offlineMode');
    if (isOffline) {
      await Promise.allSettled([
        this.loadDailyRecommend(),
        this.loadDustyGems({ recordHistory: false }),
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
      this.loadDustyGems({ recordHistory: false }),
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
    this.syncDustyShowcaseHeader();
  }

  async loadDustyGems({ recordHistory = false } = {}) {
    if (this.loadingDustyGems) return;
    this.loadingDustyGems = true;
    const epoch = this.epoch;
    const previousBatch = recordHistory ? [...(state.get('dustyGems') || [])] : [];
    try {
      if (this.dustyExhausted) this.dustySeen.clear();
      const res = await api.getDustyGems({
        limit: DUSTY_SHOWCASE_LIMIT,
        excludeKeys: [...this.dustySeen.keys()],
        excludeIds: [...this.dustySeen.values()].filter(Number.isSafeInteger),
      });
      if (epoch !== this.epoch) return;
      if (res.ok && res.data) {
        const songs = Array.isArray(res.data.songs) ? res.data.songs : [];
        if (recordHistory && previousBatch.length > 0 && songs.length > 0) {
          this.dustyBatchHistory.push(previousBatch);
          if (this.dustyBatchHistory.length > 20) this.dustyBatchHistory.shift();
          this.dustyForwardHistory = [];
        }
        songs.forEach(song => this.dustySeen.set(getTrackKey(song), Number(song.id) || null));
        this.dustyExhausted = !!res.data.exhausted;
        const reportedTotal = Number(res.data.total_candidates ?? res.data.counts?.all);
        if (Number.isFinite(reportedTotal) && reportedTotal > 0) {
          this.dustyTotal = reportedTotal;
        }
        state.set({ dustyGems: songs });
      }
    } catch {} finally {
      if (this.epoch === epoch) {
        this.loadingDustyGems = false;
        this.renderDustyGems();
        this.renderHeroCards();
      }
    }
  }

  showPrevDustyBatch() {
    if (!this.dustyBatchHistory.length) return false;
    const current = state.get('dustyGems') || [];
    const prev = this.dustyBatchHistory.pop();
    if (current.length > 0) {
      this.dustyForwardHistory.push([...current]);
    }
    state.set({ dustyGems: prev });
    this.renderDustyGems();
    return true;
  }

  showNextDustyBatch() {
    if (this.dustyForwardHistory.length > 0) {
      const current = state.get('dustyGems') || [];
      const next = this.dustyForwardHistory.pop();
      if (current.length > 0) {
        this.dustyBatchHistory.push([...current]);
      }
      state.set({ dustyGems: next });
      this.renderDustyGems();
      return Promise.resolve(true);
    }
    return this.loadDustyGems({ recordHistory: true });
  }

  syncDustyShowcaseHeader() {
    if (typeof document === 'undefined') return;
    const insightsCount = Number(state.get('listeningInsights')?.dusty_gems_count) || 0;
    const gemsCount = (state.get('dustyGems') || []).length;
    const total = Math.max(this.dustyTotal || 0, insightsCount, gemsCount);

    const subtitle = document.getElementById('discover-dusty-subtitle');
    if (subtitle) {
      subtitle.textContent = total > 0
        ? `共 ${formatCount(total)} 首长期未听 · 唤醒曲库里被遗忘的好歌`
        : '唤醒曲库中超过 30 天未听或从未听过的好歌';
    }

    const prevBtn = document.getElementById('discover-dusty-prev');
    if (prevBtn) {
      prevBtn.disabled = this.dustyBatchHistory.length === 0;
    }

    const refreshBtn = document.getElementById('discover-dusty-refresh');
    if (refreshBtn) {
      const label = refreshBtn.querySelector?.('.btn-label');
      const nextLabel = this.dustyForwardHistory.length === 0 && this.dustyExhausted ? '再看一轮' : '换一批';
      if (label) label.textContent = nextLabel;
      else refreshBtn.textContent = nextLabel;
    }

    const viewAllBtn = document.getElementById('discover-dusty-view-all');
    if (viewAllBtn) {
      const label = viewAllBtn.querySelector?.('.btn-label');
      const nextText = total > 0 ? `查看全部 ${formatCount(total)} 首` : '查看全部';
      if (label) label.textContent = nextText;
      else viewAllBtn.textContent = nextText;
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
      if (epoch === this.epoch) {
        this.loadingDaily = false;
        this.renderDailyList();
        this.renderHeroCards();
      }
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
    if (this.exploreSaveTimer) {
      clearTimeout(this.exploreSaveTimer);
      this.exploreSaveTimer = null;
    }
    if (this.liveTuneTimer) {
      clearTimeout(this.liveTuneTimer);
      this.liveTuneTimer = null;
    }
    if (this.liveTuneNoticeTimer) {
      clearTimeout(this.liveTuneNoticeTimer);
      this.liveTuneNoticeTimer = null;
    }
    this.liveTunePending = false;
    this.liveTuneFeedbackVisible = false;
    this.liveTuneRecentNotice = false;
    globalThis.document?.getElementById('discover-extra')?.replaceChildren();
    this.dustySeen.clear();
    this.dustyBatchHistory = [];
    this.dustyForwardHistory = [];
    this.dustyTotal = 0;
    this.exploreSeen.clear();
    this.dustyExhausted = false;
    this._lastRadioStageKey = '';
    this.loadedForUserId = this.smartRadioBreakdown = null;
    this.loadingDaily = this.loadingFm = this.loadingDustyGems = this.loadingSmartRadio = false;
    player.resetRadioSession();
    state.set({ dailyRecommend: [], dailyRecommendMeta: null, personalFmQueue: [], smartRadioSongs: [],
      dustyGems: [], listeningInsights: null, radioBreakdown: null });
    this.syncDustyShowcaseHeader();
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
   * Start a Smart Radio session or replace its upcoming tracks.
   */
  async startSmartRadio() {
    if (this.loadingSmartRadio) return;
    const epoch = this.epoch;
    const replay = Boolean(state.get('radioExhausted'));
    this.loadingSmartRadio = true;
    this.liveTuneFeedbackVisible = false;
    this.liveTuneRecentNotice = false;
    this.renderHeroCards();
    try {
      await player.requestRadio(state.get('playMode') === 'smart_radio' && !replay ? 'rearrange' : 'start', replay);
      this.smartRadioBreakdown = state.get('radioBreakdown');
    } finally {
      if (epoch === this.epoch) {
        this.loadingSmartRadio = false;
        this.renderHeroCards();
      }
    }
  }

  /**
   * Intuitive primary Smart Radio control:
   * - Idle -> Start Smart Radio
   * - Active & Playing -> Pause (player.togglePlay)
   * - Active & Paused -> Resume (player.togglePlay)
   * - Exhausted -> Start fresh replay round
   */
  async handleSmartRadioPrimaryAction() {
    if (this.loadingSmartRadio) return;
    if (state.get('radioExhausted')) {
      return this.startSmartRadio();
    }
    if (state.get('playMode') === 'smart_radio' && state.get('currentPlayingSong')) {
      player.togglePlay();
      this.renderHeroCards();
      return;
    }
    return this.startSmartRadio();
  }

  async startHeartbeatMode() {
    return this.handleSmartRadioPrimaryAction();
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

  resolveSmartRadioStageCovers() {
    const isSmartActive = state.get('playMode') === 'smart_radio' && Boolean(state.get('currentPlayingSong'));
    const activeSong = isSmartActive ? state.get('currentPlayingSong') : null;
    const pool = [
      ...(state.get('smartRadioSongs') || []),
      ...(state.get('dustyGems') || []),
      ...(state.get('dailyRecommend') || []),
      ...(state.get('catalog') || []).slice(0, 24),
    ];

    const seen = new Set();
    const covers = [];
    if (activeSong?.cover_url) {
      const url = String(activeSong.cover_url).trim();
      if (url) {
        seen.add(url);
        covers.push({ url, title: activeSong.title || '心动电台' });
      }
    }
    for (const song of pool) {
      const url = String(song?.cover_url || '').trim();
      if (!url || seen.has(url)) continue;
      seen.add(url);
      covers.push({ url, title: song?.title || '心动电台' });
      if (covers.length >= 3) break;
    }

    return {
      primary: covers[0] || null,
      peeks: [covers[1]?.url || '', covers[2]?.url || ''],
    };
  }

  renderRadioCoverStage() {
    if (typeof document === 'undefined') return;
    const stage = document.getElementById('discover-radio-stage');
    const coverHost = document.getElementById('discover-radio-cover-host');
    const cueIcon = document.getElementById('discover-radio-stage-cue-icon');
    const isSmartActive = state.get('playMode') === 'smart_radio' && Boolean(state.get('currentPlayingSong'));
    const isSmartPlaying = isSmartActive && Boolean(state.get('isPlaying'));

    if (stage) {
      if (stage.dataset) {
        stage.dataset.radioPlaying = String(isSmartPlaying);
      }
      stage.setAttribute?.('aria-label', isSmartPlaying ? '暂停心动电台' : (isSmartActive ? '继续心动电台' : '开启心动电台'));
      stage.setAttribute?.('title', isSmartPlaying ? '点击暂停心动电台' : (isSmartActive ? '点击继续心动电台' : '点击开启心动电台'));
    }

    if (cueIcon && typeof document.createElementNS === 'function') {
      const desiredIcon = isSmartPlaying ? 'pause' : 'play';
      if (cueIcon.dataset?.activeIcon !== desiredIcon) {
        cueIcon.textContent = '';
        cueIcon.appendChild(createIcon(desiredIcon, { size: 14 }));
        if (cueIcon.dataset) cueIcon.dataset.activeIcon = desiredIcon;
      }
    }

    const { primary, peeks } = this.resolveSmartRadioStageCovers();
    const stageKey = `${primary?.url || 'fallback'}|${peeks[0]}|${peeks[1]}`;
    if (stageKey === this._lastRadioStageKey && coverHost?.children?.length > 0) {
      return;
    }
    this._lastRadioStageKey = stageKey;

    if (stage && typeof stage.querySelector === 'function') {
      const stack1 = stage.querySelector('.discover-vinyl-stack-1');
      const stack2 = stage.querySelector('.discover-vinyl-stack-2');
      if (stack1?.style) {
        stack1.style.backgroundImage = peeks[0] ? `url("${getSizedCoverUrl(peeks[0], 192)}")` : '';
      }
      if (stack2?.style) {
        stack2.style.backgroundImage = peeks[1] ? `url("${getSizedCoverUrl(peeks[1], 192)}")` : '';
      }
    }

    if (coverHost) {
      coverHost.textContent = '';
      if (primary?.url) {
        coverHost.appendChild(createElement('img', {
          className: 'discover-radio-cover-img',
          attributes: {
            src: getSizedCoverUrl(primary.url, 192),
            alt: primary.title || '心动电台封面',
          },
        }));
      } else {
        coverHost.appendChild(createElement('div', {
          className: 'discover-radio-cover-fallback',
          children: [createIcon('disc', { size: 34 })],
        }));
      }
    }
  }

  renderHeroCards() {
    if (typeof document === 'undefined') return;
    this.syncExploreSliderUI();

    const rawRatio = state.get('settings')?.smart_radio_explore_ratio;
    const ratio = Number.isFinite(Number(rawRatio)) ? Number(rawRatio) : 0.4;
    const pct = Math.max(0, Math.min(100, Math.round(ratio * 100)));
    const isSmartMode = state.get('playMode') === 'smart_radio';
    const currentSong = isSmartMode ? state.get('currentPlayingSong') : null;
    const isSmartActive = isSmartMode && Boolean(currentSong);
    const isSmartPlaying = isSmartActive && Boolean(state.get('isPlaying'));
    const exhausted = Boolean(state.get('radioExhausted'));

    // Status pill
    const statusPill = document.getElementById('discover-radio-status-pill');
    if (statusPill) {
      let stateKey = 'idle';
      let statusText = '待机';
      if (this.loadingSmartRadio || this.liveTunePending) {
        stateKey = 'loading';
        statusText = '调频中';
      } else if (exhausted) {
        stateKey = 'paused';
        statusText = '本轮已播完';
      } else if (this.liveTuneRecentNotice && isSmartActive) {
        stateKey = 'tuned';
        statusText = '已按新口味更新';
      } else if (isSmartPlaying) {
        stateKey = 'playing';
        statusText = '正在播放';
      } else if (isSmartActive) {
        stateKey = 'paused';
        statusText = '已暂停';
      }
      if (statusPill.dataset) statusPill.dataset.state = stateKey;
      const textNode = statusPill.querySelector?.('.radio-status-text');
      if (textNode) textNode.textContent = statusText;
    }

    // Headline, subline & queue button
    const headline = document.getElementById('discover-radio-headline');
    const subline = document.getElementById('discover-radio-subline');
    const queueBtn = document.getElementById('discover-radio-queue-btn');
    if (isSmartActive && currentSong) {
      if (headline) {
        headline.textContent = currentSong.title || '心动电台';
        headline.classList?.add('is-clickable');
        headline.setAttribute?.('title', isSmartPlaying ? '点击暂停心动电台' : '点击继续心动电台');
      }
      if (subline) {
        const artistText = formatArtists(currentSong.artists);
        const reason = String(currentSong.recommend_reason || '').trim();
        subline.textContent = reason ? `${artistText} · ${reason}` : (artistText || '心动电台实时编排');
      }
      if (queueBtn) {
        const queueLen = (state.get('playQueue') || []).length;
        queueBtn.hidden = queueLen === 0;
        queueBtn.textContent = queueLen > 0 ? `队列 ${queueLen} 首` : '队列';
      }
    } else {
      const mood = this.formatSmartRadioMood(pct);
      if (headline) {
        headline.textContent = mood.title;
        headline.classList?.remove('is-clickable');
        headline.setAttribute?.('title', mood.title);
      }
      if (subline) subline.textContent = mood.sub;
      if (queueBtn) queueBtn.hidden = true;
    }

    // Intuitive primary button + Next button + Live Tuning feedback chip
    const heartbeatBtn = document.getElementById('discover-heartbeat-btn');
    if (heartbeatBtn) {
      heartbeatBtn.disabled = this.loadingSmartRadio;
      if (this.loadingSmartRadio) {
        updateActionButton(heartbeatBtn, 'refresh', '调频中...');
      } else if (exhausted) {
        updateActionButton(heartbeatBtn, 'refresh', '开启新一轮');
      } else if (isSmartPlaying) {
        updateActionButton(heartbeatBtn, 'pause', '暂停电台');
      } else if (isSmartActive) {
        updateActionButton(heartbeatBtn, 'play', '继续电台');
      } else {
        updateActionButton(heartbeatBtn, 'play', '开启电台');
      }
    }

    const nextBtn = document.getElementById('discover-radio-next-btn');
    if (nextBtn) {
      nextBtn.hidden = !isSmartActive || exhausted;
    }

    const liveFeedback = document.getElementById('discover-radio-live-feedback');
    if (liveFeedback) {
      liveFeedback.hidden = !(this.liveTuneFeedbackVisible && isSmartActive && !exhausted && !this.liveTunePending);
    }

    this.renderRadioCoverStage();
    this.syncTunerPlayingState();
  }

  _dispatchTabNavigation(detail) {
    if (typeof window === 'undefined' || typeof window.dispatchEvent !== 'function') return;
    const EventCtor = typeof CustomEvent === 'function' ? CustomEvent : null;
    if (EventCtor) {
      window.dispatchEvent(new EventCtor('switch-tab', { detail }));
    }
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
    const dustyCount = Number(data?.dusty_gems_count) || this.dustyTotal || (state.get('dustyGems') || []).length;
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
      hint: '点击进入沉睡宝藏专题页，按歌单与听歌记忆自由探索',
      onActivate: () => this._dispatchTabNavigation({ tab: 'dusty' }),
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
    this.syncDustyShowcaseHeader();
    grid.textContent = '';

    if (gems.length === 0) {
      grid.appendChild(createElement('div', {
        className: 'discover-recent-empty',
        text: this.loadingDustyGems ? '正在从本地曲库挖掘沉睡宝藏...' : '同步红心歌单并听歌一段时间后，这里会定期唤醒超过 30 天未听的好歌。',
      }));
      return;
    }

    gems.slice(0, DUSTY_SHOWCASE_LIMIT).forEach((song, idx) => {
      const reasonText = String(song.recommend_reason || '').trim() || '曲库深处 · 长期未听';
      const card = createElement('div', {
        className: 'discover-dusty-card',
        dataset: { trackKey: getTrackKey(song) },
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
          createElement('div', {
            className: 'discover-dusty-cover-wrap',
            children: [
              createElement('img', {
                className: 'discover-dusty-cover',
                attributes: {
                  src: getSizedCoverUrl(song.cover_url, 192, '藏'),
                  alt: song.title || '封面',
                  loading: 'lazy',
                },
              }),
              createElement('span', {
                className: 'discover-dusty-play-overlay',
                attributes: { 'aria-hidden': 'true' },
                children: [createIcon('play', { size: 16 })],
              }),
            ],
          }),
          createElement('div', {
            className: 'discover-dusty-meta',
            children: [
              createElement('div', { className: 'discover-dusty-title', text: song.title || '未知曲目', attributes: { title: song.title || '未知曲目' } }),
              createElement('div', { className: 'discover-dusty-artist', text: formatArtists(song.artists) }),
              createElement('div', { className: 'discover-dusty-reason', text: reasonText, attributes: { title: reasonText } }),
            ],
          }),
        ],
      });
      card.appendChild(quickNextButton(song));
      grid.appendChild(card);
    });
    syncPlayingRowIndicators(state.get('currentPlayingSong'), Boolean(state.get('isPlaying')), grid);
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
