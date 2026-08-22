/**
 * Pruner Studio Frontend Tests - Lyrics Engine
 */
import assert from 'node:assert/strict';
import { LyricsEngine, lyricsManager } from '../../web/js/lyrics.js';

export async function runLyricsTests() {
  console.log('Testing lyrics.js...');

  // 1. Standard LRC parsing
  const lrc = `
[ti:Test Song]
[ar:Test Artist]
[00:05.50]First line of lyric
[00:12.30]Second line of lyric
[00:20.00]Third line of lyric
[01:05.10]Chorus line starts here
  `;

  const parsed = LyricsEngine.parseLrc(lrc);
  assert.equal(parsed.length, 4, 'Should parse exactly 4 timestamped lines');
  assert.equal(parsed[0].time, 5.5);
  assert.equal(parsed[0].text, 'First line of lyric');
  assert.equal(parsed[1].time, 12.3);
  assert.equal(parsed[3].time, 65.1);

  // 2. Translation mapping
  const tlrc = `
[00:05.50]第一行歌词翻译
[00:12.30]第二行歌词翻译
[01:05.10]副歌翻译
  `;

  const parsedWithTranslation = LyricsEngine.parseLrc(lrc, tlrc);
  assert.equal(parsedWithTranslation[0].translation, '第一行歌词翻译');
  assert.equal(parsedWithTranslation[1].translation, '第二行歌词翻译');
  assert.equal(parsedWithTranslation[2].translation, ''); // No translation for line 3
  assert.equal(parsedWithTranslation[3].translation, '副歌翻译');

  // 3. Binary search getActiveLineIndex
  assert.equal(LyricsEngine.getActiveLineIndex(parsed, 0), -1, 'Before first lyric should return -1');
  assert.equal(LyricsEngine.getActiveLineIndex(parsed, 5.4), -1);
  assert.equal(LyricsEngine.getActiveLineIndex(parsed, 5.5), 0);
  assert.equal(LyricsEngine.getActiveLineIndex(parsed, 10.0), 0);
  assert.equal(LyricsEngine.getActiveLineIndex(parsed, 12.3), 1);
  assert.equal(LyricsEngine.getActiveLineIndex(parsed, 30.0), 2);
  assert.equal(LyricsEngine.getActiveLineIndex(parsed, 90.0), 3);

  // 4. Edge cases
  assert.deepEqual(LyricsEngine.parseLrc(''), []);
  assert.deepEqual(LyricsEngine.parseLrc(null), []);
  assert.equal(LyricsEngine.getActiveLineIndex([], 10), -1);

  // 5. Deck rolling lyrics: render, keyboard interaction, and active-line sync
  await runDeckLyricsViewTests();

  console.log('✓ lyrics.js tests passed!');
}

async function runDeckLyricsViewTests() {
  const FakeNode = class FakeNode {};
  const hadNode = Object.prototype.hasOwnProperty.call(globalThis, 'Node');
  const originalNode = globalThis.Node;
  const originalDocument = globalThis.document;

  const matchesSelector = (el, selector) => {
    const classMatch = /^\.([\w-]+(?:\.[\w-]+)*)$/.exec(selector);
    if (classMatch) {
      const classes = String(el.className).split(/\s+/);
      return classMatch[1].split('.').every(c => classes.includes(c));
    }
    const dataMatch = /^\[data-deck-index="(-?\d+)"\]$/.exec(selector);
    if (dataMatch) {
      return String(el.dataset.deckIndex) === dataMatch[1];
    }
    return false;
  };

  const makeElement = (tag) => {
    const el = Object.create(FakeNode.prototype);
    Object.assign(el, {
      tagName: String(tag).toUpperCase(),
      className: '',
      children: [],
      dataset: {},
      _listeners: {},
      offsetTop: 100,
      offsetHeight: 40,
      scrollTop: 0,
      clientHeight: 280,
      scrollTo() {},
      scrollIntoView() {},
      setAttribute(name, value) {
        if (name === 'id') el.id = String(value);
      },
      addEventListener(type, handler) {
        (el._listeners[type] = el._listeners[type] || []).push(handler);
      },
      appendChild(child) {
        el.children.push(child);
        return child;
      },
      querySelector(selector) {
        return el.querySelectorAll(selector)[0] || null;
      },
      querySelectorAll(selector) {
        return el.children.filter(child => matchesSelector(child, selector));
      },
    });
    el.classList = {
      add: (...cls) => {
        const set = new Set(String(el.className).split(/\s+/).filter(Boolean));
        cls.forEach(c => set.add(c));
        el.className = [...set].join(' ');
      },
      remove: (...cls) => {
        const set = new Set(String(el.className).split(/\s+/).filter(Boolean));
        cls.forEach(c => set.delete(c));
        el.className = [...set].join(' ');
      },
      contains: (c) => String(el.className).split(/\s+/).includes(c),
    };
    let text = '';
    Object.defineProperty(el, 'textContent', {
      get: () => text,
      set(value) {
        text = String(value);
        if (text === '') el.children = [];
      },
    });
    return el;
  };

  const deckContainer = makeElement('div');
  globalThis.Node = FakeNode;
  globalThis.document = {
    getElementById: (id) => (id === 'deck-lyrics-container' ? deckContainer : null),
    createElement: makeElement,
    createTextNode: (t) => ({ textContent: String(t), nodeType: 3 }),
  };

  try {
    const seekTargets = [];
    lyricsManager.init('lyrics-drawer-scroller', (seconds) => seekTargets.push(seconds));
    lyricsManager.loadLyrics('[00:01.00]第一行\n[00:02.00]第二行\n[00:03.00]第三行', '');

    // Full rolling list is rendered into the deck container
    assert.equal(deckContainer.children.length, 3, 'renderDeckLyrics must render every lyric line');
    assert.equal(deckContainer.children[0].dataset.deckIndex, '0');

    // Space/Enter on a focused line seeks AND stops propagation so the deck
    // level Space (skip-to-end) shortcut cannot double-fire.
    const firstLine = deckContainer.children[0];
    const keydown = firstLine._listeners.keydown[0];
    let prevented = 0;
    let stopped = 0;
    keydown({ key: ' ', preventDefault: () => { prevented += 1; }, stopPropagation: () => { stopped += 1; } });
    assert.equal(prevented, 1, 'Line keydown must preventDefault');
    assert.equal(stopped, 1, 'Line keydown must stopPropagation to avoid the deck skip shortcut double-fire');
    assert.equal(seekTargets[seekTargets.length - 1], 1, 'Line keydown must seek to the line time');

    // Non-activation keys must not seek
    keydown({ key: 'ArrowDown', preventDefault: () => {}, stopPropagation: () => {} });
    assert.equal(seekTargets.length, 1, 'Non activation keys must not trigger seek');

    // updateTime drives the rolling active line
    lyricsManager.updateTime(2.5);
    assert.equal(deckContainer.children[1].classList.contains('active'), true, 'Line 2 must be active at t=2.5');
    assert.equal(deckContainer.children[0].classList.contains('active'), false, 'Previous line must lose active');
  } finally {
    if (hadNode) {
      globalThis.Node = originalNode;
    } else {
      delete globalThis.Node;
    }
    globalThis.document = originalDocument;
  }
}
