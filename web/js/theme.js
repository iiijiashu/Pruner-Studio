/**
 * Pruner Studio - Theme & Appearance Engine
 * Applies the selected theme, accent presets/custom/cover mode,
 * layout density (comfortable | compact), and font scale (0.9..1.25) synchronously from
 * localStorage ('pruner_appearance') at import time to eliminate FOUC, while syncing with
 * backend SettingsStore `appearance` without ever overriding Keep (#62a368) or Prune (#df584c).
 */
import { state } from './state.js';
import { hslToRgb } from './utils.js';

export const APPEARANCE_STORAGE_KEY = 'pruner_appearance';

export const THEME_OPTIONS = [
  { id: 'dark', label: '曜石深灰', bg: '#111215', surface: '#18191e', text: '#f2f1ed' },
  { id: 'midnight', label: 'OLED 纯黑', bg: '#000000', surface: '#0d0d10', text: '#f2f1ed' },
  { id: 'light', label: '纸感浅白', bg: '#f5f3ee', surface: '#fdfcfa', text: '#1b1a18' },
  { id: 'celadon', label: '晨雾青瓷', bg: '#ebf0ee', surface: '#f7faf9', text: '#172420' },
  { id: 'parchment', label: '暖阳羊皮', bg: '#f3ede2', surface: '#fbf8f2', text: '#241e17' },
  { id: 'nordic', label: '深海雾蓝', bg: '#111822', surface: '#19222e', text: '#f2f1ed' },
  { id: 'espresso', label: '暖咖黑胶', bg: '#181412', surface: '#201a17', text: '#f2f1ed' },
  { id: 'emerald', label: '松林暗夜', bg: '#101714', surface: '#19231e', text: '#f2f1ed' },
  { id: 'dynamic', label: '专辑流光', bg: '#131518', surface: '#1b1d20', text: '#f2f1ed' },
  { id: 'auto', label: '跟随系统', bg: '#18191e', surface: '#f5f3ee', text: '#f2f1ed' },
];

export const ACCENT_PRESETS = [
  { id: 'sage', label: '鼠尾草', hex: '#62a368' },
  { id: 'mist', label: '雾蓝', hex: '#6b8fa8' },
  { id: 'lavender', label: '薰衣草', hex: '#a896c2' },
  { id: 'amber', label: '琥珀', hex: '#d69e4e' },
  { id: 'coral', label: '珊瑚', hex: '#e07a5f' },
  { id: 'rose', label: '玫瑰', hex: '#c86b85' },
];

export const DENSITY_OPTIONS = [
  { id: 'comfortable', label: '舒适' },
  { id: 'compact', label: '紧凑' },
];

export const FONT_SCALE_OPTIONS = [
  { value: 0.9, label: '90%' },
  { value: 1.0, label: '100%' },
  { value: 1.1, label: '110%' },
  { value: 1.25, label: '125%' },
];

const VALID_THEMES = new Set(THEME_OPTIONS.map(option => option.id));
const VALID_ACCENT_MODES = new Set(['preset', 'custom', 'cover']);
const VALID_DENSITIES = new Set(['comfortable', 'compact']);
const DEFAULT_ACCENT = '#62a368';

export function getDefaultAppearance() {
  return {
    theme: 'dark',
    accent_mode: 'preset',
    accent: DEFAULT_ACCENT,
    density: 'comfortable',
    font_scale: 1.0,
  };
}

