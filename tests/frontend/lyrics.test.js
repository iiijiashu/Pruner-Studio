/**
 * Pruner Studio Frontend Tests - Lyrics Engine
 */
import assert from 'node:assert/strict';
import { LyricsEngine } from '../../web/js/lyrics.js';

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

  console.log('✓ lyrics.js tests passed!');
}
