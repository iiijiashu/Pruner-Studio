import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
import { mock } from 'node:test';
import { api } from '../../web/js/api.js';
import { deckManager } from '../../web/js/deck.js';
import { player } from '../../web/js/player.js';
import { lyricsManager } from '../../web/js/lyrics.js';
import { state } from '../../web/js/state.js';
import { toast } from '../../web/js/toast.js';
import { modal } from '../../web/js/modal.js';
import { stageManager } from '../../web/js/stage.js';
import { openSongContextMenu } from '../../web/js/context_menu.js';

class Element extends EventTarget {
  constructor(id = '', tagName = 'DIV') {
    super();
    this.id = id;
    this.tagName = tagName;
    this.style = {};
    this.dataset = {};
    this.children = [];
    this.classes = new Set();
    this.classList = {
      add: (...names) => names.forEach(name => this.classes.add(name)),
      remove: name => this.classes.delete(name),
      contains: name => this.classes.has(name),
      toggle: (name, active) => {
        const next = active === undefined ? !this.classes.has(name) : Boolean(active);
        if (next) this.classes.add(name);
        else this.classes.delete(name);
        return next;
      },
    };
  }
  setAttribute(name, value) { this[name] = value; }
  focus() { document.activeElement = this; }
  click() { this.dispatchEvent(new Event('click')); }
  contains(el) { return el === this; }
  closest() { return null; }
  querySelector() { return null; }
  appendChild(child) { if (child) this.children.push(child); return child; }
}

function fire(el, type, details = {}) {
  const event = Object.assign(new Event(type, { cancelable: true }), details);
  el.dispatchEvent(event);
  return event;
}

function testDeckButtonContractAndGestures(nodes) {
  // Finding 3: Verify all 7 original Deck IDs live directly on <button> elements in index.html
  const indexHtml = readFileSync(new URL('../../web/index.html', import.meta.url), 'utf8');
  for (const id of [
    'deck-dislike-btn',
    'deck-undo-btn',
    'deck-relisten-btn',
    'deck-skip-btn',
    'deck-play-btn',
    'deck-chorus-jump-btn',
    'deck-like-btn',
  ]) {
    assert.match(
      indexHtml,
      new RegExp(`<button[^>]*\\bid="${id}"`),
      `#${id} must live directly on <button> element`,
    );
  }

  // Finding 1: Swipe gestures (cancel, drag-back to 0, vertical release turn, valid commit)
  const deck = Object.create(deckManager);
  const actions = [];
  deck.handleKeepAction = () => actions.push('keep');
  deck.handleDestructiveAction = () => actions.push('prune');
  const card = new Element();
  const badges = [new Element(), new Element()];
  card.querySelector = name => badges[name === '.indicator-like' ? 0 : 1];
  deck.attachDragListeners(card);
  const point = (target, type, x, y = 0, id = 1, button = 0) =>
    fire(target, type, { clientX: x, clientY: y, pointerType: 'touch', pointerId: id, button });
  const start = (x = 0, y = 0) => point(card, 'pointerdown', x, y);
  const move = (x, y = 0) => point(document, 'pointermove', x, y);
  const up = (x = 0, y = 0) => point(document, 'pointerup', x, y);

  for (const cancel of [
    () => point(document, 'pointercancel', -120, 0),
    () => point(document, 'pointerdown', 0, 0, 2),
    () => move(110, 140),
    () => fire(window, 'blur'),
  ]) {
    for (const distance of [-120, 120]) {
      start(); move(distance); cancel(); up(distance);
      assert.equal(card.classList.contains('is-dragging'), false);
      assert.equal(card.style.transform, 'translate(0, 0) rotate(0deg)');
      assert.deepEqual(badges.map(b => b.style.opacity), ['0', '0']);
    }
  }
  // Drag past threshold then pull back to start (0,0) on release -> zero actions
  start(); move(-120, 0); up(0, 0);
  // Drag past threshold then release with dominant vertical displacement -> zero actions
  start(); move(-120, 0); up(-120, 180);
  // Tap without drag / non-primary button -> zero actions
  start(); up(0, 0);
  point(card, 'pointerdown', 0, 0, 1, 2); move(150); up(150);
  assert.deepEqual(actions, []);

  // Valid horizontal commits
  start(); move(120, 0); up(120, 0);
  start(); up(0, 0);
  start(); move(-120, 0); up(-120, 0);
  assert.deepEqual(actions, ['keep', 'prune']);
  start(); move(-120, 0); deck.detachDrag(); up(-120, 0);
  assert.equal(actions.length, 2);

  // Finding 2: Overlay drawer focus + ArrowLeft must NOT trigger Deck prune shortcuts
  const viewDeck = nodes.get('view-deck');
  const queueDrawer = nodes.get('play-queue-drawer');
  const pendingDrawer = nodes.get('pending-actions-drawer');
  viewDeck.classList.add('active');
  queueDrawer.classList.add('open');
  const drawerButton = new Element('play-queue-close-btn', 'BUTTON');
  drawerButton.closest = s => s.includes('play-queue-drawer') ? queueDrawer : null;
  document.querySelector = s => (s.includes('#play-queue-drawer.open') ? queueDrawer : null);
  const keyResult = deck.handleKeydown({
    key: 'ArrowLeft',
    target: drawerButton,
    preventDefault: mock.fn(),
  });
  assert.equal(keyResult, false);
  assert.equal(actions.length, 2);

  queueDrawer.classList.remove('open');
  pendingDrawer.classList.add('open');
  document.querySelector = s => (s.includes('#pending-actions-drawer.open') ? pendingDrawer : null);
  const pendingKeyResult = deck.handleKeydown({
    key: 'ArrowLeft',
    target: drawerButton,
    preventDefault: mock.fn(),
  });
  assert.equal(pendingKeyResult, false);
  assert.equal(actions.length, 2);
  pendingDrawer.classList.remove('open');
  document.querySelector = () => null;
}

