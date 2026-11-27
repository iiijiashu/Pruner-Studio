import assert from 'node:assert/strict';
import { auth, connectionBadge } from '../../web/js/auth.js';
import { api } from '../../web/js/api.js';
import { state } from '../../web/js/state.js';
import { SearchViewManager } from '../../web/js/search_view.js';
import { checkForUpdate } from '../../web/js/playback_settings.js';
import { createMockDom } from './wave4_experience.test.js';

export async function runPhase4Tests() {
  const originalDocument = globalThis.document;
  const restore = [];
  function stub(object, key, value) {
    const original = object[key];
    object[key] = value;
    restore.push(() => { object[key] = original; });
  }
  const dom = createMockDom();
  function node(tag) {
    const el = dom.makeEl(tag);
    el.append = (...children) => children.forEach(child =>
      el.appendChild(typeof child === 'string' ? dom.mockDoc.createTextNode(child) : child));
    el.replaceChildren = () => { el.children.length = 0; };
    return el;
  }
  try {
    globalThis.document = dom.mockDoc;
    document.createElement = node;
    assert.deepEqual(['unconfigured', 'configured', 'reachable', 'authenticated', 'writable'].map(connectionBadge),
      ['未配置', '已配置 · 未探测', '搜索可用', '已验身份', '可写歌单']);
    const panel = node('section');
    panel.isConnected = true;
    stub(api, 'getAccounts', async () => ({ok: true, data: {active_account_id: '1',
      saved_accounts: [{account_id:'1', display_name:'One'}, {account_id:'2', display_name:'Two'}]}}));
    stub(api, 'getChannels', async () => ({ok: true, data: {channels: [
      {channel: 'qq', display_name: 'QQ音乐', connection_state: 'configured'}]}}));
    stub(auth, 'prepareAccountChange', async () => {});
    stub(auth, 'checkSession', async () => true);
    const switched = [], events = [];
    stub(api, 'switchAccount', async id => { switched.push(id); return {ok:true}; });
    stub(state, 'emit', name => events.push(name));
    stub(state, 'get', key => key === 'authenticated');
    await auth.renderHub(panel);
    const buttons = panel.querySelectorAll('button');
    assert.match(panel.textContent, /One · 当前账号/);
    assert.match(panel.textContent, /已配置 · 未探测/);
    const switcher = [...buttons].find(b => b.textContent === '切换');
    switcher.dispatchEvent({type:'click', currentTarget:switcher});
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.deepEqual(switched, ['2']);
    assert.deepEqual(events, ['auth:logout', 'auth:success']);

    const view = new SearchViewManager();
    view.render = () => {};
    const cursors = {qq:[0, 3], netease:[50, 0], migu:null};
    const calls = [];
    stub(api, 'searchMulti', async (...args) => {
      calls.push(args);
      return {ok:true, data:{tracks:[], has_more:calls.length === 1, next_cursors:cursors}};
    });
    await view.search('测试');
    await view.loadMore();
    assert.deepEqual(calls[1].slice(3), [0, cursors]);

    const output = node('small');
    dom.domById.set('settings-update-result', output);
    for (const [status, message] of [['ok','发现新版本 0.5.0'], ['no_releases','目前尚无公开发行版。'],
                                     ['unreachable','无法获取发布信息，请稍后重试。']]) {
      stub(api, 'checkAppUpdate', async () => ({ok:true, data:{status, message, has_update:status === 'ok',
        release_url:'https://github.com/iiijiashu/Pruner-Studio/releases/tag/v0.5.0'}}));
      await checkForUpdate();
      assert.ok(output.textContent.includes(message));
      if (status === 'ok') assert.equal(output.querySelector('a').textContent, '查看发行说明');
    }
  } finally {
    restore.reverse().forEach(fn => fn());
    globalThis.document = originalDocument;
  }
}
