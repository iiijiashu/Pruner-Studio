import { getTrackKey, serializeTrackForStorage } from './utils.js';
/**
 * Pruner Studio - Unified Global Audio Player & Queue Engine
 * Owns native direct playback and the two same-origin mixer decks, play queue & modes (order/loop/single/shuffle/fm/intelligence),
 * parallel audio+lyrics with audio_id alignment, ms->sec chorus normalization & timeline marker,
 * single transparent CDN retry preserving position, next-track metadata prefetch,
 * honest verified quality/source badges & candidate switcher, real 64x64 cover palette extraction,
 * Media Session API (Windows SMTC), and clean auth:logout teardown.
 */
import { api } from './api.js';
import { state } from './state.js';
import { lyricsManager } from './lyrics.js';
import { toast } from './toast.js';
import { modal } from './modal.js';
import {
  createElement,
  formatDuration,
  formatSeconds,
  isPositiveSongId,
  writableNeteaseTrack,
  formatArtists,
  getSizedCoverUrl,
  isSafeUrl,
  normalizeTimeToSeconds,
  extractCoverPalette,
  getFallbackPalette,
  balancedShuffleFallback,
  syncPlayingRowIndicators,
} from './utils.js';
import { createIcon, setButtonIcon } from './icons.js';

export function volumeToGain(volumeFraction) {
  const clamped = Math.max(0, Math.min(1, Number(volumeFraction) || 0));
  if (clamped <= 0) return 0;
  return Math.pow(clamped, 2);
}

const QUALITY_LABELS = {
  jymaster: '超清母带',
  sky: '沉浸声',
  hires: 'Hi-Res',
  lossless: '无损',
  exhigh: '极高',
  higher: '较高',
  standard: '标准',
};

const PROVIDER_LABELS = {
  kugou_lite: '酷狗概念版',
  kugou: '酷狗',
  migu: '咪咕',
  qq: 'QQ音乐',
  kuwo: '酷我',
  bilibili: 'B站',
  gdstudio: 'GDStudio',
  joox: 'JOOX',
  netease: '网易云',
  custom: '自定义源',
  custom_endpoint: '自定义源',
};

const PREFETCH_TTL_MS = 5 * 60 * 1000;
const MAX_PREFETCH_ENTRIES = 10;
const GENUINE_LISTEN_MIN_MS = 30000;

export function isPlayableAudioUrl(url) {
  if (!url || typeof url !== 'string') return false;
  const trimmed = url.trim();
  return /^https?:\/\//i.test(trimmed) || trimmed.startsWith('/api/stream/');
}

/**
 * Returns true ONLY for same-origin `/api/stream/` URLs that can safely enter
 * a Web Audio MediaElementAudioSourceNode graph without CORS muting.
 * Cross-origin CDN URLs MUST bypass Web Audio and play directly on native <audio>.
 * @param {string} url
 * @returns {boolean}
 */
export function isSameOriginStreamUrl(url) {
  if (!url || typeof url !== 'string') return false;
  const trimmed = url.trim();
  if (trimmed.startsWith('/api/stream/')) return true;
  if (typeof window !== 'undefined' && window.location?.origin) {
    try {
      const parsed = new URL(trimmed, window.location.origin);
      return parsed.origin === window.location.origin && parsed.pathname.startsWith('/api/stream/');
    } catch {
      return false;
    }
  }
  return false;
}

/**
 * Formats verified backend audio metadata into honest, non-fabricated badge text.
 * Only displays fields actually returned by backend.
 * @param {Object|null} meta
 * @returns {{ text: string, detail: string, kind: 'original'|'highres'|'cloud'|'local'|'external'|'alternative' } | null}
 */
export function formatHonestAudioBadge(meta) {
  if (!meta || typeof meta !== 'object') return null;

  const parts = [];
  const formatStr = typeof meta.format === 'string' && meta.format.trim() ? meta.format.trim().toUpperCase() : '';
  const levelKey = typeof meta.level === 'string' ? meta.level.toLowerCase() : '';
  const levelLabel = levelKey === 'lossless'
    ? (formatStr ? `无损 ${formatStr}` : '无损')
    : (QUALITY_LABELS[levelKey] || '');
  const sampleRate = Number(meta.sample_rate) || 0;
  const bitrate = Number(meta.bitrate) || 0;

  // Source prefix
  let sourceKind = 'original';
  if (meta.source === 'local' || meta.is_local) {
    sourceKind = 'local';
    parts.push('本地');
  } else if (meta.source === 'cloud' || meta.is_cloud) {
    sourceKind = 'cloud';
    parts.push('云盘');
  } else if (meta.source === 'external') {
    sourceKind = 'external';
    const providerKey = String(meta.source_provider || '').toLowerCase();
    const providerLabel = PROVIDER_LABELS[providerKey] || meta.source_provider || '';
    parts.push(providerLabel ? `外部源 · ${providerLabel}` : '外部源');
  } else if (meta.is_alternative || meta.source === 'alternative') {
    sourceKind = 'alternative';
    parts.push('替代音源');
  } else if (meta.source === 'netease') {
    sourceKind = 'alternative';
    parts.push('网易云候选');
  }

  // Quality / format
  if (levelLabel && !parts.includes(levelLabel)) {
    if (sourceKind === 'original' && ['jymaster', 'hires', 'lossless', 'sky'].includes(levelKey)) {
      sourceKind = 'highres';
    }
    parts.push(levelLabel);
  } else if (formatStr) {
    parts.push(formatStr);
  }

  // Verified technical metrics (only when actually returned)
  if (sampleRate >= 8000) {
    const khz = sampleRate % 1000 === 0 ? `${sampleRate / 1000}kHz` : `${(sampleRate / 1000).toFixed(1)}kHz`;
    parts.push(khz);
  } else if (bitrate >= 1000) {
    parts.push(`${Math.round(bitrate / 1000)}kbps`);
  }

  if (meta.free_trial) {
    parts.push('试听片段');
  }

  if (parts.length === 0) return null;

  const detailParts = [];
  if (formatStr) detailParts.push(`格式: ${formatStr}`);
  if (bitrate >= 1000) detailParts.push(`码率: ${Math.round(bitrate / 1000)} kbps`);
  if (sampleRate >= 8000) detailParts.push(`采样率: ${sampleRate} Hz`);
  if (meta.alternative_info?.title) {
    const altArtist = Array.isArray(meta.alternative_info.artists) && meta.alternative_info.artists.length > 0
      ? formatArtists(meta.alternative_info.artists)
      : (meta.alternative_info.artist || '');
    detailParts.push(`匹配音源: ${meta.alternative_info.title}${altArtist ? ` - ${altArtist}` : ''}`);
  }
  if (meta.source_provider) {
    detailParts.push(`来源: ${PROVIDER_LABELS[String(meta.source_provider).toLowerCase()] || meta.source_provider}`);
  }
  if (meta.quality_upgraded_from) {
    const fromLabel = formatQualityUpgradedFrom(meta.quality_upgraded_from);
    if (fromLabel) {
      detailParts.push(`跨渠道音质择优升级 (原: ${fromLabel})`);
    }
  }

  return {
    text: parts.join(' · '),
    detail: detailParts.join(' | ') || parts.join(' · '),
    kind: sourceKind,
  };
}

function formatQualityUpgradedFrom(raw) {
  if (!raw) return '';
  if (typeof raw === 'string') return raw.trim();
  if (typeof raw !== 'object') return String(raw);
  const parts = [];
  const provKey = String(raw.source_provider || raw.source || '').toLowerCase();
  if (provKey === 'original' || provKey === 'netease') parts.push('网易云');
  else if (PROVIDER_LABELS[provKey]) parts.push(PROVIDER_LABELS[provKey]);
  else if (raw.source_provider) parts.push(String(raw.source_provider));

  const lvlKey = String(raw.level || '').toLowerCase();
  const fmtStr = typeof raw.format === 'string' && raw.format.trim() ? raw.format.trim().toUpperCase() : '';
  if (QUALITY_LABELS[lvlKey]) parts.push(QUALITY_LABELS[lvlKey]);
  if (fmtStr && !parts.includes(fmtStr)) parts.push(fmtStr);
  const br = Number(raw.bitrate) || 0;
  if (br >= 1000) parts.push(`${Math.round(br / 1000)}kbps`);
  return parts.join(' · ') || '原音源';
}

function createStubAudio() {
  const listeners = new Map();
  let srcVal = null;
  const stub = {
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(fn);
    },
    removeEventListener(type, fn) {
      listeners.get(type)?.delete(fn);
    },
    dispatchEvent(evt) {
      const type = typeof evt === 'string' ? evt : evt?.type;
      listeners.get(type)?.forEach(fn => fn(evt));
    },
    play: async function () {
      this.paused = false;
    },
    pause: function () {
      this.paused = true;
    },
    load: () => {},
    getAttribute(name) {
      return name === 'src' ? srcVal : null;
    },
    setAttribute(name, val) {
      if (name === 'src') srcVal = String(val);
    },
    removeAttribute(name) {
      if (name === 'src') srcVal = null;
    },
    get src() {
      return srcVal || '';
    },
    set src(val) {
      srcVal = val === null || val === undefined || val === '' ? null : String(val);
    },
    currentTime: 0,
    duration: 0,
    volume: 0.8,
    paused: true,
  };
  return stub;
}

export const EQ_FREQUENCIES = [32, 64, 125, 250, 500, 1000, 2000, 4000, 8000, 16000];
export const EQ_PRESETS = {
  flat: Array(10).fill(0),
  pop: [-1, 1, 2, 2, 0, -1, 1, 3, 2, 1],
  vocal: [-3, -2, -1, 1, 2, 3, 4, 2, 0, -1],
  bass: [5, 5, 4, 2, 1, 0, 0, 0, 0, 0],
  classical: [2, 2, 1, 0, -1, -1, 0, 2, 3, 3],
  electronic: [4, 3, 1, -1, -2, 0, 1, 3, 3, 4],
};

class AudioPlayer {
  constructor() {
    this.playerInstanceId = globalThis.crypto?.randomUUID?.() || Math.random().toString(36).slice(2);
    this.lyricsMsgSeq = 0;
    if (typeof Audio !== 'undefined') {
      // directAudio NEVER connects to MediaElementAudioSourceNode (prevents CORS muting on external CDN URLs)
      this.directAudio = new Audio();
      this.directAudio.preload = 'metadata';
      this.directAudio.volume = volumeToGain(0.8);

      // streamAudio is used ONLY for same-origin /api/stream/{token} URLs with Web Audio processing
      this.streamAudio = new Audio();
      this.streamAudio.preload = 'metadata';
      this.streamAudio.volume = 1;

      this.audio = this.directAudio;
    } else {
      this.directAudio = createStubAudio();
      this.streamAudio = createStubAudio();
      this.audio = this.directAudio;
    }
    this.standbyAudio = typeof Audio !== 'undefined' ? new Audio() : createStubAudio();
    this.standbyAudio.preload = 'auto';
    this.fade = null;
    this.deckGains = new Map();
    this.bindAudioElementEvents(this.standbyAudio);
    this.bindAudioElementEvents(this.directAudio);
    this.bindAudioElementEvents(this.streamAudio);

    // Safe same-origin-only Web Audio graph nodes
    this.audioCtx = null;
    this.mediaSourceNode = null;
    this.compressorNode = null;
    this.gainNode = null;
    this.analyserNode = null;
    this.analyserData = null;
    this.activeUsesWebAudio = false;
    this.audioCtxWatchdogTimer = null;
    this.endTransitionTimer = null;
    this.advancingSequence = null;

    // Playback & Queue state
    this.isChorusLooping = false;
    this.currentSong = null;
    this.requestSequence = 0;
    this.sessionOwner = null;
    this.pendingResumeSeekSec = null;
    this.playbackRate = 1;
    this.sleepMode = 'off';
    this._pendingRetrySeekCleanup = null;
    this.isLoadingTrack = false;
    this.userPausedSequence = -1;
    this.activeSrc = '';
    this._retryingAudioForSeq = null;
    this.paletteSequence = 0;
    this.retriedCurrentTrack = false;
    this.lastSavedPosition = 0;
    this.prefetchedAudio = new Map(); // songId -> { data, fetchedAt }
    this.prefetchedForCurrentTrack = false;
    this.fmRefillInFlight = false;
    this.smartRadioRefillInFlight = false;
    this.muted = false;
    this.previousVolume = 0.8;
    this.targetVolume = 0.8;

    // Balanced Shuffle Bag & exact playHistoryStack
    this.shuffleBag = [];
    this.shuffleBagCursor = 0;
    this.shuffleQueueSig = '';
    this.smartShuffleSeq = 0;
    this.playHistoryStack = [];
    this.explicitNextIndices = [];

    // Genuine Play History Recording (>=30s or completed, excluding Deck quick prunes)
    this.listenAccumMs = 0;
    this.listenLastTickMs = 0;
    this.listenRecordedForSeq = -1;

    // Desktop Floating Lyrics throttled broadcaster
    this.lastDesktopPushAt = 0;
    this.lastDesktopLineIdx = -2;

    this.initMediaSessionHandlers();

    if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
      const stopOnExit = () => {
        this.scheduleSavePlaybackSession(true, { keepalive: true });
        this.pauseAllAudio({ markUserPause: false });
      };
      window.addEventListener('beforeunload', stopOnExit);
      window.addEventListener('pagehide', stopOnExit);
    }

