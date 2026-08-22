/**
 * Pruner Studio - Synchronized Lyrics Engine
 * Parses standard LRC strings with translation mapping and coordinates playback sync.
 */
import { createElement } from './utils.js';

/**
 * Chooses scroll behavior honoring prefers-reduced-motion.
 * @returns {'smooth'|'auto'}
 */
function preferredScrollBehavior() {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return 'smooth';
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
  } catch {
    return 'smooth';
  }
}

export class LyricsEngine {
  /**
   * Parse LRC format string into array of timestamped lines with optional translations.
   * @param {string} lrcString
   * @param {string} [tlyricsString]
   * @returns {Array<{ time: number, text: string, translation?: string }>}
   */
  static parseLrc(lrcString, tlyricsString = '') {
    if (!lrcString || typeof lrcString !== 'string') {
      return [];
    }

    const timeRegex = /\[(\d{2}):(\d{2})(?:\.(\d{2,3}))?\]/g;
    const lines = lrcString.split('\n');
    const result = [];

    // Parse translated lyrics into map by rounded second key
    const translationMap = new Map();
    if (tlyricsString && typeof tlyricsString === 'string') {
      const tlines = tlyricsString.split('\n');
      for (const tline of tlines) {
        let match;
        timeRegex.lastIndex = 0;
        const lineText = tline.replace(timeRegex, '').trim();
        if (!lineText) continue;

        timeRegex.lastIndex = 0;
        while ((match = timeRegex.exec(tline)) !== null) {
          const minutes = parseInt(match[1], 10);
          const seconds = parseInt(match[2], 10);
          const millis = match[3] ? parseInt(match[3].padEnd(3, '0').slice(0, 3), 10) : 0;
          const time = minutes * 60 + seconds + millis / 1000;
          const timeKey = Math.round(time * 10) / 10;
          translationMap.set(timeKey, lineText);
        }
      }
    }

    for (const line of lines) {
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

        result.push({
          time,
          text: rawText,
          translation: translationMap.get(timeKey) || '',
        });
      }
    }

    // Sort chronologically
    result.sort((a, b) => a.time - b.time);
    return result;
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
    this.currentActiveIndex = -1;
    this.container = null;
    this.onSeekCallback = null;
  }

  init(containerId, onSeek) {
    if (typeof document !== 'undefined') {
      this.container = document.getElementById(containerId);
    }
    this.onSeekCallback = onSeek;
  }

  getDeckContainer() {
    if (typeof document === 'undefined') return null;
    return document.getElementById('deck-lyrics-container');
  }

  loadLyrics(lrcString, tlyricsString = '') {
    this.lyricsData = LyricsEngine.parseLrc(lrcString, tlyricsString);
    this.currentActiveIndex = -1;
    this.render();
    this.renderDeckLyrics();
  }

  render() {
    if (!this.container) return;
    this.container.textContent = ''; // Safe clear

    if (this.lyricsData.length === 0) {
      const emptyEl = createElement('div', {
        className: 'empty-state',
        children: [
          createElement('div', { className: 'empty-state-title', text: '暂无歌词' }),
          createElement('div', { className: 'empty-state-desc', text: '当前曲目未匹配到歌词文件。' }),
        ]
      });
      this.container.appendChild(emptyEl);
      return;
    }

    this.lyricsData.forEach((item, index) => {
      const lineChildren = [
        createElement('div', { className: 'lyric-text', text: item.text })
      ];

      if (item.translation) {
        lineChildren.push(createElement('div', { className: 'lyric-line-translation', text: item.translation }));
      }

      const lineEl = createElement('div', {
        className: 'lyric-line',
        dataset: { index: String(index), time: String(item.time) },
        attributes: { role: 'button', tabindex: '0', 'aria-label': `跳转到 ${item.time.toFixed(1)} 秒` },
        events: {
          click: () => {
            if (this.onSeekCallback) {
              this.onSeekCallback(item.time);
            }
          },
          keydown: (event) => {
            if ((event.key === 'Enter' || event.key === ' ') && this.onSeekCallback) {
              event.preventDefault();
              event.stopPropagation();
              this.onSeekCallback(item.time);
            }
          }
        },
        children: lineChildren
      });

      this.container.appendChild(lineEl);
    });
  }

  renderDeckLyrics() {
    const deckContainer = this.getDeckContainer();
    if (!deckContainer) return;
    deckContainer.textContent = ''; // Safe clear

    if (this.lyricsData.length === 0) {
      const emptyEl = createElement('div', {
        className: 'deck-lyrics-empty',
        text: '暂无同步歌词'
      });
      deckContainer.appendChild(emptyEl);
      return;
    }

    this.lyricsData.forEach((item, index) => {
      const lineChildren = [
        createElement('div', { className: 'deck-lyric-text', text: item.text })
      ];
      if (item.translation) {
        lineChildren.push(createElement('div', { className: 'deck-lyric-translation', text: item.translation }));
      }

      const lineEl = createElement('div', {
        className: 'deck-lyric-line',
        dataset: { deckIndex: String(index), time: String(item.time) },
        attributes: { role: 'button', tabindex: '0', 'aria-label': `跳转到 ${item.time.toFixed(1)} 秒` },
        children: lineChildren,
        events: {
          click: () => {
            if (this.onSeekCallback) {
              this.onSeekCallback(item.time);
            }
          },
          keydown: (event) => {
            if ((event.key === 'Enter' || event.key === ' ') && this.onSeekCallback) {
              event.preventDefault();
              // Stop Space from also triggering the deck-level skip shortcut
              event.stopPropagation();
              this.onSeekCallback(item.time);
            }
          }
        }
      });

      deckContainer.appendChild(lineEl);
    });

    deckContainer.scrollTop = 0;
    if (this.currentActiveIndex >= 0) {
      this.updateDeckActiveLyric(this.currentActiveIndex);
    }
  }

  updateDeckActiveLyric(newIndex) {
    const deckContainer = this.getDeckContainer();
    if (!deckContainer || this.lyricsData.length === 0) return;

    const oldActive = deckContainer.querySelector('.deck-lyric-line.active');
    if (oldActive) {
      oldActive.classList.remove('active');
    }

    if (newIndex >= 0) {
      const newActive = deckContainer.querySelector(`[data-deck-index="${newIndex}"]`);
      if (newActive) {
        newActive.classList.add('active');

        // Smooth scroll container to center the active line
        const containerHeight = deckContainer.clientHeight || 280;
        const targetTop = newActive.offsetTop - (containerHeight / 2) + (newActive.offsetHeight / 2);
        deckContainer.scrollTo({
          top: Math.max(0, targetTop),
          behavior: preferredScrollBehavior()
        });
      }
    }
  }

  updateTime(currentTime) {
    if (!this.lyricsData || this.lyricsData.length === 0) return;

    const newIndex = LyricsEngine.getActiveLineIndex(this.lyricsData, currentTime);
    if (newIndex !== this.currentActiveIndex) {
      this.currentActiveIndex = newIndex;

      // Update Deck Continuous Rolling Lyrics
      this.updateDeckActiveLyric(newIndex);

      // Update Drawer Lyrics if open
      if (this.container) {
        const oldEl = this.container.querySelector('.lyric-line.active');
        if (oldEl) oldEl.classList.remove('active');

        if (newIndex >= 0) {
          const newEl = this.container.querySelector(`[data-index="${newIndex}"]`);
          if (newEl) {
            newEl.classList.add('active');
            newEl.scrollIntoView({ behavior: preferredScrollBehavior(), block: 'center' });
          }
        }
      }
    }
  }
}

export const lyricsManager = new LyricsViewManager();
