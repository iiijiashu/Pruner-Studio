import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { state } from '../../web/js/state.js';
import { api } from '../../web/js/api.js';
import { player } from '../../web/js/player.js';
import { renderDesktopLyricsPreview } from '../../web/js/playback_settings.js';
import { syncPlayingRowIndicators } from '../../web/js/utils.js';

const read = (p) => fs.readFileSync(p, 'utf8');

function makeClassList(initial = []) {
  const set = new Set(initial.filter(Boolean));
  return {
    add: (...cls) => cls.forEach((c) => set.add(c)),
    remove: (...cls) => cls.forEach((c) => set.delete(c)),
    toggle: (c, force) => {
      const next = force === undefined ? !set.has(c) : Boolean(force);
      if (next) set.add(c);
      else set.delete(c);
      return next;
    },
    contains: (c) => set.has(c),
    toArray: () => Array.from(set),
  };
}

function makeSandboxElement(id, initialClasses = []) {
  return {
    id,
    hidden: id === 'dl-more-popover',
    textContent: '',
    value: '',
    offsetWidth: 42,
    dataset: {},
    attrs: {},
    parentElement: {},
    classList: makeClassList(initialClasses),
    style: {
      setProperty(k, v) {
        this[k] = v;
      },
    },
    children: [],
    listeners: {},
    appendChild(child) {
      this.children.push(child);
    },
    contains(el) {
      return this.children.includes(el);
    },
    removeChild(el) {
      this.children = this.children.filter((c) => c !== el);
    },
    remove() {},
    querySelectorAll() {
      return [];
    },
    setAttribute(k, v) {
      this.attrs[k] = String(v);
      this[k] = v;
    },
    getAttribute(k) {
      return this.attrs[k];
    },
    addEventListener(type, fn) {
      if (!this.listeners[type]) this.listeners[type] = [];
      this.listeners[type].push(fn);
    },
    async dispatch(type, evt = {}) {
      const fns = this.listeners[type] || [];
      for (const fn of fns) {
        await fn(evt);
      }
    },
  };
}

