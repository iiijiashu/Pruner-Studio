/**
 * Pruner Studio Frontend Tests - Utils
 */
import assert from 'node:assert/strict';
import { isSafeUrl, getSafeImageUrl, formatDuration, formatArtists, deepClone, createElement, estimateRemainingTime, getCoverAccent } from '../../web/js/utils.js';

export async function runUtilsTests() {
  console.log('Testing utils.js...');

  // 1. isSafeUrl tests
  assert.equal(isSafeUrl('https://p1.music.126.net/abc.jpg'), true, 'https should be safe');
  assert.equal(isSafeUrl('http://p1.music.126.net/abc.jpg'), true, 'http should be safe');
  assert.equal(isSafeUrl('data:image/png;base64,iVBORw0KGgoAAA'), true, 'data:image should be safe');
  assert.equal(isSafeUrl('/api/audio?id=123'), true, 'relative path starting with / should be safe');
  assert.equal(isSafeUrl('javascript:alert(1)'), false, 'javascript: must be rejected');
  assert.equal(isSafeUrl('vbscript:msgbox(1)'), false, 'vbscript: must be rejected');
  assert.equal(isSafeUrl('//evil.com/payload'), false, 'protocol-relative // should be rejected');
  assert.equal(isSafeUrl(''), false, 'empty string should be rejected');
  assert.equal(isSafeUrl(null), false, 'null should be rejected');

  // 2. getSafeImageUrl fallback test
  const safeImg = getSafeImageUrl('https://example.com/cover.jpg');
  assert.equal(safeImg, 'https://example.com/cover.jpg');
  const unsafeImg = getSafeImageUrl('javascript:attack()', 'Cover');
  assert.ok(unsafeImg.startsWith('data:image/svg+xml'), 'Unsafe URL must return fallback SVG data URI');

  // 3. formatDuration tests
  assert.equal(formatDuration(0), '0:00');
  assert.equal(formatDuration(45), '0:45');
  assert.equal(formatDuration(125), '2:05');
  assert.equal(formatDuration(3600), '60:00');
  // Auto milliseconds detection
  assert.equal(formatDuration(215000), '3:35');
  assert.equal(formatDuration(null), '0:00');

  // 4. formatArtists tests
  assert.equal(formatArtists(['Artist A', 'Artist B']), 'Artist A / Artist B');
  assert.equal(formatArtists([{ name: 'Artist 1' }, { name: 'Artist 2' }]), 'Artist 1 / Artist 2');
  assert.equal(formatArtists('Solo Artist'), 'Solo Artist');
  assert.equal(formatArtists([]), '未知歌手');
  assert.equal(formatArtists(null), '未知歌手');

  // 5. estimateRemainingTime tests
  assert.equal(estimateRemainingTime(0), '约 0 分钟');
  assert.equal(estimateRemainingTime(10), '约 1 分钟');
  assert.equal(estimateRemainingTime(100), '约 6 分钟');

  // 6. getCoverAccent tests
  const accent1 = getCoverAccent('song_123');
  assert.ok(accent1.startsWith('hsl('), 'Accent color must be an HSL color string');

  // 7. deepClone tests
  const original = { id: 1, nested: { list: [1, 2, 3] } };
  const cloned = deepClone(original);
  assert.deepEqual(cloned, original);
  cloned.nested.list.push(4);
  assert.equal(original.nested.list.length, 3, 'Original should remain unmodified');

  // 8. createElement supports both documented child forms and style props.
  const originalDocument = globalThis.document;
  const originalNode = globalThis.Node;
  class FakeNode {
    constructor(tag = '') {
      this.tagName = tag.toUpperCase();
      this.children = [];
      this.attributes = new Map();
      this.dataset = {};
      this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
    }
    appendChild(child) { this.children.push(child); }
    setAttribute(key, value) { this.attributes.set(key, value); }
    addEventListener() {}
  }
  try {
    globalThis.Node = FakeNode;
    globalThis.document = {
      createElement: tag => new FakeNode(tag),
      createTextNode: text => ({ textContent: text }),
    };
    const child = createElement('span', { text: 'child' });
    const propsParent = createElement('div', { style: 'display:flex', children: [child] });
    const positionalParent = createElement('div', {}, [child]);
    assert.equal(propsParent.children.length, 1, 'props.children must render');
    assert.equal(positionalParent.children.length, 1, 'positional children must render');
    assert.equal(propsParent.attributes.get('style'), 'display:flex');
  } finally {
    globalThis.document = originalDocument;
    globalThis.Node = originalNode;
  }

  console.log('✓ utils.js tests passed!');
}
