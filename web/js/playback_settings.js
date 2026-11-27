import { EQ_PRESETS, player } from './player.js';
import { api } from './api.js';
import { state } from './state.js';
import { modal } from './modal.js';
import { formatSeconds } from './utils.js';
import { toast } from './toast.js';

const colors = {
  cinema_white: ['#ffffff', '#e1e1e5'], emerald_flow: ['#67dbab', '#eff7f2'],
  vinyl_gold: ['#e8b76a', '#fff4e2'], light_desktop_ink: ['#181a1f', '#52535a'],
  sakura_mist: ['#ffbad1', '#fff4f4'], game_contrast: ['#7be082', '#ffffff'],
  apple_glass: ['#8ad4ff', '#edf2f7'], neon_duo: ['#48e5ff', '#f2f5ff'],
};
const styleFields = [
  'preset',
  'surface_style',
  'font_size',
  'font_weight',
  'shadow_strength',
  'bg_opacity',
  'align',
  'mode',
  'stroke',
  'show_translation',
  'wheel_volume',
];
const byId = id => (typeof document !== 'undefined' ? document.getElementById(id) : null);
const bind = (id, event, callback) => byId(id)?.addEventListener(event, callback);

export function renderDesktopLyricsPreview(rawStyle = {}) {
  if (typeof document === 'undefined') return;
  const style = rawStyle && typeof rawStyle === 'object' ? rawStyle : {};
  const preset = colors[style.preset] ? style.preset : 'cinema_white';
  const [playedColor, unplayedColor] = colors[preset] || ['#ffffff', '#e1e1e5'];
  const fontSize = Math.max(18, Math.min(48, Math.round(Number(style.font_size) || 28)));
  const fontWeight = Math.max(400, Math.min(800, Math.round(Number(style.font_weight) || 700)));
  const shadowStrength = Math.max(0, Math.min(1.5, Number.isFinite(Number(style.shadow_strength)) ? Number(style.shadow_strength) : 1.0));
  const bgOpacity = Math.max(0, Math.min(0.8, Number.isFinite(Number(style.bg_opacity)) ? Number(style.bg_opacity) : 0));
  const surfaceStyle = ['borderless', 'pill', 'acrylic'].includes(String(style.surface_style || ''))
    ? String(style.surface_style)
    : 'borderless';
  const align = ['left', 'center', 'right'].includes(String(style.align || '')) ? String(style.align) : 'center';
  const mode = style.mode === 'single' ? 'single' : 'double';
  const stroke = Boolean(style.stroke);
  const showTranslation = style.show_translation !== false;

  const fontValEl = byId('settings-font_size-val');
  if (fontValEl) fontValEl.textContent = `${fontSize} px`;
  const shadowValEl = byId('settings-shadow_strength-val');
  if (shadowValEl) shadowValEl.textContent = `${shadowStrength.toFixed(1)}×`;
  const bgValEl = byId('settings-bg_opacity-val');
  if (bgValEl) bgValEl.textContent = `${Math.round(bgOpacity * 100)}%`;

  const box = byId('settings-lyrics-preview-box');
  const lineEl = byId('settings-lyrics-preview-line');
  const nextEl = byId('settings-lyrics-preview-next');
  const transEl = byId('settings-lyrics-preview-trans');
  if (!box) return;

  box.dataset.surface = surfaceStyle;
  box.dataset.preset = preset;
  box.className = `lyrics-preview-box align-${align}`;

  const isLightInk = preset === 'light_desktop_ink';
  if (surfaceStyle === 'pill') {
    const a = isLightInk ? Math.max(0.76, bgOpacity) : Math.max(0.62, bgOpacity);
    box.style.backgroundColor = isLightInk ? `rgba(250, 250, 248, ${a})` : `rgba(16, 18, 24, ${a})`;
    box.style.borderColor = isLightInk ? 'rgba(24, 26, 31, 0.16)' : 'rgba(255, 255, 255, 0.16)';
    box.style.boxShadow = isLightInk ? '0 4px 16px rgba(0, 0, 0, 0.12)' : '0 8px 20px rgba(0, 0, 0, 0.35)';
    box.style.borderRadius = '14px';
  } else if (surfaceStyle === 'acrylic') {
    const a = isLightInk ? Math.max(0.34, bgOpacity) : Math.max(0.28, bgOpacity);
    box.style.backgroundColor = isLightInk ? `rgba(248, 248, 246, ${a})` : `rgba(18, 20, 26, ${a})`;
    box.style.borderColor = isLightInk ? 'rgba(24, 26, 31, 0.14)' : 'rgba(255, 255, 255, 0.14)';
    box.style.boxShadow = 'none';
    box.style.borderRadius = '10px';
  } else {
    box.style.backgroundColor = bgOpacity > 0 ? `rgba(16, 17, 22, ${(bgOpacity * 0.85).toFixed(2)})` : 'transparent';
    box.style.borderColor = 'transparent';
    box.style.boxShadow = 'none';
    box.style.borderRadius = '8px';
  }

  const shadowAlpha = (0.65 * shadowStrength).toFixed(2);
  const shadowCss = shadowStrength > 0
    ? `0 2px ${(4 + 6 * shadowStrength).toFixed(1)}px rgba(0, 0, 0, ${shadowAlpha})`
    : 'none';
  const strokeCss = stroke ? '1px rgba(10, 12, 16, 0.85)' : '0px transparent';

  if (lineEl) {
    lineEl.style.fontSize = `${Math.max(16, Math.min(34, Math.round(fontSize * 0.85)))}px`;
    lineEl.style.fontWeight = String(fontWeight);
    lineEl.style.color = style.played_color || playedColor;
    lineEl.style.textShadow = shadowCss;
    lineEl.style.webkitTextStroke = strokeCss;
  }
  if (nextEl) {
    nextEl.hidden = mode === 'single';
    nextEl.style.color = style.unplayed_color || unplayedColor;
    nextEl.style.fontWeight = String(Math.max(400, fontWeight - 100));
    nextEl.style.textShadow = shadowCss;
  }
  if (transEl) {
    transEl.hidden = !showTranslation;
    transEl.style.color = style.unplayed_color || unplayedColor;
    transEl.style.textShadow = shadowCss;
  }
}