async function testDesktopLyricsHtmlOverhaul() {
  const html = read('web/desktop_lyrics.html');
  assert.doesNotMatch(html, /min-height:\s*76px/);
  assert.doesNotMatch(html, /padding-top:\s*14px/);
  for (const surface of ['borderless', 'pill', 'acrylic']) {
    assert.ok(html.includes(`data-surface="${surface}"`), `Missing surface CSS selector: ${surface}`);
  }
  for (const edge of ['left', 'right', 'top', 'bottom', 'top-left', 'top-right', 'bottom-left', 'bottom-right']) {
    assert.ok(html.includes(`data-resize-edge="${edge}"`), `Missing resize edge handle: ${edge}`);
  }
  assert.ok(html.includes('id="dl-more-popover"'));

  const runVm = async (search = '') => {
    const els = new Map();
    const events = new Map();
    const calls = [];
    const getEl = (id) => {
      if (!els.has(id)) els.set(id, makeSandboxElement(id));
      return els.get(id);
    };
    const rootEl = makeSandboxElement('documentElement');
    const docEl = {
      getElementById: getEl,
      documentElement: rootEl,
      hidden: false,
      querySelector: (sel) => (sel === '.floating-controls' ? getEl('__floating_controls') : null),
      listeners: {},
      addEventListener(type, fn) {
        if (!this.listeners[type]) this.listeners[type] = [];
        this.listeners[type].push(fn);
      },
      createElement: () => makeSandboxElement(Symbol()),
      createElementNS: () => makeSandboxElement(Symbol()),
    };
    let esUrl = '';
    const page = {
      URLSearchParams,
      performance: { now: () => 100 },
      location: { search },
      window: { close() {} },
      document: docEl,
      requestAnimationFrame: () => 1,
      cancelAnimationFrame: () => {},
      setTimeout: (fn) => {
        if (typeof fn === 'function' && search === '') fn();
        return 1;
      },
      clearTimeout: () => {},
      ResizeObserver: class {
        observe() {}
      },
      EventSource: class {
        constructor(url) {
          esUrl = url;
        }
        addEventListener(k, fn) {
          events.set(k, fn);
        }
      },
      fetch: async (path, opts) => {
        const method = opts?.method || 'GET';
        const body = opts?.body ? JSON.parse(opts.body) : {};
        calls.push({ path, method, body });
        if (path === '/api/settings' && method === 'PUT') {
          return {
            ok: true,
            json: async () => ({
              ok: true,
              data: { desktop_lyrics_style: body.desktop_lyrics_style || {} },
            }),
          };
        }
        return { ok: true, json: async () => ({ ok: true, data: {} }) };
      },
    };
    vm.runInNewContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], page);
    return { els, events, calls, getEl, rootEl, getEsUrl: () => esUrl };
  };

  // Interactive mode tests
  const sim = await runVm('');
  assert.equal(sim.getEsUrl(), '/api/events');
  const shell = sim.getEl('dl-shell');

  // 1. Resize handle dispatches /resize-start with exact edge, NOT /drag
  await shell.dispatch('pointerdown', {
    button: 0,
    target: {
      closest: (sel) => (sel === '.dl-resize-handle' ? { dataset: { resizeEdge: 'bottom-right' } } : null),
    },
  });
  assert.equal(sim.calls.at(-1).path, '/api/desktop/lyrics-window/resize-start');
  assert.deepEqual(sim.calls.at(-1).body, { edge: 'bottom-right' });

  // 2. #dl-more-btn toggles #dl-more-popover
  const pop = sim.getEl('dl-more-popover');
  assert.equal(pop.hidden, true);
  await sim.getEl('dl-more-btn').dispatch('click', {});
  assert.equal(pop.hidden, false);
  await sim.getEl('dl-more-btn').dispatch('click', {});
  assert.equal(pop.hidden, true);

  // 3. #dl-surface-btn cycles borderless -> pill -> acrylic
  await sim.getEl('dl-surface-btn').dispatch('click', {});
  assert.equal(sim.calls.at(-1).body.desktop_lyrics_style.surface_style, 'pill');
  assert.equal(shell.dataset.surface, 'pill');
  await sim.getEl('dl-surface-btn').dispatch('click', {});
  assert.equal(sim.calls.at(-1).body.desktop_lyrics_style.surface_style, 'acrylic');
  assert.equal(shell.dataset.surface, 'acrylic');

  // 4. #dl-center-btn posts /center
  await sim.getEl('dl-center-btn').dispatch('click', {});
  assert.equal(sim.calls.at(-1).path, '/api/desktop/lyrics-window/center');

  // 5. Ctrl+Wheel scales font size and saves
  await shell.dispatch('wheel', { ctrlKey: true, deltaY: -120 });
  assert.equal(sim.rootEl.style['--dl-font-size'], '30px');
  assert.equal(sim.calls.at(-1).path, '/api/settings');
  assert.equal(sim.calls.at(-1).body.desktop_lyrics_style.font_size, 30);

  // 6. wheel_volume disabled by default, enabled when set true
  const beforeWheelCount = sim.calls.length;
  await shell.dispatch('wheel', { ctrlKey: false, deltaY: -120 });
  assert.equal(sim.calls.length, beforeWheelCount);
  sim.events.get('desktop_lyrics_style')({
    data: JSON.stringify({ style: { wheel_volume: true } }),
  });
  await shell.dispatch('wheel', { ctrlKey: false, deltaY: -120 });
  assert.equal(sim.calls.at(-1).path, '/api/desktop/command');
  assert.equal(sim.calls.at(-1).body.action, 'volume_up');

  // 7. Single-line intro shows next_line in rowA and hides rowB even when title is present
  sim.events.get('desktop_lyrics_style')({
    data: JSON.stringify({ style: { mode: 'single' } }),
  });
  sim.events.get('desktop_lyrics')({
    data: JSON.stringify({
      title: 'Intro Track',
      artist: 'Artist',
      line: '',
      next_line: 'First incoming lyric line',
      is_playing: true,
    }),
  });
  assert.equal(sim.getEl('dl-row-a').textContent, 'First incoming lyric line');
  assert.equal(sim.getEl('dl-row-a').classList.contains('is-active-lrc'), true);
  assert.equal(sim.getEl('dl-row-b').classList.contains('is-hidden'), true);

  // Preview isolation (?preview=1)
  const previewSim = await runVm('?preview=1');
  assert.equal(previewSim.getEsUrl(), '/api/events?lyrics_preview=1');
  assert.equal(previewSim.getEl('__floating_controls').hidden, true);
  const pBefore = previewSim.calls.length;
  await previewSim.getEl('dl-next-btn').dispatch('click', {});
  assert.equal(previewSim.calls.length, pBefore);
}