    state.on('state:change', ({ keys }) => {
      if (keys.some(key => ['playQueue', 'playQueueIndex', 'playMode', 'activeTab'].includes(key))) {
        this.scheduleSavePlaybackSession(true);
      }
    });
    state.on('change:settings', cfg => {
      if (cfg.playback_rate !== this.settingsRate) {
        this.settingsRate = cfg.playback_rate;
        if (cfg.playback_rate) this.setPlaybackRate(cfg.playback_rate, false);
      }
      if (cfg.audio_output_device_id !== this.outputDeviceId) {
        this.outputDeviceId = cfg.audio_output_device_id;
        this.setOutputDevice(this.outputDeviceId || 'default').catch(() => {});
      }
      this.applyWebAudioSettings();

    });
    state.on('change:likedSongIds', () => this.pushDesktopLyricsState(true));
    state.on('change:isStageOpen', () => {

      this.updateInlineLyricUI(this.audio?.currentTime || 0);
    });
    state.on('change:desktopLyricsOpen', () => {
      this.updateDesktopLyricsButtonUI();
    });
  }

  /**
   * Safely stops and clears an HTMLMediaElement without assigning an empty string to .src
   * (assigning empty string resolves to the page document URL and triggers spurious error events).
   * @param {HTMLAudioElement|Object|null} el
   */
  resetMediaElement(el) {
    if (!el) return;
    try { el.pause(); } catch {}
    try { el.removeAttribute?.('src'); } catch {}
    try { el.load?.(); } catch {}
  }

  isHealthyMediaElement(el) {
    if (!el) return false;
    if (el.error) return false;
    if (el.networkState === 3) return false;
    return true;
  }

  hasActiveMediaSrc() {
    if (!this.audio || !this.isHealthyMediaElement(this.audio)) return false;
    const attr = typeof this.audio.getAttribute === 'function' ? this.audio.getAttribute('src') : this.audio.src;
    return Boolean(attr && String(attr).trim());
  }

  clearAudioCtxWatchdog() {
    if (this.audioCtxWatchdogTimer) {
      clearTimeout(this.audioCtxWatchdogTimer);
      this.audioCtxWatchdogTimer = null;
    }
  }

  /**
   * Synchronously creates and wakes the Web Audio context inside a user gesture
   * (before any async `/api/audio` fetch) so Chromium/WebView2 autoplay policy
   * never leaves `audioCtx.state === 'suspended'`.
   */
  unlockAudioContextSync() {
    if (typeof Audio === 'undefined') return;
    if (!this.audioCtx && !this.graphFailed) {
      this.ensureWebAudioGraph();
    }
    if (this.audioCtx?.state === 'suspended') {
      try {
        this.audioCtx.resume?.().catch?.(() => {});
      } catch {}
    }
  }

  resumeAudioContextWithFallback(seq, audioUrl) {
    if (!this.activeUsesWebAudio || !this.audioCtx || this.audioCtx.state !== 'suspended') return;
    this.clearAudioCtxWatchdog();
    let settled = false;
    try {
      const p = this.audioCtx.resume?.();
      if (p && typeof p.then === 'function') {
        p.then(() => {
          settled = true;
          this.clearAudioCtxWatchdog();
        }).catch(() => {
          settled = true;
          this.clearAudioCtxWatchdog();
          this.fallbackToDirect(seq, audioUrl);
        });
      }
    } catch {
      this.fallbackToDirect(seq, audioUrl);
      return;
    }
    this.audioCtxWatchdogTimer = setTimeout(() => {
      this.audioCtxWatchdogTimer = null;
      if (settled || seq !== this.requestSequence || this.activeSrc !== audioUrl || !this.activeUsesWebAudio) return;
      if (this.userPausedSequence === seq) return;
      if (this.audioCtx?.state === 'suspended') {
        this.fallbackToDirect(seq, audioUrl);
      }
    }, 650);
  }

  /**
   * Unconditionally pauses BOTH dual <audio> elements, suspends running Web Audio context,
   * cancels pending mid-load autoplay when markUserPause is true, and resets UI/lyrics state.
   * Eliminates ghost audio after Pause or rapid track switching.
   */
  pauseAllAudio({ markUserPause = true } = {}) {
    clearTimeout(this.endTransitionTimer);
    this.clearAudioCtxWatchdog();
    this.finishFade();
    this.standbyAudio.pause();
    if (markUserPause) {
      this.userPausedSequence = this.requestSequence;
    }
    this.resumeAutoplaySequence = null;
    try {
      this.directAudio.pause();
    } catch {}
    try {
      this.streamAudio.pause();
    } catch {}
    if (this.audioCtx?.state === 'running') {
      try {
        this.audioCtx.suspend?.().catch?.(() => {});
      } catch {}
    }

    state.set({ isPlaying: false });
    this.setPlayButtonLoadingUI(false);
    this.updatePlayButtonUI(false);
    lyricsManager.setPlaybackState(false);
    this.updateMediaSessionPlaybackState('paused');

    this.pushDesktopLyricsState(true);
  }

  bindAudioElementEvents(el) {
    const onPlayLike = () => {
      if (el !== this.audio) {
        if (this.fade?.outgoing !== el) this.resetMediaElement(el);
        return;
      }
      if (this.userPausedSequence === this.requestSequence) {
        this.pauseAllAudio({ markUserPause: false });
        return;
      }
      if (!this.activeSrc) return;
      this.isBuffering = false;
      this.listenLastTickMs ||= Date.now();
      state.set({ isPlaying: true, playbackError: null });
      this.updatePlayButtonUI(true);
      lyricsManager.setPlaybackState(true, () => this.audio.currentTime || 0);
      this.updateMediaSessionPlaybackState('playing');

      this.pushDesktopLyricsState(true);
    };

    el.addEventListener('waiting', () => {
      if (el !== this.audio) return;
      this.isBuffering = true;
      this.accumulateListenTime();
      this.listenLastTickMs = 0;
      this.pushDesktopLyricsState(true);
    });
    el.addEventListener('play', onPlayLike);
    el.addEventListener('playing', () => {
      onPlayLike();
      if (el === this.audio && this.fade && !el.paused) this.startFade();
    });

    el.addEventListener('pause', () => {
      if (el !== this.audio) return;
      this.accumulateListenTime();
      this.listenLastTickMs = 0;
      this.maybeRecordGenuineListen(false, true);
      this.scheduleSavePlaybackSession(true);
      state.set({ isPlaying: false });
      this.updatePlayButtonUI(false);
      lyricsManager.setPlaybackState(false);
      this.updateMediaSessionPlaybackState('paused');

      this.pushDesktopLyricsState(true);
    });

    el.addEventListener('timeupdate', () => {
      for (const deck of [this.directAudio, this.streamAudio, this.standbyAudio]) {
        if (deck !== this.audio && deck !== this.fade?.outgoing && !deck.paused) this.resetMediaElement(deck);
      }
      if (el !== this.audio || !this.activeSrc || this.pendingResumeSeekSec != null) return;
      const currentTime = this.audio.currentTime || 0;
      const duration = this.getNormalizedDuration();
      this.lastSavedPosition = currentTime;
      state.set({ currentTime, duration });

      if (!this.audio.paused && !this.isBuffering) {
        this.scheduleSavePlaybackSession();
        this.accumulateListenTime();
        this.maybeRecordGenuineListen(false);
      }

      // Chorus looping check (ms -> sec normalized)
      if (this.isChorusLooping && this.currentSong) {
        const chorusEnd = normalizeTimeToSeconds(this.currentSong.chorus_end, duration);
        const chorusStart = normalizeTimeToSeconds(this.currentSong.chorus_start, duration);
        if (chorusEnd > chorusStart && currentTime >= chorusEnd) {
          this.audio.currentTime = chorusStart;
        }
      }

      // Lightweight next-track URL metadata prefetch (~15s before end or 4s into track)
      if (!this.prefetchedForCurrentTrack && duration > 0 && (currentTime >= 4 || (duration - currentTime) <= 18)) {
        this.prefetchedForCurrentTrack = true;
        this.prefetchNextTrackMetadata();
      }

      this.scheduleEndTransition(duration, currentTime);
      this.updateTimelineUI(currentTime, duration);
      lyricsManager.updateTime(currentTime);
      this.updateInlineLyricUI(currentTime);
      this.pushDesktopLyricsState(false);
      const nowPerf = Date.now();
      if (!this._lastMediaSessionPosSyncMs || (nowPerf - this._lastMediaSessionPosSyncMs) >= 5000) {
        this._lastMediaSessionPosSyncMs = nowPerf;
        this.updateMediaSessionPosition();
      }
    });

    el.addEventListener('durationchange', () => {
      if (el !== this.audio || !this.activeSrc) return;
      this.updateMediaSessionPosition();
    });

    el.addEventListener('seeked', () => {
      if (el !== this.audio || !this.activeSrc) return;
      this.updateMediaSessionPosition();
    });

    el.addEventListener('ratechange', () => {
      if (el !== this.audio || !this.activeSrc) return;
      this.updateMediaSessionPosition();
    });

    el.addEventListener('loadedmetadata', () => {
      if (el !== this.audio || !this.activeSrc) return;
      this.applyPendingResumeSeek(el, this.requestSequence);
      const duration = this.getNormalizedDuration();
      state.set({ duration, currentTime: el.currentTime || 0 });
      this.updateTimelineUI(this.audio.currentTime || 0, duration);
      this.updateChorusUI(this.currentSong, duration);
      this.updateMediaSessionPosition();
    });

    el.addEventListener('ended', () => {
      if (el !== this.audio || !this.activeSrc || this.advancingSequence === this.requestSequence) return;
      this.accumulateListenTime();
      this.maybeRecordGenuineListen(true);
      state.set({ isPlaying: false });
      this.updatePlayButtonUI(false);
      lyricsManager.setPlaybackState(false);

      this.pushDesktopLyricsState(true);
      this.listenLastTickMs = 0;
      if (this.sleepMode === 'track') {
        this.setSleepTimer('off');
        this.pauseAllAudio();
      } else this.handleTrackEnded();
    });

    el.addEventListener('error', () => {
      if (el !== this.audio) return;
      const attrSrc = typeof el.getAttribute === 'function' ? el.getAttribute('src') : el.src;
      if (!attrSrc || !this.activeSrc || attrSrc !== this.activeSrc || this.isLoadingTrack) {
        return;
      }
      if (this.fade) this.fallbackToDirect(this.requestSequence, this.activeSrc);
      else this.handleAudioElementError();
    });
  }

  // --- Safe Same-Origin-Only Web Audio Graph (Loudness Normalization + 150ms Crossfade + Bass Glow) ---

  ensureWebAudioGraph() {
    if (this.audioCtx) return true;
    const AudioCtxCtor = typeof window !== 'undefined'
      ? (window.AudioContext || window.webkitAudioContext)
      : null;
    if (!AudioCtxCtor || typeof Audio === 'undefined') return false;
    if (this.graphFailed) return false;
    let ctx;
    try {
      ctx = new AudioCtxCtor();
      const source = ctx.createMediaElementSource(this.streamAudio);
      const replayGain = ctx.createGain();
      const compressor = ctx.createDynamicsCompressor();
      const gain = ctx.createGain();
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      analyser.smoothingTimeConstant = 0.82;

      const deckAGain = ctx.createGain();
      const deckBGain = ctx.createGain();
      const standbySource = ctx.createMediaElementSource(this.standbyAudio);
      source.connect(deckAGain);
      standbySource.connect(deckBGain);
      this.deckGains.set(this.streamAudio, deckAGain.gain);
      this.deckGains.set(this.standbyAudio, deckBGain.gain);
      let previous = deckAGain;
      this.eqFilters = EQ_FREQUENCIES.map((frequency, i) => {
        const filter = ctx.createBiquadFilter();
        filter.type = i === 0 ? 'lowshelf' : i === 9 ? 'highshelf' : 'peaking';
        filter.frequency.value = frequency;
        filter.Q.value = 1;
        previous.connect(filter);
        previous = filter;
        return filter;
      });
      deckBGain.connect(this.eqFilters[0]);
      previous.connect(replayGain);
      replayGain.connect(compressor);
      compressor.connect(gain);
      gain.connect(analyser);
      analyser.connect(ctx.destination);

      this.audioCtx = ctx;
      if (this.outputDeviceId && ctx.setSinkId) ctx.setSinkId(this.outputDeviceId).catch(() => {});
      this.mediaSourceNode = source;
      this.replayGainNode = replayGain;
      this.compressorNode = compressor;
      this.gainNode = gain;
      this.analyserNode = analyser;
      this.analyserData = new Uint8Array(analyser.frequencyBinCount);
      this.applyWebAudioSettings();
      return true;
    } catch (err) {
      this.graphFailed = true;
      this.audioCtx = null;
      ctx?.close?.().catch?.(() => {});
      console.warn('Web Audio graph init skipped:', err);
      return false;
    }
  }

  applyWebAudioSettings() {
    if (!this.audioCtx || !this.compressorNode || !this.gainNode) return;
    const cfg = state.get('settings') || {};
    this.applyEqualizerSettings(cfg);
    const normEnabled = cfg.loudness_normalization !== false;
    const now = this.audioCtx.currentTime;
    const loudness = this.currentLoudness || state.get('audioMeta')?.loudness || null;
    try {
      if (normEnabled) {
        const hasTagGain = Boolean(
          loudness
          && loudness.source
          && loudness.source !== 'none'
          && Number.isFinite(Number(loudness.computed_gain_linear))
        );
        if (hasTagGain) {
          const targetGain = Math.max(0.15, Math.min(2.5, Number(loudness.computed_gain_linear)));
          if (this.replayGainNode?.gain) {
            this.replayGainNode.gain.setTargetAtTime(targetGain, now, 0.03);
          }
          // Transparent brickwall safety limiter preserving original dynamic range
          this.compressorNode.threshold.setValueAtTime(-2, now);
          this.compressorNode.knee.setValueAtTime(6, now);
          this.compressorNode.ratio.setValueAtTime(12, now);
          this.compressorNode.attack.setValueAtTime(0.003, now);
          this.compressorNode.release.setValueAtTime(0.12, now);
        } else {
          // Adaptive leveling fallback with makeup gain when ReplayGain/R128 tags are absent
          if (this.replayGainNode?.gain) {
            this.replayGainNode.gain.setTargetAtTime(1.15, now, 0.03);
          }
          this.compressorNode.threshold.setValueAtTime(-16, now);
          this.compressorNode.knee.setValueAtTime(22, now);
          this.compressorNode.ratio.setValueAtTime(3.5, now);
          this.compressorNode.attack.setValueAtTime(0.005, now);
          this.compressorNode.release.setValueAtTime(0.18, now);
        }
      } else {
        if (this.replayGainNode?.gain) {
          this.replayGainNode.gain.setTargetAtTime(1.0, now, 0.02);
        }
        this.compressorNode.threshold.setValueAtTime(0, now);
        this.compressorNode.knee.setValueAtTime(0, now);
        this.compressorNode.ratio.setValueAtTime(1, now);
      }
      this.gainNode.gain.setTargetAtTime(this.muted ? 0 : volumeToGain(this.targetVolume), now, 0.02);
    } catch {}
  }

  /**
   * Selects the safe audio element for audioUrl:
   * - Same-origin `/api/stream/...` URLs route through `streamAudio` + Web Audio graph (when supported).
   * - Cross-origin direct CDN URLs ALWAYS route through untouched `directAudio` (never muted by CORS!).
   * @param {string} audioUrl
   */
  finishFade() {
    const fade = this.fade;
    this.fade = null;
    if (!fade) return;
    clearTimeout(fade.timer);
    this.resetMediaElement(fade.outgoing);
    for (const param of this.deckGains.values()) {
      try {
        param.cancelScheduledValues(this.audioCtx.currentTime);
        param.value = 1;
      } catch { param.value = 1; }
    }
  }

  reserveOutgoing() {
    const outgoing = this.streamAudio;
    this.streamAudio = this.standbyAudio;
    this.standbyAudio = outgoing;
    this.audio = this.streamAudio;
    this.fade = { outgoing, started: false, timer: setTimeout(() => this.finishFade(), 5000) };
  }

  startFade() {
    const fade = this.fade;
    if (!fade || fade.started) return;
    const now = this.audioCtx.currentTime;
    const duration = Math.max(0, Math.min(500, Number(state.get('settings')?.crossfade_ms ?? 150)));
    try {
      fade.started = true;
      for (const [deck, start, end] of [[fade.outgoing, 1, 0], [this.audio, 0, 1]]) {
        const gain = this.deckGains.get(deck);
        gain.cancelScheduledValues(now);
        gain.setValueAtTime(start, now);
        gain.linearRampToValueAtTime(end, now + duration / 1000);
      }
      clearTimeout(fade.timer);
      fade.timer = setTimeout(() => this.finishFade(), duration);
    } catch { this.finishFade(); }
  }

  showAudioBypass(active) {
    state.set({ audioBypass: active });
    globalThis.document?.querySelectorAll?.('.audio-bypass').forEach(el => { el.hidden = !active; });
  }

  prepareAudioElementForUrl(audioUrl) {
    this.clearAudioCtxWatchdog();
    const useGraph = isSameOriginStreamUrl(audioUrl) && typeof Audio !== 'undefined' && this.ensureWebAudioGraph();
    if (!useGraph) this.finishFade();
    this.audio = useGraph ? this.streamAudio : this.directAudio;
    this.resetMediaElement(useGraph ? this.directAudio : this.streamAudio);
    this.activeUsesWebAudio = useGraph;
    this.showAudioBypass(!useGraph);
    if (useGraph) {
      if (this.audioCtx.state === 'suspended') {
        this.resumeAudioContextWithFallback(this.requestSequence, audioUrl);
      }
      this.deckGains.get(this.streamAudio).value = this.fade ? 0 : 1;
      this.applyWebAudioSettings();
    } else this.directAudio.volume = this.muted ? 0 : volumeToGain(this.targetVolume);
    return this.audio;
  }

  fallbackToDirect(seq, url) {
    if (seq !== this.requestSequence || this.activeSrc !== url || !this.activeUsesWebAudio) return;
    this.clearAudioCtxWatchdog();
    const resumeAt = (this.pendingResumeSeekSec !== null && this.pendingResumeSeekSec > 0)
      ? this.pendingResumeSeekSec
      : (this.streamAudio?.currentTime > 0 ? this.streamAudio.currentTime : 0);
    this.finishFade();
    this.audio = this.directAudio;
    this.activeUsesWebAudio = false;
    this.showAudioBypass(true);
    this.resetMediaElement(this.streamAudio);
    this.directAudio.src = url;
    this.directAudio.playbackRate = this.playbackRate;
    this.directAudio.volume = this.muted ? 0 : volumeToGain(this.targetVolume);
    if (resumeAt > 0) {
      this.pendingResumeSeekSec = resumeAt;
      this.resumeAutoplaySequence = this.userPausedSequence !== seq ? seq : null;
      if ((this.directAudio.readyState ?? 0) >= 1) {
        this.applyPendingResumeSeek(this.directAudio, seq);
      }
      return;
    }
    if (this.userPausedSequence !== seq) {
      this.startElementPlayback(this.directAudio, seq, url);
    }
  }

  applyPendingResumeSeek(el, expectedSeq = this.requestSequence) {
    if (el !== this.audio || !this.activeSrc || expectedSeq !== this.requestSequence) return false;
    if (this.pendingResumeSeekSec === null) return false;
    const rawTarget = Number(this.pendingResumeSeekSec);
    this.pendingResumeSeekSec = null;
    const shouldAutoplay = this.resumeAutoplaySequence === expectedSeq && this.userPausedSequence !== expectedSeq;
    this.resumeAutoplaySequence = null;

    const dur = Number(el.duration);
    let targetSec = Number.isFinite(rawTarget) && rawTarget > 0 ? rawTarget : 0;
    if (Number.isFinite(dur) && dur > 0) {
      if (targetSec >= Math.max(0, dur - 1.5)) {
        targetSec = 0;
      } else {
        targetSec = Math.max(0, Math.min(targetSec, Math.max(0, dur - 0.5)));
      }
    }
    if (targetSec > 0) {
      try {
        el.currentTime = targetSec;
      } catch {
        targetSec = 0;
      }
    }
    this.lastSavedPosition = targetSec;
    if (shouldAutoplay) {
      this.startElementPlayback(el, expectedSeq, this.activeSrc);
    }
    return true;
  }

  startElementPlayback(el, seq, audioUrl) {
    if (!el || seq !== this.requestSequence || el !== this.audio || this.activeSrc !== audioUrl) return;
    if (this.userPausedSequence === seq) {
      this.pauseAllAudio({ markUserPause: false });
      return;
    }
    if (this.activeUsesWebAudio && this.audioCtx?.state === 'suspended') {
      this.resumeAudioContextWithFallback(seq, audioUrl);
    }
    let playPromise;
    try {
      playPromise = el.play();
    } catch (err) {
      this.handlePlayRejection(err, seq, audioUrl, el);
      return;
    }
    if (playPromise && typeof playPromise.catch === 'function') {
      playPromise.catch((err) => this.handlePlayRejection(err, seq, audioUrl, el));
    }
  }

  handlePlayRejection(err, seq, audioUrl, el) {
    if (seq !== this.requestSequence || el !== this.audio || this.activeSrc !== audioUrl) return;
    if (this.userPausedSequence === seq) return;
    // An active error-retry handler owns media reset, state updates, and recovery for this sequence.
    if (this._retryingAudioForSeq === seq) return;
    const name = String(err?.name || '');
    if (name === 'AbortError') {
      if (el?.error) {
        this.handleAudioElementError();
      } else if (el?.paused) {
        state.set({ isPlaying: false });
        this.updatePlayButtonUI(false);
      }
      return;
    }
    if (name === 'NotAllowedError') {
      const pos = this.lastSavedPosition || el?.currentTime || 0;
      if (pos > 0) this.pendingResumeSeekSec = pos;
      state.set({ isPlaying: false, playbackError: '浏览器阻止了自动播放，请再次点击播放按钮' });
      this.updatePlayButtonUI(false);
      toast.info('请点击播放', '浏览器限制了自动播放，再次点击播放按钮即可继续。');
      return;
    }
    if (el?.error && !this.retriedCurrentTrack && this.currentSong) {
      this.handleAudioElementError();
      return;
    }
    if (this.activeUsesWebAudio && !el?.error) {
      this.fallbackToDirect(seq, audioUrl);
      return;
    }
    if (!this.retriedCurrentTrack && this.currentSong) {
      this.handleAudioElementError();
      return;
    }
    const resumePos = this.lastSavedPosition || el?.currentTime || 0;
    this.resetMediaElement(this.audio);
    this.activeSrc = '';
    if (resumePos > 0) this.pendingResumeSeekSec = resumePos;
    const reason = err?.message || '无法播放当前音源';
    state.set({ isPlaying: false, playbackError: reason });
    this.updatePlayButtonUI(false);
    this.updateSourceBadgeUI(state.get('audioMeta'), '播放失败');
  }

  getFrequencyData() {
    if (!this.activeUsesWebAudio || !this.analyserNode) return null;
    this.analyserNode.getByteFrequencyData(this.analyserData);
    return this.analyserData;
  }

  // --- Genuine Play History Recording ---

  accumulateListenTime() {
    if (this.listenLastTickMs > 0) {
      const delta = Date.now() - this.listenLastTickMs;
      if (delta > 0) {
        this.listenAccumMs += delta;
      }
    }
    if (this.listenLastTickMs) this.listenLastTickMs = Date.now();
  }

  maybeRecordGenuineListen(completed = false, flush = false) {
    const song = this.currentSong;
    if (!song || !this.currentPlaybackEventId || song._origin === 'deck') return;
    if (!completed && this.listenAccumMs < GENUINE_LISTEN_MIN_MS) return;
    if (!flush && !completed && this.listenRecordedForSeq === this.requestSequence) return;
    this.listenRecordedForSeq = this.requestSequence;
    const payload = { ...serializeTrackForStorage(song), ...this.playbackEventScope, event_id: this.currentPlaybackEventId,
      song_id: getTrackKey(song).startsWith('netease:') ? Number(song.id) : 0 };
    const seq = this.requestSequence;
    return api.recordPlayback(payload, Math.round(this.listenAccumMs), completed).then(res => {
      if (res?.ok) state.emit('history:updated');
      else if (seq === this.requestSequence) this.listenRecordedForSeq = -1;
    }).catch(() => { if (seq === this.requestSequence) this.listenRecordedForSeq = -1; });
  }

  scheduleSavePlaybackSession(immediate = false, { keepalive = false } = {}) {
    if (!this.sessionOwner || this.sessionOwner !== state.getUserId()) return;
    if (!immediate && Date.now() - this.lastSessionSave < 5000) return;
    this.lastSessionSave = Date.now();
    const queue = state.get('playQueue') || [];
    const index = state.get('playQueueIndex') ?? -1;
    const start = Math.max(0, Math.min(queue.length - 221, index - 110));
    const payload = {
      ...state.writeScope(),
      current_track: serializeTrackForStorage(this.currentSong), is_playing: Boolean(state.get('isPlaying')),
      position_sec: this.pendingResumeSeekSec ?? (this.activeSrc ? this.audio.currentTime || 0 : 0),
      duration_sec: this.getNormalizedDuration() || state.get('duration') || 0,
      queue: queue.slice(start, start + 221).map(serializeTrackForStorage), queue_index: index - start,
      play_mode: state.get('playMode'), volume: this.targetVolume,
      muted: this.muted, playback_rate: this.playbackRate, active_tab: state.get('activeTab'),
    };
    if (keepalive) {
      this.queuedSession = null;
      return api.savePlaybackSession(payload, { keepalive });
    }
    this.queuedSession = { payload, owner: this.sessionOwner };
    if (!this.sessionWriter) {
      this.sessionWriter = this.flushPlaybackSessions().finally(() => { this.sessionWriter = null; });
    }
    return this.sessionWriter;
  }

  async flushPlaybackSessions() {
    while (this.queuedSession) {
      const { owner, payload } = this.queuedSession;
      this.queuedSession = null;
      if (owner === this.sessionOwner && owner === state.getUserId()) await api.savePlaybackSession(payload);
    }
  }

  async restorePlaybackSession() {
    this.stopAndReset();
    const seq = this.requestSequence;
    const generation = this.restoreGeneration;
    const owner = state.getUserId();
    const scope = state.writeScope();
    const queueVersion = state.get('queueVersion');
    const res = await api.getPlaybackSession();
    if (owner !== state.getUserId() || generation !== this.restoreGeneration || scope.session_generation !== state.get('sessionGeneration')) return;
    if (seq !== this.requestSequence || queueVersion !== state.get('queueVersion')) {
      this.sessionOwner = owner;
      this.scheduleSavePlaybackSession(true);
      return;
    }
    if (!res.ok || (res.data?.account_id && String(res.data.account_id) !== scope.expected_account_id)) return;
    const data = res.data;
    if (data && (data.current_track || data.queue?.length)) {
      const queue = data.queue || [];
      this.currentSong = data.current_track || queue[Math.max(0, data.queue_index)] || queue[0];
      this.pendingResumeSeekSec = Number(data.position_sec) || 0;
      this.setVolume(data.volume ?? 0.8, Boolean(data.muted));
      this.setPlaybackRate(data.playback_rate || 1, false);
      state.set({ playQueueOrigin: '播放队列', playQueue: queue, playQueueIndex: data.queue_index ?? -1, playMode: data.play_mode || 'order',
        currentPlayingSong: this.currentSong, currentTime: this.pendingResumeSeekSec, duration: data.duration_sec,
        resumableSession: { ...data, current_track: this.currentSong }, isPlaying: false });
      this.updateTrackInfoUI(this.currentSong);
      this.updateHeartUI(this.currentSong);
      this.updatePlayModeUI();
      this.updateTimelineUI(this.pendingResumeSeekSec, data.duration_sec);
    }
    this.sessionOwner = owner;
  }

  applyEqualizerSettings({ eq_enabled, eq_preset, eq_bands }) {
    const bands = eq_bands || EQ_PRESETS[eq_preset] || EQ_PRESETS.flat;
    this.eqFilters?.forEach((filter, i) => {
      filter.gain.value = eq_enabled ? Math.max(-12, Math.min(12, Number(bands[i]) || 0)) : 0;
    });
  }

  async setOutputDevice(deviceId) {
    if (this.audioCtx?.setSinkId) await this.audioCtx.setSinkId(deviceId);
    for (const el of [this.directAudio, this.streamAudio, this.standbyAudio]) {
      if (el.setSinkId) await el.setSinkId(deviceId);
    }
  }

  setPlaybackRate(value, persist = true) {
    this.playbackRate = Math.min(2, Math.max(0.5, Number(value) || 1));
    for (const el of [this.directAudio, this.streamAudio, this.standbyAudio]) el.playbackRate = this.playbackRate;
    state.set({ playbackRate: this.playbackRate });
    this.updateMediaSessionPosition();
    this.pushDesktopLyricsState(true);
    if (persist) {
      state.updateSettings({ playback_rate: this.playbackRate });
      api.saveSettings({ playback_rate: this.playbackRate });
      this.scheduleSavePlaybackSession(true);
    }
  }

  setSleepTimer(mode) {
    clearInterval(this.sleepInterval);
    if (this.sleepFading) {
      this.sleepFading = false;
      this.directAudio.volume = this.muted ? 0 : volumeToGain(this.targetVolume);
      this.applyWebAudioSettings();
    }
    this.sleepMode = String(mode);
    this.sleepDeadline = Number(mode) > 0 ? Date.now() + Number(mode) * 60000 : 0;
    this.updateSleepTimer();
    if (this.sleepDeadline) this.sleepInterval = setInterval(() => this.updateSleepTimer(), 500);
  }

  updateSleepTimer() {
    const remaining = Math.ceil((this.sleepDeadline - Date.now()) / 1000);
    if (this.sleepDeadline && remaining <= 0) {
      this.pauseAllAudio();
      this.setSleepTimer('off');
      return;
    }
    if (this.sleepDeadline && remaining <= 3 && !this.audio.paused) {
      this.sleepFading = true;
      const gain = (this.muted ? 0 : volumeToGain(this.targetVolume)) * (this.sleepDeadline - Date.now()) / 3000;
      if (this.activeUsesWebAudio) this.gainNode.gain.setTargetAtTime(Math.max(0, gain), this.audioCtx.currentTime, 0.15);
      else this.directAudio.volume = Math.max(0, gain);
    }
    state.set({ sleepMode: this.sleepMode, sleepRemaining: Math.max(0, remaining) });
  }

  reorderQueue(from, to) {
    const queue = [...state.get('playQueue')];
    if (![from, to].every(i => Number.isInteger(i) && i >= 0 && i < queue.length) || from === to) return;
    const idx = state.get('playQueueIndex');
    queue.splice(to, 0, queue.splice(from, 1)[0]);
    let next = idx;
    if (idx === from) next = to;
    else if (from < idx && to >= idx) next--;
    else if (to <= idx && from > idx) next++;
    this.explicitNextIndices = (this.explicitNextIndices || []).map(i => {
      if (i === from) return to;
      if (from < i && to >= i) return i - 1;
      if (to <= i && from > i) return i + 1;
      return i;
    });
    state.set({ playQueue: queue, playQueueIndex: next });
  }

  // --- Desktop Floating Lyrics & Windows Global Hotkeys ---

  async toggleDesktopLyricsWindow(forceOpen = null) {
    const nextOpen = forceOpen !== null ? Boolean(forceOpen) : !state.get('desktopLyricsOpen');
    const locked = Boolean(state.get('desktopLyricsLocked'));
    const res = await api.setDesktopLyricsWindow(nextOpen, locked);
    if (res.ok && res.data) {
      state.set({
        desktopLyricsOpen: Boolean(res.data.open),
        desktopLyricsLocked: Boolean(res.data.locked),
      });
      if (res.data.open) {
        this.pushDesktopLyricsState(true);
      }
      if (res.data.supported === false) {
        toast.info('桌面歌词预览', '当前为浏览器无窗口模式，可打开 /desktop_lyrics.html 独立悬浮预览。');
      } else {
        toast.info('桌面悬浮歌词', res.data.open ? '桌面歌词窗口已开启' : '桌面歌词窗口已关闭');
      }
      return res.data;
    }
    toast.warning('桌面歌词暂不可用', res.error?.message || '未能切换桌面歌词窗口。');
    return null;
  }

  async toggleDesktopLyricsLock(forceLocked = null) {
    const nextLocked = forceLocked !== null ? Boolean(forceLocked) : !state.get('desktopLyricsLocked');
    const isOpen = Boolean(state.get('desktopLyricsOpen'));
    const res = await api.setDesktopLyricsWindow(isOpen, nextLocked);
    if (res.ok && res.data) {
      state.set({
        desktopLyricsOpen: Boolean(res.data.open),
        desktopLyricsLocked: Boolean(res.data.locked),
      });
      toast.info('桌面歌词穿透锁', nextLocked ? '已锁定（鼠标穿透）' : '已解锁（可拖动）');
      return res.data;
    }
    return null;
  }

  pushDesktopLyricsState(force = false, osd = null) {
    if (!state.get('desktopLyricsOpen') && !state.get('lyricsPreviewSubscribers')) return;
    const now = Date.now();
    const curTime = this.pendingResumeSeekSec ?? (this.audio.currentTime || 0);
    const snap = lyricsManager.getActiveLyricSnapshot(curTime);
    if (!force && !osd && snap.line_index === this.lastDesktopLineIdx && now - this.lastDesktopPushAt < 320) {
      return;
    }
    this.lastDesktopPushAt = now;
    this.lastDesktopLineIdx = snap.line_index;

    const song = this.currentSong;
    const payload = {
      title: song?.title || 'Pruner Studio',
      artist: song ? formatArtists(song.artists) : '',
      album: song?.album || '',
      is_playing: Boolean(state.get('isPlaying')),
      current_time: curTime,
      duration: this.getNormalizedDuration(),
      offset_sec: lyricsManager.getSongOffset(), playback_rate: this.audio.playbackRate || 1,
      gpu_performance_mode: state.get('settings')?.gpu_performance_mode || 'balanced',
      game_mode: Boolean(state.get('settings')?.game_mode),
      volume: this.targetVolume,
      muted: this.muted,
      player_instance_id: this.playerInstanceId, msg_seq: ++this.lyricsMsgSeq,
      playback_seq: this.requestSequence, buffering: Boolean(this.isLoadingTrack || this.isBuffering),
      liked: state.isSongLiked(song),
      line: snap.line,
      translation: snap.translation,
      next_line: snap.next_line,
      line_index: snap.line_index,
      line_start: snap.line_start ?? 0,
      words: Array.isArray(snap.words) ? snap.words : [],
      sent_at: Date.now(),
      locked: Boolean(state.get('desktopLyricsLocked')),
    };
    if (osd && typeof osd === 'object' && osd.text) {
      payload.osd = osd;
    }
    api.pushDesktopPlaybackState(payload).catch(() => {});
  }

  handleGlobalHotkeyAction(action, payload = null) {
    const act = String(action || '').toLowerCase();
    if (act === 'next') {
      this.playNext(true);
      this.pushDesktopLyricsState(true, { text: '下一首', kind: 'track' });
    } else if (act === 'prev') {
      this.playPrev();
      this.pushDesktopLyricsState(true, { text: '上一首', kind: 'track' });
    } else if (act === 'toggle_play') {
      this.togglePlay();
    } else if (act === 'toggle_like') {
      this.toggleLikeCurrentSong();
    } else if (act === 'volume_up') {
      this.adjustVolume(0.05, { showOsd: true });
    } else if (act === 'volume_down') {
      this.adjustVolume(-0.05, { showOsd: true });
    } else if (act === 'toggle_mute') {
      this.toggleMute({ showOsd: true });
    } else if (act === 'set_volume') {
      const val = Number(payload?.value);
      if (Number.isFinite(val)) {
        this.setVolume(val, val <= 0, { showOsd: false });
      }
    } else if (act === 'toggle_desktop_lyrics_lock') {
      this.pushDesktopLyricsState(true, {
        text: state.get('desktopLyricsLocked') ? '桌面歌词已锁定穿透' : '桌面歌词已解锁',
        kind: 'lock',
      });
    } else if (act === 'toggle_game_mode') {
      globalThis.window?.dispatchEvent?.(new CustomEvent('pruner:toggle-game-mode'));
    } else if (act === 'lyric_offset_minus' || act === 'lyric_offset_plus') {
      const next = lyricsManager.adjustSongOffset(act === 'lyric_offset_plus' ? 0.5 : -0.5);
      this.pushDesktopLyricsState(true, { text: `歌词偏移 ${next > 0 ? '+' : ''}${next.toFixed(1)}s`, kind: 'lyric' });
    } else if (act === 'open_lyrics_settings') {
      window.dispatchEvent(new CustomEvent('switch-tab', { detail: { tab: 'cloud', subTab: 'settings' } }));
      document.getElementById('desktop-lyrics-settings')?.scrollIntoView({ block: 'start' });
    }
    else if (act === 'toggle_desktop_lyrics') {
      // Backend desktop.py already toggled lyrics_ctrl and broadcasts `event: desktop_lyrics` with window state.
      // Avoid issuing a conflicting second POST /api/desktop/lyrics-window toggle; push lyrics if open.
      if (state.get('desktopLyricsOpen')) {
        this.pushDesktopLyricsState(true);
      }
    }
  }

  getNormalizedDuration() {
    if (this.audio && Number.isFinite(this.audio.duration) && this.audio.duration > 0) {
      return this.audio.duration;
    }
    if (this.currentSong?.duration) {
      return normalizeTimeToSeconds(this.currentSong.duration, 0);
    }
    return 0;
  }

  // --- Media Session API (Windows SMTC & Lockscreen Controls) ---

  initMediaSessionHandlers() {
    if (typeof navigator === 'undefined' || !navigator.mediaSession) return;
    const ms = navigator.mediaSession;
    const safeHandler = (action, fn) => {
      try {
        ms.setActionHandler(action, fn);
      } catch {}
    };
    safeHandler('play', () => this.togglePlay(true));
    safeHandler('pause', () => this.togglePlay(false));
    safeHandler('previoustrack', () => this.playPrev());
    safeHandler('nexttrack', () => this.playNext(true));
    safeHandler('seekbackward', (details) => this.seekDelta(-(details?.seekOffset || 5)));
    safeHandler('seekforward', (details) => this.seekDelta(details?.seekOffset || 5));
    safeHandler('seekto', (details) => {
      if (details && typeof details.seekTime === 'number') {
        this.seek(details.seekTime);
      }
    });
  }

  updateMediaSessionMetadata(song) {
    if (typeof navigator === 'undefined' || !navigator.mediaSession || typeof MediaMetadata === 'undefined') return;
    if (!song) {
      try {
        navigator.mediaSession.metadata = null;
        navigator.mediaSession.playbackState = 'none';
      } catch {}
      return;
    }
    try {
      const artwork = [];
      const rawCover = String(song.cover_url || state.get('audioMeta')?.cover_url || '').trim();
      const origin = typeof window !== 'undefined' && window.location?.origin && window.location.origin !== 'null'
        ? window.location.origin
        : '';
      if (rawCover.startsWith('/api/local-cover/') || rawCover.startsWith('/api/opensubsonic/cover/')) {
        const absUrl = origin ? `${origin}${rawCover}` : rawCover;
        artwork.push({ src: absUrl, sizes: '384x384', type: 'image/jpeg' });
      } else if (isSafeUrl(rawCover)) {
        const isNeteaseCdn = /^https:\/\/[^/]+\.(?:music\.)?126\.net\//i.test(rawCover);
        if (isNeteaseCdn && origin) {
          artwork.push(
            { src: `${origin}/api/cover?url=${encodeURIComponent(rawCover)}&size=96`, sizes: '96x96', type: 'image/jpeg' },
            { src: `${origin}/api/cover?url=${encodeURIComponent(rawCover)}&size=192`, sizes: '192x192', type: 'image/jpeg' },
            { src: `${origin}/api/cover?url=${encodeURIComponent(rawCover)}&size=384`, sizes: '384x384', type: 'image/jpeg' },
          );
        } else {
          artwork.push(
            { src: getSizedCoverUrl(rawCover, 96), sizes: '96x96', type: 'image/jpeg' },
            { src: getSizedCoverUrl(rawCover, 192), sizes: '192x192', type: 'image/jpeg' },
            { src: getSizedCoverUrl(rawCover, 384), sizes: '384x384', type: 'image/jpeg' },
          );
        }
      }
      navigator.mediaSession.metadata = new MediaMetadata({
        title: song.title || '未知曲目',
        artist: formatArtists(song.artists),
        album: song.album || '',
        artwork,
      });
    } catch {}
  }

  updateMediaSessionPlaybackState(playbackState) {
    if (typeof navigator === 'undefined' || !navigator.mediaSession) return;
    try {
      navigator.mediaSession.playbackState = playbackState;
      this.updateMediaSessionPosition();
    } catch {}
  }

  updateMediaSessionPosition() {
    if (typeof navigator === 'undefined' || !navigator.mediaSession || typeof navigator.mediaSession.setPositionState !== 'function') return;
    const duration = this.getNormalizedDuration();
    if (!Number.isFinite(duration) || duration <= 0) return;
    const rawPos = Number(this.audio?.currentTime);
    const position = Number.isFinite(rawPos) ? Math.min(duration, Math.max(0, rawPos)) : 0;
    const rate = Number.isFinite(Number(this.playbackRate)) && Number(this.playbackRate) > 0 ? Number(this.playbackRate) : 1;
    try {
      navigator.mediaSession.setPositionState({ duration, playbackRate: rate, position });
    } catch {}
  }

  // --- Queue Management & Auto-Advance ---

  /**
   * Loads a list of songs into the unified playback queue and starts playing at startIndex.
   * @param {Array} songs
   * @param {number} [startIndex=0]
   * @param {string|null} [mode=null]
   * @param {string|{origin: string}} [origin] Display label for this listening queue.
   */
  async playQueue(songs, startIndex = 0, mode = null, origin = '') {
    if (!Array.isArray(songs) || songs.length === 0) return;
    this.unlockAudioContextSync();
    const cleanQueue = songs.filter(Boolean);
    if (cleanQueue.length === 0) return;
    const clampedIndex = Math.max(0, Math.min(startIndex, cleanQueue.length - 1));
    this.explicitNextIndices = [];
    if (mode) {
      state.setPlayMode(mode);
    }
    state.set({
      playQueueOrigin: (typeof origin === 'string' ? origin : origin?.origin)
        || ({ fm: '私人漫游', smart_radio: '真·心动电台' }[mode] || '播放队列'),
      playQueue: cleanQueue,
      playQueueIndex: clampedIndex,
    });
    this.updatePlayModeUI();
    await this.playSong(cleanQueue[clampedIndex], true, { preserveQueue: true });
  }

  enqueueNext(song) {
    if (!song) return;
    const queue = [...(state.get('playQueue') || [])];
    const idx = state.get('playQueueIndex') ?? -1;
    const insertAt = idx >= 0 ? idx + 1 : queue.length;
    queue.splice(insertAt, 0, song);
    this.explicitNextIndices = (this.explicitNextIndices || []).map(i => (i >= insertAt ? i + 1 : i));
    this.explicitNextIndices.unshift(insertAt);
    state.set({ playQueue: queue });
    toast.quiet(`已加入下一首：${song.title || '曲目'}`);
  }

  enqueueEnd(song) {
    if (!song) return;
    const queue = [...(state.get('playQueue') || [])];
    queue.push(song);
    state.set({ playQueue: queue });
  }

  removeFromQueue(index) {
    const queue = [...(state.get('playQueue') || [])];
    if (index < 0 || index >= queue.length) return;
    let currentIdx = state.get('playQueueIndex') ?? -1;
    const removingCurrent = index === currentIdx;
    queue.splice(index, 1);
    this.explicitNextIndices = (this.explicitNextIndices || [])
      .filter(i => i !== index)
      .map(i => (i > index ? i - 1 : i));
    if (index < currentIdx) {
      currentIdx -= 1;
    } else if (index === currentIdx) {
      currentIdx = Math.min(currentIdx, queue.length - 1);
    }
    state.set({ playQueue: queue, playQueueIndex: currentIdx });
    if (removingCurrent && this.currentSong) {
      if (queue.length) return this.playSong(queue[currentIdx], state.get('isPlaying'), { preserveQueue: true });
      const owner = this.sessionOwner;
      this.stopAndReset();
      this.sessionOwner = owner;
    }
    return this.scheduleSavePlaybackSession(true);
  }

  clearQueue() {
    this.shuffleBag = [];
    this.shuffleBagCursor = 0;
    this.shuffleQueueSig = '';
    this.playHistoryStack = [];
    this.explicitNextIndices = [];
    this.explicitNextIndices = [];
    state.set({ playQueueOrigin: '', playQueue: [], playQueueIndex: -1, playHistoryStack: [] });
  }

  cyclePlayMode() {
    const cycle = ['order', 'loop', 'single', 'shuffle'];
    const current = state.get('playMode') || 'order';
    const idx = cycle.indexOf(current);
    const next = cycle[(idx + 1) % cycle.length];
    state.setPlayMode(next);
    if (next === 'shuffle') {
      this.rebuildShuffleBag(state.get('playQueue') || [], this.currentSong);
    }
    this.updatePlayModeUI();
    const labels = { order: '顺序播放', loop: '列表循环', single: '单曲循环', shuffle: '均衡随机' };
    toast.quiet(`播放模式：${labels[next] || next}`);
  }

  getQueueSignature(queue = []) {
    return JSON.stringify(queue.map(getTrackKey));
  }

  /**
   * Builds a Martin Fiedler Balanced Dithered Shuffle round with 0 repeats per round.
   * Synchronously populates from client-side Fiedler fallback, then asynchronously upgrades
   * upcoming unplayed slots via backend POST /api/queue/smart-shuffle (play_history cooldown).
   */
  rebuildShuffleBag(queue = [], currentSong = null) {
    if (!Array.isArray(queue) || queue.length === 0) {
      this.shuffleBag = [];
      this.shuffleBagCursor = 0;
      this.shuffleQueueSig = '';
      return;
    }
    const sig = this.getQueueSignature(queue);
    this.shuffleQueueSig = sig;
    const ordered = balancedShuffleFallback(queue, currentSong ? getTrackKey(currentSong) : null);
    const idToIndices = new Map();
    queue.forEach((song, idx) => {
      const k = getTrackKey(song);
      if (!idToIndices.has(k)) idToIndices.set(k, []);
      idToIndices.get(k).push(idx);
    });
    const bagIndices = [];
    for (const item of ordered) {
      const k = getTrackKey(item);
      const list = idToIndices.get(k);
      if (list && list.length > 0) {
        bagIndices.push(list.shift());
      }
    }
    this.shuffleBag = bagIndices;
    this.shuffleBagCursor = currentSong && bagIndices.length > 0 && getTrackKey(queue[bagIndices[0]]) === getTrackKey(currentSong)
      ? 1
      : 0;

    // Async backend Martin Fiedler + SQLite play_history recency cooldown upgrade
    if (queue.length >= 4 && state.get('authenticated') && !state.get('offlineMode')) {
      const seq = ++this.smartShuffleSeq;
      const slimTracks = queue.map((s, idx) => ({
        id: s.id,
        title: s.title || '',
        artists: s.artists || [],
        album: s.album || '',
        _qidx: idx,
      }));
      api.smartShuffle(slimTracks).then((res) => {
        if (seq !== this.smartShuffleSeq || this.shuffleQueueSig !== sig) return;
        const srvTracks = res?.ok && Array.isArray(res.data?.tracks) ? res.data.tracks : null;
        if (!srvTracks || srvTracks.length !== queue.length) return;
        const playedSet = new Set(this.shuffleBag.slice(0, this.shuffleBagCursor));
        const curIdx = state.get('playQueueIndex') ?? -1;
        if (curIdx >= 0) playedSet.add(curIdx);
        const remainingIndices = [];
        for (const t of srvTracks) {
          const qi = Number(t._qidx);
          if (Number.isInteger(qi) && qi >= 0 && qi < queue.length && !playedSet.has(qi)) {
            remainingIndices.push(qi);
          }
        }
        if (remainingIndices.length > 0) {
          this.shuffleBag = [...this.shuffleBag.slice(0, this.shuffleBagCursor), ...remainingIndices];
        }
      }).catch(() => {});
    }
  }

  getNextQueueIndex(userTriggered = false, peekOnly = false) {
    const queue = state.get('playQueue') || [];
    if (queue.length === 0) return -1;
    const currentIdx = state.get('playQueueIndex') ?? -1;
    const mode = state.get('playMode') || 'order';

    if (mode === 'single' && !userTriggered) {
      return currentIdx >= 0 ? currentIdx : 0;
    }
    while (Array.isArray(this.explicitNextIndices) && this.explicitNextIndices.length > 0) {
      const explicitIdx = this.explicitNextIndices[0];
      if (Number.isInteger(explicitIdx) && explicitIdx >= 0 && explicitIdx < queue.length && explicitIdx !== currentIdx) {
        if (!peekOnly) this.explicitNextIndices.shift();
        return explicitIdx;
      }
      this.explicitNextIndices.shift();
    }
    if (mode === 'shuffle' && queue.length > 1) {
      const sig = this.getQueueSignature(queue);
      if (this.shuffleQueueSig !== sig || this.shuffleBag.length === 0) {
        this.rebuildShuffleBag(queue, queue[currentIdx] || this.currentSong);
      }
      if (this.shuffleBagCursor >= this.shuffleBag.length) {
        if (peekOnly) return this.shuffleBag[0] ?? 0;
        this.rebuildShuffleBag(queue, queue[currentIdx] || this.currentSong);
      }
      let nextBagIdx = this.shuffleBag[this.shuffleBagCursor];
      if (nextBagIdx === currentIdx && this.shuffleBagCursor + 1 < this.shuffleBag.length) {
        if (!peekOnly) this.shuffleBagCursor += 1;
        nextBagIdx = this.shuffleBag[this.shuffleBagCursor + (peekOnly ? 1 : 0)];
      }
      if (!peekOnly) {
        this.shuffleBagCursor += 1;
      }
      return Number.isInteger(nextBagIdx) && nextBagIdx >= 0 && nextBagIdx < queue.length ? nextBagIdx : 0;
    }
    const nextIdx = currentIdx + 1;
    if (nextIdx < queue.length) return nextIdx;
    if (mode === 'smart_radio') return -1;
    if (mode === 'loop' || mode === 'intelligence' || userTriggered) {
      return 0;
    }
    return -1;
  }

  async playNext(userTriggered = false) {
    const seq = this.requestSequence;
    const mode = state.get('playMode') || 'order';
    const queue = state.get('playQueue') || [];
    const currentIdx = state.get('playQueueIndex') ?? -1;

    // Auto-refill Personal FM when nearing end of batch
    if (mode === 'fm' && queue.length - currentIdx <= 2) {
      if (await this.refillPersonalFmQueue() === false) return;
    }
    // Auto-refill True Heartbeat Smart Radio when <= 3 tracks remain
    if (mode === 'smart_radio' && queue.length - currentIdx <= 3) {
      if (await this.refillSmartRadioQueue() === false) return;
    }

    if (seq !== this.requestSequence || (!userTriggered && this.userPausedSequence === seq)) return;
    const updatedQueue = state.get('playQueue') || [];
    if (updatedQueue.length === 0) return;

    const nextIdx = this.getNextQueueIndex(userTriggered, false);
    if (nextIdx < 0 || !updatedQueue[nextIdx]) return;

    state.set({ playQueueIndex: nextIdx });
    await this.playSong(updatedQueue[nextIdx], true, { preserveQueue: true });
  }

  async playPrev() {
    // If more than 3s into current track, restart track first
    if ((this.audio.currentTime || 0) > 3 && this.audio.src) {
      this.seek(0);
      return;
    }
    // Exact back-traversal via playHistoryStack (critical for balanced shuffle & radio modes)
    if (this.playHistoryStack.length > 0) {
      const prevEntry = this.playHistoryStack.pop();
      state.set({ playHistoryStack: [...this.playHistoryStack] });
      const queue = state.get('playQueue') || [];
      const matchedIdx = queue.findIndex(s => getTrackKey(s) === getTrackKey(prevEntry));
      if (matchedIdx >= 0) {
        state.set({ playQueueIndex: matchedIdx });
        await this.playSong(queue[matchedIdx], true, { preserveQueue: true, fromHistory: true });
        return;
      }
      if (prevEntry) {
        await this.playSong(prevEntry, true, { preserveQueue: true, fromHistory: true });
        return;
      }
    }

    const queue = state.get('playQueue') || [];
    if (queue.length === 0) return;
    const currentIdx = state.get('playQueueIndex') ?? 0;
    const prevIdx = currentIdx > 0 ? currentIdx - 1 : queue.length - 1;
    state.set({ playQueueIndex: prevIdx });
    await this.playSong(queue[prevIdx], true, { preserveQueue: true, fromHistory: true });
  }

  async handleTrackEnded() {
    // Deck preview tracks must stop cleanly without advancing the global listening queue
    if (this.currentSong?._origin === 'deck') {
      return;
    }
    const mode = state.get('playMode') || 'order';
    if (mode === 'single' && this.currentSong) {
      await this.playSong(this.currentSong, true, { preserveQueue: true, origin: this.currentSong?._origin });
      return;
    }
    // Avoid auto-jumping away while actively reviewing a single track on Deck unless queue has multiple tracks
    const queue = state.get('playQueue') || [];
    if (queue.length > 1 || mode === 'fm' || mode === 'intelligence' || mode === 'smart_radio' || mode === 'loop') {
      await this.playNext(false);
    }
  }

  queueContext() {
    return [state.get('queueVersion'), state.get('playMode'), state.getUserId()];
  }

  queueContextMatches(before) {
    return this.queueContext().every((value, i) => value === before[i]);
  }

  async refillPersonalFmQueue() {
    if (this.fmRefillInFlight || !state.get('authenticated')) return;
    this.fmRefillInFlight = true;
    const context = this.queueContext();
    try {
      const res = await api.getPersonalFm();
      if (!this.queueContextMatches(context)) return false;
      if (res.ok && res.data && Array.isArray(res.data.songs) && res.data.songs.length > 0) {
        const existing = state.get('playQueue') || [];
        const existingIds = new Set(existing.map(s => String(s.id)));
        const fresh = res.data.songs.filter(s => !existingIds.has(String(s.id)));
        if (fresh.length > 0) {
          const merged = [...existing, ...fresh];
          state.set({
            playQueue: merged,
            personalFmQueue: fresh,
          });
        }
      }
    } catch {} finally {
      this.fmRefillInFlight = false;
    }
  }

  resetRadioSession() {
    this.radioSession = null;
    this.radioRefillRequest = this.radioManualRequest = null;
    this.radioOpSeq = (this.radioOpSeq || 0) + 1;
    state.set({ radioExhausted: false });
  }

  async requestRadio(mode, allowReplay = false) {
    const manual = mode !== 'refill' || allowReplay;
    if (!manual && (this.radioManualRequest || this.radioRefillRequest)) {
      return this.radioManualRequest || this.radioRefillRequest;
    }
    const opSeq = (this.radioOpSeq || 0) + Number(manual);
    this.radioOpSeq = opSeq;
    if (manual) this.radioRefillRequest = null;
    const context = this.queueContext();
    const scope = state.writeScope();
    const interrupted = manual && this.radioSession?.refillId;
    if (!this.radioSession || (mode === 'start' && !allowReplay && !interrupted && !this.radioSession.supersededRequestIds?.size)) {
      this.radioSession = { id: crypto.randomUUID(), ids: new Set(), keys: new Set() };
    }
    const session = this.radioSession;
    session.supersededRequestIds ??= new Set();
    session.reclaimedIds ??= new Set();
    if (interrupted) {
      session.supersededRequestIds.add(interrupted);
      session.refillId = null;
    }
    if (allowReplay) { session.ids.clear(); session.keys.clear(); }
    const remember = song => {
      const key = getTrackKey(song);
      if (key) session.keys.add(key);
      if (key.startsWith('netease:')) session.ids.add(Number(song.id));
    };
    if (mode !== 'start' && !allowReplay) (state.get('playQueue') || []).forEach(remember);
    const payload = { ...scope, mode, radio_session_id: session.id, request_id: crypto.randomUUID(),
      limit: mode === 'refill' ? 20 : 30, explore_ratio: state.get('settings')?.smart_radio_explore_ratio ?? 0.4,
      allow_replay: allowReplay, exclude_ids: [...session.ids], exclude_keys: [...session.keys] };
    const superseded = [...session.supersededRequestIds];
    if (superseded.length) payload.superseded_request_ids = superseded;
    if (!manual) session.refillId = payload.request_id;
    let accepted = false;
    const work = api.postSmartRadio(payload).then(async res => {
      if (opSeq !== this.radioOpSeq || session !== this.radioSession || !this.queueContextMatches(context) ||
          scope.session_generation !== state.get('sessionGeneration') ||
          res.status === 409 || res.error?.code === 'SESSION_GENERATION_MISMATCH') {
        return false;
      }
      if (!res.ok) { toast.warning('电台暂不可用', res.error?.message); return false; }
      const batch = res.data;
      if (!batch || batch.request_id !== payload.request_id || batch.radio_session_id !== session.id) return false;
      accepted = true;
      if (manual) {
        superseded.forEach(id => {
          session.supersededRequestIds.delete(id);
          session.reclaimedIds.add(id);
        });
      }
      const songs = (batch.songs || []).filter(song => {
        if (session.keys.has(getTrackKey(song))) return false;
        remember(song);
        return true;
      });
      state.set({ radioExhausted: Boolean(batch.exhausted), radioBreakdown: batch.breakdown });
      if (!songs.length) { toast.info('电台暂无新曲', batch.reason || '本轮推荐已用尽'); return; }
      if (mode === 'start' || allowReplay) {
        await this.playQueue(songs, 0, 'smart_radio');
      } else {
        const queue = state.get('playQueue') || [];
        const prefix = mode === 'rearrange' ? queue.slice(0, state.get('playQueueIndex') + 1) : queue;
        state.set({ playQueue: [...prefix, ...songs] });
        this.scheduleSavePlaybackSession(true);
      }
      if (opSeq === this.radioOpSeq) state.set({ smartRadioSongs: songs });
    });
    const slot = manual ? 'radioManualRequest' : 'radioRefillRequest';
    this[slot] = work;
    try { return await work; }
    catch {
      return false;
    }
    finally {
      if (!manual && !accepted && !session.reclaimedIds.has(payload.request_id)) {
        session.supersededRequestIds.add(payload.request_id);
      }
      if (this[slot] === work) this[slot] = null;
      if (!manual && session.refillId === payload.request_id) session.refillId = null;
    }
  }

  refillSmartRadioQueue() {
    if (!state.get('radioExhausted')) return this.requestRadio('refill');
  }

  isPositiveNumericSongId(id) {
    return isPositiveSongId(id);
  }

  scheduleEndTransition(duration, currentTime) {
    clearTimeout(this.endTransitionTimer);
    const fadeMs = Number(state.get('settings')?.crossfade_ms ?? 150);
    const remaining = (duration - currentTime) * 1000 / (this.audio.playbackRate || 1);
    if (!fadeMs || !this.activeUsesWebAudio || remaining <= 0 || remaining > 1200 || this.audio.paused
        || this.isBuffering || this.isChorusLooping || this.sleepMode === 'track' || this.currentSong?._origin === 'deck') return;
    const seq = this.requestSequence;
    this.endTransitionTimer = setTimeout(() => {
      if (seq !== this.requestSequence || this.audio.paused || this.isBuffering || this.audio.seeking
          || this.sleepMode === 'track' || this.isChorusLooping || state.get('playMode') === 'single'
          || state.get('settings')?.crossfade_ms === 0 || this.audio.duration - this.audio.currentTime > fadeMs/1000*this.audio.playbackRate + .08) return;
      const queue = state.get('playQueue') || [];
      const next = queue[this.getNextQueueIndex(false, true)];
      const cached = next && this.prefetchedAudio.get(getTrackKey(next));
      if (cached && Date.now() - cached.fetchedAt < PREFETCH_TTL_MS && isSameOriginStreamUrl(cached.data.url)) {
        if (this.advancingSequence === seq) return;
        this.advancingSequence = seq;
        this.maybeRecordGenuineListen(true);
        this.playNext(false).finally(() => {
          if (this.advancingSequence === seq) this.advancingSequence = null;
        });
      }
    }, Math.max(0, remaining - fadeMs));
  }

  resolveTrackAudio(song, {forceRefresh = false, candidateId = null} = {}) {
    if (song.local_path) return api.getLocalStream(song.local_path);
    if (song.provider && song.provider !== 'netease' && (song.external_id || song.id)) {
      return api.getAudio(0, {
        provider: song.provider,
        external_id: song.external_id || String(song.id),
        title: song.title || '',
        artist: formatArtists(song.artists),
        duration: song.duration || 0,
        stream_url_hint: song.stream_url_hint || song.url || '',
        force_refresh: forceRefresh,
      });
    }
    const options = {};
    if (forceRefresh) options.force_refresh = true;
    if (candidateId) options.candidate_id = candidateId;
    if (song.title) options.title = song.title;
    const artistHint = Array.isArray(song.artists) && song.artists.length > 0 ? song.artists.join(';') : '';
    if (artistHint) options.artist = artistHint;
    if (Number(song.duration) > 0) options.duration = Math.round(Number(song.duration));
    return api.getAudio(song.id, Object.keys(options).length ? options : null);
  }

  formatAudioResolveError(result) {
    const code = String(result?.error?.code || '').toUpperCase();
    const rawMsg = String(result?.error?.message || '').trim();
    if (code === 'AUDIO_UNAVAILABLE') {
      return '当前曲目暂无可用音源，可点击“切换音源版本”手动选择';
    }
    if (code === 'SONG_NOT_IN_CATALOG') {
      return '本地曲库中缺少该曲信息，请重试或切换音源版本';
    }
    if (code === 'UPSTREAM_RISK_CONTROL') {
      return rawMsg || '请求过于频繁，触发上游频控，请稍后再试';
    }
    if (code === 'UPSTREAM_UNAVAILABLE' || code === 'NETWORK_ERROR') {
      return '网络连接异常，无法获取音源，请检查网络后重试';
    }
    if (code === 'SESSION_CHANGED' || code === 'SESSION_EXPIRED' || code === 'NOT_AUTHENTICATED') {
      return rawMsg || '登录状态已变更，请重试或重新登录';
    }
    if (code === 'INTERNAL_ERROR') {
      return '音源解析出现内部异常，请重试或切换音源版本';
    }
    return rawMsg || '当前曲目暂无可用音源';
  }

  formatStreamFailureMessage(errorCode = 0, refreshErrorMsg = '') {
    if (refreshErrorMsg) {
      return `音频流加载失败：${refreshErrorMsg}`;
    }
    if (errorCode === 2) {
      return '音频流加载失败：网络连接中断或上游代理异常，请点击播放重试';
    }
    if (errorCode === 3) {
      return '音频流加载失败：音频解码出错，请尝试切换音源版本';
    }
    if (errorCode === 4) {
      return '音频流加载失败：音频格式不受支持或链接已失效，请切换音源版本';
    }
    return '音频流加载失败，可能已失效，请点击播放重试或切换音源版本';
  }

  async prefetchNextTrackMetadata() {
    const queue = state.get('playQueue') || [];
    if (queue.length <= 1) return;
    const nextIdx = this.getNextQueueIndex(false, true);
    if (nextIdx < 0 || nextIdx === state.get('playQueueIndex')) return;
    const nextSong = queue[nextIdx];
    if (!nextSong) return;

    const key = getTrackKey(nextSong);
    const seq = this.requestSequence;
    const cached = this.prefetchedAudio.get(key);
    if (cached && (Date.now() - cached.fetchedAt) < PREFETCH_TTL_MS) return;

    try {
      const res = await this.resolveTrackAudio(nextSong);
      if (seq !== this.requestSequence) return;
      if (res.ok && res.data && isPlayableAudioUrl(res.data.url)) {
        this.prefetchedAudio.set(key, { data: res.data, fetchedAt: Date.now() });
        if (!this.fade && this.activeUsesWebAudio && Number(state.get('settings')?.crossfade_ms) > 0
            && isSameOriginStreamUrl(res.data.url)) this.standbyAudio.src = res.data.url;
        if (this.prefetchedAudio.size > MAX_PREFETCH_ENTRIES) {
          const oldest = this.prefetchedAudio.keys().next().value;
          this.prefetchedAudio.delete(oldest);
        }
      }
    } catch {}
  }

  consumePrefetchedAudio(key) {
    const cached = this.prefetchedAudio.get(key);
    if (!cached) return null;
    this.prefetchedAudio.delete(key);
    if (Date.now() - cached.fetchedAt > PREFETCH_TTL_MS) return null;
    return cached.data;
  }

  // --- Core Playback ---

  /**
   * Play a specific song object.
   * Fetches audio and lyrics in parallel; if audio resolves to a different audio_id,
   * fetches lyrics for that actual audio_id (falling back to song.id if empty).
   * @param {Song} song
   * @param {boolean} [autoStart=true]
   * @param {{ preserveQueue?: boolean, candidateId?: string, overrideAudioData?: Object, forceRefresh?: boolean }} [options]
   */
  async playSong(song, autoStart = true, options = {}) {
    this._pendingRetrySeekCleanup?.();
    if (!song) return;
    if (autoStart) {
      this.unlockAudioContextSync();
    }

    // Flush any pending genuine listen time for previous track and push onto playHistoryStack
    if (this.currentSong) {
      this.accumulateListenTime();
      this.maybeRecordGenuineListen(false, true);
      if (!options.fromHistory && getTrackKey(song) !== getTrackKey(this.currentSong)) {
        this.playHistoryStack.push({ ...this.currentSong });
        if (this.playHistoryStack.length > 100) {
          this.playHistoryStack.shift();
        }
        state.set({ playHistoryStack: [...this.playHistoryStack] });
      }
    }

    this.playbackEventScope = state.writeScope();
    this.currentPlaybackEventId = `ev_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
    const normalizedResumeSeek = (options.resumeSeekSec !== null && options.resumeSeekSec !== undefined && Number(options.resumeSeekSec) > 0)
      ? Number(options.resumeSeekSec)
      : null;
    this.pendingResumeSeekSec = normalizedResumeSeek;
    this.resumeAutoplaySequence = null;
    this.isBuffering = false;
    state.set({ resumableSession: null });
    const requestId = ++this.requestSequence;
    if (autoStart) {
      this.userPausedSequence = -1;
    }
    this.retriedCurrentTrack = false;
    this.prefetchedForCurrentTrack = false;
    this.lastSavedPosition = normalizedResumeSeek || 0;
    this.listenAccumMs = 0;
    this.listenLastTickMs = 0;

    this.clearAudioCtxWatchdog();
    this.finishFade();
    if (autoStart && !normalizedResumeSeek && this.activeUsesWebAudio && !this.audio.paused
        && isSameOriginStreamUrl(this.activeSrc) && Number(state.get('settings')?.crossfade_ms ?? 150) > 0) {
      this.reserveOutgoing();
    }
    this.activeSrc = '';
    this.resetMediaElement(this.directAudio);
    if (!this.fade) {
      this.resetMediaElement(this.streamAudio);
      this.resetMediaElement(this.standbyAudio);
    }
    lyricsManager.setPlaybackState(false);
    lyricsManager.loadLyrics('', '', { songId: song.id });
    this.updateInlineLyricUI(normalizedResumeSeek || 0);

    this.currentSong = { ...song, _origin: options.origin || 'queue' };
    state.set({
      currentPlayingSong: this.currentSong,
      playbackError: null,
      audioMeta: null,
      currentTime: normalizedResumeSeek || 0,
      duration: normalizeTimeToSeconds(song.duration, 0),
    });

    // Keep playQueue coherent when single tracks are played directly
    if (!options.preserveQueue) {
      state.set({ playQueueOrigin: options.origin || '播放队列' });
      const queue = state.get('playQueue') || [];
      const existingIdx = queue.findIndex(item => getTrackKey(item) === getTrackKey(song));
      if (existingIdx >= 0) {
        state.set({ playQueueIndex: existingIdx });
      } else {
        state.set({ playQueue: [this.currentSong], playQueueIndex: 0 });
      }
    }

    this.isLoadingTrack = true;
    this.setPlayButtonLoadingUI(true);
    this.scheduleSavePlaybackSession(true);
    this.updateTrackInfoUI(this.currentSong);
    this.updateHeartUI(this.currentSong);
    this.updateChorusUI(this.currentSong, this.getNormalizedDuration());
    this.updateMediaSessionMetadata(this.currentSong);
    this.applyCoverPalette(this.currentSong);
    this.pushDesktopLyricsState(true);

    // Prepare audio + lyrics parallel requests:
    // 1. Local indexed file -> POST /api/local-library/stream
    // 2. Direct cross-channel track (provider + external_id without NetEase song_id) -> GET /api/audio?id=0&provider=...
    // 3. Standard NetEase song -> GET /api/audio?id=...
    const isLocalFile = Boolean(song.local_path);
    const hasNeteaseId = (!song.provider || song.provider === 'netease') && this.isPositiveNumericSongId(song.id);
    const prefetched = !options.forceRefresh && !options.candidateId && !options.overrideAudioData
      ? this.consumePrefetchedAudio(getTrackKey(song)) : null;
    const known = options.overrideAudioData || prefetched;
    const audioPromise = known && isPlayableAudioUrl(known.url)
      ? Promise.resolve({ ok: true, data: known }) : this.resolveTrackAudio(song, options);

    const lyricsPromise = (!isLocalFile && hasNeteaseId)
      ? api.getLyrics(song.id)
      : ((!isLocalFile && song.provider && song.provider !== 'netease')
        ? api.getLyrics(0, null, {
            provider: song.provider,
            external_id: song.external_id || '',
            track_key: getTrackKey(song),
            title: song.title || '',
            artist: formatArtists(song.artists),
          })
        : Promise.resolve({ ok: false, data: null, error: null }));

    let alternateLyricsApplied = false;
    lyricsPromise.then(res => {
      if (requestId !== this.requestSequence || alternateLyricsApplied || !res?.ok || !res.data) return;
      this.mergeChorusMetadata(res.data);
      this.applyLyricsPayload(res.data);
    }).catch(() => {});

    let resolvedAudioData = null;
    let audioErrorMsg = '';
    this.isLoadingTrack = true;
    try {
      let result = await audioPromise;
      if (requestId === this.requestSequence && !result?.ok
          && (result?.status === 409 || result?.error?.code === 'SESSION_CHANGED')) {
        // Transparent single retry when startup status hydration bumps session_generation mid-flight.
        result = await this.resolveTrackAudio(song, options);
      }
      if (result?.ok) resolvedAudioData = result.data;
      else audioErrorMsg = this.formatAudioResolveError(result);
    } catch {
      audioErrorMsg = '音频接口请求失败，请检查网络连接后重试';
    } finally {
      if (requestId === this.requestSequence) {
        this.isLoadingTrack = false;
        this.setPlayButtonLoadingUI(false);
      }
    }
    if (requestId !== this.requestSequence) return;

    const audioUrl = resolvedAudioData?.url || resolvedAudioData?.stream_url;
    if (resolvedAudioData && isPlayableAudioUrl(audioUrl)) {
      state.recordRecentPlayed(this.currentSong);
      this.mergeChorusMetadata(resolvedAudioData);
      const isAlt = Boolean(
        resolvedAudioData.is_alternative
        || resolvedAudioData.source === 'alternative'
        || resolvedAudioData.source === 'external'
      );
      this.currentLoudness = resolvedAudioData.loudness || null;
      if (resolvedAudioData.cover_url && !this.currentSong.cover_url) {
        this.currentSong = { ...this.currentSong, cover_url: resolvedAudioData.cover_url };
        this.updateMediaSessionMetadata(this.currentSong);
      }
      state.set({
        audioSourceType: resolvedAudioData.source || (isAlt ? 'alternative' : 'original'),
        alternativeInfo: resolvedAudioData.alternative_info || null,
        audioMeta: resolvedAudioData,
        playbackError: null,
      });
      this.applyWebAudioSettings();
      this.updateSourceBadgeUI(resolvedAudioData);
      this.updateChorusUI(this.currentSong, this.getNormalizedDuration());

      // Safely route ONLY same-origin /api/stream/ URLs through Web Audio; external CDN plays natively
      this.prepareAudioElementForUrl(audioUrl);
      this.activeSrc = audioUrl;
      const waitForResume = this.pendingResumeSeekSec !== null && this.pendingResumeSeekSec > 0;
      this.resumeAutoplaySequence = (autoStart && waitForResume && this.userPausedSequence !== requestId) ? requestId : null;
      if (!this.isHealthyMediaElement(this.audio)) {
        this.resetMediaElement(this.audio);
      }
      if (this.audio.getAttribute('src') !== audioUrl) this.audio.src = audioUrl;
      this.audio.playbackRate = this.playbackRate;
      if (waitForResume) {
        if (this.userPausedSequence === requestId) {
          this.pauseAllAudio({ markUserPause: false });
          return;
        }
        if ((this.audio.readyState ?? 0) >= 1) {
          this.applyPendingResumeSeek(this.audio, requestId);
        }
      } else if (autoStart) {
        if (this.userPausedSequence === requestId) {
          this.pauseAllAudio({ markUserPause: false });
          return;
        }
        this.startElementPlayback(this.audio, requestId, audioUrl);
      }

      // If audio resolved to a different audio_id than song.id, fetch matching lyrics for actual audio_id
      const actualAudioId = resolvedAudioData.audio_id
        ?? resolvedAudioData.alternative_info?.id
        ?? resolvedAudioData.id
        ?? null;
      if (
        !isLocalFile
        && hasNeteaseId
        && this.isPositiveNumericSongId(actualAudioId)
        && String(actualAudioId) !== String(song.id)
      ) {
        try {
          const altLyricsRes = await api.getLyrics(song.id, actualAudioId);
          if (requestId !== this.requestSequence) return;
          if (altLyricsRes.ok && altLyricsRes.data && (altLyricsRes.data.yrc || altLyricsRes.data.lyrics)) {
            alternateLyricsApplied = true;
            this.mergeChorusMetadata(altLyricsRes.data);
            this.applyLyricsPayload(altLyricsRes.data);
            this.updateChorusUI(this.currentSong, this.getNormalizedDuration());
          }
        } catch {}
      }
    } else {
      this.finishFade();
      this.activeSrc = '';
      this.resetMediaElement(this.audio);
      state.set({isPlaying: false});
      state.set({
        audioMeta: null,
        playbackError: audioErrorMsg || '当前曲目无可播放音源',
      });
      this.updatePlayButtonUI(false);
      this.updateSourceBadgeUI(null, audioErrorMsg || '无可用音源');
      toast.warning('无法播放此曲', audioErrorMsg || '未找到可用音源，可点击“切换音源版本”手动选择。');
    }
  }

  mergeChorusMetadata(payload) {
    if (!payload || !this.currentSong) return;
    if (payload.chorus_start !== undefined && payload.chorus_start !== null && !this.currentSong.chorus_start) {
      this.currentSong.chorus_start = payload.chorus_start;
    }
    if (payload.chorus_end !== undefined && payload.chorus_end !== null && !this.currentSong.chorus_end) {
      this.currentSong.chorus_end = payload.chorus_end;
    }
  }

  applyLyricsPayload(data) {
    if (!data) {
      lyricsManager.loadLyrics('', '', { songId: this.currentSong?.id });
      return;
    }
    lyricsManager.loadLyrics(data.lyrics || '', data.tlyrics || '', {
      yrc: data.yrc || '',
      romalrc: data.romalrc || '',
      ttml_source: data.ttml_source || '',
      songId: this.currentSong?.id,
    });
    const liveTime = (this.audio && Number.isFinite(this.audio.currentTime) && this.audio.currentTime > 0)
      ? this.audio.currentTime
      : (state.get('currentTime') || 0);
    if (state.get('isStageOpen')) {
      lyricsManager.syncStageToTime(liveTime, { instantScroll: true });
    } else if (liveTime > 0) {
      lyricsManager.updateTime(liveTime);
    }
    this.updateInlineLyricUI(liveTime);
    this.pushDesktopLyricsState(true);
  }

  /**
   * Single transparent retry on expired CDN URL while preserving playback position.
   * Single-flight guarded and verifies activeSrc + requestSequence before acting.
   */
  async handleAudioElementError() {
    if (this._retryingAudioForSeq === this.requestSequence || this.isLoadingTrack || !this.currentSong) return;
    const attrSrc = typeof this.audio?.getAttribute === 'function' ? this.audio.getAttribute('src') : this.audio?.src;
    if (!attrSrc || !this.activeSrc || attrSrc !== this.activeSrc) return;

    const song = this.currentSong;
    const reqId = this.requestSequence;
    this._retryingAudioForSeq = reqId;
    const errorElement = this.audio;
    const mediaErrorCode = Number(errorElement?.error?.code || 0);
    const resumeAt = (this.pendingResumeSeekSec !== null && this.pendingResumeSeekSec > 0)
      ? this.pendingResumeSeekSec
      : (this.lastSavedPosition || this.audio.currentTime || 0);
    let refreshErrorMsg = '';

    try {
      if (!this.retriedCurrentTrack) {
        this.retriedCurrentTrack = true;

        try {
          const refreshed = await this.resolveTrackAudio(song, { forceRefresh: true });
          if (reqId !== this.requestSequence || this.audio !== errorElement) return;
          const freshUrl = refreshed?.data?.url || refreshed?.data?.stream_url;
          if (refreshed?.ok && isPlayableAudioUrl(freshUrl)) {
            this.pendingResumeSeekSec = null;
            this.prepareAudioElementForUrl(freshUrl);
            this.activeSrc = freshUrl;
            this.audio.src = freshUrl;
            this.audio.playbackRate = this.playbackRate;
            const activeEl = this.audio;
            const onMeta = () => {
              cleanup();
              if (reqId !== this.requestSequence || this.audio !== activeEl || this.activeSrc !== freshUrl
                  || (activeEl.getAttribute?.('src') ?? activeEl.src) !== freshUrl) return;
              if (resumeAt > 0 && Number.isFinite(activeEl.duration) && activeEl.duration > 0) {
                const clamped = resumeAt >= Math.max(0, activeEl.duration - 1.5) ? 0 : Math.min(resumeAt, Math.max(0, activeEl.duration - 0.5));
                if (clamped > 0) {
                  try { activeEl.currentTime = clamped; } catch {}
                }
              }
            };
            const cleanup = () => {
              activeEl.removeEventListener('loadedmetadata', onMeta);
              if (this._pendingRetrySeekCleanup === cleanup) this._pendingRetrySeekCleanup = null;
            };
            this._pendingRetrySeekCleanup?.();
            this._pendingRetrySeekCleanup = cleanup;
            activeEl.addEventListener('loadedmetadata', onMeta);
            if ((activeEl.readyState ?? 0) >= 1) onMeta();
            state.set({ audioMeta: refreshed.data, playbackError: null });
            this.updateSourceBadgeUI(refreshed.data);
            if (this.userPausedSequence === reqId) {
              this.pauseAllAudio({ markUserPause: false });
              return;
            }
            this.startElementPlayback(activeEl, reqId, freshUrl);
            return;
          }
          if (refreshed && !refreshed.ok && refreshed.error?.message) {
            refreshErrorMsg = this.formatAudioResolveError(refreshed);
          }
        } catch {}
      }

      if (reqId !== this.requestSequence || this.audio !== errorElement) return;
      this.clearAudioCtxWatchdog();
      this.finishFade();
      this.resetMediaElement(this.audio);
      this.activeSrc = '';
      this.resumeAutoplaySequence = null;
      this.pendingResumeSeekSec = resumeAt > 0 ? resumeAt : null;
      const errMessage = this.formatStreamFailureMessage(mediaErrorCode, refreshErrorMsg);
      state.set({ isPlaying: false, playbackError: errMessage });
      this.updatePlayButtonUI(false);
      this.updateSourceBadgeUI(state.get('audioMeta'), '播放失败');
      toast.error('播放出错', `"${song.title || '曲目'}" ${errMessage}`);
    } finally {
      if (this._retryingAudioForSeq === reqId) this._retryingAudioForSeq = null;
    }
  }

  /**
   * Real cover color extraction (64x64 same-origin /api/cover -> 32x32 offscreen canvas -> 4 quadrants).
   * Updates --cover-accent, --cover-accent-subtle, --cover-accent-2, --cover-accent-2-subtle,
   * and state.coverPaletteColors (4 RGB triplets for AMLL fluid mesh canvas).
   * Never overrides Keep (#62a368) or Prune (#df584c).
   */
  applyCoverPalette(song) {
    const seq = ++this.paletteSequence;
    const seed = `${song.id}_${song.title}_${song.album || ''}`;
    const immediate = getFallbackPalette(seed);
    this.applyPaletteCssVars(immediate);

    extractCoverPalette(song.cover_url, seed).then((palette) => {
      if (seq !== this.paletteSequence || !palette) return;
      this.applyPaletteCssVars(palette);
    }).catch(() => {});
  }

  applyPaletteCssVars(palette) {
    if (!palette) return;
    state.set({
      coverAccent: palette.accent,
      coverAccent2: palette.accent2,
      coverPaletteColors: Array.isArray(palette.colors) ? palette.colors : null,
    });
    if (typeof document !== 'undefined' && document.documentElement) {
      const rootStyle = document.documentElement.style;
      rootStyle.setProperty('--cover-accent', palette.accent);
      rootStyle.setProperty('--cover-accent-subtle', palette.accentSubtle);
      rootStyle.setProperty('--cover-accent-2', palette.accent2);
      rootStyle.setProperty('--cover-accent-2-subtle', palette.accent2Subtle);
    }
  }

  /**
   * Clean stop and full state reset on auth:logout.
   */
  stopAndReset() {
    this.showAudioBypass(false);
    this.clearAudioCtxWatchdog();
    this.finishFade();
    this.resetMediaElement(this.standbyAudio);
    this.restoreGeneration = (this.restoreGeneration || 0) + 1;
    this.sessionOwner = null;
    this.queuedSession = null;
    this.currentPlaybackEventId = null;
    this.isBuffering = false;
    this.setSleepTimer('off');
    this.pendingResumeSeekSec = null;
    this.resumeAutoplaySequence = null;
    state.set({ resumableSession: null });
    this._pendingRetrySeekCleanup?.();
    this.requestSequence += 1;
    this.paletteSequence += 1;
    this.isLoadingTrack = false;
    this.setPlayButtonLoadingUI(false);
    this.userPausedSequence = -1;
    this.activeSrc = '';
    this._retryingAudioForSeq = null;

    this.resetMediaElement(this.directAudio);
    this.resetMediaElement(this.streamAudio);
    if (this.audioCtx?.state === 'running') {
      try {
        this.audioCtx.suspend?.().catch?.(() => {});
      } catch {}
    }
    this.audio = this.directAudio;
    this.activeUsesWebAudio = false;
    this.currentSong = null;
    this.isChorusLooping = false;
    this.prefetchedAudio.clear();
    this.shuffleBag = [];
    this.shuffleBagCursor = 0;
    this.shuffleQueueSig = '';
    this.playHistoryStack = [];
    this.explicitNextIndices = [];
    this.listenAccumMs = 0;
    this.listenLastTickMs = 0;
    lyricsManager.setPlaybackState(false);
    lyricsManager.loadLyrics('');
    this.updateInlineLyricUI(0);

    state.set({
      currentPlayingSong: null,
      isPlaying: false,
      currentTime: 0,
      duration: 0,
      playQueueOrigin: '', playQueue: [],
      playQueueIndex: -1,
      playHistoryStack: [],
      audioMeta: null,
      playbackError: null,
      isStageOpen: false,
    });

    this.updateTrackInfoUI(null);
    this.updatePlayButtonUI(false);
    this.updateSourceBadgeUI(null);
    this.updateTimelineUI(0, 0);
    this.updateChorusUI(null, 0);
    this.updateInlineLyricUI(0);
    this.updateMediaSessionMetadata(null);
  }

  togglePlay(forceState = null) {
    const isElActivelyPlaying = (el) => Boolean(el && !el.paused && !el.error);
    const isAnyPlaying = isElActivelyPlaying(this.directAudio) || isElActivelyPlaying(this.streamAudio) || Boolean(state.get('isPlaying'));
    if (forceState === false || (forceState === null && isAnyPlaying)) {
      this.pauseAllAudio({ markUserPause: true });
      return;
    }

    this.unlockAudioContextSync();

    if (!this.hasActiveMediaSrc() || !this.activeSrc) {
      if (this.isLoadingTrack) {
        if (forceState === true) {
          this.userPausedSequence = -1;
          return;
        }
        this.pauseAllAudio({ markUserPause: true });
        return;
      }
      if (this.audio?.error || this.audio?.networkState === 3) {
        this.resetMediaElement(this.audio);
        this.activeSrc = '';
      }
      const current = state.get('currentPlayingSong')
        || this.currentSong
        || (state.get('playQueue') || [])[state.get('playQueueIndex') || 0]
        || (state.get('deckQueue') || [])[state.get('deckIndex') || 0];
      if (current) {
        const hasQueue = (state.get('playQueue') || []).length > 0;
        const resumeSeekSec = (this.pendingResumeSeekSec !== null && this.pendingResumeSeekSec > 0)
          ? this.pendingResumeSeekSec
          : (this.lastSavedPosition > 0 ? this.lastSavedPosition : null);
        return this.playSong(current, true, {
          preserveQueue: Boolean(state.get('resumableSession')) || hasQueue,
          resumeSeekSec,
        });
      }
      return;
    }

    this.userPausedSequence = -1;
    const otherEl = this.audio === this.streamAudio ? this.directAudio : this.streamAudio;
    try {
      otherEl.pause();
    } catch {}
    const seq = this.requestSequence;
    const url = this.activeSrc;
    if (this.pendingResumeSeekSec !== null && this.pendingResumeSeekSec > 0) {
      this.resumeAutoplaySequence = seq;
      if ((this.audio.readyState ?? 0) >= 1) {
        this.applyPendingResumeSeek(this.audio, seq);
      }
      return;
    }
    this.startElementPlayback(this.audio, seq, url);
  }

  seek(seconds) {
    if (!this.currentSong) return;
    if (!Number.isNaN(seconds) && Number.isFinite(seconds)) {
      const dur = this.getNormalizedDuration() || 9999;
      this.audio.currentTime = Math.max(0, Math.min(seconds, dur));
      this.updateTimelineUI(this.audio.currentTime, this.getNormalizedDuration());
      lyricsManager.updateTime(this.audio.currentTime);
      this.updateInlineLyricUI(this.audio.currentTime);
      this.updateMediaSessionPosition();
    }
  }

  seekDelta(deltaSeconds) {
    const target = (this.audio.currentTime || 0) + deltaSeconds;
    this.seek(target);
  }

  setVolume(volumeFraction, muted = Number(volumeFraction) === 0, { showOsd = false } = {}) {
    const clamped = Math.max(0, Math.min(1, Number(volumeFraction) || 0));
    this.targetVolume = clamped;
    this.muted = Boolean(muted);
    const effective = this.muted ? 0 : clamped;
    const gainValue = this.muted ? 0 : volumeToGain(clamped);
    if (clamped > 0) this.previousVolume = clamped;
    this.directAudio.volume = gainValue;
    if (this.activeUsesWebAudio && this.gainNode && this.audioCtx) {
      try {
        this.gainNode.gain.setTargetAtTime(gainValue, this.audioCtx.currentTime, 0.02);
      } catch {}
    } else if (!this.activeUsesWebAudio && this.audio && this.audio !== this.directAudio) {
      this.audio.volume = gainValue;
    }
    const pct = Math.round(clamped * 100);
    const visualPct = this.muted ? 0 : pct;
    if (typeof document !== 'undefined') {
      for (const sliderId of ['player-volume-slider', 'stage-volume-slider']) {
        const slider = document.getElementById(sliderId);
        if (slider) {
          slider.value = String(pct);
          slider.style?.setProperty?.('--vol-pct', `${visualPct}%`);
        }
      }
      for (const pctId of ['player-volume-pct', 'stage-volume-pct']) {
        const pctEl = document.getElementById(pctId);
        if (pctEl) pctEl.textContent = this.muted ? '静音' : `${pct}%`;
      }
    }
    this.updateVolumeIconUI();
    if (showOsd) {
      this.pushDesktopLyricsState(true, {
        text: this.muted ? '已静音' : `音量 ${pct}%`,
        progress: effective,
        kind: 'volume',
      });
    } else {
      this.pushDesktopLyricsState(true);
    }
    this.scheduleSavePlaybackSession(true);
  }

  adjustVolume(deltaFraction, { showOsd = true } = {}) {
    const base = this.muted && deltaFraction > 0 ? 0 : this.targetVolume;
    const next = Math.max(0, Math.min(1, Math.round((base + Number(deltaFraction || 0)) * 100) / 100));
    this.setVolume(next, next <= 0, { showOsd });
    return next;
  }

  toggleMute({ showOsd = false } = {}) {
    if (this.muted || this.targetVolume === 0) {
      this.setVolume(this.targetVolume || this.previousVolume || 0.8, false, { showOsd });
    } else {
      this.setVolume(this.targetVolume, true, { showOsd });
    }
  }

  // --- Chorus Jump (ms -> sec Normalized) ---

  jumpToChorus() {
    const song = this.currentSong;
    if (!song) return;

    if (!this.activeSrc) {
      const origin = song._origin || this.currentSong?._origin;
      this.playSong(song, true, { preserveQueue: true, ...(origin ? { origin } : {}) }).then(() => {
        if (this.activeSrc && this.currentSong && getTrackKey(this.currentSong) === getTrackKey(song)) this.performChorusJump(this.currentSong);
      });
      return;
    }
    this.performChorusJump(song);
  }

  performChorusJump(song) {
    if (!song) return;
    const duration = this.getNormalizedDuration() || normalizeTimeToSeconds(song.duration, 0) || 180;
    const startSec = normalizeTimeToSeconds(song.chorus_start, duration);

    if (startSec > 0 && startSec < duration) {
      this.seek(startSec);
      if (this.audio.paused) {
        this.audio.play().catch(console.warn);
      }
      toast.info('跳转副歌', `已定位至副歌 (${formatSeconds(startSec)})`);
    } else {
      const estimate = Math.floor(duration * 0.35);
      this.seek(estimate);
      if (this.audio.paused) {
        this.audio.play().catch(console.warn);
      }
      toast.info('跳至约 35%', `该曲无副歌标记，已定位至约 35% 处 (${formatSeconds(estimate)})`);
    }
  }

  toggleChorusLoop() {
    this.isChorusLooping = !this.isChorusLooping;
    state.set({ isChorusActive: this.isChorusLooping });
    if (typeof document !== 'undefined') {
      const btn = document.getElementById('player-chorus-btn');
      if (btn) btn.classList.toggle('active', this.isChorusLooping);
    }
    if (this.isChorusLooping) {
      this.jumpToChorus();
    }
  }

  // --- Heart / Like, Audio Candidates Picker & Similar Songs ---

  async toggleLikeCurrentSong(targetSong = null) {
    const song = targetSong || this.currentSong;
    if (!song) return;
    if (!writableNeteaseTrack(song)) {
      const tkey = getTrackKey(song);
      if (!tkey) return;
      const currentlyLiked = state.isSongLiked(song);
      const nextLiked = !currentlyLiked;
      state.setSongLiked(song, nextLiked);
      this.updateHeartUI(this.currentSong);
      try {
        const res = await api.setFavorite?.(tkey, nextLiked, song);
        if (!res?.ok) {
          state.setSongLiked(song, currentlyLiked);
          this.updateHeartUI(this.currentSong);
          toast.error('收藏更新失败', res?.error?.message || '未能确认本地收藏状态。');
          return;
        }
        toast.success(nextLiked ? '已加入本地收藏' : '已取消本地收藏', `"${song.title || '曲目'}"`);
        if (nextLiked) this.triggerHeartPopUI();
      } catch {
        state.setSongLiked(song, currentlyLiked);
        this.updateHeartUI(this.currentSong);
        toast.warning('收藏未确认', '请刷新后检查收藏状态。');
      }
      return;
    }
    if (!song.id) return;
    if (!state.get('authenticated') || state.get('offlineMode')) {
      toast.warning('需要登录', '登录后可收藏或取消红心歌曲。');
      return;
    }
    if (this.heartWritePending || state.get('hasUnknownResult')) {
      toast.warning('状态待核对', '请等待当前操作完成或核对待处理记录。');
      return;
    }
    const generation = state.get('sessionGeneration');
    const currentlyLiked = state.isSongLiked(song.id);
    const nextLiked = !currentlyLiked;
    state.setSongLiked(song.id, nextLiked);
    this.updateHeartUI(this.currentSong);

    this.heartWritePending = true;
    let res;
    try {
      res = await api.likeTrack(song.id, nextLiked);
      if (generation !== state.get('sessionGeneration')) return;
      const data = res.data;
      const confirmed = Array.isArray(data?.confirmed_ids) && data.confirmed_ids.map(String).includes(String(song.id));
      if (!res.ok || !confirmed || data?.unknown_items?.length || data?.pending_operation_ids?.length || data?.failed_items?.length) {
        state.setSongLiked(song.id, currentlyLiked);
        this.updateHeartUI(this.currentSong);
        if (data?.unknown_items?.length || data?.pending_operation_ids?.length) {
          const pendingIds = data.pending_operation_ids?.length ? data.pending_operation_ids : [data.operation_id];
          state.restorePendingOperations(pendingIds.map(operation_id => ({
            operation_id, action: nextLiked ? 'like' : 'unlike', target: { song_ids: [song.id] },
          })));
          toast.warning('状态待核对', data.message || '远端尚未确认红心状态，已保留待核对。');
          const pending = await api.getPendingOperations();
          if (generation === state.get('sessionGeneration') && pending.ok && Array.isArray(pending.data?.operations)) {
            state.restorePendingOperations(pending.data.operations);
          }
        } else {
          toast.error('红心同步失败', res.error?.message || data?.failed_items?.[0]?.reason || '远端尚未确认红心状态。');
        }
        return;
      }
    } catch {
      if (generation === state.get('sessionGeneration')) {
        state.setSongLiked(song.id, currentlyLiked);
        this.updateHeartUI(this.currentSong);
        toast.error('红心同步失败', '未能核对远端喜欢状态。');
      }
      return;
    } finally {
      this.heartWritePending = false;
    }
    toast.success(nextLiked ? '已喜欢' : '已取消红心', `"${song.title || '曲目'}"`);
    if (nextLiked) {
      this.triggerHeartPopUI();
      api.submitFeedback(song.id, 'like_explicit', 'player').catch(() => {});
    }
  }

  async openAudioCandidatesModal(targetSong = null, triggerElement = null) {
    const song = targetSong || this.currentSong;
    if (!song?.id) {
      toast.info('未选择曲目', '请先播放一首歌曲再切换音源版本。');
      return;
    }
    if (!writableNeteaseTrack(song)) {
      toast.info('本地音频直播', '当前正在直接播放本地无损文件，无需切换云端音源版本。');
      return;
    }
    const listEl = document.getElementById('audio-candidates-list');
    const titleEl = document.getElementById('audio-candidates-title');
    if (titleEl) {
      titleEl.textContent = `切换音源版本 · ${song.title || '曲目'}`;
    }
    if (listEl) {
      listEl.textContent = '';
      listEl.appendChild(createElement('div', {
        className: 'empty-state',
        children: [
          createElement('div', { className: 'spinner' }),
          createElement('div', { className: 'empty-state-desc', text: '正在检索网易云、云盘、本地与外部音源候选...' }),
        ],
      }));
    }
    modal.open('#audio-candidates-modal', triggerElement);

    const res = await api.getAudioCandidates(song.id);
    if (!listEl) return;
    listEl.textContent = '';

    const candidates = res.ok && res.data
      ? (Array.isArray(res.data.candidates) ? res.data.candidates : (Array.isArray(res.data) ? res.data : []))
      : [];

    if (!res.ok || candidates.length === 0) {
      listEl.appendChild(createElement('div', {
        className: 'empty-state',
        children: [
          createElement('div', { className: 'empty-state-title', text: '未找到其他可用音源' }),
          createElement('div', { className: 'empty-state-desc', text: res.error?.message || '可在“设置与同步 - 偏好设置”中启用外部音源或扫描本地曲库。' }),
        ],
      }));
      return;
    }

    candidates.forEach(cand => {
      const badge = formatHonestAudioBadge(cand);
      const useBtn = createElement('button', {
        className: 'btn btn-primary btn-sm',
        text: '播放此版本',
        events: {
          click: async () => {
            modal.close('#audio-candidates-modal');
            const preservedOrigin = this.currentSong && getTrackKey(song) === getTrackKey(this.currentSong)
              ? this.currentSong._origin
              : song._origin;
            const playOpts = {
              preserveQueue: true,
              ...(preservedOrigin ? { origin: preservedOrigin } : {}),
            };
            const candUrl = cand.url || cand.stream_url;
            if (isPlayableAudioUrl(candUrl)) {
              await this.playSong(song, true, { ...playOpts, overrideAudioData: { ...cand, url: candUrl } });
            } else if (cand.candidate_id || cand.id) {
              await this.playSong(song, true, { ...playOpts, candidateId: String(cand.candidate_id || cand.id) });
            }
          },
        },
      });

      const metaLine = [
        formatArtists(cand.artists || cand.artist || song.artists),
        cand.album || '',
        cand.duration || cand.duration_ms ? formatDuration(cand) : '',
      ].filter(Boolean).join(' · ');

      const row = createElement('div', {
        className: 'candidate-item-row',
        children: [
          createElement('div', {
            className: 'candidate-item-info',
            children: [
              createElement('div', { className: 'candidate-item-title', text: cand.title || song.title || '候选音源' }),
              createElement('div', { className: 'candidate-item-meta', text: metaLine }),
              badge ? createElement('span', { className: `badge badge-source-${badge.kind}`, text: badge.text }) : null,
            ],
          }),
          useBtn,
        ],
      });
      listEl.appendChild(row);
    });
  }

  async openSimilarSongsModal(targetSong = null, triggerElement = null) {
    const song = targetSong || this.currentSong;
    if (!song?.id) {
      toast.info('未选择曲目', '请先播放一首歌曲以发现相似歌曲。');
      return;
    }
    if (!getTrackKey(song).startsWith('netease:') || !this.isPositiveNumericSongId(song.id)) {
      toast.info('本地音频', '本地音乐文件暂不支持网易云相似歌曲推荐。');
      return;
    }
    const listEl = document.getElementById('similar-songs-list');
    const titleEl = document.getElementById('similar-songs-title');
    if (titleEl) {
      titleEl.textContent = `相似歌曲 · ${song.title || '曲目'}`;
    }
    if (listEl) {
      listEl.textContent = '';
      listEl.appendChild(createElement('div', {
        className: 'empty-state',
        children: [
          createElement('div', { className: 'spinner' }),
          createElement('div', { className: 'empty-state-desc', text: '正在加载相似歌曲...' }),
        ],
      }));
    }
    modal.open('#similar-songs-modal', triggerElement);

    const res = await api.getSimilarSongs(song.id);
    if (!listEl) return;
    listEl.textContent = '';

    const songs = res.ok && res.data
      ? (Array.isArray(res.data.songs) ? res.data.songs : (Array.isArray(res.data) ? res.data : []))
      : [];

    if (!res.ok || songs.length === 0) {
      listEl.appendChild(createElement('div', {
        className: 'empty-state',
        children: [
          createElement('div', { className: 'empty-state-title', text: '暂无相似歌曲推荐' }),
          createElement('div', { className: 'empty-state-desc', text: res.data?.reason || res.error?.message || '当前网络或接口未返回相似结果。' }),
        ],
      }));
      return;
    }

    songs.forEach((simSong, idx) => {
      const row = createElement('div', {
        className: 'candidate-item-row',
        children: [
          createElement('div', {
            className: 'candidate-item-info',
            children: [
              createElement('div', { className: 'candidate-item-title', text: simSong.title || '未知曲目' }),
              createElement('div', {
                className: 'candidate-item-meta',
                text: `${formatArtists(simSong.artists)}${simSong.recommend_reason ? ` · ${simSong.recommend_reason}` : ''}`,
              }),
            ],
          }),
          createElement('div', {
            style: 'display: flex; gap: 6px;',
            children: [
              createElement('button', {
                className: 'btn btn-ghost btn-sm',
                text: '下一首',
                events: { click: () => this.enqueueNext(simSong) },
              }),
              createElement('button', {
                className: 'btn btn-primary btn-sm',
                text: '播放',
                events: {
                  click: () => {
                    modal.close('#similar-songs-modal');
                    this.playQueue(songs, idx, null, '相似歌曲');
                  },
                },
              }),
            ],
          }),
        ],
      });
      listEl.appendChild(row);
    });
  }

  // --- UI Updaters ---

  updateTrackInfoUI(song) {
    if (typeof document === 'undefined') return;
    const coverEl = document.getElementById('player-track-cover');
    const titleEl = document.getElementById('player-track-title');
    const artistEl = document.getElementById('player-track-artist');

    if (coverEl) {
      coverEl.src = song ? getSizedCoverUrl(song.cover_url, 96, '音') : getSizedCoverUrl('', 96, '音');
      coverEl.alt = song?.title || '封面';
    }
    if (titleEl) {
      titleEl.textContent = song?.title || '未选择曲目';
    }
    if (artistEl) {
      artistEl.textContent = song ? formatArtists(song.artists) : '就绪';
    }
  }

  updateHeartUI(song) {
    if (typeof document === 'undefined') return;
    const isLiked = song ? state.isSongLiked(song) : false;
    for (const id of ['player-heart-btn', 'stage-heart-btn']) {
      const btn = document.getElementById(id);
      if (!btn) continue;
      btn.classList.toggle('is-liked', isLiked);
      if (!isLiked) {
        btn.classList.remove('heart-pop');
      }
      btn.setAttribute('aria-pressed', isLiked ? 'true' : 'false');
      btn.title = isLiked ? '取消喜欢' : '红心喜欢';
      setButtonIcon(btn, isLiked ? 'heart-filled' : 'heart', '', { size: 15 });
    }
  }

  /**
   * Triggers heart-pop spring animation on confirmed like actions only.
   */
  triggerHeartPopUI() {
    if (typeof document === 'undefined') return;
    for (const id of ['player-heart-btn', 'stage-heart-btn']) {
      const btn = document.getElementById(id);
      if (!btn || !btn.classList?.contains?.('is-liked')) continue;
      btn.classList.remove('heart-pop');
      void btn.offsetWidth;
      btn.classList.add('heart-pop');
    }
  }

  /**
   * Synchronizes active playing row/card indicators across rendered views.
   */
  syncPlayingRowUI() {
    syncPlayingRowIndicators(state.get('currentPlayingSong'), Boolean(state.get('isPlaying')));
  }

  updateSourceBadgeUI(meta, errorLabel = '') {
    if (typeof document === 'undefined') return;
    const badge = formatHonestAudioBadge(meta);
    for (const id of ['player-source-badge', 'stage-source-badge']) {
      const badgeEl = document.getElementById(id);
      if (!badgeEl) continue;
      badgeEl.textContent = '';
      if (errorLabel) {
        badgeEl.className = 'badge badge-danger player-quality-badge';
        badgeEl.textContent = errorLabel;
        badgeEl.title = '点击切换音源版本';
      } else if (badge) {
        badgeEl.className = `badge badge-source-${badge.kind} player-quality-badge`;
        badgeEl.textContent = badge.text;
        badgeEl.title = badge.detail;
      } else {
        badgeEl.className = 'sr-only';
      }
    }
  }

  setPlayButtonLoadingUI(loading) {
    if (typeof document === 'undefined') return;
    for (const id of ['player-play-btn', 'deck-play-btn', 'stage-play-btn']) {
      const btn = document.getElementById(id);
      if (!btn) continue;
      btn.setAttribute?.('aria-busy', loading ? 'true' : 'false');
      if (loading) {
        btn.title = '正在加载音源…';
      }
    }
  }

  updatePlayButtonUI(isPlaying) {
    if (typeof document === 'undefined') return;
    const iconName = isPlaying ? 'pause' : 'play';
    const text = isPlaying ? '暂停' : '播放';

    const mainBtn = document.getElementById('player-play-btn');
    if (mainBtn) {
      setButtonIcon(mainBtn, iconName, '', { size: 16 });
      mainBtn.title = text;
      mainBtn.setAttribute('aria-label', text);
    }

    const deckPlayBtn = document.getElementById('deck-play-btn');
    if (deckPlayBtn) {
      setButtonIcon(deckPlayBtn, iconName, '', { size: 16 });
      deckPlayBtn.title = `${text} [P]`;
      deckPlayBtn.setAttribute('aria-label', text);
    }

    const stagePlayBtn = document.getElementById('stage-play-btn');
    if (stagePlayBtn) {
      setButtonIcon(stagePlayBtn, iconName, '', { size: 20 });
      stagePlayBtn.title = text;
      stagePlayBtn.setAttribute('aria-label', text);
    }
  }

  updatePlayModeUI() {
    if (typeof document === 'undefined') return;
    const mode = state.get('playMode') || 'order';
    const iconMap = {
      order: 'repeat',
      loop: 'repeat',
      single: 'repeat-1',
      shuffle: 'shuffle',
      fm: 'radio',
      intelligence: 'sparkles',
      smart_radio: 'sparkles',
    };
    const titleMap = {
      order: '顺序播放 (点击切换)',
      loop: '列表循环 (点击切换)',
      single: '单曲循环 (点击切换)',
      shuffle: '均衡随机 (点击切换)',
      fm: '私人漫游模式',
      intelligence: '心动模式',
      smart_radio: '心动电台 (自动续播)',
    };
    for (const id of ['player-mode-btn', 'stage-mode-btn']) {
      const btn = document.getElementById(id);
      if (!btn) continue;
      setButtonIcon(btn, iconMap[mode] || 'repeat', '', { size: 15 });
      btn.title = titleMap[mode] || '切换播放模式';
      btn.classList.toggle('active', mode !== 'order');
    }
  }

  updateVolumeIconUI() {
    if (typeof document === 'undefined') return;
    const icon = this.muted || this.targetVolume === 0 ? 'volume-mute' : 'volume';
    for (const id of ['player-mute-btn', 'stage-mute-btn']) {
      const btn = document.getElementById(id);
      if (!btn) continue;
      setButtonIcon(btn, icon, '', { size: 15 });
    }
  }

  updateChorusUI(song, durationSec) {
    if (typeof document === 'undefined') return;
    const dur = durationSec || normalizeTimeToSeconds(song?.duration, 0);
    const startSec = song ? normalizeTimeToSeconds(song.chorus_start, dur) : 0;
    const endSec = song ? normalizeTimeToSeconds(song.chorus_end, dur) : 0;
    const hasChorus = dur > 0 && startSec > 0 && startSec < dur;

    for (const markerId of ['player-chorus-marker', 'stage-chorus-marker']) {
      const marker = document.getElementById(markerId);
      if (!marker) continue;
      if (hasChorus) {
        const leftPct = Math.max(0, Math.min(98, (startSec / dur) * 100));
        const widthPct = endSec > startSec
          ? Math.max(2, Math.min(100 - leftPct, ((endSec - startSec) / dur) * 100))
          : 8;
        marker.style.left = `${leftPct}%`;
        marker.style.width = `${widthPct}%`;
        marker.classList.remove('hidden');
      } else {
        marker.classList.add('hidden');
      }
    }

    const playerChorusBtn = document.getElementById('player-chorus-btn');
    if (playerChorusBtn) {
      playerChorusBtn.title = hasChorus ? `跳转副歌 (${formatSeconds(startSec)})` : '跳至 35% (无副歌标记)';
      const label = playerChorusBtn.querySelector('.btn-label');
      if (label) label.textContent = hasChorus ? '跳转副歌' : '跳至 35%';
    }
    const deckChorusBtn = document.getElementById('deck-chorus-jump-btn');
    if (deckChorusBtn) {
      const deckSong = (state.get('deckQueue') || [])[state.get('deckIndex') || 0];
      const target = deckSong && song && getTrackKey(deckSong) === getTrackKey(song) ? song : deckSong;
      const deckStart = normalizeTimeToSeconds(target?.chorus_start, normalizeTimeToSeconds(target?.duration));
      deckChorusBtn.disabled = !deckSong;
      deckChorusBtn.title = deckStart > 0 ? `跳转副歌 (${formatSeconds(deckStart)}) [C / ↑]` : '跳至 35% [C / ↑]';
      const label = deckChorusBtn.querySelector('.btn-label');
      if (label) label.textContent = deckStart > 0 ? '副歌' : '约 35%';
    }
  }

  updateInlineLyricUI(currentTime = 0) {
    if (typeof document === 'undefined') return;
    const el = document.getElementById('player-inline-lyric');
    if (!el) return;
    if (state.get('isStageOpen') || !this.currentSong) {
      el.textContent = '';
      el.classList.add('hidden');
      return;
    }
    const snap = lyricsManager.getActiveLyricSnapshot(currentTime);
    const text = String(snap?.line || '').trim();
    if (!text) {
      el.textContent = '';
      el.classList.add('hidden');
      return;
    }
    el.textContent = text;
    el.title = text;
    el.classList.remove('hidden');
  }

  updateDesktopLyricsButtonUI() {
    if (typeof document === 'undefined') return;
    const isOpen = Boolean(state.get('desktopLyricsOpen'));
    const btn = document.getElementById('player-desktop-lyrics-btn');
    if (btn) {
      btn.classList.toggle('active', isOpen);
      btn.setAttribute('aria-pressed', isOpen ? 'true' : 'false');
    }
    const stageBtn = document.getElementById('stage-desktop-lyrics-btn');
    if (stageBtn) {
      stageBtn.classList.toggle('active', isOpen);
      stageBtn.setAttribute('aria-pressed', isOpen ? 'true' : 'false');
    }
  }

  updateTimelineUI(currentTime, duration) {
    if (typeof document === 'undefined') return;
    const percent = duration > 0 ? Math.max(0, Math.min(100, (currentTime / duration) * 100)) : 0;

    for (const prefix of ['player', 'stage']) {
      const currentEl = document.getElementById(`${prefix}-time-current`);
      const totalEl = document.getElementById(`${prefix}-time-total`);
      const progressEl = document.getElementById(`${prefix}-timeline-progress`);
      const sliderEl = document.getElementById(`${prefix}-timeline-slider`);

      if (currentEl) currentEl.textContent = formatSeconds(currentTime).padStart(5, '0');
      if (totalEl) totalEl.textContent = formatSeconds(duration).padStart(5, '0');
      if (progressEl) progressEl.style.width = `${percent}%`;
      if (sliderEl?.parentElement?.style?.setProperty) {
        sliderEl.parentElement.style.setProperty('--progress-pct', `${percent}%`);
      }
      if (sliderEl && !sliderEl.matches?.(':active')) {
        sliderEl.value = String(percent);
      }
    }
  }
}

export const player = new AudioPlayer();
