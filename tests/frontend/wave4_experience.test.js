/**
 * Pruner Studio - Wave 4 Frontend Experience Verification Suite
 * Covers:
 * 1. Theme & Appearance Engine (`theme.js` FOUC-free localStorage apply, light/midnight/auto, accents, density, font-scale)
 * 2. Light Theme CSS Tokens & WCAG >= 4.5:1 Contrast + Immutable Keep/Prune Semantics
 * 3. Online-First Search Palette (`'all'` default, localStorage channel memory, max-5 `曲库中` strip above online results, footer switch-tab)
 * 4. Full-Page Search View (`#view-search`, 100/page `offset` + `has_more` pagination, quality sorting, partial channel errors)
 * 5. Right-Click Context Menu (`openSongContextMenu`, discover `不再推荐`, viewport clamping, Arrow/Enter/Esc & outside dismiss)
 * 6. Cloud SSE Exponential Backoff Reconnect (`1s -> 2s -> 5s -> 15s`), `disconnectEvents` cancel, `desktop_lyrics_style` SSE, & Channel card declutter
 * 7. Desktop Floating Lyrics (`computeWordProgress(words, t)`, transparent background, style persistence & commands)
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

if (typeof globalThis.window === 'undefined') {
  globalThis.window = {
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => {},
    innerWidth: 1280,
    innerHeight: 800,
  };
}
if (typeof globalThis.navigator === 'undefined') {
  globalThis.navigator = { onLine: true };
}
if (typeof globalThis.localStorage === 'undefined') {
  const store = new Map();
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  };
}

import { api } from '../../web/js/api.js';
import { state } from '../../web/js/state.js';
import { modal } from '../../web/js/modal.js';
import {
  themeManager,
  applyAppearance,
  computeContrastRatio,
  APPEARANCE_STORAGE_KEY,
} from '../../web/js/theme.js';
import { searchPalette, SEARCH_CHANNEL_STORAGE_KEY } from '../../web/js/search_palette.js';
import {
  searchView,
  computeTrackQualityScore,
  SEARCH_VIEW_PAGE_SIZE,
} from '../../web/js/search_view.js';
import {
  openSongContextMenu,
  closeSongContextMenu,
  buildSongContextMenuItems,
} from '../../web/js/context_menu.js';
import { cloudManager, CHANNEL_CATALOG, SSE_BACKOFF_DELAYS } from '../../web/js/cloud.js';

export function createMockDom() {
  class MockNode {}
  const docListeners = new Map();
  const winListeners = new Map();
  const stylesRoot = new Map();
  const rootEl = {
    dataset: {},
    style: {
      setProperty: (k, v) => stylesRoot.set(k, String(v)),
      getPropertyValue: (k) => stylesRoot.get(k) || '',
    },
  };

  const makeEl = (tag = 'div', id = '') => {
    const el = Object.create(MockNode.prototype);
    const styles = new Map();
    const classes = new Set();
    const attrs = {};
    const listeners = new Map();
    let textVal = '';
    let valueVal = '';

    Object.assign(el, {
      tagName: String(tag).toUpperCase(),
      id,
      children: [],
      parentNode: null,
      dataset: {},
      attributes: attrs,
      disabled: false,
      checked: false,
      open: false,
      style: {
        setProperty: (k, v) => styles.set(k, String(v)),
        getPropertyValue: (k) => styles.get(k) || '',
        set left(v) { styles.set('left', String(v)); },
        get left() { return styles.get('left') || ''; },
        set top(v) { styles.set('top', String(v)); },
        get top() { return styles.get('top') || ''; },
        set position(v) { styles.set('position', String(v)); },
        set opacity(v) { styles.set('opacity', String(v)); },
      },
      classList: {
        add: (...cls) => cls.forEach(c => classes.add(c)),
        remove: (...cls) => cls.forEach(c => classes.delete(c)),
        contains: (c) => classes.has(c),
        toggle: (c, force) => {
          const on = force === undefined ? !classes.has(c) : Boolean(force);
          if (on) classes.add(c);
          else classes.delete(c);
          return on;
        },
      },
      setAttribute(name, val) {
        attrs[name] = String(val);
        if (name === 'id') this.id = String(val);
        if (name === 'disabled') this.disabled = true;
        if (name === 'open') this.open = true;
        if (name === 'value') valueVal = String(val);
      },
      getAttribute(name) {
        return Object.prototype.hasOwnProperty.call(attrs, name) ? attrs[name] : null;
      },
      hasAttribute(name) {
        return Object.prototype.hasOwnProperty.call(attrs, name);
      },
      addEventListener(type, fn) {
        if (!listeners.has(type)) listeners.set(type, new Set());
        listeners.get(type).add(fn);
      },
      removeEventListener(type, fn) {
        listeners.get(type)?.delete(fn);
      },
      dispatchEvent(evt) {
        const e = typeof evt === 'string' ? { type: evt } : evt;
        listeners.get(e.type)?.forEach(fn => fn(e));
      },
      click() {
        this.dispatchEvent({ type: 'click', stopPropagation() {}, preventDefault() {} });
      },
      focus() {
        mockDoc.activeElement = this;
      },
      select() {},
      contains(target) {
        if (target === this) return true;
        return this.children.some(c => typeof c.contains === 'function' && c.contains(target));
      },
      getBoundingClientRect() {
        return { left: 20, top: 20, bottom: 50, width: 210, height: 260 };
      },
      appendChild(child) {
        if (!child) return child;
        if (child._isFragment) {
          child.children.forEach(c => { c.parentNode = this; this.children.push(c); });
        } else {
          child.parentNode = this;
          this.children.push(child);
        }
        return child;
      },
      removeChild(child) {
        const idx = this.children.indexOf(child);
        if (idx >= 0) {
          this.children.splice(idx, 1);
          child.parentNode = null;
        }
      },
      querySelector(sel) {
        return this.querySelectorAll(sel)[0] || null;
      },
      querySelectorAll(sel) {
        const out = [];
        const matchNode = (n) => {
          if (sel.startsWith('.')) {
            return n.classList?.contains(sel.slice(1));
          }
          if (sel.startsWith('#')) {
            return n.id === sel.slice(1);
          }
          const attrMatch = /^\[([a-zA-Z0-9_-]+)(?:="([^"]*)")?\]$/.exec(sel);
          if (attrMatch) {
            const attrName = attrMatch[1];
            const attrVal = attrMatch[2];
            if (attrName.startsWith('data-')) {
              const camel = attrName.slice(5).replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
              const has = n.dataset && Object.prototype.hasOwnProperty.call(n.dataset, camel);
              if (!has) return false;
              return attrVal === undefined ? true : String(n.dataset[camel]) === attrVal;
            }
            return attrVal === undefined ? n.hasAttribute?.(attrName) : n.getAttribute?.(attrName) === attrVal;
          }
          return String(n.tagName || '').toLowerCase() === sel.toLowerCase();
        };
        const walk = (node) => {
          for (const ch of (node.children || [])) {
            if (matchNode(ch)) out.push(ch);
            walk(ch);
          }
        };
        walk(this);
        return out;
      },
    });

    Object.defineProperty(el, 'className', {
      get: () => [...classes].join(' '),
      set: (val) => {
        classes.clear();
        String(val || '').split(/\s+/).filter(Boolean).forEach(c => classes.add(c));
      },
    });
    Object.defineProperty(el, 'textContent', {
      get: () => {
        if (textVal) return textVal;
        return el.children.map(c => c.textContent || '').join('');
      },
      set: (val) => {
        textVal = String(val);
        if (textVal === '') el.children = [];
      },
    });
    Object.defineProperty(el, 'value', {
      get: () => valueVal,
      set: (val) => { valueVal = String(val); },
    });

    return el;
  };

  const domById = new Map();
  const bodyEl = makeEl('body');

  const mockDoc = {
    documentElement: rootEl,
    body: bodyEl,
    activeElement: null,
    createElement: (tag) => makeEl(tag),
    createTextNode: (txt) => ({ nodeType: 3, textContent: String(txt) }),
    createDocumentFragment: () => ({
      _isFragment: true,
      children: [],
      appendChild(c) { this.children.push(c); return c; },
    }),
    getElementById: (id) => domById.get(id) || null,
    querySelector: (sel) => bodyEl.querySelector(sel),
    querySelectorAll: (sel) => bodyEl.querySelectorAll(sel),
    addEventListener(type, fn) {
      if (!docListeners.has(type)) docListeners.set(type, new Set());
      docListeners.get(type).add(fn);
    },
    removeEventListener(type, fn) {
      docListeners.get(type)?.delete(fn);
    },
    dispatchEvent(evt) {
      docListeners.get(evt.type)?.forEach(fn => fn(evt));
    },
  };

  const registerId = (id, tag = 'div') => {
    const el = makeEl(tag, id);
    domById.set(id, el);
    bodyEl.appendChild(el);
    return el;
  };

  return { MockNode, makeEl, mockDoc, rootEl, bodyEl, domById, registerId, docListeners, winListeners };
}

export async function runWave4ExperienceTests() {
  console.log('Testing Wave 4 frontend experience deliverables...');

  const origDoc = globalThis.document;
  const origWin = globalThis.window;
  const hadNode = Object.prototype.hasOwnProperty.call(globalThis, 'Node');
  const origNode = globalThis.Node;
  const origEventSource = globalThis.EventSource;
  const origSetTimeout = globalThis.setTimeout;
  const origClearTimeout = globalThis.clearTimeout;
  const origSearchMulti = api.searchMulti;
  const origModalClose = modal.close;
  const origModalOpen = modal.open;

  try {
    // ----------------------------------------------------------------------
    // 1. Theme & Appearance Engine (`theme.js`) + FOUC-free localStorage load
    // ----------------------------------------------------------------------
    const env1 = createMockDom();
    globalThis.Node = env1.MockNode;
    globalThis.document = env1.mockDoc;

    let prefersLight = false;
    const dispatchedWindowEvents = [];
    globalThis.window = {
      innerWidth: 1280,
      innerHeight: 800,
      matchMedia: (q) => ({
        matches: q.includes('light') ? prefersLight : false,
        addEventListener: () => {},
      }),
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: (evt) => dispatchedWindowEvents.push(evt),
    };

    localStorage.setItem(APPEARANCE_STORAGE_KEY, JSON.stringify({
      theme: 'light',
      accent_mode: 'preset',
      accent: '#6b8fa8',
      density: 'compact',
      font_scale: 1.1,
    }));
    const bootApplied = themeManager.initFromStorage();
    assert.equal(bootApplied.resolvedTheme, 'light');
    assert.equal(env1.rootEl.dataset.theme, 'light', 'theme.js must apply data-theme from localStorage');
    assert.equal(env1.rootEl.dataset.density, 'compact', 'theme.js must apply data-density from localStorage');
    assert.equal(env1.rootEl.style.getPropertyValue('--font-scale'), '1.1', 'theme.js must apply --font-scale');
    assert.equal(env1.rootEl.style.getPropertyValue('--accent'), '#6b8fa8');
    assert.ok(env1.rootEl.style.getPropertyValue('--accent-subtle').startsWith('rgba('));
    assert.ok(['#111214', '#ffffff'].includes(env1.rootEl.style.getPropertyValue('--accent-contrast')));

    // Auto theme respects prefers-color-scheme
    prefersLight = true;
    const autoLight = applyAppearance({ theme: 'auto', accent: '#62a368', density: 'comfortable', font_scale: 1.0 });
    assert.equal(autoLight.resolvedTheme, 'light');
    assert.equal(env1.rootEl.dataset.theme, 'light');
    prefersLight = false;
    const autoDark = applyAppearance({ theme: 'auto', accent: '#62a368', density: 'comfortable', font_scale: 1.0 });
    assert.equal(autoDark.resolvedTheme, 'dark');
    assert.equal(env1.rootEl.dataset.theme, 'dark');

    // Cover accent mode picks state.coverAccent
    state.set({ coverAccent: '#d69e4e' });
    themeManager.setAppearance({ theme: 'midnight', accent_mode: 'cover', density: 'comfortable', font_scale: 1.0 });
    assert.equal(env1.rootEl.dataset.theme, 'midnight');
    assert.equal(env1.rootEl.style.getPropertyValue('--accent'), '#d69e4e', 'cover mode must bind --accent to coverAccent');

    // ----------------------------------------------------------------------
    // 2. Light Theme CSS Token Presence & Contrast >= 4.5:1 + Immutable Semantics
    // ----------------------------------------------------------------------
    const baseCss = fs.readFileSync(path.join(process.cwd(), 'web', 'css', 'base.css'), 'utf8');
    const mainCss = fs.readFileSync(path.join(process.cwd(), 'web', 'css', 'main.css'), 'utf8');
    const componentsCss = fs.readFileSync(path.join(process.cwd(), 'web', 'css', 'components.css'), 'utf8');

    assert.equal(mainCss.includes('./search.css'), true, 'main.css must import search.css');
    assert.equal(baseCss.includes('--accent:'), true);
    assert.equal(baseCss.includes('--accent-subtle:'), true);
    assert.equal(baseCss.includes('--accent-contrast:'), true);
    assert.equal(baseCss.includes('calc(16px * var(--font-scale'), true, 'html font-size must scale via --font-scale');
    assert.equal(baseCss.includes('html[data-theme="midnight"]'), true);
    assert.equal(baseCss.includes('html[data-theme="light"]'), true);
    assert.equal(baseCss.includes('html[data-density="compact"]'), true);
    assert.equal(baseCss.includes('--color-keep: #62a368;') && baseCss.includes('--color-prune: #df584c;'), true, 'Keep/Prune colors must stay immutable');
    assert.equal(componentsCss.includes('.context-menu'), true, 'components.css must style context menu');

    // Verify light theme body contrast >= 4.5:1
    assert.ok(computeContrastRatio('#1d1d1f', '#f6f5f2') >= 4.5, 'Light primary text on #f6f5f2 must be >= 4.5:1');
    assert.ok(computeContrastRatio('#1d1d1f', '#ffffff') >= 4.5, 'Light primary text on #ffffff must be >= 4.5:1');
    assert.ok(computeContrastRatio('#5b5b60', '#f6f5f2') >= 4.5, 'Light secondary text on #f6f5f2 must be >= 4.5:1');
    assert.ok(computeContrastRatio('#5b5b60', '#f0efeb') >= 4.5, 'Light secondary text on #f0efeb must be >= 4.5:1');

    // ----------------------------------------------------------------------
    // 3. Online-First Search Palette (`search_palette.js`)
    // ----------------------------------------------------------------------
    localStorage.removeItem(SEARCH_CHANNEL_STORAGE_KEY);
    const paletteModal = env1.registerId('search-palette-modal');
    const paletteInput = env1.registerId('search-palette-input', 'input');
    const paletteChannelBar = env1.registerId('search-palette-channel-bar');
    const paletteResults = env1.registerId('search-palette-results');
    const paletteFooter = env1.makeEl('div');
    paletteFooter.className = 'search-palette-footer';
    paletteModal.appendChild(paletteFooter);

    modal.open = (sel) => { if (sel === '#search-palette-modal') paletteModal.classList.add('open'); };
    modal.close = (sel) => { if (!sel || sel === '#search-palette-modal') paletteModal.classList.remove('open'); };

    // Populate 8 local tracks matching "晴天" so we assert max-5 cap
    const localCatalog = Array.from({ length: 8 }, (_, i) => ({
      id: 1000 + i,
      title: `晴天 本地版 ${i + 1}`,
      artists: ['周杰伦'],
      album: '叶惠美',
      duration: 240000,
      source_playlist_id: 10,
    }));
    state.set({ catalog: localCatalog, cloudSongs: [], localSongs: [], offlineMode: false });

    const paletteApiCalls = [];
    api.searchMulti = async (q, channels, limit, offset) => {
      paletteApiCalls.push({ q, channels, limit, offset });
      return {
        ok: true,
        data: {
          query: q,
          offset: offset ?? 0,
          limit,
          has_more: true,
          tracks: [
            { id: 0, external_id: 'kg_1', provider: 'kugou_lite', title: '晴天 (线上母带)', artists: ['周杰伦'], duration: 269000, available_channels: ['kugou_lite'] },
          ],
          channel_errors: {},
        },
        error: null,
      };
    };

    searchPalette.init();
    searchPalette.open();
    assert.equal(searchPalette.activeChannel, 'all', 'Ctrl+K palette must open on "all" by default');

    paletteInput.value = '晴天';
    searchPalette.query = '晴天';
    await searchPalette.runOnlineSearch('晴天');
    assert.equal(paletteApiCalls[0].limit, 30);
    assert.equal(paletteApiCalls[0].offset, 0);
    assert.equal(searchPalette.localResults.length, 5, 'Palette local strip ("曲库中") must cap at max 5 matches');

    const localHeader = paletteResults.querySelector('[data-palette-section="local"]');
    const onlineHeader = paletteResults.querySelector('[data-palette-section="online"]');
    assert.ok(localHeader && onlineHeader, 'Palette must render both local and online headers');
    assert.ok(
      paletteResults.children.indexOf(localHeader) < paletteResults.children.indexOf(onlineHeader),
      'Local "曲库中" strip must appear ABOVE online results',
    );

    // Channel persistence in localStorage
    searchPalette.setChannel('migu');
    assert.equal(localStorage.getItem(SEARCH_CHANNEL_STORAGE_KEY), 'migu', 'Selected channel must persist in localStorage');

    // Empty-selection Enter & footer action dispatch switch-tab { tab: 'search', query }
    dispatchedWindowEvents.length = 0;
    searchPalette.activeIndex = -1;
    searchPalette.query = '晴天';
    paletteInput.dispatchEvent({ type: 'keydown', key: 'Enter', isComposing: false, preventDefault() {} });
    assert.equal(dispatchedWindowEvents.length, 1);
    assert.equal(dispatchedWindowEvents[0].type, 'switch-tab');
    assert.equal(dispatchedWindowEvents[0].detail.tab, 'search');
    assert.equal(dispatchedWindowEvents[0].detail.query, '晴天');

    // ----------------------------------------------------------------------
    // 4. Full-Page Search View (`search_view.js`, 100/page offset + has_more)
    // ----------------------------------------------------------------------
    const viewSearchEl = env1.registerId('view-search', 'section');
    const searchViewCalls = [];
    api.searchMulti = async (q, channels, limit, offset) => {
      searchViewCalls.push({ q, channels, limit, offset });
      if (offset === 0) {
        return {
          ok: true,
          data: {
            query: q,
            offset: 0,
            limit,
            has_more: true,
            tracks: [
              { id: 0, external_id: 'mp3_1', provider: 'kuwo', title: '稻香 (MP3)', artists: ['周杰伦'], format: 'mp3', bitrate: 128000, available_channels: ['kuwo'] },
              { id: 0, external_id: 'flac_1', provider: 'migu', title: '稻香 (Hi-Res)', artists: ['周杰伦'], level: 'hires', format: 'flac', sample_rate: 96000, available_channels: ['migu', 'qq'] },
            ],
            channel_errors: { bilibili: 'timeout' },
          },
          error: null,
        };
      }
      return {
        ok: true,
        data: {
          query: q,
          offset,
          limit,
          has_more: false,
          tracks: [
            { id: 0, external_id: 'page2_1', provider: 'qq', title: '稻香 (Live)', artists: ['周杰伦'], format: 'flac', available_channels: ['qq'] },
          ],
          channel_errors: {},
        },
        error: null,
      };
    };

    searchView.init();
    await searchView.open('稻香');
    assert.equal(SEARCH_VIEW_PAGE_SIZE, 100);
    assert.equal(searchViewCalls[0].limit, 100);
    assert.equal(searchViewCalls[0].offset, 0);
    assert.equal(searchView.tracks.length, 2);
    assert.equal(searchView.hasMore, true);
    assert.ok(viewSearchEl.textContent.includes('部分渠道暂未响应'), 'Partial channel errors must render non-blocking notice');
    assert.ok(env1.domById.has('search-view-load-more-btn') || viewSearchEl.querySelector('#search-view-load-more-btn'));

    // Quality sorting ranks FLAC/Hi-Res above 128k MP3
    assert.ok(computeTrackQualityScore(searchView.tracks[1]) > computeTrackQualityScore(searchView.tracks[0]));
    searchView.setSort('quality');
    assert.equal(searchView.getSortedTracks()[0].external_id, 'flac_1', 'Quality sort must rank Hi-Res FLAC first');

    // Pagination loadMore advances offset by 100
    await searchView.loadMore();
    assert.equal(searchViewCalls[1].offset, 100, 'loadMore must request offset=100');
    assert.equal(searchView.tracks.length, 3);
    assert.equal(searchView.hasMore, false);

    // ----------------------------------------------------------------------
    // 5. Right-Click Context Menu (`context_menu.js`)
    // ----------------------------------------------------------------------
    const ctxHost = env1.registerId('context-menu-host');
    const sampleSong = { id: 5511, title: '夜曲', artists: ['周杰伦'], album: '十一月的萧邦', duration: 226000 };

    const discoverItems = buildSongContextMenuItems(sampleSong, { source: 'discover' });
    assert.deepEqual(
      discoverItems.filter(i => i.id).map(i => i.id),
      ['play', 'enqueue_next', 'add_to_playlist', 'toggle_like', 'not_interested', 'similar', 'copy'],
      'Discover context menu must expose all 7 items including 不再推荐',
    );
    const genericItems = buildSongContextMenuItems(sampleSong, { source: 'generic' });
    assert.equal(genericItems.some(i => i.id === 'not_interested'), false, 'Generic context menu must omit 不再推荐');

    // Viewport clamping + keyboard ArrowDown + Esc dismiss
    const menuEl = openSongContextMenu({
      clientX: 1275,
      clientY: 795,
      preventDefault() {},
      stopPropagation() {},
    }, sampleSong, { source: 'discover' });
    assert.ok(menuEl, 'openSongContextMenu must mount menu into #context-menu-host');
    assert.equal(ctxHost.children.length, 1);
    const clampedLeft = parseInt(menuEl.style.left, 10);
    const clampedTop = parseInt(menuEl.style.top, 10);
    assert.ok(clampedLeft <= 1280 - 210 - 8, `Left coordinate ${clampedLeft} must clamp inside viewport`);
    assert.ok(clampedTop <= 800 - 260 - 8, `Top coordinate ${clampedTop} must clamp inside viewport`);

    // Keyboard navigation ArrowDown focuses next item; Escape closes
    env1.mockDoc.dispatchEvent({ type: 'keydown', key: 'ArrowDown', preventDefault() {}, stopPropagation() {} });
    assert.equal(env1.mockDoc.activeElement.getAttribute('data-menu-id'), 'enqueue_next');
    env1.mockDoc.dispatchEvent({ type: 'keydown', key: 'Escape', preventDefault() {}, stopPropagation() {} });
    assert.equal(ctxHost.children.length, 0, 'Escape must dismiss context menu cleanly');
    closeSongContextMenu();

    // ----------------------------------------------------------------------
    // 6. SSE Exponential Backoff Reconnect & Channel Card Declutter (`cloud.js`)
    // ----------------------------------------------------------------------
    assert.deepEqual(SSE_BACKOFF_DELAYS, [1000, 2000, 5000, 15000]);

    const scheduledTimers = [];
    let timerSeq = 1;
    globalThis.setTimeout = (fn, delay) => {
      const id = timerSeq++;
      scheduledTimers.push({ id, fn, delay, cleared: false });
      return id;
    };
    globalThis.clearTimeout = (id) => {
      const found = scheduledTimers.find(t => t.id === id);
      if (found) found.cleared = true;
    };

    const createdSockets = [];
    class FakeEventSource {
      constructor(url) {
        this.url = url;
        this.closed = false;
        this.listeners = new Map();
        this.onopen = null;
        this.onmessage = null;
        this.onerror = null;
        createdSockets.push(this);
      }
      addEventListener(type, fn) {
        if (!this.listeners.has(type)) this.listeners.set(type, new Set());
        this.listeners.get(type).add(fn);
      }
      close() {
        this.closed = true;
      }
      emit(type, dataObj) {
        const evt = { type, data: JSON.stringify(dataObj) };
        this.listeners.get(type)?.forEach(fn => fn(evt));
      }
    }
    globalThis.EventSource = FakeEventSource;

    cloudManager.disconnectEvents();
    state.set({ authenticated: true, offlineMode: false });
    cloudManager.connectEvents();
    assert.equal(createdSockets.length, 1);

    // Handle desktop_lyrics_style SSE live event
    createdSockets[0].emit('desktop_lyrics_style', { style: { font_size: 34, align: 'left' } });
    assert.equal(state.get('settings').desktop_lyrics_style.font_size, 34);
    assert.equal(state.get('settings').desktop_lyrics_style.align, 'left');

    // Trigger 4 errors and verify 1000 -> 2000 -> 5000 -> 15000 backoff progression
    const observedDelays = [];
    for (let i = 0; i < 4; i++) {
      const sock = createdSockets[createdSockets.length - 1];
      sock.onerror();
      assert.equal(sock.closed, true, 'Errored EventSource must be closed before scheduling retry');
      const latestTimer = scheduledTimers[scheduledTimers.length - 1];
      observedDelays.push(latestTimer.delay);
      latestTimer.fn();
    }
    assert.deepEqual(observedDelays, [1000, 2000, 5000, 15000], 'SSE reconnect must follow 1s -> 2s -> 5s -> 15s backoff');
    assert.equal(createdSockets.length, 5);

    // Pending reconnect timer is cancelled cleanly on disconnectEvents() / auth:logout
    const lastSock = createdSockets[createdSockets.length - 1];
    lastSock.onerror();
    const pendingTimer = scheduledTimers[scheduledTimers.length - 1];
    assert.equal(pendingTimer.cleared, false);
    cloudManager.disconnectEvents();
    assert.equal(pendingTimer.cleared, true, 'disconnectEvents must cancel pending SSE reconnect timer');
    assert.equal(cloudManager.eventSource, null);

    // Channel card declutter: raw credentials collapsed inside closed-by-default <details>
    const kugouDef = CHANNEL_CATALOG.find(c => c.id === 'kugou_lite');
    const cardEl = cloudManager.buildChannelCard(
      kugouDef,
      { channel: 'kugou_lite', enabled: true, has_credential: true, api_base: '', vip_label: '概念版VIP' },
      null,
    );
    const detailsEl = cardEl.querySelector('details');
    assert.ok(detailsEl, 'Channel card with credentials must render <details> disclosure');
    assert.equal(detailsEl.hasAttribute('open'), false, 'Channel credentials <details> must be closed by default');
    assert.equal(detailsEl.querySelector('.channel-credentials-summary').textContent, '高级凭据');
    assert.ok(cardEl.textContent.includes('测试连通性'), 'Primary 测试连通性 action must stay visible without expanding');

    // Appearance card mounts into #settings-appearance-host
    const appearanceHost = env1.registerId('settings-appearance-host');
    cloudManager.renderAppearanceCard();
    assert.ok(appearanceHost.querySelector('.appearance-card'), 'Appearance card must mount into #settings-appearance-host');

    // ----------------------------------------------------------------------
    // 7. Desktop Floating Lyrics (`web/desktop_lyrics.html`)
    // ----------------------------------------------------------------------
    const desktopHtml = fs.readFileSync(path.join(process.cwd(), 'web', 'desktop_lyrics.html'), 'utf8');
    assert.equal(desktopHtml.includes('background: transparent'), true);
    assert.equal(desktopHtml.includes('/api/desktop/command'), true);
    assert.equal(desktopHtml.includes('desktop_lyrics_style'), true);
    assert.equal(desktopHtml.includes('payload.window?.locked'), true);

    const scriptMatch = /<script>([\s\S]*?)<\/script>/i.exec(desktopHtml);
    assert.ok(scriptMatch, 'desktop_lyrics.html must contain inline script');
    const sandbox = { window: {}, globalThis: {} };
    vm.runInNewContext(scriptMatch[1], sandbox);
    const computeWordProgress = sandbox.computeWordProgress || sandbox.window.computeWordProgress;
    assert.equal(typeof computeWordProgress, 'function', 'desktop_lyrics.html must define computeWordProgress(words, t)');

    const words = [
      { start: 1.0, dur: 0.5, text: '你' },
      { start: 1.5, dur: 1.0, text: '好' },
    ];
    assert.deepEqual(computeWordProgress(words, 0.5).map(w => w.progress), [0, 0]);
    assert.deepEqual(computeWordProgress(words, 1.25).map(w => w.progress), [0.5, 0]);
    assert.deepEqual(computeWordProgress(words, 2.0).map(w => w.progress), [1, 0.5]);
    assert.deepEqual(computeWordProgress(words, 3.0).map(w => w.progress), [1, 1]);

    console.log('✓ Wave 4 frontend experience tests passed!');
  } finally {
    cloudManager.disconnectEvents();
    closeSongContextMenu();
    globalThis.document = origDoc;
    globalThis.window = origWin;
    if (hadNode) globalThis.Node = origNode;
    else delete globalThis.Node;
    globalThis.EventSource = origEventSource;
    globalThis.setTimeout = origSetTimeout;
    globalThis.clearTimeout = origClearTimeout;
    api.searchMulti = origSearchMulti;
    modal.close = origModalClose;
    modal.open = origModalOpen;
    localStorage.removeItem(APPEARANCE_STORAGE_KEY);
    localStorage.removeItem(SEARCH_CHANNEL_STORAGE_KEY);
    state.set({
      authenticated: false,
      offlineMode: false,
      catalog: [],
      coverAccent: 'hsl(140, 20%, 54%)',
    });
  }
}