function testZeroSsePreviewAndUnifiedSettings() {
  const indexHtml = read('web/index.html');
  const lyricsSection = indexHtml.split('<section id="desktop-lyrics-settings"')[1].split('</section>')[0];
  assert.doesNotMatch(lyricsSection, /<iframe\b/i);
  assert.ok(lyricsSection.includes('id="settings-lyrics-preview-stage"'));
  for (const backdrop of ['dark', 'light', 'wallpaper']) {
    assert.ok(lyricsSection.includes(`data-preview-backdrop="${backdrop}"`));
  }
  for (const id of [
    'settings-surface_style',
    'settings-font_weight',
    'settings-shadow_strength',
    'settings-wheel_volume',
    'settings-font_size-val',
    'settings-shadow_strength-val',
    'settings-bg_opacity-val',
    'settings-lyrics-center-btn',
  ]) {
    assert.ok(lyricsSection.includes(`id="${id}"`), `Missing settings control ${id}`);
  }
  for (const sec of ['playback-settings', 'desktop-lyrics-settings', 'about-settings']) {
    assert.match(indexHtml, new RegExp(`<section id="${sec}" class="cloud-section-card playback-settings-card"`));
  }

  const origDoc = globalThis.document;
  const origES = globalThis.EventSource;
  let esConstructed = 0;
  const domMap = new Map();
  const getNode = (id) => {
    if (!domMap.has(id)) domMap.set(id, makeSandboxElement(id));
    return domMap.get(id);
  };
  try {
    globalThis.EventSource = class {
      constructor() {
        esConstructed += 1;
      }
    };
    globalThis.document = { getElementById: getNode };
    renderDesktopLyricsPreview({
      preset: 'emerald_flow',
      surface_style: 'pill',
      font_size: 32,
      font_weight: 600,
      shadow_strength: 1.2,
      bg_opacity: 0.4,
      align: 'left',
      mode: 'single',
      stroke: true,
      show_translation: false,
    });
    assert.equal(esConstructed, 0);
    assert.equal(getNode('settings-font_size-val').textContent, '32 px');
    assert.equal(getNode('settings-shadow_strength-val').textContent, '1.2×');
    assert.equal(getNode('settings-bg_opacity-val').textContent, '40%');
    const box = getNode('settings-lyrics-preview-box');
    assert.equal(box.dataset.surface, 'pill');
    assert.equal(box.className, 'lyrics-preview-box align-left');
    assert.equal(box.style.borderRadius, '14px');
    assert.equal(getNode('settings-lyrics-preview-next').hidden, true);
    assert.equal(getNode('settings-lyrics-preview-trans').hidden, true);
    assert.equal(getNode('settings-lyrics-preview-line').style.fontWeight, '600');
  } finally {
    globalThis.document = origDoc;
    globalThis.EventSource = origES;
  }
}

function testCssDesignConstraintsAndSwitchGuard() {
  const baseCss = read('web/css/base.css');
  const compCss = read('web/css/components.css');
  const cloudCss = read('web/css/cloud.css');
  const playerCss = read('web/css/player.css');
  const discoverCss = read('web/css/discover.css');
  const catalogCss = read('web/css/catalog.css');

  for (const [name, content] of [
    ['base.css', baseCss],
    ['components.css', compCss],
    ['cloud.css', cloudCss],
    ['player.css', playerCss],
    ['discover.css', discoverCss],
    ['catalog.css', catalogCss],
  ]) {
    assert.doesNotMatch(content, /backdrop-filter\s*:/i, `Forbidden backdrop-filter in ${name}`);
  }
  assert.ok(compCss.includes('.playback-settings-card input[type="checkbox"]:not(.switch input)'));
  assert.ok(baseCss.includes('[data-gpu-mode="eco"] .btn:hover'));
  assert.ok(baseCss.includes('html[data-game-mode="true"] .catalog-song-card:hover .catalog-card-cover'));
  assert.ok(baseCss.includes('transform: none !important;'));
}

