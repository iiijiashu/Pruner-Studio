/**
 * Pruner Studio - Zero-Reflow Synchronized LRC & YRC Word-by-Word Lyrics Engine
 * Parses standard LRC + NetEase YRC + translations + romaji.
 * Renders ONLY currently visible containers; updates only the single active line's word highlight
 * via requestAnimationFrame (strictly paused on document.hidden, window blur, or paused playback).
 */
import { toast } from './toast.js';
import { createElement } from './utils.js';

function preferredScrollBehavior() {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return 'smooth';
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
  } catch {
    return 'smooth';
  }
}

function parseTimestampMap(lrcString) {
  const entries = [];
  if (!lrcString || typeof lrcString !== 'string') return entries;
  const timeRegex = /\[(\d{2}):(\d{2})(?:\.(\d{2,3}))?\]/g;
  const lines = lrcString.split('\n');
  for (const line of lines) {
    timeRegex.lastIndex = 0;
    const text = line.replace(timeRegex, '').trim();
    if (!text) continue;
    timeRegex.lastIndex = 0;
    let match;
    while ((match = timeRegex.exec(line)) !== null) {
      const minutes = parseInt(match[1], 10);
      const seconds = parseInt(match[2], 10);
      const millis = match[3] ? parseInt(match[3].padEnd(3, '0').slice(0, 3), 10) : 0;
      const time = minutes * 60 + seconds + millis / 1000;
      entries.push({ time, text });
    }
  }
  entries.sort((a, b) => a.time - b.time);
  return entries;
}

function findNearestTimedText(entries, targetTime, toleranceSec = 0.65) {
  if (!entries || entries.length === 0) return '';
  let bestText = '';
  let bestDiff = toleranceSec;
  for (let i = 0; i < entries.length; i++) {
    const diff = Math.abs(entries[i].time - targetTime);
    if (diff <= bestDiff) {
      bestDiff = diff;
      bestText = entries[i].text;
    } else if (entries[i].time > targetTime + toleranceSec) {
      break;
    }
  }
  return bestText;
}

export class LyricsEngine {
  /**
   * Parse LRC format string into array of timestamped lines with optional translations & romaji.
   * @param {string} lrcString
   * @param {string} [tlyricsString='']
   * @param {string} [romalrcString='']
   * @returns {Array<{ time: number, text: string, translation?: string, romaji?: string, words?: null, isYrc?: boolean }>}
   */
  static parseLrc(lrcString, tlyricsString = '', romalrcString = '') {
    if (!lrcString || typeof lrcString !== 'string') {
      return [];
    }

    const timeRegex = /\[(\d{2}):(\d{2})(?:\.(\d{2,3}))?\]/g;
    const lines = lrcString.split('\n');
    const result = [];

    const translationMap = new Map();
    const translationList = parseTimestampMap(tlyricsString);
    for (const item of translationList) {
      translationMap.set(Math.round(item.time * 10) / 10, item.text);
    }

    const romajiList = parseTimestampMap(romalrcString);

    for (const line of lines) {
      if (line.trim().startsWith('{')) continue; // skip JSON metadata header lines
      let match;
      timeRegex.lastIndex = 0;
      const rawText = line.replace(timeRegex, '').trim();
      if (!rawText) continue;

      timeRegex.lastIndex = 0;
      while ((match = timeRegex.exec(line)) !== null) {
        const minutes = parseInt(match[1], 10);
        const seconds = parseInt(match[2], 10);
        const millis = match[3] ? parseInt(match[3].padEnd(3, '0').slice(0, 3), 10) : 0;
        const time = minutes * 60 + seconds + millis / 1000;
        const timeKey = Math.round(time * 10) / 10;

        const translation = translationMap.get(timeKey)
          || findNearestTimedText(translationList, time, 0.45);
        const romaji = findNearestTimedText(romajiList, time, 0.45);

        result.push({
          time,
          text: rawText,
          translation: translation || '',
          romaji: romaji || '',
          words: null,
          isYrc: false,
        });
      }
    }

    result.sort((a, b) => a.time - b.time);
    return result;
  }

