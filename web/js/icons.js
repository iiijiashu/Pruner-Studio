/**
 * Pruner Studio - Crisp Inline SVG Icon Library
 * 1.5px stroke monochrome SVG icons built safely via createElementNS (zero innerHTML, zero emojis).
 */

const SVG_NS = 'http://www.w3.org/2000/svg';

/**
 * Icon path/shape definitions on a 24x24 viewBox.
 * Each entry is an array of [tag, attributes].
 */
const ICON_DEFS = {
  compass: [
    ['circle', { cx: '12', cy: '12', r: '9.5' }],
    ['polygon', { points: '16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76' }],
  ],
  library: [
    ['path', { d: 'M4 5v14' }],
    ['path', { d: 'M9 5v14' }],
    ['path', { d: 'M14 5l6 14' }],
    ['path', { d: 'M14 5v14' }],
  ],
  layers: [
    ['polygon', { points: '12 3 2 8.5 12 14 22 8.5 12 3' }],
    ['polyline', { points: '2 13.5 12 19 22 13.5' }],
    ['polyline', { points: '2 18 12 23.5 22 18' }],
  ],
  cloud: [
    ['path', { d: 'M17.5 19H7a4.5 4.5 0 0 1-.4-8.98A6 6 0 0 1 18.2 9.4 3.8 3.8 0 0 1 17.5 19z' }],
  ],
  settings: [
    ['circle', { cx: '12', cy: '12', r: '3' }],
    ['path', { d: 'M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.08a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.08a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.08a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z' }],
  ],
  sidebar: [
    ['rect', { x: '3', y: '4', width: '18', height: '16', rx: '2' }],
    ['line', { x1: '9', y1: '4', x2: '9', y2: '20' }],
  ],
  menu: [
    ['line', { x1: '4', y1: '7', x2: '20', y2: '7' }],
    ['line', { x1: '4', y1: '12', x2: '20', y2: '12' }],
    ['line', { x1: '4', y1: '17', x2: '20', y2: '17' }],
  ],
  search: [
    ['circle', { cx: '11', cy: '11', r: '7' }],
    ['line', { x1: '20', y1: '20', x2: '16.65', y2: '16.65' }],
  ],
  play: [
    ['polygon', { points: '7 5 19 12 7 19 7 5', fill: 'currentColor', stroke: 'none' }],
  ],
  pause: [
    ['rect', { x: '6', y: '5', width: '4', height: '14', rx: '1', fill: 'currentColor', stroke: 'none' }],
    ['rect', { x: '14', y: '5', width: '4', height: '14', rx: '1', fill: 'currentColor', stroke: 'none' }],
  ],
  'skip-prev': [
    ['polygon', { points: '19 20 9 12 19 4 19 20' }],
    ['line', { x1: '5', y1: '5', x2: '5', y2: '19' }],
  ],
  'skip-next': [
    ['polygon', { points: '5 4 15 12 5 20 5 4' }],
    ['line', { x1: '19', y1: '5', x2: '19', y2: '19' }],
  ],
  'rewind-5': [
    ['polyline', { points: '11 17 6 12 11 7' }],
    ['polyline', { points: '18 17 13 12 18 7' }],
  ],
  'forward-5': [
    ['polyline', { points: '13 17 18 12 13 7' }],
    ['polyline', { points: '6 17 11 12 6 7' }],
  ],
  repeat: [
    ['polyline', { points: '17 2 21 6 17 10' }],
    ['path', { d: 'M3 11V9a3 3 0 0 1 3-3h15' }],
    ['polyline', { points: '7 22 3 18 7 14' }],
    ['path', { d: 'M21 13v2a3 3 0 0 1-3 3H3' }],
  ],
  'repeat-1': [
    ['polyline', { points: '17 2 21 6 17 10' }],
    ['path', { d: 'M3 11V9a3 3 0 0 1 3-3h15' }],
    ['polyline', { points: '7 22 3 18 7 14' }],
    ['path', { d: 'M21 13v2a3 3 0 0 1-3 3H3' }],
    ['path', { d: 'M12 9v6' }],
  ],
  shuffle: [
    ['polyline', { points: '16 3 21 3 21 8' }],
    ['line', { x1: '4', y1: '20', x2: '21', y2: '3' }],
    ['polyline', { points: '21 16 21 21 16 21' }],
    ['line', { x1: '15', y1: '15', x2: '21', y2: '21' }],
    ['line', { x1: '4', y1: '4', x2: '9', y2: '9' }],
  ],
  radio: [
    ['circle', { cx: '12', cy: '12', r: '2' }],
    ['path', { d: 'M16.24 7.76a6 6 0 0 1 0 8.48' }],
    ['path', { d: 'M7.76 16.24a6 6 0 0 1 0-8.48' }],
    ['path', { d: 'M19.07 4.93a10 10 0 0 1 0 14.14' }],
    ['path', { d: 'M4.93 19.07a10 10 0 0 1 0-14.14' }],
  ],
  heart: [
    ['path', { d: 'M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z' }],
  ],
  'heart-filled': [
    ['path', { d: 'M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z', fill: 'currentColor' }],
  ],
  volume: [
    ['polygon', { points: '11 5 6 9 2 9 2 15 6 15 11 19 11 5' }],
    ['path', { d: 'M15.54 8.46a5 5 0 0 1 0 7.07' }],
    ['path', { d: 'M19.07 4.93a10 10 0 0 1 0 14.14' }],
  ],
  'volume-mute': [
    ['polygon', { points: '11 5 6 9 2 9 2 15 6 15 11 19 11 5' }],
    ['line', { x1: '22', y1: '9', x2: '16', y2: '15' }],
    ['line', { x1: '16', y1: '9', x2: '22', y2: '15' }],
  ],
  queue: [
    ['line', { x1: '4', y1: '6', x2: '20', y2: '6' }],
    ['line', { x1: '4', y1: '12', x2: '14', y2: '12' }],
    ['line', { x1: '4', y1: '18', x2: '14', y2: '18' }],
    ['polygon', { points: '17 12 21 15 17 18 17 12', fill: 'currentColor', stroke: 'none' }],
  ],
  lyrics: [
    ['path', { d: 'M4 6h16' }],
    ['path', { d: 'M4 12h12' }],
    ['path', { d: 'M4 18h8' }],
    ['circle', { cx: '18', cy: '17', r: '2.5' }],
    ['path', { d: 'M20.5 17V10' }],
  ],
  maximize: [
    ['polyline', { points: '15 3 21 3 21 9' }],
    ['polyline', { points: '9 21 3 21 3 15' }],
    ['line', { x1: '21', y1: '3', x2: '14', y2: '10' }],
    ['line', { x1: '3', y1: '21', x2: '10', y2: '14' }],
  ],
  minimize: [
    ['polyline', { points: '4 14 10 14 10 20' }],
    ['polyline', { points: '20 10 14 10 14 4' }],
    ['line', { x1: '14', y1: '10', x2: '21', y2: '3' }],
    ['line', { x1: '3', y1: '21', x2: '10', y2: '14' }],
  ],
  close: [
    ['line', { x1: '18', y1: '6', x2: '6', y2: '18' }],
    ['line', { x1: '6', y1: '6', x2: '18', y2: '18' }],
  ],
  check: [
    ['polyline', { points: '20 6 9 17 4 12' }],
  ],
  trash: [
    ['polyline', { points: '3 6 5 6 21 6' }],
    ['path', { d: 'M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6' }],
    ['path', { d: 'M10 11v6' }],
    ['path', { d: 'M14 11v6' }],
    ['path', { d: 'M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2' }],
  ],
  undo: [
    ['polyline', { points: '9 14 4 9 9 4' }],
    ['path', { d: 'M20 20v-7a4 4 0 0 0-4-4H4' }],
  ],
  clock: [
    ['circle', { cx: '12', cy: '12', r: '9' }],
    ['polyline', { points: '12 7 12 12 15.5 14' }],
  ],
  'skip-end': [
    ['polyline', { points: '15 10 20 15 15 20' }],
    ['path', { d: 'M4 4v7a4 4 0 0 0 4 4h12' }],
  ],
  zap: [
    ['polygon', { points: '13 2 3 14 12 14 11 22 21 10 12 10 13 2' }],
  ],
  sliders: [
    ['line', { x1: '4', y1: '21', x2: '4', y2: '14' }],
    ['line', { x1: '4', y1: '10', x2: '4', y2: '3' }],
    ['line', { x1: '12', y1: '21', x2: '12', y2: '12' }],
    ['line', { x1: '12', y1: '8', x2: '12', y2: '3' }],
    ['line', { x1: '20', y1: '21', x2: '20', y2: '16' }],
    ['line', { x1: '20', y1: '12', x2: '20', y2: '3' }],
    ['line', { x1: '1', y1: '14', x2: '7', y2: '14' }],
    ['line', { x1: '9', y1: '8', x2: '15', y2: '8' }],
    ['line', { x1: '17', y1: '16', x2: '23', y2: '16' }],
  ],
  sparkles: [
    ['path', { d: 'M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3z' }],
    ['path', { d: 'M19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8L19 16z' }],
  ],
  plus: [
    ['line', { x1: '12', y1: '5', x2: '12', y2: '19' }],
    ['line', { x1: '5', y1: '12', x2: '19', y2: '12' }],
  ],
  'plus-list': [
    ['line', { x1: '4', y1: '6', x2: '18', y2: '6' }],
    ['line', { x1: '4', y1: '12', x2: '14', y2: '12' }],
    ['line', { x1: '4', y1: '18', x2: '12', y2: '18' }],
    ['line', { x1: '18', y1: '13', x2: '18', y2: '21' }],
    ['line', { x1: '14', y1: '17', x2: '22', y2: '17' }],
  ],
  table: [
    ['rect', { x: '3', y: '4', width: '18', height: '16', rx: '2' }],
    ['line', { x1: '3', y1: '10', x2: '21', y2: '10' }],
    ['line', { x1: '3', y1: '15', x2: '21', y2: '15' }],
  ],
  grid: [
    ['rect', { x: '3', y: '3', width: '7', height: '7', rx: '1.5' }],
    ['rect', { x: '14', y: '3', width: '7', height: '7', rx: '1.5' }],
    ['rect', { x: '3', y: '14', width: '7', height: '7', rx: '1.5' }],
    ['rect', { x: '14', y: '14', width: '7', height: '7', rx: '1.5' }],
  ],
  filter: [
    ['polygon', { points: '22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3' }],
  ],
  folder: [
    ['path', { d: 'M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z' }],
  ],
  refresh: [
    ['polyline', { points: '23 4 23 10 17 10' }],
    ['path', { d: 'M20.49 15a9 9 0 1 1-2.12-9.36L23 10' }],
  ],
  download: [
    ['path', { d: 'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4' }],
    ['polyline', { points: '7 10 12 15 17 10' }],
    ['line', { x1: '12', y1: '15', x2: '12', y2: '3' }],
  ],
  upload: [
    ['path', { d: 'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4' }],
    ['polyline', { points: '17 8 12 3 7 8' }],
    ['line', { x1: '12', y1: '3', x2: '12', y2: '15' }],
  ],
  'chevron-right': [
    ['polyline', { points: '9 18 15 12 9 6' }],
  ],
  'chevron-down': [
    ['polyline', { points: '6 9 12 15 18 9' }],
  ],
  'check-circle': [
    ['circle', { cx: '12', cy: '12', r: '9' }],
    ['polyline', { points: '9 12.5 11 14.5 15.5 10' }],
  ],
  'x-circle': [
    ['circle', { cx: '12', cy: '12', r: '9' }],
    ['line', { x1: '15', y1: '9', x2: '9', y2: '15' }],
    ['line', { x1: '9', y1: '9', x2: '15', y2: '15' }],
  ],
  'alert-circle': [
    ['circle', { cx: '12', cy: '12', r: '9' }],
    ['line', { x1: '12', y1: '8', x2: '12', y2: '12.5' }],
    ['line', { x1: '12', y1: '16', x2: '12.01', y2: '16' }],
  ],
  info: [
    ['circle', { cx: '12', cy: '12', r: '9' }],
    ['line', { x1: '12', y1: '16', x2: '12', y2: '12' }],
    ['line', { x1: '12', y1: '8', x2: '12.01', y2: '8' }],
  ],
  ban: [
    ['circle', { cx: '12', cy: '12', r: '9' }],
    ['line', { x1: '5.6', y1: '5.6', x2: '18.4', y2: '18.4' }],
  ],
  disc: [
    ['circle', { cx: '12', cy: '12', r: '9' }],
    ['circle', { cx: '12', cy: '12', r: '2.5' }],
  ],
  'arrow-right': [
    ['line', { x1: '5', y1: '12', x2: '19', y2: '12' }],
    ['polyline', { points: '13 6 19 12 13 18' }],
  ],
  minus: [
    ['line', { x1: '5', y1: '12', x2: '19', y2: '12' }],
  ],
  lock: [
    ['rect', { x: '5', y: '11', width: '14', height: '10', rx: '2' }],
    ['path', { d: 'M8 11V7a4 4 0 0 1 8 0v4' }],
  ],
  unlock: [
    ['rect', { x: '5', y: '11', width: '14', height: '10', rx: '2' }],
    ['path', { d: 'M8 11V7a4 4 0 0 1 7.6-1.7' }],
  ],
  shield: [
    ['path', { d: 'M12 3l7 3v5c0 4.5-3 8.2-7 10-4-1.8-7-5.5-7-10V6l7-3z' }],
    ['polyline', { points: '9 12 11 14 15 10' }],
  ],
  globe: [
    ['circle', { cx: '12', cy: '12', r: '9' }],
    ['line', { x1: '3', y1: '12', x2: '21', y2: '12' }],
    ['path', { d: 'M12 3a13 13 0 0 1 0 18' }],
    ['path', { d: 'M12 3a13 13 0 0 0 0 18' }],
  ],
  pip: [
    ['rect', { x: '3', y: '4', width: '18', height: '16', rx: '2' }],
    ['rect', { x: '11', y: '12', width: '8', height: '6', rx: '1', fill: 'currentColor', stroke: 'none' }],
  ],
  activity: [
    ['polyline', { points: '3 12 7 12 10 5 14 19 17 12 21 12' }],
  ],
  gem: [
    ['polygon', { points: '6 4 18 4 22 9 12 20 2 9 6 4' }],
    ['line', { x1: '2', y1: '9', x2: '22', y2: '9' }],
    ['polyline', { points: '12 20 8 9 12 4 16 9 12 20' }],
  ],
  key: [
    ['circle', { cx: '8', cy: '14', r: '4' }],
    ['path', { d: 'M11.5 11.5L20 3' }],
    ['path', { d: 'M17 6l3 3' }],
    ['path', { d: 'M14.5 8.5l2.5 2.5' }],
  ],
  move: [
    ['polyline', { points: '5 9 2 12 5 15' }],
    ['polyline', { points: '9 5 12 2 15 5' }],
    ['polyline', { points: '15 19 12 22 9 19' }],
    ['polyline', { points: '19 9 22 12 19 15' }],
    ['line', { x1: '2', y1: '12', x2: '22', y2: '12' }],
    ['line', { x1: '12', y1: '2', x2: '12', y2: '22' }],
  ],
  copy: [
    ['rect', { x: '9', y: '9', width: '11', height: '11', rx: '2' }],
    ['path', { d: 'M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1' }],
  ],
  sun: [
    ['circle', { cx: '12', cy: '12', r: '4' }],
    ['line', { x1: '12', y1: '2', x2: '12', y2: '4.5' }],
    ['line', { x1: '12', y1: '19.5', x2: '12', y2: '22' }],
    ['line', { x1: '4.93', y1: '4.93', x2: '6.7', y2: '6.7' }],
    ['line', { x1: '17.3', y1: '17.3', x2: '19.07', y2: '19.07' }],
    ['line', { x1: '2', y1: '12', x2: '4.5', y2: '12' }],
    ['line', { x1: '19.5', y1: '12', x2: '22', y2: '12' }],
    ['line', { x1: '4.93', y1: '19.07', x2: '6.7', y2: '17.3' }],
    ['line', { x1: '17.3', y1: '6.7', x2: '19.07', y2: '4.93' }],
  ],
  moon: [
    ['path', { d: 'M21 12.79A9 9 0 1 1 11.21 3a7 7 0 0 0 9.79 9.79z' }],
  ],
  monitor: [
    ['rect', { x: '2', y: '3', width: '20', height: '14', rx: '2' }],
    ['line', { x1: '8', y1: '21', x2: '16', y2: '21' }],
    ['line', { x1: '12', y1: '17', x2: '12', y2: '21' }],
  ],
  palette: [
    ['path', { d: 'M12 21a9 9 0 1 1 8.5-6.1c-.4 1.2-1.6 1.6-2.8 1.6h-2a2 2 0 0 0-2 2c0 1.4-1.1 2.5-1.7 2.5z' }],
    ['circle', { cx: '7.5', cy: '10.5', r: '1' }],
    ['circle', { cx: '10.5', cy: '7', r: '1' }],
    ['circle', { cx: '14.5', cy: '7.5', r: '1' }],
    ['circle', { cx: '16.5', cy: '11', r: '1' }],
  ],
  'more-horizontal': [
    ['circle', { cx: '5', cy: '12', r: '1.2', fill: 'currentColor' }],
    ['circle', { cx: '12', cy: '12', r: '1.2', fill: 'currentColor' }],
    ['circle', { cx: '19', cy: '12', r: '1.2', fill: 'currentColor' }],
  ],
};

