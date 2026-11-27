import { api } from './api.js';
import { createIcon } from './icons.js';
import { state } from './state.js';
import { player } from './player.js';
import { lyricsManager } from './lyrics.js';
import { toast } from './toast.js';
import { THEME_OPTIONS, ACCENT_PRESETS, themeManager } from './theme.js';
import { cloudManager } from './cloud.js';
import { createElement, formatArtists, getSizedCoverUrl, bindTimelineTooltip, bindVolumeWheel } from './utils.js';

const FLUID_CANVAS_SIZE = 160;
const COLOR_LERP_MS = 600;
const DEFAULT_FLUID_COLORS = [
  [54, 88, 72],
  [44, 72, 92],
  [78, 52, 86],
  [48, 66, 62],
];

function cloneRgbTriplets(colors) {
  const src = Array.isArray(colors) && colors.length === 4 ? colors : DEFAULT_FLUID_COLORS;
  return src.map(c => (Array.isArray(c) && c.length >= 3 ? [Number(c[0]) || 0, Number(c[1]) || 0, Number(c[2]) || 0] : [50, 70, 75]));
}

class ImmersiveStageManager {
  constructor() {
    this.overlay = null;
    this.ambientHost = null;
    this.previouslyFocused = null;
    this.windowBlurred = false;

    // 160x160 AMLL fluid canvas state
    this.fluidCanvas = null;
    this.fluidCtx = null;
    this.fluidRafId = null;
    this.lastFluidFrameMs = 0;
    this.fluidPhase = 0;
    this.fromColors = cloneRgbTriplets(DEFAULT_FLUID_COLORS);
    this.currentColors = cloneRgbTriplets(DEFAULT_FLUID_COLORS);
    this.targetColors = cloneRgbTriplets(DEFAULT_FLUID_COLORS);
    this.colorLerpStart = 0;
    this.bassEnergy = 0;
    this.lastCoverSrc = '';
  }

