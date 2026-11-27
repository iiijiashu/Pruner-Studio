/**
 * Pruner Studio - Utility Functions
 * Safe DOM creation, URL validation, discrete cover resizing, real cover palette extraction,
 * ms/sec normalization, manifest hashing, formatters, and helpers.
 */

const ALLOWED_COVER_SIZES = [64, 96, 192, 384];
const NETEASE_IMAGE_HOST_RE = /^https?:\/\/[a-z0-9.-]*126\.net\//i;
const PALETTE_CACHE = new Map();
const MAX_PALETTE_CACHE = 48;

/**
 * Validates whether a URL is safe to use in href or src.
 * Restricts to http, https, data:image/, or relative paths starting with '/'.
 * @param {string} url
 * @returns {boolean}
 */
export function isSafeUrl(url) {
  if (!url || typeof url !== 'string') return false;
  const trimmed = url.trim();
  if (trimmed.startsWith('https://') || trimmed.startsWith('http://')) {
    return true;
  }
  if (trimmed.startsWith('data:image/')) {
    return true;
  }
  if (trimmed.startsWith('/') && !trimmed.startsWith('//')) {
    return true;
  }
  return false;
}

/**
 * Safely sanitizes and returns an image URL, or returns a fallback quiet placeholder SVG data URI.
 * @param {string} url
 * @param {string} fallbackText
 * @returns {string}
 */
export function getSafeImageUrl(url, fallbackText = '') {
  if (isSafeUrl(url)) {
    return url;
  }
  // Minimalist warm-paper vinyl disc placeholder SVG (harmonizes across light & dark themes)
  const labelChar = String(fallbackText || '').trim().slice(0, 1);
  const safeAttr = encodeURIComponent(labelChar || 'vinyl');
  return `data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160" viewBox="0 0 160 160" data-fallback="${safeAttr}"><defs><linearGradient id="sleeve" x1="0" y1="0" x2="1" y2="1"><stop offset="0%25" stop-color="%23ede6da"/><stop offset="100%25" stop-color="%23dfd6c6"/></linearGradient><radialGradient id="disc" cx="42%25" cy="36%25" r="68%25"><stop offset="0%25" stop-color="%233c3834"/><stop offset="55%25" stop-color="%23262320"/><stop offset="100%25" stop-color="%231c1a18"/></radialGradient></defs><rect width="160" height="160" fill="url(%23sleeve)"/><rect x="1" y="1" width="158" height="158" fill="none" stroke="%23241e17" stroke-opacity="0.08" stroke-width="1"/><circle cx="80" cy="80" r="56" fill="url(%23disc)"/><circle cx="80" cy="80" r="48" fill="none" stroke="%23f5efe6" stroke-opacity="0.1" stroke-width="0.9"/><circle cx="80" cy="80" r="39" fill="none" stroke="%23f5efe6" stroke-opacity="0.08" stroke-width="0.9"/><circle cx="80" cy="80" r="30" fill="none" stroke="%23f5efe6" stroke-opacity="0.07" stroke-width="0.8"/><path d="M40 44 A54 54 0 0 1 116 40" fill="none" stroke="%23ffffff" stroke-opacity="0.14" stroke-width="3" stroke-linecap="round"/><circle cx="80" cy="80" r="19" fill="%236d8d72"/><circle cx="80" cy="80" r="15" fill="none" stroke="%23f8f4ec" stroke-opacity="0.35" stroke-width="0.8"/><circle cx="80" cy="80" r="4.2" fill="%23efe9de"/></svg>`;
}

/**
 * Clamps cover size to discrete memory-safe buckets (64, 96, 192, 384) and appends
 * `?param={size}y{size}` for NetEase 126.net URLs so large full-res bitmaps are never decoded.
 * @param {string} url
 * @param {number} [size=192]
 * @param {string} [fallbackText='音']
 * @returns {string}
 */
