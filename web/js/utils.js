/**
 * Pruner Studio - Utility Functions
 * Safe DOM creation, URL validation, formatters, and helpers.
 */

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
  // Quiet Bone/Carbon SVG placeholder
  const safeText = encodeURIComponent(fallbackText || '');
  return `data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120" viewBox="0 0 120 120"><rect width="120" height="120" fill="%23161713"/><rect x="1" y="1" width="118" height="118" fill="none" stroke="%23f1efe7" stroke-opacity="0.1" stroke-width="1"/><text x="50%" y="54%" dominant-baseline="middle" text-anchor="middle" fill="%2396938a" font-family="sans-serif" font-size="14">${safeText}</text></svg>`;
}

/**
 * Formats duration in seconds or milliseconds into MM:SS format.
 * @param {number} durationInSecondsOrMs
 * @returns {string}
 */
export function formatDuration(durationInSecondsOrMs) {
  if (!durationInSecondsOrMs || isNaN(durationInSecondsOrMs)) return '0:00';
  let sec = Number(durationInSecondsOrMs);
  // Auto-detect if duration is in milliseconds
  if (sec > 10000) {
    sec = Math.floor(sec / 1000);
  } else {
    sec = Math.floor(sec);
  }
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
      } else if (child instanceof Node) {
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
 * Estimates remaining minutes based on remaining count and average review time (e.g. ~3.5s per song).
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
 * Generates or extracts a normalized readable accent color for the ambient cover field.
 * Saturation clamped between 25-50%, lightness clamped between 45-65% for quiet elegance.
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