  init() {
    if (typeof document === 'undefined') return;
    this.overlay = document.getElementById('immersive-stage-overlay');
    this.ambientHost = document.getElementById('stage-ambient-host');
    if (!this.overlay) return;

    this.windowBlurred = document.hasFocus?.() === false;
    this.motion = window.matchMedia?.('(prefers-reduced-motion: reduce)');
    this.motion?.addEventListener?.('change', () => this.syncAmbientPauseState());
    if (typeof MutationObserver !== 'undefined') {
      this.themeObserver = new MutationObserver(() => {
        if (state.get('isStageOpen') && this.mode === 'fluid' && this.fluidCtx) this.renderFluidFrame(performance.now());
      });
      this.themeObserver.observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});
    }
    const syncFullscreen = (nativeState = this.nativeFullscreen) => {
      this.nativeFullscreen = Boolean(nativeState);
      const active = this.nativeFullscreen || Boolean(document.fullscreenElement);
      document.documentElement.dataset.fullscreen = String(active);
      const button = document.getElementById('stage-fullscreen-btn');
      button?.setAttribute('aria-pressed', String(active));
      button?.replaceChildren?.(createIcon(active ? 'minimize' : 'maximize'));
    };
    syncFullscreen();
    state.on('window:fullscreen', syncFullscreen);
    document.addEventListener('fullscreenchange', () => syncFullscreen());
    document.getElementById('stage-fullscreen-btn')?.addEventListener('click', () => this.toggleFullscreen());
    this.bindCoverTilt();
    this.bindControls();
    this.bindLifecycle();

    const syncOrigin = () => {
      const badge = document.getElementById('stage-origin-badge');
      if (!badge) return;
      const origin = state.get('currentPlayingSong')?._origin === 'deck'
        ? '歌单清理试听' : state.get('playQueueOrigin');
      badge.hidden = !origin;
      badge.textContent = origin ? '来自' + origin : '';
      badge.title = badge.textContent;
    };
    syncOrigin();
    state.on('change:playQueueOrigin', syncOrigin);
    state.on('change:currentPlayingSong', (song) => {
      syncOrigin();
      if (state.get('isStageOpen')) {
        this.syncSongMeta(song);
      }
    });

    state.on('change:coverPaletteColors', (colors) => {
      this.setPaletteColors(colors);
    });

    state.on('change:isPlaying', () => {
      this.syncCoverBreathing();
      this.syncAmbientPauseState();
    });

    state.on('change:settings', (cfg) => {
      this.applyStageMode();
      lyricsManager.setBlurDepth(Boolean(cfg?.lyrics_blur_depth));
      if (cfg && cfg.audio_reactive_glow === false) {
        this.setBassGlow(0);
      }
      this.syncAmbientPauseState();
    });

    state.on('auth:logout', () => {
      if (state.get('isStageOpen')) this.close();
    });

    state.on('change:windowVisible', () => this.syncAmbientPauseState());
    state.on('change:windowMinimized', () => this.syncAmbientPauseState());
    this.applyStageMode();
    this.syncAmbientPauseState();
    lyricsManager.setBlurDepth(Boolean(state.get('settings')?.lyrics_blur_depth));
  }

  async toggleFullscreen() {
    if (this.fullscreenPending) return;
    this.fullscreenPending = true;
    try {
      const result = await api.toggleDesktopFullscreen();
      if (!result.ok) throw new Error(result.error?.message);
      if (result.data.supported) {
        state.emit('window:fullscreen', result.data.fullscreen);
      } else if (document.fullscreenElement) {
        await document.exitFullscreen();
      } else {
        await document.documentElement.requestFullscreen();
      }
    } catch {
      toast.quiet('未能切换窗口全屏');
    } finally {
      this.fullscreenPending = false;
    }
  }

  bindCoverTilt() {
    const cover = document.getElementById('stage-cover-frame');
    if (!cover) return;
    this.clearTilt = () => {
      cover.style.removeProperty('will-change');
      cover.style.removeProperty('transform');
      cover.classList.remove('tilted');
    };
    const allowed = () => !this.shouldPauseAmbient() && state.get('settings')?.enable_3d_effects !== false;
    cover.addEventListener('pointerenter', () => {
      if (!allowed()) return;
      cover.style.willChange = 'transform';
      cover.classList.add('tilted');
    });
    cover.addEventListener('pointermove', e => {
      if (!cover.classList.contains('tilted')) return;
      const rect = cover.getBoundingClientRect();
      const x = (e.clientX - rect.left) / rect.width;
      const y = (e.clientY - rect.top) / rect.height;
      cover.style.transform = 'perspective(900px) rotateX(' + (0.5-y)*10 + 'deg) rotateY(' + (x-0.5)*10 + 'deg)';
      cover.style.setProperty('--pointer', (x*100)+'% '+(y*100)+'%');
    });
    cover.addEventListener('pointerleave', this.clearTilt);
  }

  applyStageMode() {
    if (!this.overlay) return;
    const cfg = state.get('settings') || {};
    const mode = cfg.stage_theme || 'fluid';
    const changed = this.mode !== mode || this.gpuMode !== (cfg.gpu_performance_mode || 'balanced');
    this.mode = mode;
    this.gpuMode = cfg.gpu_performance_mode || 'balanced';
    this.overlay.dataset.stageTheme = mode;
    const lyricSize = Math.max(18, Math.min(40, Number(cfg.stage_lyric_size) || 24));
    const lyricAlign = cfg.stage_lyric_align === 'center' ? 'center' : 'left';
    this.overlay.style?.setProperty?.('--stage-lyric-size', `${lyricSize}px`);
    const scroller = typeof document !== 'undefined' ? document.getElementById('stage-lyrics-container') : null;
    if (scroller?.dataset) {
      scroller.dataset.lyricAlign = lyricAlign;
    }
    const select = document.getElementById('stage-theme');
    if (select) select.value = mode;
    const spectrum = document.getElementById('stage-spectrum-mode');
    if (spectrum) {
      spectrum.hidden = mode !== 'visualizer';
      spectrum.value = cfg.visualizer_mode || 'bars';
    }
    this.syncCustomizerUI();
    if (changed && state.get('isStageOpen')) this.mountAmbientLayer();
  }

  renderCustomizerPanel() {
    if (typeof document === 'undefined') return;
    const themeHost = document.getElementById('stage-theme-chips');
    const accentHost = document.getElementById('stage-accent-chips');
    const app = themeManager.getAppearance();
    if (themeHost && !themeHost.children.length) {
      THEME_OPTIONS.forEach(opt => {
        themeHost.appendChild(createElement('button', {
          className: `stage-chip-btn ${app.theme === opt.id ? 'active' : ''}`,
          dataset: { stageThemeChoice: opt.id },
          attributes: { type: 'button', title: opt.label, 'aria-pressed': String(app.theme === opt.id) },
          events: {
            click: () => {
              cloudManager.updateAppearance({ theme: opt.id }, `已切换至「${opt.label}」主题`);
              this.syncCustomizerUI();
            },
          },
          children: [
            createElement('span', {
              className: 'stage-chip-swatch',
              style: `background: linear-gradient(135deg, ${opt.bg} 50%, ${opt.surface} 50%);`,
            }),
            createElement('span', { text: opt.label }),
          ],
        }));
      });
    }
    if (accentHost && !accentHost.children.length) {
      ACCENT_PRESETS.forEach(preset => {
        accentHost.appendChild(createElement('button', {
          className: `stage-chip-btn ${app.accent_mode === 'preset' && app.accent === preset.hex ? 'active' : ''}`,
          dataset: { stageAccentChoice: preset.hex },
          attributes: { type: 'button', title: preset.label, 'aria-pressed': String(app.accent_mode === 'preset' && app.accent === preset.hex) },
          events: {
            click: () => {
              cloudManager.updateAppearance({ accent_mode: 'preset', accent: preset.hex }, `强调色已设为${preset.label}`);
              this.syncCustomizerUI();
            },
          },
          children: [
            createElement('span', { className: 'stage-chip-swatch', style: `background-color: ${preset.hex};` }),
            createElement('span', { text: preset.label }),
          ],
        }));
      });
      accentHost.appendChild(createElement('button', {
        className: `stage-chip-btn ${app.accent_mode === 'cover' ? 'active' : ''}`,
        text: '跟随封面',
        dataset: { stageAccentCover: 'true' },
        attributes: { type: 'button', 'aria-pressed': String(app.accent_mode === 'cover') },
        events: {
          click: () => {
            cloudManager.updateAppearance({ accent_mode: 'cover' }, '强调色将随专辑封面变化');
            this.syncCustomizerUI();
          },
        },
      }));
    }
    this.syncCustomizerUI();
  }

  syncCustomizerUI() {
    if (typeof document === 'undefined') return;
    const cfg = state.get('settings') || {};
    const app = themeManager.getAppearance();
    document.querySelectorAll('[data-stage-theme-choice]').forEach(btn => {
      const active = btn.dataset.stageThemeChoice === app.theme;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-pressed', String(active));
    });
    document.querySelectorAll('[data-stage-accent-choice]').forEach(btn => {
      const active = app.accent_mode === 'preset' && btn.dataset.stageAccentChoice === app.accent;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-pressed', String(active));
    });
    const coverBtn = document.querySelector('[data-stage-accent-cover="true"]');
    if (coverBtn) {
      const active = app.accent_mode === 'cover';
      coverBtn.classList.toggle('active', active);
      coverBtn.setAttribute('aria-pressed', String(active));
    }
    const size = Math.max(18, Math.min(40, Number(cfg.stage_lyric_size) || 24));
    const sizeSlider = document.getElementById('stage-lyric-size-slider');
    const sizeVal = document.getElementById('stage-lyric-size-val');
    if (sizeSlider) sizeSlider.value = String(size);
    if (sizeVal) sizeVal.textContent = `${size}px`;
    document.querySelectorAll('[data-stage-align-btn]').forEach(btn => {
      const active = btn.dataset.stageAlignBtn === (cfg.stage_lyric_align === 'center' ? 'center' : 'left');
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-pressed', String(active));
    });
    const blurToggle = document.getElementById('stage-blur-toggle');
    if (blurToggle) blurToggle.checked = Boolean(cfg.lyrics_blur_depth);
    const tiltToggle = document.getElementById('stage-tilt-toggle');
    if (tiltToggle) tiltToggle.checked = cfg.enable_3d_effects !== false;
  }

  bindControls() {
    const closeBtn = document.getElementById('stage-close-btn');
    const playBtn = document.getElementById('stage-play-btn');
    const prevBtn = document.getElementById('stage-prev-btn');
    const nextBtn = document.getElementById('stage-next-btn');
    const heartBtn = document.getElementById('stage-heart-btn');
    const sourceBtn = document.getElementById('stage-candidates-btn');
    const similarBtn = document.getElementById('stage-similar-btn');
    const transToggle = document.getElementById('stage-toggle-translation');
    const romaToggle = document.getElementById('stage-toggle-romaji');
    const offsetMinus = document.getElementById('stage-offset-minus');
    const offsetLabel = document.getElementById('stage-offset-label');
    const offsetPlus = document.getElementById('stage-offset-plus');
    const desktopLyricsBtn = document.getElementById('stage-desktop-lyrics-btn');
    const slider = document.getElementById('stage-timeline-slider');

    if (closeBtn) closeBtn.addEventListener('click', () => this.close());
    if (playBtn) playBtn.addEventListener('click', () => player.togglePlay());
    if (prevBtn) prevBtn.addEventListener('click', () => player.playPrev());
    if (nextBtn) nextBtn.addEventListener('click', () => player.playNext(true));
    if (heartBtn) heartBtn.addEventListener('click', () => player.toggleLikeCurrentSong());
    document.getElementById('stage-source-badge')?.addEventListener('click', () => player.openAudioCandidatesModal());
    if (sourceBtn) sourceBtn.addEventListener('click', () => player.openAudioCandidatesModal(null, sourceBtn));
    if (similarBtn) similarBtn.addEventListener('click', () => player.openSimilarSongsModal(null, similarBtn));

    if (transToggle) {
      transToggle.classList.toggle('active', lyricsManager.showTranslation);
      transToggle.addEventListener('click', () => {
        if (!lyricsManager.lyricsData.some(line => line.translation)) { toast.quiet('此曲暂无翻译'); return; }
        const next = !lyricsManager.showTranslation;
        transToggle.classList.toggle('active', next);
        lyricsManager.setDisplayOptions({ showTranslation: next });
      });
    }

    if (romaToggle) {
      romaToggle.classList.toggle('active', lyricsManager.showRomaji);
      romaToggle.addEventListener('click', () => {
        if (!lyricsManager.lyricsData.some(line => line.romaji)) { toast.quiet('此曲暂无罗马音'); return; }
        const next = !lyricsManager.showRomaji;
        romaToggle.classList.toggle('active', next);
        lyricsManager.setDisplayOptions({ showRomaji: next });
      });
    }

    const applyOffsetAndSync = (nextOffset) => {
      const liveTime = this.getLivePlaybackTime();
      lyricsManager.syncStageToTime(liveTime, { instantScroll: false });
      player.pushDesktopLyricsState(true);
      toast.quiet(nextOffset === 0 ? '歌词偏移已重置为 0.0s' : `歌词偏移 ${nextOffset > 0 ? '+' : ''}${nextOffset.toFixed(1)}s`);
    };

    if (offsetMinus) {
      offsetMinus.addEventListener('click', () => {
        const val = lyricsManager.adjustSongOffset(-0.5);
        applyOffsetAndSync(val);
      });
    }
    if (offsetPlus) {
      offsetPlus.addEventListener('click', () => {
        const val = lyricsManager.adjustSongOffset(0.5);
        applyOffsetAndSync(val);
      });
    }
    if (offsetLabel) {
      offsetLabel.addEventListener('click', () => {
        const val = lyricsManager.setSongOffset(0);
        applyOffsetAndSync(val);
      });
    }

    if (desktopLyricsBtn) {
      desktopLyricsBtn.addEventListener('click', () => {
        player.toggleDesktopLyricsWindow();
      });
    }

    if (slider) {
      slider.addEventListener('input', (e) => {
        const pct = Number(e.target.value);
        const dur = state.get('duration') || 0;
        player.seek((pct / 100) * dur);
      });
      bindTimelineTooltip(
        slider,
        document.getElementById('stage-timeline-tooltip'),
        () => state.get('duration') || 0,
      );
    }

    document.getElementById('stage-mode-btn')?.addEventListener('click', () => player.cyclePlayMode());
    document.getElementById('stage-mute-btn')?.addEventListener('click', () => player.toggleMute());
    document.getElementById('stage-volume-slider')?.addEventListener('input', (e) => {
      player.setVolume(Number(e.target.value) / 100);
    });
    bindVolumeWheel(
      document.getElementById('stage-volume-group'),
      (delta) => player.adjustVolume(delta, { showOsd: false }),
    );

    const customBtn = document.getElementById('stage-customize-btn');
    const customPop = document.getElementById('stage-customize-popover');
    const moreBtn = document.getElementById('stage-more-btn');
    const morePop = document.getElementById('stage-more-popover');

    const closeMorePopover = () => {
      if (!morePop) return;
      morePop.classList.add('hidden');
      moreBtn?.setAttribute('aria-expanded', 'false');
    };
    const closeCustomizePopover = () => {
      if (!customPop) return;
      customPop.classList.add('hidden');
      customBtn?.setAttribute('aria-expanded', 'false');
    };

    if (customBtn && customPop) {
      this.renderCustomizerPanel();
      customBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        closeMorePopover();
        const willOpen = customPop.classList.contains('hidden');
        customPop.classList.toggle('hidden', !willOpen);
        customBtn.setAttribute('aria-expanded', String(willOpen));
        if (willOpen) this.syncCustomizerUI();
      });
      document.addEventListener('click', (e) => {
        if (customPop.classList.contains('hidden')) return;
        if (e.target?.closest?.('#stage-customize-popover, #stage-customize-btn')) return;
        closeCustomizePopover();
      });
    }

    if (moreBtn && morePop) {
      moreBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        closeCustomizePopover();
        const willOpen = morePop.classList.contains('hidden');
        morePop.classList.toggle('hidden', !willOpen);
        moreBtn.setAttribute('aria-expanded', String(willOpen));
      });
      morePop.addEventListener('click', (e) => {
        if (e.target?.closest?.('button')) {
          closeMorePopover();
        }
      });
      document.addEventListener('click', (e) => {
        if (morePop.classList.contains('hidden')) return;
        if (e.target?.closest?.('#stage-more-popover, #stage-more-btn')) return;
        closeMorePopover();
      });
    }

    const lyricsScroller = document.getElementById('stage-lyrics-container');
    if (lyricsScroller) {
      lyricsScroller.addEventListener('scroll', () => this.syncLyricsScrollMask(), { passive: true });
    }
    document.getElementById('stage-lyric-size-slider')?.addEventListener('input', (e) => {
      const sz = Math.max(18, Math.min(40, Number(e.target.value) || 24));
      this.overlay?.style?.setProperty?.('--stage-lyric-size', `${sz}px`);
      const valEl = document.getElementById('stage-lyric-size-val');
      if (valEl) valEl.textContent = `${sz}px`;
    });
    document.getElementById('stage-lyric-size-slider')?.addEventListener('change', (e) => {
      const sz = Math.max(18, Math.min(40, Number(e.target.value) || 24));
      cloudManager.saveSettings({ stage_lyric_size: sz }, `舞台歌词字号已设为 ${sz}px`);
    });
    document.querySelectorAll('[data-stage-align-btn]').forEach(btn => {
      btn.addEventListener('click', () => {
        const align = btn.dataset.stageAlignBtn === 'center' ? 'center' : 'left';
        cloudManager.saveSettings({ stage_lyric_align: align }, align === 'center' ? '歌词已设为居中对齐' : '歌词已设为左对齐');
      });
    });
    document.getElementById('stage-blur-toggle')?.addEventListener('change', (e) => {
      cloudManager.saveSettings({ lyrics_blur_depth: Boolean(e.target.checked) }, e.target.checked ? '已开启歌词景深虚化' : '已关闭歌词景深虚化');
    });
    document.getElementById('stage-tilt-toggle')?.addEventListener('change', (e) => {
      cloudManager.saveSettings({ enable_3d_effects: Boolean(e.target.checked) }, e.target.checked ? '已开启 3D 封面倾斜' : '已关闭 3D 封面倾斜');
    });
  }

  bindLifecycle() {
    document.addEventListener('visibilitychange', () => {
      this.syncAmbientPauseState();
    });

    window.addEventListener('blur', () => {
      this.windowBlurred = true;
      this.syncAmbientPauseState();
    });

    window.addEventListener('focus', () => {
      this.windowBlurred = false;
      this.syncAmbientPauseState();
    });
  }

  getLivePlaybackTime() {
    if (player.audio && Number.isFinite(player.audio.currentTime) && player.audio.currentTime > 0) {
      return player.audio.currentTime;
    }
    return state.get('currentTime') || 0;
  }

  setPaletteColors(colors) {
    if (!Array.isArray(colors) || colors.length !== 4) return;
    this.fromColors = cloneRgbTriplets(this.currentColors);
    this.targetColors = cloneRgbTriplets(colors);
    this.colorLerpStart = typeof performance !== 'undefined' ? performance.now() : Date.now();
    if (state.get('isStageOpen') && this.fluidCtx) {
      if (this.mode !== 'visualizer') this.renderFluidFrame(this.colorLerpStart);
    }
  }

  setBassGlow(energy01) {
    const enabled = state.get('settings')?.audio_reactive_glow !== false;
    const clamped = enabled && state.get('isPlaying') && state.get('isStageOpen')
      ? Math.max(0, Math.min(1, Number(energy01) || 0))
      : 0;
    this.bassEnergy = clamped;
    if (this.overlay?.style) {
      this.overlay.style.setProperty('--bass-glow', clamped.toFixed(3));
    }
  }

  mountAmbientLayer() {
    if (!this.ambientHost) return;
    this.unmountAmbientLayer();
    const status = document.getElementById('stage-spectrum-status');
    if (status) status.textContent = '';
    if (this.gpuMode === 'eco' || ['vinyl', 'poster'].includes(this.mode)) return;
    if (this.mode === 'visualizer') {
      const host = document.getElementById('stage-spectrum-host');
      const canvas = document.createElement('canvas');
      const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
      canvas.width = Math.round(320 * dpr);
      canvas.height = Math.round(104 * dpr);
      host.appendChild(canvas);
      this.fluidCanvas = canvas;
      this.fluidCtx = canvas.getContext('2d');
      if (this.renderSpectrum() !== false) this.syncAmbientPauseState();
      return;
    }

    const canvas = createElement('canvas', {
      className: 'stage-fluid-canvas',
      attributes: {
        id: 'stage-fluid-canvas',
        width: String(FLUID_CANVAS_SIZE),
        height: String(FLUID_CANVAS_SIZE),
        'aria-hidden': 'true',
      },
    });
    canvas.width = FLUID_CANVAS_SIZE;
    canvas.height = FLUID_CANVAS_SIZE;

    const layer = createElement('div', {
      className: 'stage-ambient-layer',
      attributes: { id: 'stage-ambient-layer', 'aria-hidden': 'true' },
      children: [
        canvas,
        createElement('div', { className: 'stage-noise-overlay', attributes: { 'aria-hidden': 'true' } }),
        createElement('div', { className: 'stage-ambient-scrim', attributes: { 'aria-hidden': 'true' } }),
      ],
    });
    this.ambientHost.appendChild(layer);
    this.fluidCanvas = canvas;
    this.outputCtx = canvas.getContext?.('2d');
    this.mesh = this.outputCtx && typeof OffscreenCanvas !== 'undefined'
      ? new OffscreenCanvas(FLUID_CANVAS_SIZE, FLUID_CANVAS_SIZE) : null;
    this.fluidCtx = this.mesh?.getContext('2d') || this.outputCtx;

    const paletteColors = state.get('coverPaletteColors');
    if (Array.isArray(paletteColors) && paletteColors.length === 4) {
      this.currentColors = cloneRgbTriplets(paletteColors);
      this.targetColors = cloneRgbTriplets(paletteColors);
      this.fromColors = cloneRgbTriplets(paletteColors);
    }

    const now = typeof performance !== 'undefined' ? performance.now() : Date.now();
    this.renderFluidFrame(now);
    this.syncAmbientPauseState();
  }

  unmountAmbientLayer() {
    this.stopFluidLoop();
    if (this.fluidCanvas) {
      this.fluidCanvas.width = 0;
      this.fluidCanvas.height = 0;
      this.fluidCanvas.remove?.();
    }
    if (this.mesh) this.mesh.width = this.mesh.height = 0;
    this.mesh = null;
    this.outputCtx = null;
    this.fluidCanvas = null;
    this.fluidCtx = null;
    this.setBassGlow(0);
    if (!this.ambientHost) return;
    this.ambientHost.textContent = '';
  }

  shouldPauseAmbient() {
    const cfg = state.get('settings') || {};
    const visualMode = cfg.visual_mode || 'soft_dynamic';
    if (cfg.game_mode === true) return true;
    if (cfg.gpu_performance_mode === 'eco' || state.get('windowVisible') === false || state.get('windowMinimized')) return true;
    if (visualMode === 'quiet_eco') return true;
    if (!state.get('isPlaying')) return true;
    if (this.windowBlurred) return true;
    if (typeof document !== 'undefined' && document.hidden) return true;
    return Boolean(this.motion?.matches);
  }

  syncAmbientPauseState() {
    const layer = typeof document !== 'undefined' ? document.getElementById('stage-ambient-layer') : null;
    const paused = this.shouldPauseAmbient();
    const root = globalThis.document?.documentElement;
    if (root?.dataset) {
      root.dataset.gpuPaused = String(paused);
      root.dataset.gpuMode = this.gpuMode || 'balanced';
    }
    if (paused || state.get('settings')?.enable_3d_effects === false) this.clearTilt?.();
    if (paused) lyricsManager.stopWordSync();
    else lyricsManager.ensureWordSync();
    if (layer) {
      layer.dataset.paused = paused ? 'true' : 'false';
    }
    const offscreenHidden = state.get('windowVisible') === false || state.get('windowMinimized') || (typeof document !== 'undefined' && document.hidden);
    if (offscreenHidden && this.mesh) {
      this.mesh.width = 0;
      this.mesh.height = 0;
      this.mesh = null;
      this.fluidCtx = this.outputCtx || null;
    }
    if (!state.get('isStageOpen') || !this.fluidCtx) {
      this.stopFluidLoop();
      return;
    }
    if (paused) {
      this.stopFluidLoop();

    } else {
      this.startFluidLoop();
    }
  }

  startFluidLoop() {
    if (this.fluidRafId !== null || typeof requestAnimationFrame !== 'function') return;
    if (!this.fluidCtx || !state.get('isStageOpen') || this.shouldPauseAmbient()) return;

    const tick = (nowMs) => {
      this.fluidRafId = null;
      if (!state.get('isStageOpen') || !this.fluidCtx || this.shouldPauseAmbient()) return;

      if (nowMs - this.lastFluidFrameMs >= 1000 / (this.gpuMode === 'high' ? 60 : this.mode === 'visualizer' ? 30 : 20) - 0.1) {
        const dt = this.lastFluidFrameMs > 0 ? Math.min(0.12, (nowMs - this.lastFluidFrameMs) / 1000) : 0.04;
        this.lastFluidFrameMs = nowMs;
        this.fluidPhase += dt * (0.28 + this.bassEnergy * 0.14);
        if (this.drawFrame(nowMs) === false) return;
      }
      this.fluidRafId = requestAnimationFrame(tick);
    };
    this.fluidRafId = requestAnimationFrame(tick);
  }

  stopFluidLoop() {
    if (this.fluidRafId !== null && typeof cancelAnimationFrame === 'function') {
      cancelAnimationFrame(this.fluidRafId);
    }
    this.fluidRafId = null;
  }

  drawFrame(now) {
    if (this.mode === 'visualizer') return this.renderSpectrum();
    else {
      const bins = state.get('settings')?.audio_reactive_glow !== false && player.getFrequencyData();
      if (bins?.length) this.setBassGlow(bins.subarray(0,7).reduce((a,b) => a+b,0) / (7*255));
      this.renderFluidFrame(now);
    }
  }

  renderSpectrum() {
    const ctx = this.fluidCtx;
    if (!ctx) return;
    const {width: w, height: h} = this.fluidCanvas;
    const bins = player.getFrequencyData();
    const status = document.getElementById('stage-spectrum-status');
    if (status) status.textContent = bins ? '' : '当前音源无法提供频谱';
    if (!bins) { ctx.clearRect(0, 0, w, h); return false; }
    const sample = i => (bins?.[i] || 0) / 255;
    const mode = state.get('settings')?.visualizer_mode || 'bars';
    ctx.clearRect(0, 0, w, h);
    ctx.strokeStyle = ctx.fillStyle = getComputedStyle(this.overlay).getPropertyValue('--cover-accent').trim() || '#85a799';
    ctx.lineWidth = 2;
    if (mode === 'bars') {
      for (let i = 0; i < 48; i++) {
        const height = 2 + sample(i * 2) * h * .88;
        ctx.fillRect(i*w/48, (h-height)/2, w/68, height);
      }
    } else if (mode === 'ring') {
      ctx.beginPath();
      for (let i = 0; i <= 96; i++) {
        const angle = i * Math.PI / 48;
        const radius = h * (.25 + .22 * sample(i % 96));
        const x = w/2 + Math.cos(angle)*radius, y = h/2 + Math.sin(angle)*radius;
        if (i === 0) ctx.moveTo(x,y);
        else ctx.lineTo(x,y);
      }
      ctx.stroke();
    } else {
      for (let row = 0; row < 7; row++) {
        ctx.beginPath();
        const depth = .52 + row*.075;
        for (let i = 0; i < 40; i++) {
          const x = w*(.5 + (i/39-.5)*depth);
          const y = h*(.24 + row*.11 - sample(i*2+row)*.27*depth);
          if (!i) ctx.moveTo(x,y);
          else ctx.lineTo(x,y);
        }
        ctx.stroke();
      }
    }
  }

  renderFluidFrame(nowMs) {
    if (!this.mesh && this.outputCtx && typeof OffscreenCanvas !== 'undefined') {
      this.mesh = new OffscreenCanvas(FLUID_CANVAS_SIZE, FLUID_CANVAS_SIZE);
      this.fluidCtx = this.mesh.getContext('2d') || this.outputCtx;
    }
    const ctx = this.fluidCtx;
    if (!ctx) return;

    // 600ms smooth color interpolation between song palettes
    const elapsed = this.colorLerpStart > 0 ? Math.max(0, nowMs - this.colorLerpStart) : COLOR_LERP_MS;
    const t = Math.min(1, elapsed / COLOR_LERP_MS);
    const eased = 1 - Math.pow(1 - t, 3);

    for (let i = 0; i < 4; i++) {
      for (let ch = 0; ch < 3; ch++) {
        this.currentColors[i][ch] = Math.round(
          this.fromColors[i][ch] + (this.targetColors[i][ch] - this.fromColors[i][ch]) * eased,
        );
      }
    }

    const w = FLUID_CANVAS_SIZE;
    const h = FLUID_CANVAS_SIZE;
    const p = this.fluidPhase;
    const glowBoost = 1 + this.bassEnergy * 0.16;

    ctx.globalCompositeOperation = 'source-over';
    const themeId = document.documentElement?.dataset?.theme;
    const light = ['light', 'celadon', 'parchment'].includes(themeId);
    ctx.fillStyle = themeId === 'celadon' ? '#edf2f0' : themeId === 'parchment' ? '#f5eee3' : light ? '#f5efe8' : '#0e0f13';
    ctx.fillRect(0, 0, w, h);

    // 4 sinusoidal oil-paint blobs orbiting smoothly
    const blobs = [
      {
        x: w * (0.26 + 0.16 * Math.sin(p * 0.85)),
        y: h * (0.28 + 0.15 * Math.cos(p * 0.65)),
        r: w * 0.56 * glowBoost,
        rgb: this.currentColors[0],
        alpha: 0.72,
      },
      {
        x: w * (0.76 + 0.14 * Math.cos(p * 0.72 + 1.2)),
        y: h * (0.30 + 0.16 * Math.sin(p * 0.58 + 0.4)),
        r: w * 0.54 * glowBoost,
        rgb: this.currentColors[1],
        alpha: 0.66,
      },
      {
        x: w * (0.32 + 0.15 * Math.cos(p * 0.62 + 2.4)),
        y: h * (0.74 + 0.14 * Math.sin(p * 0.80 + 1.8)),
        r: w * 0.58 * glowBoost,
        rgb: this.currentColors[2],
        alpha: 0.64,
      },
      {
        x: w * (0.72 + 0.16 * Math.sin(p * 0.68 + 3.5)),
        y: h * (0.72 + 0.14 * Math.cos(p * 0.74 + 2.9)),
        r: w * 0.52 * glowBoost,
        rgb: this.currentColors[3],
        alpha: 0.62,
      },
    ];

    ctx.globalCompositeOperation = light ? 'source-over' : 'screen';
    for (const blob of blobs) {
      const grad = ctx.createRadialGradient(blob.x, blob.y, 0, blob.x, blob.y, blob.r);
      const [r, g, b] = light ? blob.rgb.map(c => Math.round(164 + c * 0.35)) : blob.rgb;
      grad.addColorStop(0, `rgba(${r}, ${g}, ${b}, ${blob.alpha})`);
      grad.addColorStop(0.55, `rgba(${r}, ${g}, ${b}, ${(blob.alpha * 0.42).toFixed(2)})`);
      grad.addColorStop(1, `rgba(${r}, ${g}, ${b}, 0)`);
      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, w, h);
    }
    ctx.globalCompositeOperation = 'source-over';
    if (this.mesh) this.outputCtx.drawImage(this.mesh, 0, 0);
  }

  syncCoverBreathing() {
    if (typeof document === 'undefined') return;
    const frame = document.getElementById('stage-cover-frame');
    if (!frame) return;
    const playing = Boolean(state.get('isPlaying'));
    frame.classList.toggle('is-playing', playing);
    if (!playing) {
      this.setBassGlow(0);
    }
  }

  syncSongMeta(song) {
    if (typeof document === 'undefined') return;
    const coverEl = document.getElementById('stage-cover-img');
    const fallbackEl = document.getElementById('stage-cover-fallback');
    const titleEl = document.getElementById('stage-song-title');
    const artistEl = document.getElementById('stage-song-artist');
    const albumEl = document.getElementById('stage-song-album');

    const hasRealCover = Boolean(song && typeof song.cover_url === 'string' && song.cover_url.trim());
    if (coverEl) {
      if (hasRealCover) {
        const nextSrc = getSizedCoverUrl(song.cover_url, 384);
        if (this.lastCoverSrc && this.lastCoverSrc !== nextSrc && coverEl.classList) {
          coverEl.classList.add('cover-fading');
          setTimeout(() => coverEl.classList?.remove('cover-fading'), 240);
        }
        this.lastCoverSrc = nextSrc;
        coverEl.src = nextSrc;
        coverEl.alt = song.title || '专辑封面';
        coverEl.classList?.remove('hidden');
      } else {
        this.lastCoverSrc = '';
        coverEl.classList?.add('hidden');
      }
    }
    if (fallbackEl) {
      fallbackEl.classList?.toggle('hidden', hasRealCover);
    }
    if (titleEl) titleEl.textContent = song?.title || '未选择曲目';
    if (artistEl) artistEl.textContent = song ? formatArtists(song.artists) : '';
    if (albumEl) albumEl.textContent = song?.album ? `专辑 · ${song.album}` : '';

    this.syncCoverBreathing();
    lyricsManager.syncOffsetLabelUI();
    lyricsManager.syncTtmlPillUI();
    player.updateHeartUI(song);
    player.updateSourceBadgeUI(state.get('audioMeta'), state.get('playbackError') || '');
    player.updatePlayButtonUI(Boolean(state.get('isPlaying')));
    player.updatePlayModeUI();
    player.updateVolumeIconUI();
    const pct = Math.round((Number(player.targetVolume) || 0) * 100);
    const stageVolSlider = document.getElementById('stage-volume-slider');
    if (stageVolSlider) {
      stageVolSlider.value = String(pct);
      stageVolSlider.style?.setProperty?.('--vol-pct', `${player.muted ? 0 : pct}%`);
    }
    const stageVolPct = document.getElementById('stage-volume-pct');
    if (stageVolPct) stageVolPct.textContent = player.muted ? '静音' : `${pct}%`;
    player.updateTimelineUI(state.get('currentTime') || 0, state.get('duration') || 0);
    player.updateChorusUI(song, state.get('duration') || 0);
  }

  syncLyricsScrollMask() {
    if (typeof document === 'undefined') return;
    const scroller = document.getElementById('stage-lyrics-container');
    if (!scroller) return;
    scroller.classList.toggle('has-top-scroll', (Number(scroller.scrollTop) || 0) > 4);
  }

  async ensureLyricsLoaded(song) {
    if (!song?.id) return;
    const targetId = String(song.id);
    if (String(lyricsManager.currentSongId || '') === targetId && (lyricsManager.lyricsData || []).length > 0) {
      return;
    }
    const seq = this._lyricsLoadSeq = (this._lyricsLoadSeq || 0) + 1;
    try {
      const res = await api.getLyrics(song.id);
      if (seq !== this._lyricsLoadSeq || !state.get('isStageOpen')) return;
      if (String(state.get('currentPlayingSong')?.id || '') !== targetId) return;
      if (res?.ok && res.data) {
        const lrc = res.data.lyrics || res.data.lrc || '';
        const tlyric = res.data.tlyrics || res.data.tlyric || '';
        lyricsManager.loadLyrics(lrc, tlyric, {
          yrc: res.data.yrc || '',
          romalrc: res.data.romalrc || '',
          ttmlSource: res.data.ttml_source || '',
          songId: song.id,
        });
        lyricsManager.syncStageToTime(this.getLivePlaybackTime(), { instantScroll: true });
        this.syncLyricsScrollMask();
      }
    } catch {}
  }

  open(triggerElement = null) {
    if (!this.overlay) return;
    if (!state.get('currentPlayingSong')) {
      toast.info('暂无正在播放的曲目', '请先点击任意歌曲开始播放，即可进入全屏沉浸歌词舞台。');
      return;
    }

    this.previouslyFocused = triggerElement || (typeof document !== 'undefined' ? document.activeElement : null);
    this.overlay.classList.add('open');
    this.overlay.setAttribute('aria-hidden', 'false');
    state.set({ isStageOpen: true });
    this.mountAmbientLayer();

    const currentSong = state.get('currentPlayingSong');
    this.syncSongMeta(currentSong);
    const liveTime = this.getLivePlaybackTime();
    player.updateInlineLyricUI(liveTime);
    lyricsManager.syncStageToTime(liveTime, { instantScroll: true });
    this.syncLyricsScrollMask();
    this.ensureLyricsLoaded(currentSong);
    if (state.get('isPlaying')) {
      lyricsManager.setPlaybackState(true, () => this.getLivePlaybackTime());
    }

    const closeBtn = document.getElementById('stage-close-btn');
    if (closeBtn?.focus) closeBtn.focus();
  }

  close() {
    if (!this.overlay) return;
    this.clearTilt?.();
    const customPop = document.getElementById('stage-customize-popover');
    if (customPop) customPop.classList.add('hidden');
    const customBtn = document.getElementById('stage-customize-btn');
    if (customBtn) customBtn.setAttribute('aria-expanded', 'false');
    const morePop = document.getElementById('stage-more-popover');
    if (morePop) morePop.classList.add('hidden');
    const moreBtn = document.getElementById('stage-more-btn');
    if (moreBtn) moreBtn.setAttribute('aria-expanded', 'false');
    this.overlay.classList.remove('open');
    this.overlay.setAttribute('aria-hidden', 'true');
    state.set({ isStageOpen: false });
    player.updateInlineLyricUI(this.getLivePlaybackTime());
    // Completely unmount canvas & cancel rAF when stage closes
    this.unmountAmbientLayer();

    // Clear stage lyrics DOM to keep memory footprint lean
    const stageLyrics = document.getElementById('stage-lyrics-container');
    if (stageLyrics) {
      stageLyrics.textContent = '';
      stageLyrics.classList?.remove('has-top-scroll');
    }
    lyricsManager.invalidateStageRender();

    if (this.previouslyFocused?.focus) {
      this.previouslyFocused.focus();
    }
    this.previouslyFocused = null;
  }

  toggle(triggerElement = null) {
    if (state.get('isStageOpen')) this.close();
    else this.open(triggerElement);
  }
}

export const stageManager = new ImmersiveStageManager();