  /**
   * Parse NetEase YRC word-by-word lyrics: `[lineStartMs,lineDurMs](wordStartMs,wordDurMs,0)word...`
   * @param {string} yrcString
   * @param {string} [tlyricsString='']
   * @param {string} [romalrcString='']
   * @returns {Array<{ time: number, duration: number, text: string, words: Array<{ start: number, duration: number, text: string }>, translation: string, romaji: string, isYrc: boolean }>}
   */
  static parseYrc(yrcString, tlyricsString = '', romalrcString = '') {
    if (!yrcString || typeof yrcString !== 'string') return [];

    const translationList = parseTimestampMap(tlyricsString);
    const romajiList = parseTimestampMap(romalrcString);
    const lineHeaderRe = /^\[(\d+),(\d+)\](.*)$/;
    const wordTokenRe = /\((\d+),(\d+),\d+\)([^(]*)/g;

    const lines = yrcString.split('\n');
    const result = [];

    for (const rawLine of lines) {
      const trimmed = rawLine.trim();
      if (!trimmed || trimmed.startsWith('{')) continue;

      const headerMatch = lineHeaderRe.exec(trimmed);
      if (!headerMatch) continue;

      const lineStartMs = parseInt(headerMatch[1], 10);
      const lineDurMs = parseInt(headerMatch[2], 10);
      const body = headerMatch[3] || '';

      const words = [];
      wordTokenRe.lastIndex = 0;
      let wordMatch;
      let combined = '';
      while ((wordMatch = wordTokenRe.exec(body)) !== null) {
        const wStartMs = parseInt(wordMatch[1], 10);
        const wDurMs = parseInt(wordMatch[2], 10);
        const wText = wordMatch[3];
        if (!wText) continue;
        combined += wText;
        words.push({
          start: wStartMs / 1000,
          duration: Math.max(0.02, wDurMs / 1000),
          text: wText,
        });
      }

      const cleanText = combined.trim();
      if (!cleanText || words.length === 0) continue;

      const time = lineStartMs / 1000;
      result.push({
        time,
        duration: Math.max(0.1, lineDurMs / 1000),
        text: cleanText,
        words,
        translation: findNearestTimedText(translationList, time, 0.65),
        romaji: findNearestTimedText(romajiList, time, 0.65),
        isYrc: true,
      });
    }

    result.sort((a, b) => a.time - b.time);
    return result;
  }

  /**
   * Computes normalized fill progress [0..1] for a word at currentTime.
   * @param {{ start: number, duration: number }} word
   * @param {number} currentTime
   * @returns {number}
   */
  static computeWordProgress(word, currentTime) {
    if (!word) return 0;
    if (currentTime <= word.start) return 0;
    if (currentTime >= word.start + word.duration) return 1;
    return Math.max(0, Math.min(1, (currentTime - word.start) / word.duration));
  }

  /**
   * Find index of active lyric line at currentTime using binary search.
   * @param {Array<{ time: number, text: string }>} lyrics
   * @param {number} currentTime
   * @returns {number}
   */
  static getActiveLineIndex(lyrics, currentTime) {
    if (!lyrics || lyrics.length === 0) return -1;

    let low = 0;
    let high = lyrics.length - 1;
    let index = -1;

    while (low <= high) {
      const mid = Math.floor((low + high) / 2);
      if (lyrics[mid].time <= currentTime) {
        index = mid;
        low = mid + 1;
      } else {
        high = mid - 1;
      }
    }

    return index;
  }
}

class LyricsViewManager {
  constructor() {
    this.lyricsData = [];
    this.hasYrc = false;
    this.ttmlSource = '';
    this.currentSongId = null;
    this.timeOffset = 0;
    this.blurDepthEnabled = false;
    this.currentActiveIndex = -1;
    this.followTargets = new Map();
    this.container = null; // lyrics-drawer-scroller
    this.onSeekCallback = null;
    this.showTranslation = true;
    this.showRomaji = false;

    // Track which targets have been rendered for the current lyricsData version
    this.renderVersion = 0;
    this.renderedVersions = { deck: -1, drawer: -1, stage: -1 };

    // Active-line-only rAF word highlight loop
    this.rafId = null;
    this.timeProvider = null;
    this.isPlaybackActive = false;
    this.isWindowBlurred = false;

    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
          this.stopWordSync();
        } else if (this.isPlaybackActive) {
          this.ensureWordSync();
        }
      });
    }
    if (typeof window !== 'undefined') {
      window.addEventListener('blur', () => {
        this.isWindowBlurred = true;
        this.stopWordSync();
      });
      window.addEventListener('focus', () => {
        this.isWindowBlurred = false;
        if (this.isPlaybackActive) {
          this.ensureWordSync();
        }
      });
    }
  }

  getOffsetStorageKey(songId) {
    return songId !== null && songId !== undefined && songId !== ''
      ? `pruner_lyric_offset_${String(songId)}`
      : null;
  }

  loadSongOffset(songId) {
    this.currentSongId = songId ?? null;
    const key = this.getOffsetStorageKey(songId);
    if (!key || typeof localStorage === 'undefined') {
      this.timeOffset = 0;
      this.syncOffsetLabelUI();
      return 0;
    }
    try {
      const raw = localStorage.getItem(key);
      const parsed = raw !== null ? Number(raw) : 0;
      this.timeOffset = Number.isFinite(parsed) ? Math.max(-5, Math.min(5, Math.round(parsed * 10) / 10)) : 0;
    } catch {
      this.timeOffset = 0;
    }
    this.syncOffsetLabelUI();
    return this.timeOffset;
  }

  setSongOffset(offsetSec) {
    const clamped = Number.isFinite(offsetSec)
      ? Math.max(-5, Math.min(5, Math.round(offsetSec * 10) / 10))
      : 0;
    this.timeOffset = clamped;
    const key = this.getOffsetStorageKey(this.currentSongId);
    if (key && typeof localStorage !== 'undefined') {
      try {
        if (clamped === 0) localStorage.removeItem(key);
        else localStorage.setItem(key, String(clamped));
      } catch {}
    }
    this.syncOffsetLabelUI();
    return clamped;
  }

  getSongOffset() {
    return this.timeOffset || 0;
  }

  adjustSongOffset(deltaSec) {
    return this.setSongOffset((this.timeOffset || 0) + (Number(deltaSec) || 0));
  }

  syncOffsetLabelUI() {
    if (typeof document === 'undefined') return;
    const label = document.getElementById('stage-offset-label');
    if (!label) return;
    const val = this.timeOffset || 0;
    label.textContent = `${val > 0 ? '+' : ''}${val.toFixed(1)}s`;
    label.classList.toggle('has-offset', Math.abs(val) > 0.01);
  }

  syncTtmlPillUI() {
    if (typeof document === 'undefined') return;
    const pill = document.getElementById('stage-ttml-badge');
    if (!pill) return;
    const show = Boolean(this.ttmlSource);
    pill.classList.toggle('hidden', !show);
    if (show) {
      pill.textContent = 'AMLL 逐字';
      pill.title = `逐字歌词由 ${this.ttmlSource} 提供`;
    }
  }

  setBlurDepth(enabled) {
    this.blurDepthEnabled = Boolean(enabled);
    const stageContainer = this.getStageContainer();
    if (stageContainer && stageContainer.dataset) {
      stageContainer.dataset.lyricsBlur = this.blurDepthEnabled ? 'true' : 'false';
    }
  }

  getEffectiveTime(currentTime) {
    const raw = Number.isFinite(currentTime) ? currentTime : 0;
    return Math.max(0, raw + (this.timeOffset || 0));
  }

  /**
   * Returns active line, translation, and YRC word timings (seconds) snapshot
   * for Desktop Floating Lyrics karaoke sweep & player inline lyric.
   * @param {number} currentTime
   */
  getActiveLyricSnapshot(currentTime) {
    const eff = this.getEffectiveTime(currentTime);
    const idx = LyricsEngine.getActiveLineIndex(this.lyricsData, eff);
    const current = idx >= 0 ? this.lyricsData[idx] : null;
    const next = idx + 1 < this.lyricsData.length ? this.lyricsData[idx + 1] : null;
    const words = Array.isArray(current?.words)
      ? current.words.map(w => ({
          start: Number(w.start) || 0,
          dur: Number(w.duration) || 0,
          text: String(w.text || ''),
        }))
      : [];
    return {
      line: current?.text || '',
      translation: current?.translation || '',
      romaji: current?.romaji || '',
      next_line: next?.text || '',
      line_index: idx,
      line_start: Number(current?.time) || 0,
      words,
      has_yrc: this.hasYrc,
      offset_sec: this.timeOffset || 0,
      sent_at: Date.now(),
    };
  }

  init(containerId, onSeek) {
    if (typeof document !== 'undefined') {
      this.container = document.getElementById(containerId);
    }
    this.onSeekCallback = onSeek;
    for (const el of document.querySelectorAll?.('[data-copy-line]') || []) {
      el.addEventListener('click', () => this.copyCurrentLine());
    }
    for (const el of document.querySelectorAll?.('[data-copy-full]') || []) {
      el.addEventListener('click', () => this.copyFullLyrics());
    }
  }

  attachFollow(container, variant) {
    if (!container?.addEventListener || !container.removeEventListener || this.followTargets.has(container)) return;
    const button = document.getElementById(variant + '-resume-follow');
    if (button) button.hidden = true;
    const target = { manual: false, timer: null };
    const browse = () => {
      target.manual = true;
      if (button) button.hidden = false;
      clearTimeout(target.timer);
      target.timer = setTimeout(resume, 7000);
    };
    const resume = () => {
      clearTimeout(target.timer);
      target.manual = false;
      if (button) button.hidden = true;
      const line = container.querySelector('.active');
      if (line) this.centerLine(container, line, 'auto');
    };
    const scroll = () => {
      if (target.manual) browse();
    };
    const pointerdown = e => {
      if (e?.pointerType && e.pointerType !== 'mouse') return;
      if (e?.target === container || (Number.isFinite(e?.offsetX) && container.clientWidth && e.offsetX >= container.clientWidth)) {
        browse();
      }
    };
    const keydown = e => {
      if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End'].includes(e.key)) browse();
    };
    const listeners = { wheel: browse, touchstart: browse, touchmove: browse, pointerdown, keydown, scroll };
    for (const [event, fn] of Object.entries(listeners)) container.addEventListener(event, fn, { passive: true });
    button?.addEventListener('click', resume);
    target.resume = resume;
    target.detach = () => {
      clearTimeout(target.timer);
      for (const [event, fn] of Object.entries(listeners)) container.removeEventListener(event, fn);
      button?.removeEventListener?.('click', resume);
      if (button) button.hidden = true;
      this.followTargets.delete(container);
    };
    this.followTargets.set(container, target);
  }

  centerLine(container, line, behavior) {
    const target = this.followTargets.get(container);
    if (target?.manual) return;
    if (typeof container.scrollTo === 'function') {
      const top = (line.offsetTop || 0) - (container.clientHeight || 320) / 2 + (line.offsetHeight || 40) / 2;
      container.scrollTo({ top: Math.max(0, top), behavior });
    } else line.scrollIntoView?.({ behavior, block: 'center' });
  }

  plainLine(line) {
    return [line?.text, this.showTranslation && line?.translation].filter(Boolean).join('\n');
  }

  async copyText(text) {
    if (!text) {
      toast.quiet('暂无可复制的歌词');
      return false;
    }
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard API unavailable');
      await navigator.clipboard.writeText(text);
    } catch {
      const previous = document.activeElement;
      const field = document.createElement('textarea');
      field.value = text;
      field.style.position = 'fixed';
      field.style.opacity = '0';
      document.body.appendChild(field);
      try {
        field.select();
        if (!document.execCommand('copy')) throw new Error();
      } catch {
        toast.quiet('复制失败');
        return false;
      } finally {
        field.remove();
        previous?.focus?.({ preventScroll: true });
      }
    }
    toast.quiet('歌词已复制');
    return true;
  }

  copyCurrentLine() {
    return this.copyText(this.plainLine(this.lyricsData[this.currentActiveIndex]));
  }

  copyFullLyrics() {
    return this.copyText(this.lyricsData.map(line => this.plainLine(line)).filter(Boolean).join('\n'));
  }

  detachDrawerFollow() {
    this.followTargets.get(this.container)?.detach();
  }

  getDeckContainer() {
    if (typeof document === 'undefined') return null;
    return document.getElementById('deck-lyrics-container');
  }

  getStageContainer() {
    if (typeof document === 'undefined') return null;
    return document.getElementById('stage-lyrics-container');
  }

  isDeckVisible() {
    const deckContainer = this.getDeckContainer();
    if (!deckContainer) return false;
    // If deck is explicitly displaying static lyrics for a different non-playing song, do not hijack it
    if (deckContainer.dataset?.deckSynced === 'false') return false;
    const deckPanel = document.getElementById('view-deck');
    // Allow unit tests that mock only #deck-lyrics-container without #view-deck
    return !deckPanel || deckPanel.classList.contains('active');
  }

  isDrawerVisible() {
    if (!this.container) return false;
    const panel = document.getElementById('lyrics-drawer-panel');
    return Boolean(panel && panel.classList.contains('open'));
  }

  isStageVisible() {
    const stageContainer = this.getStageContainer();
    if (!stageContainer) return false;
    const stageRoot = document.getElementById('immersive-stage-overlay');
    return Boolean(stageRoot && stageRoot.classList.contains('open'));
  }

  setDisplayOptions({ showTranslation, showRomaji } = {}) {
    if (typeof showTranslation === 'boolean') this.showTranslation = showTranslation;
    if (typeof showRomaji === 'boolean') this.showRomaji = showRomaji;
    this.renderVersion += 1;
    this.renderVisible();
  }

  /**
   * Loads LRC/YRC lyrics data and renders ONLY the currently visible lyrics container(s).
   * @param {string} lrcString
   * @param {string} [tlyricsString='']
   * @param {{ yrc?: string, romalrc?: string, ttmlSource?: string, ttml_source?: string, songId?: string|number } | null} [extra=null]
   */
  loadLyrics(lrcString, tlyricsString = '', extra = null) {
    for (const target of this.followTargets.values()) target.detach();
    const yrcStr = extra?.yrc || '';
    const romalrcStr = extra?.romalrc || '';
    this.ttmlSource = String(extra?.ttmlSource || extra?.ttml_source || '');
    if (extra && Object.prototype.hasOwnProperty.call(extra, 'songId')) {
      this.loadSongOffset(extra.songId);
    } else if (!lrcString && !yrcStr) {
      this.currentSongId = null;
      this.timeOffset = 0;
      this.syncOffsetLabelUI();
    }

    let parsed = [];
    if (yrcStr) {
      parsed = LyricsEngine.parseYrc(yrcStr, tlyricsString, romalrcStr);
    }
    if (parsed.length === 0) {
      parsed = LyricsEngine.parseLrc(lrcString, tlyricsString, romalrcStr);
    }

    this.lyricsData = parsed;
    this.hasYrc = parsed.some(line => line.isYrc && Array.isArray(line.words) && line.words.length > 0);
    this.currentActiveIndex = -1;
    this.renderVersion += 1;
    this.renderedVersions = { deck: -1, drawer: -1, stage: -1 };

    this.syncTtmlPillUI();
    this.renderVisible();
    if (this.hasYrc && this.isPlaybackActive) {
      this.ensureWordSync();
    } else {
      this.stopWordSync();
    }
  }

  /**
   * Renders only whichever lyrics view(s) are currently visible on screen.
   */
  renderVisible() {
    if (this.isStageVisible()) {
      this.renderStageLyrics();
    }
    if (this.isDeckVisible()) {
      this.renderDeckLyrics();
    }
    if (this.isDrawerVisible()) {
      this.render();
    }
  }

  buildLineElement(item, index, variant = 'drawer') {
    const lineClass = variant === 'deck'
      ? 'deck-lyric-line'
      : (variant === 'stage' ? 'stage-lyric-line' : 'lyric-line');
    const textClass = variant === 'deck'
      ? 'deck-lyric-text'
      : (variant === 'stage' ? 'stage-lyric-text' : 'lyric-text');
    const transClass = variant === 'deck'
      ? 'deck-lyric-translation'
      : (variant === 'stage' ? 'stage-lyric-translation' : 'lyric-line-translation');
    const romaClass = variant === 'stage' ? 'stage-lyric-romaji' : 'lyric-line-romaji';

    let textEl;
    if (item.isYrc && Array.isArray(item.words) && item.words.length > 0) {
      const wordNodes = item.words.map((w, wIdx) => createElement('span', {
        className: 'yrc-word',
        text: w.text,
        dataset: {
          wIdx: String(wIdx),
          start: String(w.start),
          dur: String(w.duration),
        },
      }));
      textEl = createElement('div', { className: `${textClass} yrc-line-words` }, wordNodes);
    } else {
      textEl = createElement('div', { className: textClass, text: item.text });
    }

    const lineChildren = [textEl];

    if (this.showRomaji && item.romaji) {
      lineChildren.push(createElement('div', { className: romaClass, text: item.romaji }));
    }

    if (this.showTranslation && item.translation) {
      lineChildren.push(createElement('div', { className: transClass, text: item.translation }));
    }

    const dataset = variant === 'deck'
      ? { deckIndex: String(index), time: String(item.time) }
      : (variant === 'stage'
          ? { stageIndex: String(index), time: String(item.time) }
          : { index: String(index), time: String(item.time) });

    const seek = () => {
      this.onSeekCallback?.(item.time);
      for (const target of this.followTargets.values()) {
        if (target.manual) target.resume();
      }
    };
    return createElement('div', {
      className: lineClass,
      dataset,
      attributes: {
        role: 'button',
        tabindex: '0',
        'aria-label': `跳转到 ${item.time.toFixed(1)} 秒`,
      },
      events: {
        click: seek,
        keydown: (event) => {
          if ((event.key === 'Enter' || event.key === ' ') && this.onSeekCallback) {
            event.preventDefault();
            // Stop Space from also triggering deck-level skip shortcut
            event.stopPropagation();
            seek();
          }
        },
      },
      children: lineChildren,
    });
  }

  render() {
    if (!this.container) return;
    this.attachFollow(this.container, 'drawer');
    this.container.textContent = '';
    this.renderedVersions.drawer = this.renderVersion;

    if (this.lyricsData.length === 0) {
      this.container.appendChild(createElement('div', {
        className: 'empty-state',
        children: [
          createElement('div', { className: 'empty-state-title', text: '暂无歌词' }),
          createElement('div', { className: 'empty-state-desc', text: '当前曲目未匹配到歌词文件。' }),
        ],
      }));
      return;
    }

    const fragment = typeof document !== 'undefined' && document.createDocumentFragment
      ? document.createDocumentFragment()
      : null;

    this.lyricsData.forEach((item, index) => {
      const lineEl = this.buildLineElement(item, index, 'drawer');
      if (fragment) fragment.appendChild(lineEl);
      else this.container.appendChild(lineEl);
    });

    if (fragment) this.container.appendChild(fragment);

    if (this.currentActiveIndex >= 0) {
      this.highlightTargetLine(this.container, '.lyric-line.active', `[data-index="${this.currentActiveIndex}"]`, false);
    }
  }

  renderDeckLyrics() {
    const deckContainer = this.getDeckContainer();
    if (!deckContainer) return;
    if (deckContainer.dataset) deckContainer.dataset.deckSynced = 'true';
    deckContainer.textContent = '';
    this.renderedVersions.deck = this.renderVersion;

    if (this.lyricsData.length === 0) {
      deckContainer.appendChild(createElement('div', {
        className: 'deck-lyrics-empty',
        text: '暂无同步歌词',
      }));
      return;
    }

    this.lyricsData.forEach((item, index) => {
      deckContainer.appendChild(this.buildLineElement(item, index, 'deck'));
    });

    deckContainer.scrollTop = 0;
    if (this.currentActiveIndex >= 0) {
      this.updateDeckActiveLyric(this.currentActiveIndex);
    }
  }

  /**
   * Renders static non-synced lyrics inside #deck-lyrics-container for a Deck song
   * that is NOT currently playing globally (does NOT clobber playing song's lyricsData).
   * @param {Array<{ time: number, text: string, translation?: string }>} parsedLines
   * @param {{ emptyText?: string }} [opts]
   */
  renderDeckStaticLyrics(parsedLines = [], { emptyText = '暂无歌词' } = {}) {
    const deckContainer = this.getDeckContainer();
    if (!deckContainer) return;
    if (deckContainer.dataset) deckContainer.dataset.deckSynced = 'false';
    deckContainer.textContent = '';

    if (!Array.isArray(parsedLines) || parsedLines.length === 0) {
      deckContainer.appendChild(createElement('div', {
        className: 'deck-lyrics-empty',
        text: emptyText,
      }));
      return;
    }

    parsedLines.forEach((item) => {
      const children = [createElement('div', { className: 'deck-lyric-text', text: item.text })];
      if (this.showTranslation && item.translation) {
        children.push(createElement('div', { className: 'deck-lyric-translation', text: item.translation }));
      }
      deckContainer.appendChild(createElement('div', {
        className: 'deck-lyric-line deck-lyric-static',
        children,
      }));
    });
    deckContainer.scrollTop = 0;
  }

  estimateLineEnd(line) {
    if (!line) return 0;
    if (Number.isFinite(line.duration) && line.duration > 0) {
      return line.time + line.duration;
    }
    if (Array.isArray(line.words) && line.words.length > 0) {
      const last = line.words[line.words.length - 1];
      return (last.start || line.time) + (last.duration || 0.5);
    }
    return line.time + 3.5;
  }

  buildInterludeElement(afterIndex) {
    return createElement('div', {
      className: 'stage-interlude-dots',
      dataset: { interludeAfter: String(afterIndex) },
      attributes: { 'aria-hidden': 'true' },
      children: [
        createElement('span'),
        createElement('span'),
        createElement('span'),
      ],
    });
  }

  applyStageLineDistances(container, activeIndex) {
    if (!container || typeof container.querySelectorAll !== 'function') return;
    const lines = container.querySelectorAll('.stage-lyric-line');
    lines.forEach(lineEl => {
      const idx = Number(lineEl.dataset?.stageIndex);
      if (!Number.isFinite(idx)) return;
      const dist = activeIndex >= 0 ? Math.abs(idx - activeIndex) : idx + 1;
      const bucket = dist === 0 ? '0' : (dist === 1 ? '1' : (dist === 2 ? '2' : 'far'));
      if (lineEl.dataset) lineEl.dataset.distance = bucket;
    });
  }

  getActiveInterludeAfterIndex(effectiveTime) {
    if (!this.lyricsData || this.lyricsData.length === 0) return null;
    const first = this.lyricsData[0];
    if (first.time > 5 && effectiveTime >= 0.4 && effectiveTime < first.time - 0.5) {
      return -1;
    }
    for (let i = 0; i < this.lyricsData.length - 1; i++) {
      const end = this.estimateLineEnd(this.lyricsData[i]);
      const nextStart = this.lyricsData[i + 1].time;
      if (nextStart - end > 5 && effectiveTime >= end + 0.3 && effectiveTime < nextStart - 0.5) {
        return i;
      }
    }
    return null;
  }

  updateStageInterludes(container, effectiveTime) {
    if (!container || typeof container.querySelectorAll !== 'function') return;
    const activeAfter = this.getActiveInterludeAfterIndex(effectiveTime);
    const nodes = container.querySelectorAll('.stage-interlude-dots');
    nodes.forEach(node => {
      const isMatch = activeAfter !== null && String(node.dataset?.interludeAfter) === String(activeAfter);
      node.classList?.toggle('active', isMatch);
    });
  }

  renderStageLyrics() {
    const stageContainer = this.getStageContainer();
    if (!stageContainer) return;
    this.attachFollow(stageContainer, 'stage');
    stageContainer.textContent = '';
    if (stageContainer.dataset) {
      stageContainer.dataset.lyricsBlur = this.blurDepthEnabled ? 'true' : 'false';
    }
    this.renderedVersions.stage = this.renderVersion;

    if (this.lyricsData.length === 0) {
      stageContainer.appendChild(createElement('div', {
        className: 'stage-lyrics-empty',
        text: '纯音乐或暂无同步歌词',
      }));
      return;
    }

    const fragment = typeof document !== 'undefined' && document.createDocumentFragment
      ? document.createDocumentFragment()
      : null;
    const appendNode = (node) => {
      if (fragment) fragment.appendChild(node);
      else stageContainer.appendChild(node);
    };

    if (this.lyricsData[0].time > 5) {
      appendNode(this.buildInterludeElement(-1));
    }

    this.lyricsData.forEach((item, index) => {
      appendNode(this.buildLineElement(item, index, 'stage'));
      if (index < this.lyricsData.length - 1) {
        const gap = this.lyricsData[index + 1].time - this.estimateLineEnd(item);
        if (gap > 5) {
          appendNode(this.buildInterludeElement(index));
        }
      }
    });

    if (fragment) stageContainer.appendChild(fragment);
    if (!this.followTargets.get(stageContainer)?.manual) stageContainer.scrollTop = 0;
    this.applyStageLineDistances(stageContainer, this.currentActiveIndex);

    if (this.currentActiveIndex >= 0) {
      this.highlightTargetLine(stageContainer, '.stage-lyric-line.active', `[data-stage-index="${this.currentActiveIndex}"]`, true);
    }
  }

  highlightTargetLine(container, activeSelector, nextSelector, useScrollToCenter = true, behaviorOverride = null) {
    if (!container) return null;
    const oldActive = container.querySelector(activeSelector);
    if (oldActive && oldActive !== container.querySelector(nextSelector)) {
      oldActive.classList.remove('active');
      this.resetLineWordProgress(oldActive);
    }
    const newActive = container.querySelector(nextSelector);
    if (newActive) {
      newActive.classList.add('active');
      const behavior = behaviorOverride || preferredScrollBehavior();
      if (useScrollToCenter || this.followTargets.has(container)) this.centerLine(container, newActive, behavior);
      else newActive.scrollIntoView?.({ behavior, block: 'center' });
    }
    return newActive;
  }

  /**
   * Immediately renders (if needed), highlights, updates YRC word progress, distance grading,
   * interlude indicators, and scrolls stage lyrics to match currentTime when opening Immersive Stage mid-song.
   * @param {number} currentTime
   * @param {{ instantScroll?: boolean }} [opts]
   */
  syncStageToTime(currentTime, { instantScroll = true } = {}) {
    const stageContainer = this.getStageContainer();
    if (!stageContainer) return;

    const needsRender = this.renderedVersions.stage !== this.renderVersion
      || (this.lyricsData.length > 0 && stageContainer.children?.length === 0)
      || stageContainer.children?.length === 0;
    if (needsRender) {
      this.renderStageLyrics();
    }
    if (this.lyricsData.length === 0) return;

    const time = this.getEffectiveTime(currentTime);
    const activeIdx = LyricsEngine.getActiveLineIndex(this.lyricsData, time);
    this.currentActiveIndex = activeIdx;
    this.applyStageLineDistances(stageContainer, activeIdx);
    this.updateStageInterludes(stageContainer, time);

    if (activeIdx >= 0) {
      const behavior = instantScroll ? 'auto' : preferredScrollBehavior();
      this.highlightTargetLine(
        stageContainer,
        '.stage-lyric-line.active',
        `[data-stage-index="${activeIdx}"]`,
        true,
        behavior,
      );
      if (this.hasYrc) {
        this.updateActiveLineWords(time);
      }

    }
  }

  invalidateStageRender() {
    this.followTargets.get(this.getStageContainer())?.detach();
    this.renderedVersions.stage = -1;
  }

  updateDeckActiveLyric(newIndex) {
    const deckContainer = this.getDeckContainer();
    if (!deckContainer || this.lyricsData.length === 0) return;

    const oldActive = deckContainer.querySelector('.deck-lyric-line.active');
    if (oldActive) {
      oldActive.classList.remove('active');
      this.resetLineWordProgress(oldActive);
    }

    if (newIndex >= 0) {
      const newActive = deckContainer.querySelector(`[data-deck-index="${newIndex}"]`);
      if (newActive) {
        newActive.classList.add('active');
        const containerHeight = deckContainer.clientHeight || 280;
        const targetTop = newActive.offsetTop - (containerHeight / 2) + (newActive.offsetHeight / 2);
        deckContainer.scrollTo({
          top: Math.max(0, targetTop),
          behavior: preferredScrollBehavior(),
        });
      }
    }
  }

  resetLineWordProgress(lineEl) {
    if (!lineEl || typeof lineEl.querySelectorAll !== 'function') return;
    const words = lineEl.querySelectorAll('.yrc-word');
    words.forEach(w => {
      if (w.style) w.style.setProperty('--word-progress', '0%');
      w.classList?.remove('sung', 'singing');
    });
  }

  updateActiveLineWords(currentTime) {
    if (!this.hasYrc || this.currentActiveIndex < 0) return;
    const lineData = this.lyricsData[this.currentActiveIndex];
    if (!lineData || !lineData.isYrc || !Array.isArray(lineData.words)) return;

    const activeLineEls = [];
    if (this.isStageVisible()) {
      const el = this.getStageContainer()?.querySelector(`[data-stage-index="${this.currentActiveIndex}"]`);
      if (el) activeLineEls.push(el);
    } else if (this.isDeckVisible()) {
      const el = this.getDeckContainer()?.querySelector(`[data-deck-index="${this.currentActiveIndex}"]`);
      if (el) activeLineEls.push(el);
    }
    if (this.isDrawerVisible()) {
      const el = this.container?.querySelector(`[data-index="${this.currentActiveIndex}"]`);
      if (el) activeLineEls.push(el);
    }

    for (const lineEl of activeLineEls) {
      const wordEls = lineEl.querySelectorAll?.('.yrc-word');
      if (!wordEls || wordEls.length === 0) continue;
      for (let i = 0; i < lineData.words.length && i < wordEls.length; i++) {
        const progress = LyricsEngine.computeWordProgress(lineData.words[i], currentTime);
        const pct = `${Math.round(progress * 100)}%`;
        const wEl = wordEls[i];
        if (wEl.style) {
          wEl.style.setProperty('--word-progress', pct);
        }
        if (wEl.classList) {
          wEl.classList.toggle('sung', progress >= 0.99);
          wEl.classList.toggle('singing', progress > 0 && progress < 0.99);
        }
      }
    }
  }

  setPlaybackState(isPlaying, timeProvider = null) {
    this.isPlaybackActive = Boolean(isPlaying);
    if (typeof timeProvider === 'function') {
      this.timeProvider = timeProvider;
    }
    if (this.isPlaybackActive && this.hasYrc) {
      this.ensureWordSync();
    } else {
      this.stopWordSync();
    }
  }

  ensureWordSync() {
    if (this.rafId !== null) return;
    if (typeof requestAnimationFrame !== 'function') return;
    if (!this.hasYrc || !this.isPlaybackActive || this.isWindowBlurred) return;
    if (typeof document !== 'undefined' && (document.hidden || document.documentElement?.dataset?.gpuPaused === 'true')) return;
    if (!this.timeProvider) return;

    const tick = () => {
      this.rafId = null;
      if (!this.isPlaybackActive || !this.hasYrc || this.isWindowBlurred) return;
      if (typeof document !== 'undefined' && (document.hidden || document.documentElement?.dataset?.gpuPaused === 'true')) return;

      const t = this.timeProvider();
      if (Number.isFinite(t)) {
        // updateTime applies timeOffset and updates active-line YRC words with effectiveTime
        this.updateTime(t);
      }
      this.rafId = requestAnimationFrame(tick);
    };
    this.rafId = requestAnimationFrame(tick);
  }

  stopWordSync() {
    if (this.rafId !== null && typeof cancelAnimationFrame === 'function') {
      cancelAnimationFrame(this.rafId);
    }
    this.rafId = null;
  }

  updateTime(currentTime) {
    if (!this.lyricsData || this.lyricsData.length === 0) return;

    const effectiveTime = this.getEffectiveTime(currentTime);
    const newIndex = LyricsEngine.getActiveLineIndex(this.lyricsData, effectiveTime);
    if (newIndex !== this.currentActiveIndex) {
      this.currentActiveIndex = newIndex;

      if (this.isStageVisible()) {
        const stageContainer = this.getStageContainer();
        if (this.renderedVersions.stage !== this.renderVersion) {
          this.renderStageLyrics();
        } else {
          this.applyStageLineDistances(stageContainer, newIndex);
          this.highlightTargetLine(
            stageContainer,
            '.stage-lyric-line.active',
            `[data-stage-index="${newIndex}"]`,
            true,
          );
        }
      }

      if (this.isDeckVisible()) {
        if (this.renderedVersions.deck !== this.renderVersion) {
          this.renderDeckLyrics();
        } else {
          this.updateDeckActiveLyric(newIndex);
        }
      }

      if (this.isDrawerVisible()) {
        if (this.renderedVersions.drawer !== this.renderVersion) {
          this.render();
        } else {
          this.highlightTargetLine(
            this.container,
            '.lyric-line.active',
            `[data-index="${newIndex}"]`,
            false,
          );
        }
      }
    }

    if (this.isStageVisible()) {
      this.updateStageInterludes(this.getStageContainer(), effectiveTime);
    }

    if (this.hasYrc) {
      this.updateActiveLineWords(effectiveTime);
    }
  }
}

export const lyricsManager = new LyricsViewManager();