/**
 * Create a clean inline SVG icon element without innerHTML.
 * Works safely in Node unit tests when document.createElementNS is absent.
 * @param {string} name
 * @param {{ size?: number, className?: string, strokeWidth?: number }} [opts]
 * @returns {SVGElement|Object}
 */
export function createIcon(name, opts = {}) {
  const size = opts.size || 16;
  const strokeWidth = opts.strokeWidth || 1.5;
  const className = opts.className ? `icon-svg ${opts.className}` : 'icon-svg';

  if (typeof document === 'undefined' || typeof document.createElementNS !== 'function') {
    return {
      tagName: 'SVG',
      className,
      dataset: { icon: name },
      textContent: '',
      setAttribute() {},
      appendChild() {},
    };
  }

  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', String(strokeWidth));
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  svg.setAttribute('class', className);
  svg.dataset.icon = name;

  const shapes = ICON_DEFS[name] || ICON_DEFS.disc;
  for (const [tag, attrs] of shapes) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) {
      el.setAttribute(k, String(v));
    }
    svg.appendChild(el);
  }

  return svg;
}

/**
 * Replaces a button's children with an SVG icon and optional label span.
 * @param {HTMLElement|null} btn
 * @param {string} iconName
 * @param {string} [label='']
 * @param {{ size?: number, className?: string }} [opts]
 */
export function setButtonIcon(btn, iconName, label = '', opts = {}) {
  if (!btn) return;
  btn.textContent = '';
  if (typeof document !== 'undefined' && typeof document.createElementNS === 'function') {
    btn.appendChild(createIcon(iconName, opts));
    if (label) {
      const span = document.createElement('span');
      span.className = 'btn-label';
      span.textContent = label;
      btn.appendChild(span);
    }
  } else if (label) {
    btn.textContent = label;
  }
}

/**
 * Hydrates all static [data-icon-slot] placeholders in index.html once at boot.
 */
export function hydrateStaticIcons(root = typeof document !== 'undefined' ? document : null) {
  if (!root || typeof root.querySelectorAll !== 'function') return;
  const slots = root.querySelectorAll('[data-icon-slot]');
  slots.forEach(slot => {
    const name = slot.getAttribute('data-icon-slot');
    const size = Number(slot.getAttribute('data-icon-size')) || 16;
    if (!name) return;
    slot.textContent = '';
    slot.appendChild(createIcon(name, { size }));
  });
}

export const ICON_NAMES = Object.keys(ICON_DEFS);
