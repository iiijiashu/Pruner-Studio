import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { api } from '../../web/js/api.js';
import { lanSyncManager as manager, renderQr, secondsLeft } from '../../web/js/lan_sync.js';
import { player } from '../../web/js/player.js';
import { state } from '../../web/js/state.js';

export async function testStageThree() {
  const saved = [];
  function stub(obj, key, value) {
    const previous = Object.getOwnPropertyDescriptor(obj, key);
    saved.push(() => previous ? Object.defineProperty(obj, key, previous) : delete obj[key]);
    Object.defineProperty(obj, key, { value, configurable: true, writable: true });
  }
  const calls = [];
  const nodes = new Map();
  function node(tag) {
    return { tag, attrs: {}, children: [], events: {}, setAttribute(k, v) { this.attrs[k] = v; },
      appendChild(child) { this.children.push(child); }, append(...args) { this.children.push(...args); },
      replaceChildren() { this.children = []; }, addEventListener(k, cb) { this.events[k] = cb; } };
  }
  try {
    stub(globalThis, 'document', { createElement: node, createElementNS(ns, tag) {
      assert.equal(ns, 'http://www.w3.org/2000/svg'); return node(tag);
    }, getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, node('div'));
      return nodes.get(id);
    } });
    const svg = renderQr(['01', '10']);
    assert.equal(svg.attrs.viewBox, '0 0 2 2');
    assert.equal(svg.children[0].attrs.d, 'M1 0h1v1h-1zM0 1h1v1h-1z');
    assert.equal(secondsLeft({ expires_at: 105 }, 101100), 4);
    assert.equal(secondsLeft({ expires_at: 105 }, 106000), 0);
    stub(api, 'request', async (url, options = {}) => {
      calls.push([url, options.method || 'GET', options.body && JSON.parse(options.body)]);
      return { ok: true };
    });
    const fingerprint = Array(32).fill('AB').join(':');
    const realRefresh = manager.refresh.bind(manager);
    stub(manager, 'refresh', async () => {});
    await api.getLanStatus();
    await api.toggleLanSync({ enabled: true });
    await api.startLanPairing();
    await api.cancelLanPairing();
    await api.getLanHandoff();
    manager.showDevices([{ device_name: 'Phone', platform: 'android', device_id: 'a/b' }]);
    await nodes.get('lan-sync-devices-list').children[0].children[1].events.click();
    assert.deepEqual(calls.map(([url, method]) => [url, method]), [
      ['status', 'GET'], ['toggle', 'POST'], ['pair/start', 'POST'], ['pair/cancel', 'POST'],
      ['handoff', 'GET'], ['devices/a%2Fb', 'DELETE'],
    ].map(([route, method]) => ['/api/lan-sync/'+route, method]));
    const song = { id: 42, title: 'Sample' };
    stub(player, 'currentSong', song);
    stub(player, 'getNormalizedDuration', () => 120);
    stub(state, 'get', key => ({ playQueue: [song], playQueueIndex: 0, isPlaying: true })[key]);
    await manager.push();
    assert.equal(calls.at(-1)[0], '/api/lan-sync/handoff/push');
    assert.equal(calls.at(-1)[2].queue[0].id, 42);
    const incoming = { handoff_id: 'transfer-1', track: song, source_device_id: 'phone', source_device_name: 'Phone', account_id: 'local',
      queue: [song], queue_index: 0, is_playing: true, position_sec: 17, playback_rate: 1.25, play_mode: 'order' };
    let applied;
    stub(state, 'writeScope', () => ({ expected_account_id: 'local' }));
    stub(state, 'set', data => assert.deepEqual(data.playQueue, [song]));
    stub(player, 'playSong', (...args) => { applied = args; });
    stub(player, 'setPlaybackRate', rate => assert.equal(rate, 1.25));
    manager.showIncoming(incoming);
    await manager.apply();
    assert.deepEqual(applied, [song, true, { preserveQueue: true, resumeSeekSec: 17 }]);
    assert.deepEqual(calls.at(-1), ['/api/lan-sync/handoff/ack', 'POST', { handoff_id: 'transfer-1' }]);
    stub(api, 'getLanStatus', async () => ({ ok: true, data: { running: true, addresses: [], devices: [],
      cert_fingerprint_display: fingerprint } }));
    stub(api, 'getLanHandoff', async () => ({ ok: true, data: { pending_desktop_handoff: null } }));
    await realRefresh();
    assert.equal(nodes.get('lan-sync-cert-fp').textContent, fingerprint);
    assert.equal(manager.incoming, null);
    const css = readFileSync(new URL('../../web/css/cloud.css', import.meta.url), 'utf8');
    assert.match(css, /#lan-sync-pin[^}]+var\(--accent-text\)/);
    assert.doesNotMatch(css, /backdrop-filter:|font-size:\s*(10|11)px/);
  } finally {
    for (const restore of saved.reverse()) restore();
  }
}

