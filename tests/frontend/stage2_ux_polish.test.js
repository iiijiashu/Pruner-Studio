import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { createMockDom } from './wave4_experience.test.js';
import { api } from '../../web/js/api.js';
import { catalogManager } from '../../web/js/catalog.js';
import { discoverManager } from '../../web/js/discover.js';
import { historyManager } from '../../web/js/history.js';
import { modal } from '../../web/js/modal.js';
import { player } from '../../web/js/player.js';
import { searchPalette } from '../../web/js/search_palette.js';
import { state } from '../../web/js/state.js';

const read = path => readFileSync(new URL('../../' + path, import.meta.url), 'utf8');
const button = (node, text) => node.querySelectorAll('button').find(b => b.textContent === text);
const scopeCopy = '清空当前账号的全部播放历史（即使当前处于筛选或搜索状态，也会清空全部历史记录）。';

function testStyleContracts() {
  const base = read('web/css/base.css');
  const css = read('web/css/components.css');
  const rgb = hex => hex.match(/[\da-f]{2}/gi).map(byte => parseInt(byte, 16));
  const luminance = rgb => rgb.reduce((sum, value, i) => {
    const channel = value / 255;
    return sum + [0.2126, 0.7152, 0.0722][i] *
      (channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4);
  }, 0);
  for (const theme of ['light', 'celadon', 'parchment']) {
    const selector = 'html[data-theme="' + theme + '"]';
    const tokens = base.split(selector + ' {')[1].split('}')[0];
    for (const channel of ['netease', 'qq', 'kugou', 'migu', 'kuwo', 'bilibili']) {
      const rule = css.split('}').find(block => block.split('{')[0].includes(selector + ' .channel-pill-' + channel + ' ') ||
        block.split('{')[0].includes(selector + ' .channel-pill-' + channel + ','));
      assert.ok(rule, theme + '/' + channel);
      const ink = rgb(rule.match(/ color: #([\da-f]{6});/)[1]);
      const [r, g, b, alpha] = rule.match(/background-color: rgba\(([^)]+)/)[1].split(',').map(Number);
      for (const surface of ['night', 'card', 'elevated', 'hover', 'active']) {
        const background = rgb(tokens.match(new RegExp('--bg-' + surface + ': #([a-f0-9]{6})'))[1]);
        const composite = [r, g, b].map((c, i) => c * alpha + background[i] * (1 - alpha));
        assert.ok((luminance(composite) + 0.05) / (luminance(ink) + 0.05) >= 4.5, [theme, channel, surface].join('/'));
      }
    }
  }
  for (const file of readdirSync(new URL('../../web/css/', import.meta.url)).filter(f => f.endsWith('.css'))) {
    assert.doesNotMatch(read('web/css/' + file), /font-size:\s*(?:10|11)px\b/i);
  }
  assert.ok(read('web/index.html').includes(scopeCopy));
  assert.doesNotMatch(read('web/js/history.js'), /window\.confirm/);
  // Finding 7: catalog.css shows inline album drill-down at the same 1024px breakpoint that hides .col-album
  const catalogCss = read('web/css/catalog.css');
  const bp1024 = catalogCss.split('@media (max-width: 1024px)')[1].split('@media')[0];
  assert.ok(bp1024.includes('.col-album') && bp1024.includes('.table-song-artist-mobile'));

  // Stage 2.5 Review Fixes 2, 5, 6, 7: history row & no-match notice wrapping, Discover insights wrapping, deck/player border contrast
  const componentsCss = read('web/css/components.css');
  assert.match(componentsCss, /\.history-track\s*\{[^}]*min-width:\s*0[^}]*minmax\(0,\s*1fr\)/);
  assert.match(componentsCss, /\.history-track\s*>\s*\*\s*\{[^}]*min-width:\s*0[^}]*overflow-wrap:\s*anywhere/);
  assert.match(componentsCss, /\.history-row\s*\{[^}]*min-width:\s*0[^}]*max-width:\s*100%/);
  assert.match(componentsCss, /\.history-notice\s*\{[^}]*min-width:\s*0[^}]*max-width:\s*100%[^}]*overflow-wrap:\s*anywhere/);
  assert.match(componentsCss, /\.history-notice-text\s*\{[^}]*min-width:\s*0[^}]*overflow-wrap:\s*anywhere/);

  const deckCss = read('web/css/deck.css');
  assert.match(deckCss, /\.deck-lyrics-preview-btn:hover\s*\{[^}]*border-color:\s*var\(--accent-text,\s*var\(--border-control\)\)\s*!important/);
  const playerCss = read('web/css/player.css');
  assert.match(playerCss, /\.player-btn\.active\s*\{[^}]*border-color:\s*var\(--accent-text,\s*var\(--accent\)\)/);

  const discoverCss = read('web/css/discover.css');
  assert.match(discoverCss, /@media\s*\(max-width:\s*1480px\)\s*\{[\s\S]*?\.discover-insights-bar\s*\{[^}]*repeat\(2,\s*minmax\(0,\s*1fr\)\)/);
  assert.match(discoverCss, /\.discover-insight-value\s*\{[^}]*white-space:\s*normal[^}]*overflow-wrap:\s*anywhere/);
  assert.ok(read('web/js/discover.js').includes('title: item.value'));
}