function testRate() {
  const setPositionState = mock.fn();
  const descriptor = Object.getOwnPropertyDescriptor(navigator, 'mediaSession');
  Object.defineProperty(navigator, 'mediaSession', { configurable: true, value: { setPositionState } });
  try {
    const audio = { currentTime: 13 };
    const subject = Object.assign(Object.create(player), {
      directAudio: audio, streamAudio: {}, standbyAudio: {}, audio,
      pushDesktopLyricsState() {}, getNormalizedDuration: () => 100,
    });
    subject.setPlaybackRate(1.5, false);
    assert.deepEqual(setPositionState.mock.calls[0].arguments[0], { duration: 100, position: 13, playbackRate: 1.5 });
    assert.equal(subject.streamAudio.playbackRate, 1.5);
  } finally {
    if (descriptor) Object.defineProperty(navigator, 'mediaSession', descriptor);
    else delete navigator.mediaSession;
  }
}

function createStubScope() {
  const restores = [];
  return {
    stub(obj, name, value) {
      const original = Object.getOwnPropertyDescriptor(obj, name);
      restores.push(() => (original ? Object.defineProperty(obj, name, original) : delete obj[name]));
      obj[name] = value;
    },
    restore() {
      while (restores.length > 0) restores.pop()();
    },
  };
}

async function testDeckCandidateOriginPreservation(nodes) {
  // Finding 4: Switching candidate during Deck preview must preserve _origin: 'deck' & NOT advance global queue
  const queue = [{ id: 10, title: '队列首曲' }, { id: 20, title: '队列次曲' }];
  const deckSong = { id: 30, title: 'Deck 试听曲' };
  let liveState = {
    authenticated: true,
    offlineMode: false,
    playQueue: queue,
    playQueueIndex: 0,
    playMode: 'order',
  };
  const scope = createStubScope();
  try {
    scope.stub(state, 'get', k => (k ? liveState[k] : liveState));
    scope.stub(state, 'set', patch => { liveState = { ...liveState, ...patch }; });

    const playedCalls = [];
    const p = Object.assign(Object.create(player), {
      currentSong: { ...deckSong, _origin: 'deck' },
      activeSrc: 'https://example.com/a.mp3',
    });
    p.playSong = async (song, autoStart, opts) => {
      playedCalls.push({ song, autoStart, opts });
      p.currentSong = { ...song, _origin: opts?.origin || 'queue' };
    };
    p.playNext = mock.fn();
    scope.stub(api, 'getAudioCandidates', async () => ({
      ok: true,
      data: {
        candidates: [{
          id: 'cand-1',
          title: '无损版本',
          url: 'https://example.com/lossless.flac',
          source: 'external',
          source_provider: 'migu',
          bitrate: 950000,
        }],
      },
    }));

    const candList = nodes.get('audio-candidates-list');
    candList.children = [];
    await p.openAudioCandidatesModal(deckSong);

    const findPlayCandidateBtn = (node) => {
      if (!node) return null;
      if (node.tagName === 'BUTTON' && node.textContent === '播放此版本') return node;
      for (const c of node.children || []) {
        const found = findPlayCandidateBtn(c);
        if (found) return found;
      }
      return null;
    };
    const playCandBtn = findPlayCandidateBtn(candList);
    assert.ok(playCandBtn, 'Expected candidate play button in modal list');
    await playCandBtn.dispatchEvent(new Event('click'));
    await new Promise(r => setTimeout(r, 0));

    assert.equal(playedCalls.length, 1);
    assert.equal(playedCalls[0].opts.preserveQueue, true);
    assert.equal(playedCalls[0].opts.origin, 'deck');
    await p.handleTrackEnded();
    assert.equal(p.currentSong._origin, 'deck');
    assert.equal(p.playNext.mock.callCount(), 0);
    assert.equal(liveState.playQueueIndex, 0);
  } finally {
    scope.restore();
  }
}

