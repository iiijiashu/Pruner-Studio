/**
 * Pruner Studio - Audio Player Controller
 * Handles audio playback, chorus jump, alternative source detection, and dynamic cover accent update.
 */
import { api } from './api.js';
import { state } from './state.js';
import { lyricsManager } from './lyrics.js';
import { toast } from './toast.js';
import { formatDuration, formatArtists, getSafeImageUrl, isSafeUrl, getCoverAccent } from './utils.js';

class AudioPlayer {
  constructor() {
    if (typeof Audio !== 'undefined') {
      this.audio = new Audio();
      this.initAudioEvents();
    } else {
      this.audio = {
        addEventListener: () => {},
        removeEventListener: () => {},
        play: async () => {},
        pause: () => {},
        src: '',
        currentTime: 0,
        duration: 0,
        volume: 1,
        paused: true,
      };
    }
    this.isChorusLooping = false;
    this.currentSong = null;
    this.requestSequence = 0;
  }

  initAudioEvents() {
    this.audio.addEventListener('play', () => {
      state.set({ isPlaying: true });
      this.updatePlayButtonUI(true);
    });

    this.audio.addEventListener('pause', () => {
      state.set({ isPlaying: false });
      this.updatePlayButtonUI(false);
    });

    this.audio.addEventListener('timeupdate', () => {
      const currentTime = this.audio.currentTime;
      const duration = this.audio.duration || 0;
      state.set({ currentTime, duration });

      // Chorus looping check
      if (this.isChorusLooping && this.currentSong && this.currentSong.chorus_end) {
        if (currentTime >= this.currentSong.chorus_end) {
          this.audio.currentTime = this.currentSong.chorus_start || 0;
        }
      }

      this.updateTimelineUI(currentTime, duration);
      lyricsManager.updateTime(currentTime);
    });

    this.audio.addEventListener('loadedmetadata', () => {
      const duration = this.audio.duration || (this.currentSong ? this.currentSong.duration : 0);
      state.set({ duration });
      this.updateTimelineUI(0, duration);
    });

    this.audio.addEventListener('ended', () => {
      state.set({ isPlaying: false });
      this.updatePlayButtonUI(false);
    });

    this.audio.addEventListener('error', (e) => {
      console.warn('Audio playback error:', e);
      state.set({ isPlaying: false });
      this.updatePlayButtonUI(false);
    });
  }

  /**
   * Play a specific song object.
   * @param {Song} song
   * @param {boolean} [autoStart=true]
   */
  async playSong(song, autoStart = true) {
    if (!song) return;
    const requestId = ++this.requestSequence;
    this.audio.pause();
    this.audio.removeAttribute?.('src');
    this.audio.src = '';
    this.audio.load?.();
    lyricsManager.loadLyrics('');
    this.currentSong = song;
    state.set({ currentPlayingSong: song });

    // Update Cover Accent in CSS Variables
    const accent = getCoverAccent(`${song.id}_${song.title}_${song.album || ''}`);
    state.set({ coverAccent: accent });
    if (typeof document !== 'undefined') {
      document.documentElement.style.setProperty('--cover-accent', accent);
      const ambientField = document.getElementById('ambient-cover-field');
      if (ambientField) {
        const coverImgUrl = isSafeUrl(song.cover_url) ? song.cover_url : '';
        if (coverImgUrl) {
          ambientField.style.backgroundImage = `url("${coverImgUrl}")`;
        } else {
          ambientField.style.backgroundImage = 'none';
        }
      }
    }

    this.updateTrackInfoUI(song);

    // Fetch Audio URL
    try {
      const audioRes = await api.getAudio(song.id);
      if (requestId !== this.requestSequence) return;
      const audioUrl = audioRes.data?.url;
      if (audioRes.ok && typeof audioUrl === 'string' && /^https?:\/\//i.test(audioUrl)) {
        const isAlt = Boolean(audioRes.data.is_alternative || audioRes.data.source === 'alternative');
        state.set({
          audioSourceType: isAlt ? 'alternative' : 'original',
          alternativeInfo: audioRes.data.alternative_info || null,
        });
        this.updateSourceBadgeUI(isAlt, audioRes.data.alternative_info);

        this.audio.src = audioUrl;
        if (autoStart) {
          this.audio.play().catch(err => {
            console.log('Autoplay policy prevented audio play:', err);
          });
        }
      } else {
        this.audio.removeAttribute?.('src');
        this.audio.src = '';
        this.updateSourceBadgeUI(false, null);
      }
    } catch (err) {
      console.error('Error fetching audio:', err);
    }

    // Fetch Lyrics in parallel
    try {
      const lyricsRes = await api.getLyrics(song.id);
      if (requestId !== this.requestSequence) return;
      if (lyricsRes.ok && lyricsRes.data) {
        lyricsManager.loadLyrics(lyricsRes.data.lyrics, lyricsRes.data.tlyrics);
      } else {
        lyricsManager.loadLyrics('');
      }
    } catch (err) {
      lyricsManager.loadLyrics('');
    }
  }