export async function runStageTwoTests() {
  testStyleContracts();
  const dom = createMockDom();
  const restore = [];
  const stub = (object, key, value) => {
    const descriptor = Object.getOwnPropertyDescriptor(object, key);
    restore.push(() => descriptor ? Object.defineProperty(object, key, descriptor) : delete object[key]);
    Object.defineProperty(object, key, { configurable: true, writable: true, value });
  };
  const storage = new Map();
  const song = { id: 41, source_playlist_id: 1, title: '河流', artists: ['林'], album: '春', duration_ms: 90000 };
  const other = { ...song, id: 42, source_playlist_id: 2, title: '山', album: '冬', duration_ms: 0 };
  const played = [];
  const enqueued = [];
  try {
    stub(globalThis, 'document', dom.mockDoc);
    stub(globalThis, 'window', { addEventListener() {} });
    stub(globalThis, 'Node', dom.MockNode);
    stub(globalThis, 'localStorage', { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) });
    stub(state, 'state', { ...state.state, user: { id: 1 }, catalog: [song, other], localSongs: [], cloudSongs: [],
      playlists: [{ id: 1, name: '歌单一', kind: 'owned' }, { id: 2, name: '歌单二', kind: 'subscribed' }],
      catalogSelectedPlaylistId: '1', catalogSection: 'all', catalogActiveFilter: 'all', catalogSearchQuery: '河',
      offlineMode: false, recentPlayed: [song], playQueue: [song, song, other, song], playQueueIndex: 1 });
    stub(state, 'listeners', new Map());
    stub(player, 'playQueue', (...args) => played.push(args));
    stub(player, 'enqueueNext', track => enqueued.push(track));
    const { App } = await import('../../web/js/app.js');
    const app = new App();
    const drawer = dom.registerId('play-queue-drawer');
    const list = dom.registerId('play-queue-list');
    drawer.appendChild(list);
    const locate = dom.registerId('play-queue-locate');
    app.initPlayQueueDrawer();
    app.renderPlayQueueDrawer();
    assert.deepEqual(list.querySelectorAll('[data-queue-index]').map(el => el.dataset.queueIndex), ['1', '2', '3', '0']);
    assert.match(list.textContent, /接下来 · 2 首 · 1 分 30 秒（另 1 首时长未知）/);
    assert.equal(list.querySelector('details').open, false);
    const current = list.querySelector('[data-queue-index="1"]');
    let scroll;
    current.scrollIntoView = options => { scroll = options; };
    locate.click();
    assert.deepEqual(scroll, { behavior: 'smooth', block: 'center' });
    assert.ok(current.classList.contains('queue-located'));
    const moves = [];
    stub(player, 'reorderQueue', (...args) => moves.push(args));
    button(current, '上移').click();
    button(current, '下移').click();
    assert.deepEqual(moves, [[1, 0], [1, 2]]);
    app.queueTab = 'history';
    app.renderPlayQueueDrawer();
    assert.ok(locate.hidden && list.querySelector('img'));
    assert.match(list.textContent, /1:30/);

    const history = new historyManager.constructor();
    const entries = dom.registerId('history-list');
    const filter = dom.registerId('history-filter', 'input');
    const clear = dom.registerId('history-clear');
    const confirm = dom.registerId('history-confirm-clear');
    const opened = [];
    const clearModal = dom.registerId('history-clear-confirm-modal');
    let clearConfirmOpen = false;
    stub(modal, 'open', (...args) => {
      opened.push(args);
      if (args[0] === '#history-clear-confirm-modal') clearModal.classList.add('open');
      clearConfirmOpen = true;
    });
    stub(modal, 'close', (sel) => {
      if (sel === '#history-clear-confirm-modal') clearModal.classList.remove('open');
      clearConfirmOpen = false;
    });
    history.init();
    let reply;
    stub(api, 'getPlaybackHistory', () => new Promise(resolve => { reply = resolve; }));
    const loading = history.load();
    assert.equal(entries.dataset.state, 'loading');
    reply({ ok: false, error: { code: 'NETWORK_ERROR', message: '连接失败' } });
    await loading;
    assert.equal(entries.dataset.state, 'error');
    assert.doesNotMatch(entries.textContent, /河流/);

    // Finding 2: HTTP 500 + truncated JSON in offline mode must render error (NOT offline cache!)
    const origFetch = globalThis.fetch;
    stub(state.state, 'offlineMode', true);
    stub(globalThis, 'fetch', async () => ({
      ok: false,
      status: 500,
      headers: { get: k => (k.toLowerCase() === 'content-type' ? 'application/json' : '') },
      json: async () => { throw new SyntaxError('Unexpected end of JSON input'); },
    }));
    const freshApi = new api.constructor();
    const realHistoryRes = await freshApi.getPlaybackHistory({ q: '', offset: 0, limit: 100 });
    assert.equal(realHistoryRes.ok, false);
    assert.equal(realHistoryRes.status, 500);
    assert.equal(realHistoryRes.error.code, 'HTTP_ERROR');
    globalThis.fetch = origFetch;
    const httpErrLoad = history.load();
    reply(realHistoryRes);
    await httpErrLoad;
    assert.equal(entries.dataset.state, 'error');
    assert.ok(button(entries, '重试'));
    assert.doesNotMatch(entries.textContent, /河流/);
    stub(state.state, 'offlineMode', false);

    filter.value = '空';
    button(entries, '重试').click();
    reply({ ok: true, data: { items: [] } });
    await Promise.resolve();
    assert.match(entries.textContent, /没有匹配「空」的播放记录/);
    button(entries, '清除筛选').click();
    reply({ ok: true, data: { items: [] } });
    await Promise.resolve();
    assert.equal(entries.dataset.state, 'empty');
    const today = new Date();
    const yesterday = new Date(today);
    yesterday.setDate(today.getDate() - 1);
    const dates = [today, yesterday, new Date(2020, 1, 1)];
    const refresh = history.load();
    reply({ ok: true, data: { items: dates.map((d, id) => ({ ...song, id, last_played_at: d.toISOString() })) } });
    await refresh;
    assert.deepEqual(entries.querySelectorAll('h3').map(el => el.textContent), ['今天', '昨天', '更早']);
    const removals = [];
    history.remove = (...args) => removals.push(args);
    clear.click();
    assert.deepEqual(opened.at(-1), ['#history-clear-confirm-modal', clear]);
    assert.equal(removals.length, 0);
    confirm.click();
    assert.deepEqual(removals, [[null, state.writeScope()]]);

    // Finding 1: Stale confirm dialog opened at sessionGeneration 4 rejected at account A gen 6
    removals.length = 0;
    state.set({ user: { id: 'A' }, sessionGeneration: 4 });
    clear.click();
    assert.equal(clearConfirmOpen, true);
    state.set({ user: { id: 'B' }, sessionGeneration: 5 });
    state.set({ user: { id: 'A' }, sessionGeneration: 6 });
    confirm.click();
    assert.equal(removals.length, 0);

    // Finding 3: enqueueNext in shuffle mode plays chosen song next
    const song4 = { id: 44, title: '插播歌曲', artists: ['丁'] };
    const pQueue = [{ id: 1, title: '1' }, { id: 2, title: '2' }, { id: 3, title: '3' }];
    const playerProto = Object.getPrototypeOf(player);
    const shufflePlayer = new playerProto.constructor();
    shufflePlayer.shuffleBag = [0, 2, 1];
    shufflePlayer.shuffleBagCursor = 1;
    shufflePlayer.shuffleQueueSig = shufflePlayer.getQueueSignature(pQueue);
    state.set({ playQueue: pQueue, playQueueIndex: 0, playMode: 'shuffle' });
    shufflePlayer.enqueueNext(song4);
    assert.deepEqual(state.get('playQueue').map(s => s.id), [1, 44, 2, 3]);
    assert.equal(shufflePlayer.getNextQueueIndex(false, false), 1);

    // Restore state for search palette & catalog tests
    state.set({
      user: { id: 1 }, catalog: [song, other], localSongs: [], cloudSongs: [],
      catalogSelectedPlaylistId: '1', catalogSection: 'all', catalogActiveFilter: 'all', catalogSearchQuery: '河',
      playQueue: [song, song, other, song], playQueueIndex: 1, playMode: 'order',
    });

    const palette = new searchPalette.constructor();
    const results = dom.registerId('search-palette-results');
    const paletteInput = dom.registerId('search-palette-input', 'input');
    palette.init();
    for (let i = 0; i < 11; i++) palette.openFullSearch(String(i));
    palette.query = '  河流  ';
    palette.playResult(song);
    assert.equal(palette.recentSearches()[0], '河流');
    assert.equal(palette.recentSearches().length, 10);
    // Finding 5: Bare Tab must not preventDefault or switch channel
    let tabPrevented = false;
    palette.activeChannel = 'all';
    palette.query = '测试';
    paletteInput.dispatchEvent({ type: 'keydown', key: 'Tab', shiftKey: false, preventDefault: () => { tabPrevented = true; } });
    assert.equal(tabPrevented, false);
    assert.equal(palette.activeChannel, 'all');
    let altPrevented = false;
    paletteInput.dispatchEvent({ type: 'keydown', key: 'ArrowRight', altKey: true, preventDefault: () => { altPrevented = true; } });
    assert.equal(altPrevented, true);
    assert.notEqual(palette.activeChannel, 'all');

    state.set({ user: { id: 2 } });
    palette.openFullSearch('独立');
    state.set({ user: { id: 1 } });
    palette.open('all');
    assert.match(results.textContent, /最近搜索/);
    assert.doesNotMatch(results.textContent, /独立/);
    results.querySelector('[aria-label="移除搜索 河流"]').click();
    assert.ok(!palette.recentSearches().includes('河流'));
    button(results, '清空').click();
    assert.deepEqual(palette.recentSearches(), []);
    state.set({ user: { id: 2 } });
    assert.deepEqual(palette.recentSearches(), ['独立']);

    // Finding 6: ModalManager Tab / Shift+Tab focus trap skips hidden controls and wraps bounds
    const trapModal = dom.registerId('eq-modal');
    const firstBtn = dom.mockDoc.createElement('button');
    firstBtn.id = 'eq-first-close';
    const hiddenSelect = dom.mockDoc.createElement('select');
    hiddenSelect.hidden = true;
    const lastBtn = dom.mockDoc.createElement('button');
    lastBtn.id = 'eq-last-close';
    trapModal.appendChild(firstBtn);
    trapModal.appendChild(hiddenSelect);
    trapModal.appendChild(lastBtn);
    const origTrapQsa = trapModal.querySelectorAll.bind(trapModal);
    trapModal.querySelectorAll = (sel) => sel.includes(',')
      ? trapModal.children.filter(el => ['BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'A'].includes(el.tagName))
      : origTrapQsa(sel);
    const trapMgr = new modal.constructor();
    trapMgr.activeModal = trapModal;
    lastBtn.focus();
    let wrappedForward = false;
    dom.mockDoc.dispatchEvent({ type: 'keydown', key: 'Tab', shiftKey: false, preventDefault: () => { wrappedForward = true; } });
    assert.equal(wrappedForward, true);
    assert.equal(dom.mockDoc.activeElement, firstBtn);
    let wrappedBackward = false;
    dom.mockDoc.dispatchEvent({ type: 'keydown', key: 'Tab', shiftKey: true, preventDefault: () => { wrappedBackward = true; } });
    assert.equal(wrappedBackward, true);
    assert.equal(dom.mockDoc.activeElement, lastBtn);

    const catalog = new catalogManager.constructor();
    catalog.container = dom.registerId('catalog-view-panel');
    const banner = dom.registerId('catalog-playlist-banner');
    dom.registerId('catalog-table-wrapper');
    dom.registerId('catalog-cards-wrapper');
    const table = dom.registerId('catalog-table-body');
    dom.mockDoc.scrollingElement = { scrollTop: 320 };
    catalog.render();
    assert.match(banner.textContent, /自建歌单/);
    assert.ok(button(banner, '进入整理台'));
    button(banner, '播放全部').click();
    button(banner, '均衡随机').click();
    assert.deepEqual(played.slice(-2).map(call => call.slice(0, 3)), [[[song], 0, 'order'], [[song], 0, 'shuffle']]);
    button(table, '林').click();
    assert.deepEqual(catalog._cachedSortedSongs.map(s => s.id).sort(), [41, 42]);
    assert.match(banner.textContent, /歌手：林/);
    button(banner, '← 返回歌单《歌单一》').click();
    assert.equal(dom.mockDoc.scrollingElement.scrollTop, 320);
    assert.equal(state.get('catalogSearchQuery'), '河');
    assert.equal(state.get('catalogSelectedPlaylistId'), '1');
    button(table, '春').click();
    assert.deepEqual(catalog._cachedSortedSongs, [song]);
    assert.match(banner.textContent, /专辑：春/);
    button(banner, '← 返回歌单《歌单一》').click();
    const playCount = played.length;
    table.querySelector('.quick-next').click();
    assert.deepEqual(enqueued, [song]);
    assert.equal(played.length, playCount);
    state.set({ catalogSelectedPlaylistId: '2' });
    catalog.render();
    assert.match(banner.textContent, /收藏歌单 · 只读/);
    assert.ok(!button(banner, '进入整理台'));
    const gems = dom.registerId('discover-dusty-grid');
    state.set({ dustyGems: [song] });
    discoverManager.renderDustyGems();
    gems.querySelector('.quick-next').click();
    const daily = dom.registerId('discover-daily-list');
    state.set({ dailyRecommend: [other] });
    discoverManager.renderDailyList();
    daily.querySelector('.quick-next').click();
    assert.deepEqual(enqueued, [song, song, other]);
    assert.equal(played.length, playCount);
    // Finding 4: Combined artist string '甲 / 乙' splits into separate clickable links and matches artist '甲'
    const collab = { id: 43, source_playlist_id: 1, title: '合唱曲', artists: '甲 / 乙', album: '合辑', duration_ms: 120000 };
    const solo = { id: 44, source_playlist_id: 1, title: '独唱曲', artists: ['甲'], album: '单辑', duration_ms: 100000 };
    state.set({ catalog: [song, other, collab, solo], catalogSelectedPlaylistId: '1', catalogSearchQuery: '' });
    catalog.render();
    assert.ok(button(table, '甲'));
    assert.ok(button(table, '乙'));
    button(table, '甲').click();
    assert.deepEqual(catalog._cachedSortedSongs.map(s => s.id).sort(), [43, 44]);
    catalog.leaveDrill();

    console.log('✓ Stage 2 queue, history, recent searches, library navigation and theme contrast tests passed!');
  } finally {
    restore.reverse().forEach(fn => fn());
  }
}