async function testPlayingRowIndicatorsAndConfirmedHeartPop() {
  const rows = [
    makeSandboxElement('row-1'),
    makeSandboxElement('row-2'),
  ];
  rows[0].dataset.trackKey = 'netease:42';
  rows[1].dataset.trackKey = 'netease:99';
  const bars0 = makeSandboxElement('bars-0', ['playing-eq-bars']);
  const bars1 = makeSandboxElement('bars-1', ['playing-eq-bars']);
  rows[0].querySelector = (sel) => (sel === '.playing-eq-bars' ? bars0 : null);
  rows[1].querySelector = (sel) => (sel === '.playing-eq-bars' ? bars1 : null);

  const root = { querySelectorAll: () => rows };
  syncPlayingRowIndicators({ id: 42, title: 'T', artists: ['A'] }, true, root);
  assert.equal(rows[0].classList.contains('is-playing-row'), true);
  assert.equal(rows[1].classList.contains('is-playing-row'), false);
  assert.equal(bars0.classList.contains('is-paused'), false);

  syncPlayingRowIndicators({ id: 42, title: 'T', artists: ['A'] }, false, root);
  assert.equal(rows[0].classList.contains('is-playing-row'), true);
  assert.equal(bars0.classList.contains('is-paused'), true);

  // Confirmed-only .heart-pop test
  const origDoc = globalThis.document;
  const origLikeTrack = api.likeTrack;
  const origSubmitFeedback = api.submitFeedback;
  const origCurrentSong = player.currentSong;
  const savedState = {
    authenticated: state.get('authenticated'),
    offlineMode: state.get('offlineMode'),
    hasUnknownResult: state.get('hasUnknownResult'),
    likedSongIds: new Set(state.get('likedSongIds') || []),
    sessionGeneration: state.get('sessionGeneration'),
  };
  const heartBtns = new Map([
    ['player-heart-btn', makeSandboxElement('player-heart-btn')],
    ['stage-heart-btn', makeSandboxElement('stage-heart-btn')],
  ]);
  try {
    globalThis.document = {
      getElementById: (id) => heartBtns.get(id) || null,
      createElement: () => makeSandboxElement(Symbol()),
      createElementNS: () => makeSandboxElement(Symbol()),
      body: makeSandboxElement('body'),
    };
    state.set({
      authenticated: true,
      offlineMode: false,
      hasUnknownResult: false,
      likedSongIds: new Set(),
      sessionGeneration: 7,
    });
    const song = { id: 42, title: 'Test Track', provider: 'netease', source: 'netease' };
    player.currentSong = song;
    api.submitFeedback = async () => ({ ok: true });

    // Case 1: API failure rolls back liked state and NEVER sets heart-pop
    api.likeTrack = async () => ({ ok: false, error: { message: 'boom' } });
    await player.toggleLikeCurrentSong(song);
    assert.equal(state.isSongLiked(42), false);
    assert.equal(heartBtns.get('player-heart-btn').classList.contains('heart-pop'), false);

    // Case 2: API confirmed sets is-liked and triggers heart-pop
    api.likeTrack = async (id) => ({ ok: true, data: { confirmed_ids: [id] } });
    await player.toggleLikeCurrentSong(song);
    assert.equal(state.isSongLiked(42), true);
    assert.equal(heartBtns.get('player-heart-btn').classList.contains('heart-pop'), true);
    assert.equal(heartBtns.get('stage-heart-btn').classList.contains('heart-pop'), true);
  } finally {
    globalThis.document = origDoc;
    api.likeTrack = origLikeTrack;
    api.submitFeedback = origSubmitFeedback;
    player.currentSong = origCurrentSong;
    state.set(savedState);
    player.heartWritePending = false;
  }
}

export async function runPhase9Tests() {
  await testDesktopLyricsHtmlOverhaul();
  testZeroSsePreviewAndUnifiedSettings();
  testCssDesignConstraintsAndSwitchGuard();
  await testPlayingRowIndicatorsAndConfirmedHeartPop();
  console.log('✓ Phase 9 desktop lyrics overhaul & tactile UI tests passed!');
}