  togglePlay() {
    if (!this.audio.src) {
      const current = state.get('currentPlayingSong') || (state.get('deckQueue') || [])[state.get('deckIndex') || 0];
      if (current) {
        this.playSong(current, true);
      }
      return;
    }

    if (this.audio.paused) {
      this.audio.play().catch(err => console.warn('Play error:', err));
    } else {
      this.audio.pause();
    }
  }

  seek(seconds) {
    if (!isNaN(seconds) && isFinite(seconds)) {
      this.audio.currentTime = Math.max(0, Math.min(seconds, this.audio.duration || 9999));
    }
  }

  seekDelta(deltaSeconds) {
    const target = (this.audio.currentTime || 0) + deltaSeconds;
    this.seek(target);
  }

  setVolume(volumeFraction) {
    this.audio.volume = Math.max(0, Math.min(1, volumeFraction));
  }

  /**
   * Jump to chorus section of current song.
   */
  jumpToChorus() {
    const song = this.currentSong || (state.get('deckQueue') || [])[state.get('deckIndex') || 0];
    if (!song) return;

    if (!this.audio.src) {
      this.playSong(song, true).then(() => this.performChorusJump(song));
      return;
    }
    this.performChorusJump(song);
  }

  performChorusJump(song) {
    const start = song.chorus_start;
    if (typeof start === 'number' && !isNaN(start) && start > 0) {
      this.seek(start);
      if (this.audio.paused) {
        this.audio.play().catch(console.warn);
      }
      toast.info('跳转副歌', `已定位至副歌 (${formatDuration(start)})`);
    } else {
      // Estimate 35% into song
      const duration = this.audio.duration || song.duration || 180;
      const estimate = Math.floor(duration * 0.35);
      this.seek(estimate);
      if (this.audio.paused) {
        this.audio.play().catch(console.warn);
      }
      toast.info('副歌预估', `定位至高潮预估时间 (${formatDuration(estimate)})`);
    }
  }

  toggleChorusLoop() {
    this.isChorusLooping = !this.isChorusLooping;
    state.set({ isChorusActive: this.isChorusLooping });
    if (typeof document !== 'undefined') {
      const btn = document.getElementById('player-chorus-btn');
      if (btn) btn.classList.toggle('active', this.isChorusLooping);
    }
    if (this.isChorusLooping) {
      this.jumpToChorus();
    }
  }

  updateTrackInfoUI(song) {
    if (typeof document === 'undefined') return;
    const coverEl = document.getElementById('player-track-cover');
    const titleEl = document.getElementById('player-track-title');
    const artistEl = document.getElementById('player-track-artist');

    if (coverEl) {
      coverEl.src = isSafeUrl(song.cover_url) ? song.cover_url : getSafeImageUrl('', '音');
      coverEl.alt = song.title || '封面';
    }
    if (titleEl) {
      titleEl.textContent = song.title || '未选择曲目';
    }
    if (artistEl) {
      artistEl.textContent = formatArtists(song.artists);
    }
  }

  updateSourceBadgeUI(isAlternative, altInfo) {
    if (typeof document === 'undefined') return;
    const badgeEl = document.getElementById('player-source-badge');
    if (!badgeEl) return;

    badgeEl.textContent = '';
    if (isAlternative) {
      badgeEl.className = 'badge badge-alt';
      badgeEl.textContent = '替代音源';
      if (altInfo) {
        badgeEl.title = `匹配音源: ${altInfo.title || ''} - ${altInfo.artist || ''}`;
      } else {
        badgeEl.title = '匹配到了曲名与艺术家一致的替代播放源';
      }
    } else {
      badgeEl.className = 'sr-only';
    }
  }

  updatePlayButtonUI(isPlaying) {
    if (typeof document === 'undefined') return;
    const mainBtn = document.getElementById('player-play-btn');
    const deckPlayBtn = document.getElementById('deck-play-btn');

    const icon = isPlaying ? '⏸' : '▶';
    const text = isPlaying ? '暂停' : '播放';
    if (mainBtn) mainBtn.textContent = icon;
    if (deckPlayBtn) {
      deckPlayBtn.textContent = icon;
      deckPlayBtn.title = `${text} [P]`;
    }
  }

  updateTimelineUI(currentTime, duration) {
    if (typeof document === 'undefined') return;
    const currentEl = document.getElementById('player-time-current');
    const totalEl = document.getElementById('player-time-total');
    const progressEl = document.getElementById('player-timeline-progress');
    const sliderEl = document.getElementById('player-timeline-slider');

    if (currentEl) currentEl.textContent = formatDuration(currentTime);
    if (totalEl) totalEl.textContent = formatDuration(duration);

    const percent = duration > 0 ? (currentTime / duration) * 100 : 0;
    if (progressEl) progressEl.style.width = `${percent}%`;
    if (sliderEl && !sliderEl.matches(':active')) {
      sliderEl.value = String(percent);
    }
  }
}

export const player = new AudioPlayer();
