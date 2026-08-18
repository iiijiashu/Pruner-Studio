/**
 * Pruner Studio - Synchronized Lyrics Engine
 * Parses standard LRC strings with translation mapping and coordinates playback sync.
 */
import { createElement } from './utils.js';

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
    this.deckContainer = null;
    this.onSeekCallback = null;
  }

  init(containerId, onSeek) {
    if (typeof document !== 'undefined') {
      this.container = document.getElementById(containerId);
      this.deckContainer = document.getElementById('deck-lyrics-container');
    }
    this.onSeekCallback = onSeek;
  }

  loadLyrics(lrcString, tlyricsString = '') {
    this.lyricsData = LyricsEngine.parseLrc(lrcString, tlyricsString);
    this.currentActiveIndex = -1;
    this.render();
    this.renderDeckLyrics(-1);
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
              this.onSeekCallback(item.time);
            }
          }
        },
        children: lineChildren
      });

      this.container.appendChild(lineEl);
    });
  }

  renderDeckLyrics(activeIndex) {
    const deckContainer = this.deckContainer || (typeof document !== 'undefined' ? document.getElementById('deck-lyrics-container') : null);
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

    const prevItem = activeIndex > 0 ? this.lyricsData[activeIndex - 1] : null;
    const currentItem = activeIndex >= 0 ? this.lyricsData[activeIndex] : this.lyricsData[0];
    const nextItem = activeIndex + 1 < this.lyricsData.length ? this.lyricsData[activeIndex + 1] : null;

    const wrapper = createElement('div', { className: 'deck-lyrics-context' });

    // Previous Line
    if (prevItem) {
      wrapper.appendChild(createElement('div', {
        className: 'deck-lyric-line prev',
        text: prevItem.text,
        events: { click: () => this.onSeekCallback?.(prevItem.time) }
      }));
    } else {
      wrapper.appendChild(createElement('div', { className: 'deck-lyric-line prev spacer', text: '' }));
    }

    // Current Line + Translation
    if (currentItem) {
      const currentChildren = [
        createElement('div', { className: 'deck-lyric-text current', text: currentItem.text })
      ];
      if (currentItem.translation) {
        currentChildren.push(createElement('div', { className: 'deck-lyric-translation current', text: currentItem.translation }));
      }
      wrapper.appendChild(createElement('div', {
        className: 'deck-lyric-current-box',
        children: currentChildren,
        events: { click: () => this.onSeekCallback?.(currentItem.time) }
      }));
    }

    // Next Line
    if (nextItem) {
      wrapper.appendChild(createElement('div', {
        className: 'deck-lyric-line next',
        text: nextItem.text,
        events: { click: () => this.onSeekCallback?.(nextItem.time) }
      }));
    } else {
      wrapper.appendChild(createElement('div', { className: 'deck-lyric-line next spacer', text: '' }));
    }

    deckContainer.appendChild(wrapper);
  }

  updateTime(currentTime) {
    if (!this.lyricsData || this.lyricsData.length === 0) return;

    const newIndex = LyricsEngine.getActiveLineIndex(this.lyricsData, currentTime);
    if (newIndex !== this.currentActiveIndex) {
      this.currentActiveIndex = newIndex;

      // Update Deck Contextual Lyrics
      this.renderDeckLyrics(newIndex);

      // Update Drawer Lyrics if open
      if (this.container) {
        const oldEl = this.container.querySelector('.lyric-line.active');
        if (oldEl) oldEl.classList.remove('active');

        if (newIndex >= 0) {
          const newEl = this.container.querySelector(`[data-index="${newIndex}"]`);
          if (newEl) {
            newEl.classList.add('active');
            newEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
          }
        }
      }
    }
  }
}

export const lyricsManager = new LyricsViewManager();
