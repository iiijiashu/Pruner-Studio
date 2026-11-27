import { api } from './api.js';
import { player } from './player.js';
import { state } from './state.js';
import { serializeTrackForStorage } from './utils.js';

const el = name => document.getElementById('lan-sync-' + name);

export function renderQr(rows) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 ' + rows.length + ' ' + rows.length);
  svg.setAttribute('shape-rendering', 'crispEdges');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', '局域网设备配对二维码');
  const path = document.createElementNS(ns, 'path');
  const modules = [];
  rows.forEach((row, y) => Array.from(row).forEach((bit, x) => {
    if (bit === '1') modules.push('M' + x + ' ' + y + 'h1v1h-1z');
  }));
  path.setAttribute('fill', '#000');
  path.setAttribute('d', modules.join(''));
  svg.appendChild(path);
  return svg;
}

export function secondsLeft(pair, time = Date.now()) {
  return Math.max(0, Math.ceil(pair.expires_at - time / 1000));
}

export const lanSyncManager = {
  revision: 0,
  init() {
    if (!el('enabled-toggle')) return;
    const bind = (name, event, action) => el(name).addEventListener(event, async () => {
      try {
        el('message').textContent = '';
        await action();
      } catch (err) {
        el('message').textContent = err.message;
        await this.refresh();
      }
    });
    bind('enabled-toggle', 'change', () => this.change(api.toggleLanSync({ enabled: el('enabled-toggle').checked })));
    bind('save', 'click', () => this.change(api.toggleLanSync({ device_name: el('name').value, port: Number(el('port').value) })));
    bind('pair-btn', 'click', () => this.change(api.startLanPairing()));
    bind('cancel', 'click', () => this.change(api.cancelLanPairing()));
    bind('copy', 'click', async () => {
      await navigator.clipboard.writeText(el('uri').value);
      el('message').textContent = '已复制配对链接';
    });
    bind('push-handoff-btn', 'click', () => this.push());
    bind('apply', 'click', () => this.apply());
    bind('dismiss', 'click', () => this.acknowledge(this.incoming));
    state.on('lan_sync_status', () => this.refresh());
    state.on('lan_handoff', () => this.refresh());
    state.on('change:sessionGeneration', () => this.refresh());
    state.on('change:activeOfflineAccountId', () => this.refresh());
    state.on('change:user', () => this.refresh());
    state.on('auth:logout', () => this.showIncoming(null));
    this.refresh();
  },

  async change(request) {
    const result = await request;
    if (!result.ok) throw new Error(result.error?.message || '操作失败');
    await this.refresh();
  },

  async refresh() {
    const revision = ++this.revision;
    const [status, latest] = await Promise.all([api.getLanStatus(), api.getLanHandoff()]);
    if (revision !== this.revision) return;
    if (!status.ok) {
      el('status').textContent = '无法读取状态';
      return;
    }
    const s = status.data;
    el('cert-fp').textContent = s.cert_fingerprint_display || '尚未生成';
    el('enabled-toggle').checked = s.enabled;
    el('status').textContent = s.running ? '正在监听' : s.enabled ? '端口不可用' : '已关闭';
    el('addresses').textContent = s.running ? (s.addresses.map(ip => ip + ':' + s.port).join(' · ') || '未发现局域网地址') : '';
    if (document.activeElement !== el('name')) el('name').value = s.device_name;
    if (document.activeElement !== el('port')) el('port').value = s.port;
    el('pair-btn').disabled = el('push-handoff-btn').disabled = !s.running;
    this.showPair(s.pairing);
    this.showDevices(s.devices);
    this.showIncoming(latest.ok && s.running ? latest.data?.pending_desktop_handoff : null);
  },

  showPair(pair) {
    clearInterval(this.timer);
    const panel = el('pair-panel');
    panel.hidden = !pair;
    el('qr').replaceChildren();
    el('pin').textContent = pair?.code || '';
    el('uri').value = pair?.qr_uri || '';
    if (!pair) return;
    el('qr').appendChild(renderQr(pair.qr_rows));
    const tick = () => {
      const left = secondsLeft(pair);
      el('countdown').textContent = '剩余 ' + left + ' 秒';
      if (!left) this.showPair(null);
    };
    this.timer = setInterval(tick, 1000);
    tick();
  },

  showDevices(devices) {
    const list = el('devices-list');
    list.replaceChildren();
    if (!devices.length) list.textContent = '暂无已配对设备';
    const date = value => value ? new Date(value).toLocaleString() : '尚无记录';
    for (const device of devices) {
      const row = document.createElement('li');
      const name = document.createElement('span');
      name.textContent = device.device_name + ' · ' + device.platform + ' · 最近在线 ' +
        date(device.last_seen_at) + ' · 最近同步 ' + date(device.last_sync_at);
      const revoke = document.createElement('button');
      revoke.className = 'btn btn-secondary';
      revoke.textContent = '撤销授权';
      revoke.addEventListener('click', () => this.change(api.revokeLanDevice(device.device_id))
        .catch(err => { el('message').textContent = err.message; }));
      row.append(name, revoke);
      list.appendChild(row);
    }
  },

  showIncoming(handoff) {
    this.incoming = handoff?.source_device_id !== 'desktop' && this.matchesAccount(handoff) ? handoff : null;
    el('apply').hidden = el('dismiss').hidden = !this.incoming;
    el('incoming').textContent = this.incoming ? handoff.source_device_name + ' · ' + (handoff.track?.title || '待接续播放') : '';
  },

  matchesAccount(handoff) {
    return handoff && String(handoff.account_id) === state.writeScope().expected_account_id;
  },

  async push() {
    if (!player.currentSong) throw new Error('请先选择要接续的歌曲');
    const queue = state.get('playQueue') || [];
    const index = state.get('playQueueIndex') ?? -1;
    const start = Math.max(0, Math.min(queue.length - 221, index - 110));
    await this.change(api.pushLanHandoff({
      ...state.writeScope(), track: serializeTrackForStorage(player.currentSong),
      queue: queue.slice(start, start + 221).map(serializeTrackForStorage), queue_index: index - start,
      position_sec: player.pendingResumeSeekSec ?? player.audio?.currentTime ?? 0,
      duration_sec: player.getNormalizedDuration() || 0, is_playing: Boolean(state.get('isPlaying')),
      playback_rate: player.playbackRate || 1, play_mode: state.get('playMode') || 'order',
    }));
    el('message').textContent = '已推送当前播放';
  },

  async apply() {
    const h = this.incoming;
    if (!this.matchesAccount(h)) return;
    state.set({ playQueue: h.queue, playQueueIndex: h.queue_index, playMode: h.play_mode, playQueueOrigin: '播放队列' });
    player.setPlaybackRate(h.playback_rate, false);
    await player.playSong(h.track, h.is_playing, { preserveQueue: true, resumeSeekSec: h.position_sec });
    await this.acknowledge(h);
  },

  async acknowledge(h) {
    if (!this.matchesAccount(h)) return;
    const ack = await api.ackLanHandoff(h.handoff_id);
    if (!ack.ok) throw new Error(ack.error?.message || '接续确认失败');
    if (this.incoming?.handoff_id === h.handoff_id) this.showIncoming(null);
  },
};

