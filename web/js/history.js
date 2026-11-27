import { api } from './api.js';
import { state } from './state.js';
import { player } from './player.js';
import { createElement, formatArtists, getSizedCoverUrl, getTrackKey } from './utils.js';
import { toast } from './toast.js';
import { modal } from './modal.js';
import { openSongContextMenu } from './context_menu.js';

export function historyDateGroup(value, now = new Date()) {
  const day = new Date(value).toDateString();
  if (day === now.toDateString()) return '今天';
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  return day === yesterday.toDateString() ? '昨天' : '更早';
}

const button = (text, click) => createElement('button', {
  className: 'btn btn-ghost btn-sm', text, events: { click },
});

const sources = {
  netease: '网易云', qq: 'QQ', migu: '咪咕', kugou: '酷狗', kugou_lite: '酷狗',
  local: '本地', local_library: '本地', bilibili: 'B站', kuwo: '酷我',
};

class HistoryManager {
  constructor() {
    this.version = 0;
    this.items = [];
    this.offset = 0;
    this.pendingClearScope = null;
  }

  init() {
    document.getElementById('history-filter')?.addEventListener('input', () => this.load());
    const clearBtn = document.getElementById('history-clear');
    const dismissConfirm = () => {
      this.pendingClearScope = null;
      if (document.getElementById('history-clear-confirm-modal')?.classList?.contains('open')) {
        modal.close('#history-clear-confirm-modal');
      }
    };
    clearBtn?.addEventListener('click', () => {
      this.pendingClearScope = state.writeScope();
      modal.open('#history-clear-confirm-modal', clearBtn);
    });
    document.getElementById('history-confirm-clear')?.addEventListener('click', () => {
      const captured = this.pendingClearScope;
      this.pendingClearScope = null;
      modal.close('#history-clear-confirm-modal');
      if (!captured) return;
      const current = state.writeScope();
      if (
        String(captured.expected_account_id) === String(current.expected_account_id)
        && captured.session_generation === current.session_generation
      ) {
        this.remove(null, captured);
      }
    });
    document.getElementById('history-more')?.addEventListener('click', () => this.load(true));
    const refresh = () => { if (state.get('activeTab') === 'history') this.load(); };
    state.on('history:updated', refresh);
    state.on('change:recentPlayed', refresh);
    state.on('change:user', () => { dismissConfirm(); this.invalidate(); });
    state.on('change:activeOfflineAccountId', () => { dismissConfirm(); this.invalidate(); });
    state.on('change:sessionGeneration', dismissConfirm);
    state.on('auth:logout', () => { dismissConfirm(); this.invalidate(); });
  }

  invalidate() {
    this.version++;
    this.items = [];
    this.offset = 0;
    this.pendingClearScope = null;
    const list = document.getElementById('history-list');
    if (list) list.textContent = '';
  }

  async load(append = false) {
    const version = ++this.version;
    const owner = state.getUserId();
    const q = document.getElementById('history-filter')?.value.trim() || '';
    const offset = append ? this.offset : 0;
    const more = document.getElementById('history-more');
    if (more) more.hidden = true;
    if (!append) {
      this.items = [];
      this.offset = 0;
      this.render('loading');
    }
    const res = await api.getPlaybackHistory({ q, offset, limit: 100 });
    if (version !== this.version || owner !== state.getUserId()) return;
    if (res.ok !== true) {
      const offline = state.get('offlineMode') || globalThis.navigator?.onLine === false;
      const unavailable = ['NETWORK_ERROR', 'OFFLINE', 'OFFLINE_MODE', 'SERVICE_UNAVAILABLE'].includes(res.error?.code)
        || res.status === 503;
      if (offline && unavailable && !append) {
        this.items = (state.get('recentPlayed') || []).filter(song =>
          [song.title, formatArtists(song.artists), song.album].join(' ').toLowerCase().includes(q.toLowerCase()));
        this.render('offline', q);
      } else {
        this.render('error', q, res.error?.message, append);
      }
      return;
    }
    const rows = res.data?.items || [];
    this.offset = offset + rows.length;
    const items = append ? [...this.items, ...rows] : rows;
    this.items = [...new Map(items.map(song => [getTrackKey(song), song])).values()];
    this.render(this.items.length ? 'ready' : q ? 'no_match' : 'empty', q);
    if (more) more.hidden = rows.length < 100;
  }

  async remove(song, scope = state.writeScope()) {
    const owner = state.getUserId();
    const key = song && getTrackKey(song);
    const res = await api.deletePlaybackHistory(key, scope);
    if (owner !== state.getUserId()) return;
    if (!res.ok) { toast.error('删除失败', res.error?.message); return; }
    toast.quiet(key ? '已删除播放记录' : '已清空播放历史');
    state.replaceRecentPlayed((state.get('recentPlayed') || []).filter(row => key && getTrackKey(row) !== key));
  }

  render(status, q = '', message = '', append = false) {
    const list = document.getElementById('history-list');
    if (!list) return;
    list.dataset.state = status;
    list.textContent = '';
    const messages = {
      loading: '正在加载播放历史…', error: message || '播放历史加载失败',
      offline: this.items.length ? '离线模式 · 显示本机最近播放缓存' : '离线模式 · 没有匹配的本机播放缓存',
      no_match: `没有匹配「${q}」的播放记录`, empty: '暂无播放历史',
    };
    if (messages[status]) {
      const notice = createElement('div', {
        className: 'history-notice',
        attributes: { role: 'status' },
        children: [createElement('span', { className: 'history-notice-text', text: messages[status] })],
      });
      if (status === 'error' || status === 'offline') notice.appendChild(button('重试', () => this.load(append)));
      if (q && (status === 'no_match' || status === 'offline')) notice.appendChild(button('清除筛选', () => {
        document.getElementById('history-filter').value = '';
        this.load();
      }));
      list.appendChild(notice);
    }
    const groups = new Map(['今天', '昨天', '更早'].map(label => [label, []]));
    for (const song of this.items) groups.get(historyDateGroup(song.last_played_at || song.playedAt)).push(song);
    for (const [label, songs] of groups) {
      if (!songs.length) continue;
      list.appendChild(createElement('h3', { text: label }));
      for (const song of songs) list.appendChild(this.renderRow(song));
    }
  }

  renderRow(song) {
    const source = song.local_path ? '本地' : sources[song.provider || 'netease'] || song.provider;
    const time = new Date(song.last_played_at || song.playedAt);
    return createElement('div', { className: 'history-row', children: [
      createElement('img', { className: 'queue-item-cover', attributes: {
        src: getSizedCoverUrl(song.cover_url, 64, '音'), alt: '', loading: 'lazy',
      } }),
      createElement('div', { className: 'history-track', children: [
        createElement('strong', { text: song.title }),
        createElement('small', { text: [formatArtists(song.artists), song.album,
          song.play_count == null ? '最近播放' : `播放 ${song.play_count} 次 · 完播 ${song.completed_count || 0} 次`,
        ].filter(Boolean).join(' · ') }),
      ] }),
      createElement('span', { className: 'source-badge', text: source }),
      createElement('time', { text: Number.isNaN(time.getTime()) ? '' : time.toLocaleString() }),
      createElement('div', { className: 'history-actions', children: [
        button('播放', () => player.playSong(song, true, { origin: '播放历史' })),
        button('下一首', () => player.enqueueNext(song)),
        button('更多', e => openSongContextMenu(e, song, { source: 'history' })),
        button('删除', () => this.remove(song)),
      ] }),
    ] });
  }
}

export const historyManager = new HistoryManager();