export function normalizeHexColor(raw, fallback = DEFAULT_ACCENT) {
  if (typeof raw !== 'string') return fallback;
  const trimmed = raw.trim().toLowerCase();
  if (/^#[0-9a-f]{6}$/.test(trimmed)) return trimmed;
  if (/^#[0-9a-f]{3}$/.test(trimmed)) {
    return `#${trimmed[1]}${trimmed[1]}${trimmed[2]}${trimmed[2]}${trimmed[3]}${trimmed[3]}`;
  }
  return fallback;
}

export function normalizeAppearance(raw = null) {
  const defaults = getDefaultAppearance();
  if (!raw || typeof raw !== 'object') return defaults;

  const theme = VALID_THEMES.has(String(raw.theme || '').toLowerCase())
    ? String(raw.theme).toLowerCase()
    : defaults.theme;

  const accent_mode = VALID_ACCENT_MODES.has(String(raw.accent_mode || '').toLowerCase())
    ? String(raw.accent_mode).toLowerCase()
    : defaults.accent_mode;

  const accent = normalizeHexColor(raw.accent, defaults.accent);

  const density = VALID_DENSITIES.has(String(raw.density || '').toLowerCase())
    ? String(raw.density).toLowerCase()
    : defaults.density;

  const parsedScale = Number(raw.font_scale);
  let font_scale = defaults.font_scale;
  if (Number.isFinite(parsedScale)) {
    font_scale = Math.max(0.9, Math.min(1.25, Math.round(parsedScale * 100) / 100));
  }

  return {
    theme,
    accent_mode,
    accent,
    density,
    font_scale,
  };
}

export function parseColorToRgb(colorStr) {
  if (typeof colorStr !== 'string') return [98, 163, 104];
  const s = colorStr.trim();
  const hexMatch = /^#([0-9a-f]{6})$/i.exec(s);
  if (hexMatch) {
    const val = parseInt(hexMatch[1], 16);
    return [(val >> 16) & 255, (val >> 8) & 255, val & 255];
  }
  const shortHex = /^#([0-9a-f]{3})$/i.exec(s);
  if (shortHex) {
    const h = shortHex[1];
    return [
      parseInt(h[0] + h[0], 16),
      parseInt(h[1] + h[1], 16),
      parseInt(h[2] + h[2], 16),
    ];
  }
  const rgbMatch = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i.exec(s);
  if (rgbMatch) {
    return [
      Math.max(0, Math.min(255, Number(rgbMatch[1]))),
      Math.max(0, Math.min(255, Number(rgbMatch[2]))),
      Math.max(0, Math.min(255, Number(rgbMatch[3]))),
    ];
  }
  const hslMatch = /^hsla?\(\s*([\d.]+)\s*,\s*([\d.]+)%\s*,\s*([\d.]+)%/i.exec(s);
  if (hslMatch) {
    return hslToRgb(Number(hslMatch[1]), Number(hslMatch[2]), Number(hslMatch[3]));
  }
  return [98, 163, 104];
}

export function relativeLuminance(rgb) {
  const toLin = (c) => {
    const n = c / 255;
    return n <= 0.03928 ? n / 12.92 : ((n + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * toLin(rgb[0]) + 0.7152 * toLin(rgb[1]) + 0.0722 * toLin(rgb[2]);
}

export function computeContrastRatio(fgColor, bgColor) {
  const l1 = relativeLuminance(parseColorToRgb(fgColor));
  const l2 = relativeLuminance(parseColorToRgb(bgColor));
  const hi = Math.max(l1, l2);
  const lo = Math.min(l1, l2);
  return (hi + 0.05) / (lo + 0.05);
}

export function computeContrastColor(colorStr) {
  const darkInk = '#111214';
  const lightInk = '#ffffff';
  const cDark = computeContrastRatio(darkInk, colorStr);
  const cLight = computeContrastRatio(lightInk, colorStr);
  return cDark >= cLight ? darkInk : lightInk;
}

const THEME_SURFACE_SAMPLES = {
  dark: ['#111215', '#141519', '#15161b', '#16171c', '#18191e', '#1d1f25', '#23252d'],
  midnight: ['#000000', '#08080a', '#0a0a0d', '#0b0b0e', '#0d0d10', '#141418', '#1c1c22'],
  light: ['#fdfcfa', '#fcfbf8', '#f5f3ee', '#efede7', '#eceae3', '#e8e5dd', '#e2dfd6'],
  celadon: ['#f7faf9', '#f5f9f7', '#ebf0ee', '#e4ebe8', '#e1e9e6', '#dce5e1', '#d3dfda'],
  parchment: ['#fbf8f2', '#faf6ee', '#f3ede2', '#efe8db', '#ede5d7', '#eae1d1', '#e5dac7'],
  nordic: ['#111822', '#141c27', '#19222e', '#222d36', '#23252d', '#28333c'],
  espresso: ['#181412', '#1c1714', '#201a17', '#23252d', '#2b231f', '#312925'],
  emerald: ['#101714', '#141d19', '#19231e', '#222d25', '#23252d', '#28332b'],
  dynamic: ['#16171c', '#202228', '#272727'],
};

const rgbToHex = rgb => '#' + rgb.map(v => Math.max(0, Math.min(255, v)).toString(16).padStart(2, '0')).join('');

const blendOverSurface = (fgRgb, alpha, bgHex) => {
  const bgRgb = parseColorToRgb(bgHex);
  return rgbToHex(fgRgb.map((c, idx) => Math.round(c * alpha + bgRgb[idx] * (1 - alpha))));
};

export function computeReadableAccentText(accentColor, resolvedTheme = 'dark') {
  const baseRgb = parseColorToRgb(accentColor || DEFAULT_ACCENT);
  const themeKey = VALID_THEMES.has(resolvedTheme) ? resolvedTheme : 'dark';
  const surfaces = THEME_SURFACE_SAMPLES[themeKey] || THEME_SURFACE_SAMPLES.dark;
  const checkSurfaces = [
    ...surfaces,
    ...surfaces.map(bg => blendOverSurface(baseRgb, 0.16, bg)),
    ...surfaces.map(bg => blendOverSurface(baseRgb, 0.2, bg)),
  ];
  const isLight = themeKey === 'light' || themeKey === 'celadon' || themeKey === 'parchment';
  const targetRgb = isLight ? [16, 20, 18] : [245, 245, 247];
  const minRatio = hex => Math.min(...checkSurfaces.map(bg => computeContrastRatio(hex, bg)));
  const baseHex = rgbToHex(baseRgb);
  if (minRatio(baseHex) >= 4.6) {
    return baseHex;
  }
  for (let step = 1; step <= 40; step += 1) {
    const t = step / 40;
    const candidate = rgbToHex(baseRgb.map((c, idx) => Math.round(c * (1 - t) + targetRgb[idx] * t)));
    if (minRatio(candidate) >= 4.6) {
      return candidate;
    }
  }
  return isLight ? '#181715' : '#f5f5f7';
}

export function resolveTheme(themePref) {
  if (themePref === 'auto') {
    if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
      try {
        return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
      } catch {
        return 'dark';
      }
    }
    return 'dark';
  }
  return VALID_THEMES.has(themePref) ? themePref : 'dark';
}

export function applyAppearance(appearance, { coverColor = null } = {}) {
  const normalized = normalizeAppearance(appearance);
  const resolvedTheme = resolveTheme(normalized.theme);

  if (typeof document !== 'undefined' && document.documentElement) {
    const root = document.documentElement;
    if (root.dataset) {
      root.dataset.theme = resolvedTheme;
      root.dataset.themeMode = normalized.theme;
      root.dataset.density = normalized.density;
    }
    if (root.style && typeof root.style.setProperty === 'function') {
      root.style.setProperty('--font-scale', String(normalized.font_scale));
      const activeCover = coverColor || state.get('coverAccent');
      if (resolvedTheme === 'dynamic') {
        const rgb = parseColorToRgb(activeCover || DEFAULT_ACCENT);
        // The brightest possible surface is rgb(39,39,39).
        [12, 16, 21].forEach((base, i) => {
          const channels = rgb.map(c => base + Math.round(c * 0.07));
          root.style.setProperty('--album-' + i, 'rgb(' + channels.join(', ') + ')');
        });
      }
      const effectiveAccent = normalized.accent_mode === 'cover' && activeCover
        ? String(activeCover)
        : normalized.accent;
      const [r, g, b] = parseColorToRgb(effectiveAccent);
      root.style.setProperty('--accent', effectiveAccent);
      root.style.setProperty('--accent-subtle', `rgba(${r}, ${g}, ${b}, 0.16)`);
      root.style.setProperty('--accent-contrast', computeContrastColor(effectiveAccent));
      root.style.setProperty('--accent-text', computeReadableAccentText(effectiveAccent, resolvedTheme));
    }
  }

  return { ...normalized, resolvedTheme };
}

function readStoredAppearance() {
  try {
    if (typeof localStorage === 'undefined') return null;
    const raw = localStorage.getItem(APPEARANCE_STORAGE_KEY);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function writeStoredAppearance(appearance) {
  try {
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(APPEARANCE_STORAGE_KEY, JSON.stringify(appearance));
    }
  } catch {}
}

class ThemeManager {
  constructor() {
    this.appearance = getDefaultAppearance();
    this._listenersBound = false;
    this._mediaQuery = null;
  }

  bindSystemAndStateListeners() {
    if (this._listenersBound) return;
    this._listenersBound = true;

    if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
      try {
        this._mediaQuery = window.matchMedia('(prefers-color-scheme: light)');
        const onMediaChange = () => {
          if (this.appearance.theme === 'auto') {
            applyAppearance(this.appearance);
          }
        };
        if (typeof this._mediaQuery.addEventListener === 'function') {
          this._mediaQuery.addEventListener('change', onMediaChange);
        } else if (typeof this._mediaQuery.addListener === 'function') {
          this._mediaQuery.addListener(onMediaChange);
        }
      } catch {}
    }

    if (state && typeof state.on === 'function') {
      state.on('change:coverAccent', (coverColor) => {
        if (this.appearance.accent_mode === 'cover' || this.appearance.theme === 'dynamic') {
          applyAppearance(this.appearance, { coverColor });
        }
      });
    }
  }

  initFromStorage() {
    this.bindSystemAndStateListeners();
    const stored = readStoredAppearance();
    this.appearance = normalizeAppearance(stored);
    return applyAppearance(this.appearance);
  }

  getAppearance() {
    return { ...this.appearance };
  }

  setAppearance(partial, { persistLocal = true } = {}) {
    this.appearance = normalizeAppearance({ ...this.appearance, ...(partial || {}) });
    if (persistLocal) {
      writeStoredAppearance(this.appearance);
    }
    return applyAppearance(this.appearance);
  }

  syncFromBackendSettings(backendAppearance) {
    if (!backendAppearance || typeof backendAppearance !== 'object') return this.getAppearance();
    return this.setAppearance(backendAppearance, { persistLocal: true });
  }
}

export const themeManager = new ThemeManager();

// Synchronously apply stored appearance on import to eliminate FOUC.
themeManager.initFromStorage();