async function testFollow(nodes) {
  const lyrics = Object.create(lyricsManager);
  lyrics.followTargets = new Map();
  lyrics.renderVisible = () => {};
  lyrics.loadLyrics('[00:00]甲\n[00:05]乙', '[00:05]译文');
  lyrics.showTranslation = true;
  const pane = new Element();
  const pill = nodes.get('stage-resume-follow');
  const lines = [new Element(), new Element()];
  const scroll = mock.fn();
  pane.scrollTo = scroll;
  pane.querySelector = s => s.includes('active') ? lines.find(el => el.classes.has('active')) : lines[s.includes('1') ? 1 : 0];
  lyrics.attachFollow(pane, 'stage');
  const highlight = i => lyrics.highlightTargetLine(pane, '.active', '[data-stage-index="' + i + '"]');
  highlight(0);

  // Finding 6: Delayed programmatic scroll (1001ms later) + scrollend without user intent MUST keep manual = false
  const origNow = Date.now;
  let fakeNow = 10_000;
  Date.now = () => fakeNow;
  try {
    highlight(0);
    fakeNow += 1001;
    fire(pane, 'scrollend');
    fire(pane, 'scroll');
    assert.equal(lyrics.followTargets.get(pane).manual, false);
    assert.equal(pill.hidden, true);
  } finally {
    Date.now = origNow;
  }

  fire(pane, 'wheel'); highlight(1);
  assert.equal(lines[1].classes.has('active'), true);
  assert.equal(lines[0].classes.has('active'), false);
  assert.equal(scroll.mock.callCount(), 2);
  assert.equal(pill.hidden, false);
  pill.click();
  assert.equal(pill.hidden, true);
  assert.equal(scroll.mock.callCount(), 3);

  const scheduling = mock.method(globalThis, 'setTimeout', () => 100);
  try {
    fire(pane, 'touchstart');
    // Ongoing scroll while already manual should still keep manual = true
    fire(pane, 'scroll');
    const [resume, delay] = scheduling.mock.calls.at(-1).arguments;
    assert.equal(delay, 7000);
    resume();
    assert.equal(pill.hidden, true);
    assert.equal(scroll.mock.callCount(), 4);
  } finally {
    scheduling.mock.restore();
  }
  fire(pane, 'keydown', { key: 'PageUp' });
  lyrics.onSeekCallback = mock.fn();
  lyrics.buildLineElement(lyrics.lyricsData[1], 1, 'stage').click();
  assert.deepEqual(lyrics.onSeekCallback.mock.calls[0].arguments, [5]);
  assert.equal(pill.hidden, true);
  const descriptor = Object.getOwnPropertyDescriptor(navigator, 'clipboard');
  const writeText = mock.fn();
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
  try {
    lyrics.currentActiveIndex = 1;
    await lyrics.copyCurrentLine(); await lyrics.copyFullLyrics();
    assert.deepEqual(writeText.mock.calls.map(call => call.arguments[0]), ['乙\n译文', '甲\n乙\n译文']);
    lyrics.showTranslation = false;
    await lyrics.copyCurrentLine();
    assert.deepEqual(writeText.mock.calls[2].arguments, ['乙']);
  } finally {
    if (descriptor) Object.defineProperty(navigator, 'clipboard', descriptor);
    else delete navigator.clipboard;
    lyrics.loadLyrics('');
    assert.equal(lyrics.followTargets.size, 0);
  }
}