function render() {
  const cfg = state.get('settings') || {};
  if (byId('settings-cover-tilt')) byId('settings-cover-tilt').checked = cfg.enable_3d_effects !== false;
  const style = cfg.desktop_lyrics_style || {};
  for (const key of styleFields) {
    const el = byId('settings-' + key);
    if (el && key in style) {
      if (el.type === 'checkbox') el.checked = Boolean(style[key]);
      else el.value = String(style[key]);
    }
  }
  renderDesktopLyricsPreview(style);
  document.querySelectorAll('[data-setting]').forEach(el => {
    el.setAttribute('aria-pressed', String(el.value === String(cfg[el.dataset.setting])));
  });
  for (const prefix of ['settings', 'player']) {
    if (byId(prefix + '-rate')) byId(prefix + '-rate').value = String(player.playbackRate);
    if (byId(prefix + '-sleep')) byId(prefix + '-sleep').value = player.sleepMode;
  }
  renderPlaybackStatus();
  if (byId('eq-enabled')) byId('eq-enabled').checked = Boolean(cfg.eq_enabled);
  if (byId('eq-preset')) byId('eq-preset').value = cfg.eq_preset || 'flat';
  document.querySelectorAll('[data-eq-preset]').forEach(btn => {
    btn.setAttribute('aria-pressed', String(btn.dataset.eqPreset === (cfg.eq_preset || 'flat')));
  });
  if (byId('settings-output')) byId('settings-output').value = cfg.audio_output_device_id || 'default';
  document.querySelectorAll('[data-eq-band]').forEach(el => {
    const bands = cfg.eq_bands || EQ_PRESETS[cfg.eq_preset] || EQ_PRESETS.flat;
    el.value = bands[Number(el.dataset.eqBand)] ?? 0;
    if (el.nextElementSibling) el.nextElementSibling.textContent = `${Number(el.value) > 0 ? '+' : ''}${el.value} dB`;
  });
}