export function getSizedCoverUrl(url, size = 192, fallbackText = '音') {
  if (!isSafeUrl(url)) {
    return getSafeImageUrl('', fallbackText);
  }
  const trimmed = url.trim();
  if (!NETEASE_IMAGE_HOST_RE.test(trimmed)) {
    return trimmed;
  }

  let clamped = 192;
  const numeric = Number(size) || 192;
  for (const bucket of ALLOWED_COVER_SIZES) {
    if (numeric <= bucket) {
      clamped = bucket;
      break;
    }
    clamped = bucket;
  }

  const httpsUrl = trimmed.replace(/^http:\/\//i, 'https://');
  try {
    const parsed = new URL(httpsUrl);
    parsed.searchParams.set('param', `${clamped}y${clamped}`);
    return parsed.toString();
  } catch {
    const base = httpsUrl.split('?')[0];
    return `${base}?param=${clamped}y${clamped}`;
  }
}

/**
 * Explicitly normalizes a timestamp or duration that may arrive in milliseconds or seconds into seconds.
 * @param {number|string|null|undefined} value
 * @param {number} [trackDurationSec=0]
 * @returns {number}
 */
export function normalizeTimeToSeconds(value, trackDurationSec = 0) {
  if (value === null || value === undefined || value === '') return 0;
  const num = Number(value);
  if (!Number.isFinite(num) || num <= 0) return 0;

  if (num > 10000) {
    return num / 1000;
  }
  const durSec = trackDurationSec > 10000 ? trackDurationSec / 1000 : Number(trackDurationSec) || 0;
  if (durSec > 0 && num > durSec * 1.1) {
    return num / 1000;
  }
  if (durSec <= 0 && num > 1000) {
    return num / 1000;
  }
  return num;
}

/** Read an explicit millisecond duration first, then normalize older mixed-unit song data. */
export function durationSeconds(track) {
  const ms = Number(track?.duration_ms);
  if (Number.isFinite(ms) && ms > 0) return ms / 1000;
  const value = Number(track?.duration ?? track);
  if (!Number.isFinite(value) || value <= 0) return 0;
  if (track?.duration_unit === 'sec') return value;
  const prov = String(track?.provider || '').trim().toLowerCase();
  const src = String(track?.source || '').trim().toLowerCase();
  if (track?.duration_unit === 'ms' || track?.local_path || ['netease', 'local'].includes(prov) ||
      ['netease', 'local'].includes(src) || /^(local|netease):/.test(track?.track_key || '') ||
      track?.fee !== undefined || track?.privilege_st !== undefined || track?.is_cloud !== undefined) {
    return value / 1000;
  }
  // Unlabelled legacy inputs may contain either seconds or milliseconds.
  return value > 3600 ? value / 1000 : value;
}

export function trackDurationSummary(tracks) {
  let known = 0;
  let unknown = 0;
  for (const track of tracks) {
    const seconds = durationSeconds(track);
    if (seconds > 0) known += seconds;
    else unknown++;
  }
  const total = Math.floor(known);
  return `${tracks.length} 首 · ${Math.floor(total / 60)} 分 ${total % 60} 秒` +
    (unknown ? `（另 ${unknown} 首时长未知）` : '');
}

export function formatDuration(track) {
  return formatSeconds(durationSeconds(track));
}

export function formatSeconds(value) {
  const sec = Number.isFinite(Number(value)) ? Math.max(0, Math.floor(value)) : 0;
  const mins = Math.floor(sec / 60);
  const remainingSecs = sec % 60;
  return `${mins}:${remainingSecs.toString().padStart(2, '0')}`;
}

/**
 * Helper to safely construct DOM elements without innerHTML vulnerability.
 * @param {string} tag
 * @param {Object} [props]
 * @param {Array<HTMLElement|string|number>} [children]
 * @returns {HTMLElement}
 */
export function createElement(tag, props = {}, children = []) {
  const resolvedChildren = Array.isArray(props.children) ? props.children : children;
  if (typeof document === 'undefined') {
    return {
      tagName: tag.toUpperCase(),
      className: props.className || '',
      textContent: props.text || '',
      children: resolvedChildren,
      appendChild: () => {},
      addEventListener: () => {},
      setAttribute: () => {},
      classList: { add: () => {}, remove: () => {}, toggle: () => {}, contains: () => false },
      dataset: props.dataset || {},
    };
  }

  const element = document.createElement(tag);

  if (props.className) {
    element.className = props.className;
  }
  if (props.style) {
    element.setAttribute('style', String(props.style));
  }
  if (props.text !== undefined && props.text !== null) {
    element.textContent = String(props.text);
  }
  if (props.attributes) {
    for (const [key, value] of Object.entries(props.attributes)) {
      if (value !== null && value !== undefined) {
        element.setAttribute(key, String(value));
      }
    }
  }
  if (props.dataset) {
    for (const [key, value] of Object.entries(props.dataset)) {
      element.dataset[key] = String(value);
    }
  }
  if (props.events) {
    for (const [evtName, handler] of Object.entries(props.events)) {
      element.addEventListener(evtName, handler);
    }
  }

  if (Array.isArray(resolvedChildren)) {
    for (const child of resolvedChildren) {
      if (child === null || child === undefined) continue;
      if (typeof child === 'string' || typeof child === 'number') {
        element.appendChild(document.createTextNode(String(child)));
      } else if (typeof Node !== 'undefined' && child instanceof Node) {
        element.appendChild(child);
      } else if (typeof child === 'object' && child.tagName) {
        element.appendChild(child);
      }
    }
  }

  return element;
}

/**
 * Standard debounce function.
 * @param {Function} func
 * @param {number} wait
 * @returns {Function}
 */
export function debounce(func, wait = 250) {
  let timeout;
  return function (...args) {
    clearTimeout(timeout);
    timeout = setTimeout(() => func.apply(this, args), wait);
  };
}

/**
 * Normalizes artist list or string into a single clean string.
 * @param {string|Array<string|{name: string}>} artists
 * @returns {string}
 */
export function formatArtists(artists) {
  if (!artists) return '未知歌手';
  if (Array.isArray(artists)) {
    return artists.map(a => (typeof a === 'string' ? a : a?.name || '')).filter(Boolean).join(' / ') || '未知歌手';
  }
  return String(artists);
}

/**
 * Deep clones a simple JSON-compatible object.
 * @template T
 * @param {T} obj
 * @returns {T}
 */
export function deepClone(obj) {
  return JSON.parse(JSON.stringify(obj));
}

/**
 * Estimates remaining minutes based on remaining count and average review time (~3.5s per song).
 * @param {number} remainingCount
 * @returns {string}
 */
export function estimateRemainingTime(remainingCount) {
  if (!remainingCount || remainingCount <= 0) return '约 0 分钟';
  const totalSeconds = Math.round(remainingCount * 3.5);
  const minutes = Math.ceil(totalSeconds / 60);
  if (minutes < 60) {
    return `约 ${minutes} 分钟`;
  }
  const hours = (minutes / 60).toFixed(1);
  return `约 ${hours} 小时`;
}

/**
 * Converts RGB [0..255] to HSL { h: 0..360, s: 0..100, l: 0..100 }.
 * @param {number} r
 * @param {number} g
 * @param {number} b
 * @returns {{ h: number, s: number, l: number }}
 */
export function rgbToHsl(r, g, b) {
  const rn = r / 255;
  const gn = g / 255;
  const bn = b / 255;
  const max = Math.max(rn, gn, bn);
  const min = Math.min(rn, gn, bn);
  const delta = max - min;
  const l = (max + min) / 2;

  let h = 0;
  let s = 0;
  if (delta !== 0) {
    s = delta / (1 - Math.abs(2 * l - 1));
    if (max === rn) {
      h = 60 * (((gn - bn) / delta) % 6);
    } else if (max === gn) {
      h = 60 * ((bn - rn) / delta + 2);
    } else {
      h = 60 * ((rn - gn) / delta + 4);
    }
  }
  if (h < 0) h += 360;
  return {
    h: Math.round(h),
    s: Math.round(s * 100),
    l: Math.round(l * 100),
  };
}

/**
 * Converts HSL { h: 0..360, s: 0..100, l: 0..100 } to RGB [r, g, b] in 0..255.
 * @param {number} h
 * @param {number} s
 * @param {number} l
 * @returns {[number, number, number]}
 */
export function hslToRgb(h, s, l) {
  const hn = (((h % 360) + 360) % 360) / 360;
  const sn = Math.max(0, Math.min(100, s)) / 100;
  const ln = Math.max(0, Math.min(100, l)) / 100;
  if (sn === 0) {
    const v = Math.round(ln * 255);
    return [v, v, v];
  }
  const hue2rgb = (p, q, t) => {
    let tt = t;
    if (tt < 0) tt += 1;
    if (tt > 1) tt -= 1;
    if (tt < 1 / 6) return p + (q - p) * 6 * tt;
    if (tt < 1 / 2) return q;
    if (tt < 2 / 3) return p + (q - p) * (2 / 3 - tt) * 6;
    return p;
  };
  const q = ln < 0.5 ? ln * (1 + sn) : ln + sn - ln * sn;
  const p = 2 * ln - q;
  return [
    Math.round(hue2rgb(p, q, hn + 1 / 3) * 255),
    Math.round(hue2rgb(p, q, hn) * 255),
    Math.round(hue2rgb(p, q, hn - 1 / 3) * 255),
  ];
}

function normalizeFluidRgb(hsl, hueOffset = 0, lightOffset = 0) {
  const h = (((Math.round(hsl.h) + hueOffset) % 360) + 360) % 360;
  const s = Math.max(32, Math.min(68, Math.round(hsl.s)));
  const l = Math.max(24, Math.min(46, Math.round(hsl.l) + lightOffset));
  return hslToRgb(h, s, l);
}

/**
 * Builds dark-theme readable accent tokens + 4 AMLL fluid mesh RGB colors from primary/secondary/quadrant HSL colors.
 * Never touches Keep (#62a368) or Prune (#df584c) semantic tokens.
 */
export function buildPaletteTokens(primaryHsl, secondaryHsl = null, quadHsls = null) {
  const h1 = ((Math.round(primaryHsl.h) % 360) + 360) % 360;
  const s1 = Math.max(28, Math.min(54, Math.round(primaryHsl.s)));
  const l1 = Math.max(52, Math.min(64, Math.round(primaryHsl.l)));

  const h2 = secondaryHsl
    ? ((Math.round(secondaryHsl.h) % 360) + 360) % 360
    : (h1 + 38) % 360;
  const s2 = secondaryHsl
    ? Math.max(24, Math.min(48, Math.round(secondaryHsl.s)))
    : Math.max(24, s1 - 6);
  const l2 = secondaryHsl
    ? Math.max(46, Math.min(60, Math.round(secondaryHsl.l)))
    : Math.max(46, l1 - 6);

  const fallbackQuads = [
    normalizeFluidRgb({ h: h1, s: s1 + 8, l: 38 }, 0, 0),
    normalizeFluidRgb({ h: h2, s: s2 + 8, l: 34 }, 0, -2),
    normalizeFluidRgb({ h: h1, s: s1 + 4, l: 30 }, 110, -4),
    normalizeFluidRgb({ h: h2, s: s2 + 6, l: 32 }, -55, -2),
  ];

  const fluidColors = Array.isArray(quadHsls) && quadHsls.length === 4
    ? quadHsls.map((qHsl, idx) => (qHsl ? normalizeFluidRgb(qHsl, 0, idx % 2 === 0 ? -4 : -8) : fallbackQuads[idx]))
    : fallbackQuads;

  return {
    accent: `hsl(${h1}, ${s1}%, ${l1}%)`,
    accentSubtle: `hsla(${h1}, ${s1}%, ${l1}%, 0.18)`,
    accent2: `hsl(${h2}, ${s2}%, ${l2}%)`,
    accent2Subtle: `hsla(${h2}, ${s2}%, ${l2}%, 0.16)`,
    colors: fluidColors,
    c1: `rgb(${fluidColors[0].join(', ')})`,
    c2: `rgb(${fluidColors[1].join(', ')})`,
    c3: `rgb(${fluidColors[2].join(', ')})`,
    c4: `rgb(${fluidColors[3].join(', ')})`,
  };
}

/**
 * Generates a deterministic normalized readable accent color for fallback use.
 * Saturation clamped between 28-46%, lightness clamped between 50-62% for quiet elegance.
 * @param {string} [seedStr='']
 * @returns {string}
 */
export function getCoverAccent(seedStr = '') {
  if (!seedStr) return 'hsl(140, 20%, 54%)';
  let hash = 0;
  for (let i = 0; i < seedStr.length; i++) {
    hash = ((hash << 5) - hash) + seedStr.charCodeAt(i);
    hash |= 0;
  }
  const hue = Math.abs(hash) % 360;
  const sat = 28 + (Math.abs(hash >> 3) % 18); // 28% - 46%
  const light = 50 + (Math.abs(hash >> 6) % 12); // 50% - 62%
  return `hsl(${hue}, ${sat}%, ${light}%)`;
}

/**
 * Extracts deterministic fallback palette tokens (including 4 AMLL fluid colors) from a seed string.
 * @param {string} [seedStr='']
 */
export function getFallbackPalette(seedStr = '') {
  if (!seedStr) {
    return buildPaletteTokens({ h: 140, s: 28, l: 54 }, { h: 182, s: 26, l: 48 });
  }
  let hash = 0;
  for (let i = 0; i < seedStr.length; i++) {
    hash = ((hash << 5) - hash) + seedStr.charCodeAt(i);
    hash |= 0;
  }
  const hue = Math.abs(hash) % 360;
  const sat = 28 + (Math.abs(hash >> 3) % 18);
  const light = 50 + (Math.abs(hash >> 6) % 12);
  return buildPaletteTokens({ h: hue, s: sat, l: light });
}

/**
 * Extracts dominant HSL color for each of the 4 quadrants (TL, TR, BL, BR) from a WxH RGBA buffer.
 * @param {Uint8ClampedArray|number[]} rgba
 * @param {number} [width=32]
 * @param {number} [height=32]
 * @returns {Array<{ h: number, s: number, l: number } | null>}
 */
export function extractQuadrantHsls(rgba, width = 32, height = 32) {
  if (!rgba || rgba.length < width * height * 4) return [null, null, null, null];
  const halfW = Math.floor(width / 2);
  const halfH = Math.floor(height / 2);
  const quads = [
    { weight: 0, r: 0, g: 0, b: 0 },
    { weight: 0, r: 0, g: 0, b: 0 },
    { weight: 0, r: 0, g: 0, b: 0 },
    { weight: 0, r: 0, g: 0, b: 0 },
  ];

  for (let y = 0; y < height; y++) {
    const qRow = y < halfH ? 0 : 2;
    for (let x = 0; x < width; x++) {
      const idx = (y * width + x) * 4;
      if (rgba[idx + 3] < 180) continue;
      const r = rgba[idx];
      const g = rgba[idx + 1];
      const b = rgba[idx + 2];
      const hsl = rgbToHsl(r, g, b);
      if (hsl.l < 8 || hsl.l > 94) continue;
      const w = hsl.s < 12 ? 0.15 : (0.5 + (hsl.s / 100) * 1.6);
      const q = quads[qRow + (x < halfW ? 0 : 1)];
      q.weight += w;
      q.r += r * w;
      q.g += g * w;
      q.b += b * w;
    }
  }

  return quads.map(q => (q.weight > 0 ? rgbToHsl(q.r / q.weight, q.g / q.weight, q.b / q.weight) : null));
}

/**
 * Quantizes 32x32 RGBA pixel buffer into 2 dominant readable accents + 4 quadrant AMLL mesh colors.
 * Pure function suitable for unit testing without canvas DOM.
 * @param {Uint8ClampedArray|number[]} rgba
 * @param {number} [width=32]
 * @param {number} [height=32]
 * @returns {{ accent: string, accentSubtle: string, accent2: string, accent2Subtle: string, colors: Array<[number,number,number]>, c1: string, c2: string, c3: string, c4: string } | null}
 */
export function quantizePixelsToPalette(rgba, width = 32, height = 32) {
  if (!rgba || rgba.length < 4) return null;
  const buckets = new Array(12).fill(null).map(() => ({
    weight: 0,
    rSum: 0,
    gSum: 0,
    bSum: 0,
  }));

  for (let i = 0; i < rgba.length; i += 4) {
    const a = rgba[i + 3];
    if (a < 180) continue;
    const r = rgba[i];
    const g = rgba[i + 1];
    const b = rgba[i + 2];
    const hsl = rgbToHsl(r, g, b);
    // Skip near-black, near-white, and flat grayscale unless nothing else exists
    if (hsl.l < 10 || hsl.l > 92) continue;
    const chromaWeight = hsl.s < 12 ? 0.15 : (0.5 + (hsl.s / 100) * 1.5);
    const bucketIdx = Math.floor(hsl.h / 30) % 12;
    const bucket = buckets[bucketIdx];
    bucket.weight += chromaWeight;
    bucket.rSum += r * chromaWeight;
    bucket.gSum += g * chromaWeight;
    bucket.bSum += b * chromaWeight;
  }

  const ranked = buckets
    .filter(b => b.weight > 0)
    .sort((a, b) => b.weight - a.weight);

  if (ranked.length === 0) return null;

  const top1 = ranked[0];
  const hsl1 = rgbToHsl(top1.rSum / top1.weight, top1.gSum / top1.weight, top1.bSum / top1.weight);

  let hsl2 = null;
  for (let i = 1; i < ranked.length; i++) {
    const candidate = ranked[i];
    const cHsl = rgbToHsl(candidate.rSum / candidate.weight, candidate.gSum / candidate.weight, candidate.bSum / candidate.weight);
    const hueDiff = Math.min(Math.abs(cHsl.h - hsl1.h), 360 - Math.abs(cHsl.h - hsl1.h));
    if (hueDiff >= 28) {
      hsl2 = cHsl;
      break;
    }
  }

  const quadHsls = rgba.length >= width * height * 4 ? extractQuadrantHsls(rgba, width, height) : null;
  return buildPaletteTokens(hsl1, hsl2, quadHsls);
}

/**
 * Extracts real dominant palette colors from a 64x64 same-origin proxied cover via 32x32 offscreen canvas.
 * Gracefully falls back to deterministic palette when offline or unavailable.
 * @param {string} coverUrl
 * @param {string} [fallbackSeed='']
 * @returns {Promise<{ accent: string, accentSubtle: string, accent2: string, accent2Subtle: string }>}
 */
export async function extractCoverPalette(coverUrl, fallbackSeed = '') {
  const fallback = getFallbackPalette(fallbackSeed);
  if (!coverUrl || !isSafeUrl(coverUrl) || typeof document === 'undefined' || typeof Image === 'undefined') {
    return fallback;
  }

  const cacheKey = String(coverUrl).trim();
  if (PALETTE_CACHE.has(cacheKey)) {
    const cached = PALETTE_CACHE.get(cacheKey);
    // Refresh LRU order
    PALETTE_CACHE.delete(cacheKey);
    PALETTE_CACHE.set(cacheKey, cached);
    return cached;
  }

  let fetchUrl = '';
  if (NETEASE_IMAGE_HOST_RE.test(cacheKey)) {
    const httpsCover = cacheKey.replace(/^http:\/\//i, 'https://');
    fetchUrl = `/api/cover?url=${encodeURIComponent(httpsCover)}&size=64`;
  } else if (cacheKey.startsWith('/')) {
    fetchUrl = cacheKey;
  } else {
    return fallback;
  }

  try {
    const img = await new Promise((resolve, reject) => {
      const image = new Image();
      image.crossOrigin = 'anonymous';
      const timer = setTimeout(() => {
        image.onload = null;
        image.onerror = null;
        reject(new Error('cover_timeout'));
      }, 3000);
      image.onload = () => {
        clearTimeout(timer);
        resolve(image);
      };
      image.onerror = () => {
        clearTimeout(timer);
        reject(new Error('cover_load_error'));
      };
      image.src = fetchUrl;
    });

    const canvas = document.createElement('canvas');
    canvas.width = 32;
    canvas.height = 32;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    if (!ctx) return fallback;

    ctx.drawImage(img, 0, 0, 32, 32);
    const data = ctx.getImageData(0, 0, 32, 32).data;
    // Immediately release bitmap backing store
    canvas.width = 0;
    canvas.height = 0;

    const palette = quantizePixelsToPalette(data) || fallback;
    PALETTE_CACHE.set(cacheKey, palette);
    if (PALETTE_CACHE.size > MAX_PALETTE_CACHE) {
      const oldestKey = PALETTE_CACHE.keys().next().value;
      PALETTE_CACHE.delete(oldestKey);
    }
    return palette;
  } catch {
    return fallback;
  }
}

/**
 * Computes a deterministic short hex manifest hash for frozen batch operations.
 * @param {Array<{ action: string, playlistId: number|string, song: { id: number|string } }>} items
 * @returns {string}
 */
export function computeManifestHash(items = []) {
  const canonical = items
    .map(item => {
      const idPart = item.action === 'remove_hybrid_tracks'
        ? (getTrackKey(item.song) || String(item.song?.id ?? ''))
        : String(item.song?.id ?? '');
      return `${item.action}:${String(item.playlistId ?? '')}:${idPart}`;
    })
    .sort()
    .join('|');
  let hash = 2166136261;
  for (let i = 0; i < canonical.length; i++) {
    hash ^= canonical.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return `m_${(hash >>> 0).toString(16).padStart(8, '0')}`;
}

/**
 * Client-side Martin Fiedler Balanced Dithered Shuffle fallback.
 * Spreads tracks from the same primary artist evenly across [0, 1) and avoids adjacent same-artist repeats.
 * Guarantees 0 duplicate entries in a single round.
 * @param {Array<Object>} tracks
 * @param {string|number|null} [currentSongId=null]
 * @param {Function} [rng=Math.random]
 * @returns {Array<Object>}
 */
export function balancedShuffleFallback(tracks = [], currentSongId = null, rng = Math.random) {
  if (!Array.isArray(tracks) || tracks.length <= 1) return Array.isArray(tracks) ? [...tracks] : [];

  const getPrimaryArtist = (song) => {
    if (!song) return '';
    if (Array.isArray(song.artists) && song.artists.length > 0) {
      const first = song.artists[0];
      return String(typeof first === 'string' ? first : (first?.name || '')).trim().toLowerCase();
    }
    if (typeof song.artists === 'string') {
      return song.artists.split(/[/,&、]/)[0].trim().toLowerCase();
    }
    return '';
  };

  let pinned = null;
  const pool = [];
  for (const song of tracks) {
    if (!song) continue;
    if (currentSongId !== null && currentSongId !== undefined && !pinned && (getTrackKey(song) === currentSongId || String(song.id) === String(currentSongId))) {
      pinned = song;
    } else {
      pool.push(song);
    }
  }

  // Group by primary artist
  const buckets = new Map();
  for (const song of pool) {
    const key = getPrimaryArtist(song) || `__id_${getTrackKey(song) || song.id}`;
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(song);
  }

  // Fisher-Yates inside each artist bucket, then dither evenly across [0, 1)
  const scored = [];
  for (const [artistKey, list] of buckets.entries()) {
    for (let i = list.length - 1; i > 0; i--) {
      const j = Math.floor(rng() * (i + 1));
      [list[i], list[j]] = [list[j], list[i]];
    }
    const n = list.length;
    const step = 1 / n;
    const baseOffset = rng() * step;
    for (let i = 0; i < n; i++) {
      const jitter = (rng() - 0.5) * step * 0.45;
      const pos = ((baseOffset + i * step + jitter) % 1 + 1) % 1;
      scored.push({ song: list[i], artistKey, pos });
    }
  }

  scored.sort((a, b) => a.pos - b.pos);
  const ordered = scored.map(item => item.song);

  // Adjacent artist-separation pass
  for (let i = 1; i < ordered.length; i++) {
    const prevArtist = getPrimaryArtist(ordered[i - 1]);
    if (!prevArtist || getPrimaryArtist(ordered[i]) !== prevArtist) continue;
    for (let j = i + 1; j < ordered.length; j++) {
      const candArtist = getPrimaryArtist(ordered[j]);
      if (candArtist !== prevArtist) {
        [ordered[i], ordered[j]] = [ordered[j], ordered[i]];
        break;
      }
    }
  }

  return pinned ? [pinned, ...ordered] : ordered;
}

export function isPositiveSongId(id) {
  if (typeof id !== 'number' && !(typeof id === 'string' && /^\d+$/.test(id.trim()))) return false;
  return Number.isSafeInteger(Number(id)) && Number(id) > 0;
}

export function writableNeteaseTrack(song) {
  if (!song || typeof song !== 'object') return false;
  if (!isPositiveSongId(song.id)) return false;
  if (String(song.local_path || '').trim()) return false;
  const prov = String(song.provider || '').trim().toLowerCase();
  const src = String(song.source || '').trim().toLowerCase();
  if (prov && prov !== 'netease') return false;
  if (src && !['netease', 'cloud', 'imported'].includes(src)) return false;
  const explicitKey = typeof song.track_key === 'string' ? song.track_key.trim() : '';
  if (explicitKey && explicitKey !== `netease:${Number(song.id)}`) return false;
  return true;
}

export function getTrackKey(song = {}) {
  const localPath = String(song?.local_path || '').trim();
  if (localPath) return `local:${localPath}`;
  const prov = String(song?.provider || '').trim().toLowerCase();
  const src = String(song?.source || '').trim().toLowerCase();
  if ((prov && prov !== 'netease') || (src && !['netease', 'cloud', 'imported'].includes(src))) {
    const ns = (prov && prov !== 'netease') ? prov : src;
    return `${ns}:${song.external_id || song.id}`;
  }
  const explicitKey = typeof song?.track_key === 'string' ? song.track_key.trim() : '';
  if (explicitKey) {
    if (explicitKey.toLowerCase().startsWith('netease:')) {
      if (writableNeteaseTrack({ ...song, track_key: undefined }) && explicitKey === `netease:${Number(song.id)}`) {
        return explicitKey;
      }
    } else {
      return explicitKey;
    }
  }
  if (isPositiveSongId(song?.id)) return `netease:${Number(song.id)}`;
  return `track:${song?.title || ''}:${formatArtists(song?.artists)}`;
}

export function serializeTrackForStorage(song) {
  if (!song) return null;
  return {
    id: song.id ?? 0, track_key: getTrackKey(song),
    provider: song.provider || (song.local_path ? 'local' : 'netease'),
    external_id: song.external_id || (song.provider && song.provider !== 'netease' ? String(song.id || '') : ''),
    local_path: song.local_path || '', title: song.title || '', artists: song.artists || [],
    album: song.album || '', duration: song.duration || 0, duration_ms: Math.round(durationSeconds(song) * 1000), cover_url: song.cover_url || '',
    source_playlist_id: song.source_playlist_id ?? null, source_playlist_name: song.source_playlist_name || '',
    availability: song.availability || 'unknown',
  };
}

export function bindVolumeWheel(groupEl, onStep) {
  if (!groupEl || typeof groupEl.addEventListener !== 'function' || typeof onStep !== 'function') return;
  groupEl.addEventListener('wheel', (e) => {
    e.preventDefault?.();
    onStep(e.deltaY < 0 ? 0.05 : -0.05);
  }, { passive: false });
}

export function bindTimelineTooltip(sliderEl, tooltipEl, getDuration) {
  if (!sliderEl || !tooltipEl || typeof sliderEl.addEventListener !== 'function') return;
  const updateFromPointer = (e) => {
    const dur = Number(getDuration?.()) || 0;
    const rect = sliderEl.getBoundingClientRect?.();
    if (!rect || rect.width <= 0 || dur <= 0) {
      tooltipEl.classList.add('hidden');
      return;
    }
    const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    tooltipEl.textContent = formatSeconds(ratio * dur);
    tooltipEl.style.left = `${(ratio * 100).toFixed(2)}%`;
    tooltipEl.classList.remove('hidden');
  };
  sliderEl.addEventListener('pointermove', updateFromPointer);
  sliderEl.addEventListener('input', () => {
    const dur = Number(getDuration?.()) || 0;
    if (dur <= 0) return;
    const pct = Math.max(0, Math.min(100, Number(sliderEl.value) || 0));
    tooltipEl.textContent = formatSeconds((pct / 100) * dur);
    tooltipEl.style.left = `${pct.toFixed(2)}%`;
    tooltipEl.classList.remove('hidden');
  });
  sliderEl.addEventListener('pointerleave', () => tooltipEl.classList.add('hidden'));
  sliderEl.addEventListener('change', () => tooltipEl.classList.add('hidden'));
}

/**
 * Builds the lightweight 3-bar equalizer indicator shown beside active playing tracks.
 */
export function createPlayingEqBars() {
  return createElement('span', {
    className: 'playing-eq-bars',
    attributes: { 'aria-hidden': 'true' },
    children: [
      createElement('span'),
      createElement('span'),
      createElement('span'),
    ],
  });
}

/**
 * Updates `.is-playing-row` and `.is-paused` classes across rendered track rows/cards.
 * @param {object|null} song
 * @param {boolean} isPlaying
 * @param {ParentNode|null} [root]
 */
export function syncPlayingRowIndicators(song, isPlaying = false, root = typeof document !== 'undefined' ? document : null) {
  if (!root || typeof root.querySelectorAll !== 'function') return;
  const activeKey = song ? getTrackKey(song) : '';
  const pauseBars = !isPlaying;
  root.querySelectorAll('[data-track-key]').forEach((el) => {
    const match = Boolean(activeKey) && el.dataset?.trackKey === activeKey;
    el.classList?.toggle?.('is-playing-row', match);
    const bars = typeof el.querySelector === 'function' ? el.querySelector('.playing-eq-bars') : null;
    if (bars && bars.classList) {
      bars.classList.toggle('is-paused', pauseBars);
    }
  });
}