async function testEscapeAndFocus(nodes) {
  const { App } = await import('../../web/js/app.js');
  const app = new App();
  const el = id => nodes.get(id);
  const drawer = el('play-queue-drawer');
  const pendingDrawer = el('pending-actions-drawer');
  const popover = el('stage-customize-popover');
  const morePopover = el('player-more-popover');
  const moreBtn = el('player-more-btn');
  const stageBtn = el('player-stage-btn');
  const host = el('context-menu-host');
  const dialog = el('eq-modal');
  let stageOpen = true;
  const scope = createStubScope();
  try {
    scope.stub(state, 'get', key => (key === 'isStageOpen' ? stageOpen : (key === 'currentPlayingSong' ? { id: 1, title: 'S' } : null)));
    scope.stub(stageManager, 'close', () => { stageOpen = false; stageManager.previouslyFocused?.focus?.(); });
    app.renderPlayQueueDrawer = () => {};
    app.renderPendingDrawer = () => {};
    app.initPlayQueueDrawer();
    app.initPendingDrawer();

    // Finding 5: Drawer (z=460) sits above stage customize popover & stage (z=420)
    el('stage-queue-btn').click();
    popover.classList.remove('hidden');
    morePopover.classList.remove('hidden');
    modal.open(dialog);
    openSongContextMenu(null, { title: '歌曲' });
    document.querySelector = s => s.includes('context-menu') && host.classes.has('open') ? host : null;
    document.querySelectorAll = s => s === '.modal-backdrop.open' && dialog.classes.has('open') ? [dialog] : [];

    const layers = () => [
      host.classes.has('open'),
      dialog.classes.has('open'),
      drawer.classes.has('open'),
      !popover.classes.has('hidden'),
      stageOpen,
      !morePopover.classes.has('hidden'),
    ];
    for (let i = 0; i < 6; i++) {
      assert.deepEqual(layers(), Array.from({ length: 6 }, (_, n) => n >= i));
      const e = { key: 'Escape', preventDefault: mock.fn(), stopImmediatePropagation: mock.fn() };
      app.handleSurfaceEscape(e);
      assert.equal(e.stopImmediatePropagation.mock.callCount(), 1);
      if (i === 2) assert.equal(document.activeElement.id, 'stage-queue-btn');
      if (i === 3) {
        assert.equal(document.activeElement.id, 'stage-customize-btn');
        assert.equal(document.activeElement['aria-expanded'], 'false');
      }
      if (i === 5) assert.equal(document.activeElement.id, 'player-more-btn');
    }
    assert.deepEqual(layers(), Array(6).fill(false));

    // Finding 5b: pending-actions-drawer over open immersive stage closes pending drawer BEFORE stage
    stageOpen = true;
    el('header-pending-btn').click();
    assert.equal(pendingDrawer.classList.contains('open'), true);
    app.handleSurfaceEscape({ key: 'Escape', preventDefault: mock.fn(), stopImmediatePropagation: mock.fn() });
    assert.equal(pendingDrawer.classList.contains('open'), false);
    assert.equal(stageOpen, true);
    assert.equal(document.activeElement.id, 'header-pending-btn');

    // Finding 7: Opening Stage via real #player-stage-btn handler restores focus to visible #player-more-btn
    stageOpen = false;
    scope.stub(stageManager, 'open', trigger => {
      stageManager.previouslyFocused = trigger;
      stageOpen = true;
    });
    scope.stub(stageManager, 'toggle', trigger => (stageOpen ? stageManager.close() : stageManager.open(trigger)));
    app.initGlobalPlayerControls();
    morePopover.classList.remove('hidden');
    stageBtn.focus();
    stageBtn.click();
    assert.equal(stageOpen, true);
    assert.equal(morePopover.classList.contains('hidden'), true);
    assert.equal(stageManager.previouslyFocused.id, 'player-more-btn');
    app.handleSurfaceEscape({ key: 'Escape', preventDefault: mock.fn(), stopImmediatePropagation: mock.fn() });
    assert.equal(stageOpen, false);
    assert.equal(document.activeElement.id, 'player-more-btn');
  } finally {
    scope.restore();
    stageManager.previouslyFocused = null;
    modal.activeModal = null;
    modal.previouslyFocused = null;
  }
}

export async function runStageOneTests() {
  const saved = { document: globalThis.document, window: globalThis.window };
  const ids = [
    'view-deck', 'stage-resume-follow', 'context-menu-host', 'play-queue-drawer',
    'stage-queue-btn', 'play-queue-close-btn', 'pending-actions-drawer', 'header-pending-btn',
    'pending-drawer-close-btn', 'eq-modal', 'stage-customize-popover', 'stage-customize-btn',
    'player-more-popover', 'player-more-btn', 'player-stage-btn', 'audio-candidates-list',
    'audio-candidates-title',
  ];
  const nodes = new Map(ids.map(id => [id, new Element(id, id.endsWith('-btn') ? 'BUTTON' : 'DIV')]));
  globalThis.window = new EventTarget();
  globalThis.document = Object.assign(new EventTarget(), {
    createElement: (tag = 'div') => new Element('', String(tag).toUpperCase()),
    getElementById: id => nodes.get(id),
    querySelectorAll: () => [],
    querySelector: () => null,
  });
  const baseScope = createStubScope();
  try {
    baseScope.stub(state, 'set', () => {});
    baseScope.stub(state, 'on', () => {});
    baseScope.stub(toast, 'quiet', () => {});
    testDeckButtonContractAndGestures(nodes);
    testRate();
    await testDeckCandidateOriginPreservation(nodes);
    await testFollow(nodes);
    await testEscapeAndFocus(nodes);
    console.log('✓ Stage 1 gesture, media rate, lyrics, Deck ID & Escape tests passed!');
  } finally {
    baseScope.restore();
    mock.restoreAll();
    Object.assign(globalThis, saved);
  }
}