function renderPlaybackStatus() {
  const rate = byId('player-rate-badge');
  const sleep = byId('player-sleep-badge');
  const eq = byId('player-eq-badge');
  if (rate) {
    rate.hidden = player.playbackRate === 1;
    rate.textContent = player.playbackRate + '×';
  }
  if (sleep) {
    sleep.hidden = !player.sleepMode || player.sleepMode === 'off';
    sleep.textContent = player.sleepMode === 'track' ? '曲终' : formatSeconds(state.get('sleepRemaining'));
  }
  if (eq) {
    eq.hidden = !state.get('settings')?.eq_enabled;
    eq.textContent = state.get('audioBypass') ? 'EQ 旁路' : 'EQ';
  }
  const status = byId('eq-status');
  if (status) status.textContent = !state.get('settings')?.eq_enabled ? '已关闭'
    : state.get('audioBypass') ? '直连音源 · 音效旁路' : '音效已启用';
  const countdown = byId('player-sleep-countdown');
  if (countdown) {
    countdown.hidden = !state.get('sleepRemaining');
    countdown.textContent = formatSeconds(state.get('sleepRemaining'));
  }
}

async function loadDevices() {
  const select = byId('settings-output');
  if (!select) return;
  const supported = player.directAudio?.setSinkId && navigator.mediaDevices?.enumerateDevices;
  select.disabled = !supported;
  const hint = byId('settings-output-hint');
  if (!supported) {
    if (hint) hint.textContent = '当前运行环境使用系统默认音频设备';
    return;
  }
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    select.options.length = 1;
    devices.filter(d => d.kind === 'audiooutput' && d.deviceId && d.deviceId !== 'default')
      .forEach((d, i) => select.add(new Option(d.label || `音频设备 ${i + 1}`, d.deviceId)));
    select.value = state.get('settings')?.audio_output_device_id || 'default';
  } catch {
    if (hint) hint.textContent = '无法读取输出设备，当前使用系统默认设备';
  }
}

export function initPlaybackSettings(saveSettings) {
  const save = patch => saveSettings(patch);
  bind('settings-cover-tilt', 'change', e => save({enable_3d_effects: e.target.checked}));
  bind('stage-theme', 'change', e => save({stage_theme: e.target.value}));
  bind('stage-spectrum-mode', 'change', e => save({visualizer_mode: e.target.value}));
  document.querySelectorAll('[data-setting]').forEach(el => el.addEventListener('click', () => {
    save({ [el.dataset.setting]: el.dataset.setting === 'minimize_to_tray' ? el.value === 'true' : el.value });
  }));
  const readStyleValue = (key, target) => {
    if (target.type === 'checkbox') return Boolean(target.checked);
    if (key === 'font_weight' || target.type === 'range') return Number(target.value);
    return target.value;
  };
  for (const key of styleFields) {
    bind('settings-' + key, 'change', ({ target }) => {
      const style = { ...state.get('settings')?.desktop_lyrics_style };
      style[key] = readStyleValue(key, target);
      if (key === 'preset' && colors[target.value]) {
        [style.played_color, style.unplayed_color] = colors[target.value];
      }
      renderDesktopLyricsPreview(style);
      save({ desktop_lyrics_style: style });
    });
  }
  for (const key of ['font_size', 'shadow_strength', 'bg_opacity']) {
    bind('settings-' + key, 'input', ({ target }) => {
      const style = { ...state.get('settings')?.desktop_lyrics_style };
      style[key] = Number(target.value);
      renderDesktopLyricsPreview(style);
    });
  }
  document.querySelectorAll('[data-preview-backdrop]').forEach(btn => {
    btn.addEventListener('click', () => {
      const mode = btn.dataset.previewBackdrop || 'dark';
      const stage = byId('settings-lyrics-preview-stage');
      if (stage) stage.dataset.backdrop = mode;
      document.querySelectorAll('[data-preview-backdrop]').forEach(b => {
        const active = b.dataset.previewBackdrop === mode;
        b.classList.toggle('active', active);
        b.setAttribute('aria-pressed', String(active));
      });
    });
  });
  bind('settings-lyrics-center-btn', 'click', async () => {
    const res = await api.centerDesktopLyricsWindow();
    if (res.ok) toast.quiet('桌面歌词已水平居中');
    else toast.warning('桌面歌词暂未打开', res.error?.message || '请先打开桌面歌词浮窗。');
  });

  bind('settings-eq-btn', 'click', () => modal.open('#eq-modal'));
  for (const prefix of ['settings', 'player']) {
    bind(prefix + '-rate', 'change', e => player.setPlaybackRate(e.target.value));
    bind(prefix + '-sleep', 'change', e => player.setSleepTimer(e.target.value));
  }
  bind('eq-enabled', 'change', e => save({ eq_enabled: e.target.checked }));
  const applyPreset = name => save({ eq_enabled: true, eq_preset: name, eq_bands: EQ_PRESETS[name] });
  bind('eq-preset', 'change', e => applyPreset(e.target.value));
  bind('eq-reset', 'click', () => applyPreset('flat'));
  document.querySelectorAll('[data-eq-preset]').forEach(btn => {
    btn.addEventListener('click', () => applyPreset(btn.dataset.eqPreset));
  });
  document.querySelectorAll('[data-eq-band]').forEach(el => {
    const getBands = () => {
      const cfg = state.get('settings') || {};
      const bands = [...(cfg.eq_bands || EQ_PRESETS[cfg.eq_preset] || EQ_PRESETS.flat)];
      bands[Number(el.dataset.eqBand)] = Number(el.value);
      return bands;
    };
    el.addEventListener('input', () => {
      player.applyEqualizerSettings({ eq_enabled: true, eq_bands: getBands() });
      el.nextElementSibling.textContent = `${Number(el.value) > 0 ? '+' : ''}${el.value} dB`;
    });
    el.addEventListener('change', () => save({ eq_enabled: true, eq_bands: getBands(), eq_preset: 'custom' }));
  });
  bind('settings-output', 'change', async e => {
    const id = e.target.value;
    try { await player.setOutputDevice(id); await save({ audio_output_device_id: id }); }
    catch { toast.quiet('无法切换到所选输出设备'); render(); }
  });
  loadDevices();
  globalThis.navigator?.mediaDevices?.addEventListener?.('devicechange', loadDevices);
  bind('settings-pick-local-dir-btn', 'click', async () => {
    const res = await api.pickLocalFolder();
    if (res.data?.path) await save({ local_music_dirs: [...new Set([...(state.get('settings')?.local_music_dirs || []), res.data.path])] });
    else if (!res.ok || res.data?.supported === false) toast.quiet('请将音乐文件夹的完整路径粘贴到输入框');
  });
  if (byId('settings-version')) api.health().then(res => {
    byId('settings-version').textContent = res.data?.version ? `版本 ${res.data.version}` : '版本信息暂不可用';
  });
  bind('settings-check-update', 'click', checkForUpdate);
  state.on('change:sleepRemaining', renderPlaybackStatus);
  state.on('change:audioBypass', renderPlaybackStatus);
  for (const key of ['settings', 'playbackRate', 'sleepMode']) state.on('change:' + key, render);
  render();
}

export async function checkForUpdate() {
  const el = byId('settings-update-result');
  el.textContent = '正在检查更新…';
  const res = await api.checkAppUpdate();
  el.textContent = res.ok && res.data?.message ? res.data.message : '当前无法检查更新';
  if (res.ok && res.data?.status === 'ok' && res.data.has_update && res.data.release_url) {
    const link = document.createElement('a');
    link.href = res.data.release_url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = '查看发行说明';
    el.append(' ', link);
  }
}
