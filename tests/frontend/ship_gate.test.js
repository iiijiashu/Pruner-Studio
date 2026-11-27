import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
import { api } from '../../web/js/api.js';
import { auth } from '../../web/js/auth.js';
import { catalogManager } from '../../web/js/catalog.js';
import { buildSongContextMenuItems } from '../../web/js/context_menu.js';
import { player } from '../../web/js/player.js';
import { RuleEngine } from '../../web/js/rules.js';
import { state } from '../../web/js/state.js';
import { toast } from '../../web/js/toast.js';
import { formatDuration, getTrackKey } from '../../web/js/utils.js';

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
}

export async function runShipGate() {
  const savedState = state.get();
  const cleanup = [];
  const stub = (obj, name, value) => {
    const original = Object.getOwnPropertyDescriptor(obj, name);
    cleanup.push(() => original ? Object.defineProperty(obj, name, original) : delete obj[name]);
    obj[name] = value;
  };
  try {
    const catalog = {id: 1, title: 'catalog', duration_ms: 5000};
    const local = {id: 1, local_path: 'C:/music/A,B.flac', duration: 5000};
    const foreign = {id: 2, track_key: 'qq:2', duration: 5000};
    const bad = [{id: 1, source: 'qq'}, {id: '1e2'}, {id: true}, {id: 1.5}, {id: 5, provider: 'qq'}];
    state.set({catalog: [catalog], cloudSongs: [{...catalog, title: 'duplicate'}, foreign], localSongs: [local],
      catalogSection: 'all'});
    const pool = state.getRuleEvaluationPool();
    assert.deepEqual(pool, [catalog, foreign, local]);
    assert.deepEqual(catalogManager.getSourceTracksForSection(), pool);
    const rule = {id: 'short', enabled: true, match_clauses: [{field: 'duration', operator: 'lt', value: 90}]};
    assert.deepEqual(RuleEngine.runSimulation(pool, [rule]).ruleMatches.get('short'), pool);
    for (const track of [...pool, {id: 99, provider: 'netease', duration: 5000}]) {
      assert.equal(formatDuration(track), '0:05');
      assert.equal(RuleEngine.evaluateRule(track, rule), true);
    }
    assert.deepEqual(catalogManager.writableIds([...pool, ...bad, catalog]), [1]);
    for (const track of [foreign, local, ...bad]) assert.equal(state.resolveDestructiveAction(track, 1).valid, false);

    assert.equal(getTrackKey({id:1, source:'qq'}), 'qq:1');
    for (const track of [{id:1, provider:'netease'}, {id:1, local_path:'C:/s'}, {track_key:'netease:1'}, {fee:0}]) {
      assert.equal(formatDuration({...track, duration:3000}), '0:03');
      assert.ok(RuleEngine.evaluateClause({...track, duration:3000}, {field:'duration',operator:'equals',value:3}));
    }
    assert.equal(formatDuration({provider:'netease',duration:3000,duration_unit:'sec'}), '50:00');
    for (const [field, operator, value, expected] of JSON.parse(readFileSync(new URL('../rule_validation.json', import.meta.url)))) {
      assert.equal(RuleEngine.validClause({field, operator, value}), expected, value);
    }
    state.set({catalog:[{...catalog,source_playlist_id:10},{...catalog,source_playlist_id:20}],
      cloudSongs:[{...catalog,source_playlist_id:'cloud',album:'extra'}], localSongs:[], catalogSelectedIds:new Set(['20:1'])});
    const unique = state.getRuleEvaluationPool();
    assert.equal(unique.length, 1);
    assert.equal(unique[0].album, 'extra');
    assert.equal(RuleEngine.runSimulation(unique,[rule]).ruleMatches.get('short').length, 1);
    assert.deepEqual(catalogManager.writableIds(unique), [1]);
    assert.deepEqual(catalogManager.selectedSongs().map(s => s.source_playlist_id), [20]);
    const ids = Array.from({length: 1002}, (_, i) => i + 1);
    const scope = {expected_account_id: '7', session_generation: 2};
    const success = chunk => ({ok: true, data: {playlist_id: 25, successful_ids: chunk}});
    const writes = [];
    let failure;
    stub(api, 'createPlaylist', async (name, chunk, privacy, actual) => {
      assert.equal(actual, scope);
      writes.push(chunk);
      return success(chunk);
    });
    stub(api, 'addTracksToPlaylist', async (id, chunk, actual) => {
      assert.equal(id, 25);
      assert.equal(actual, scope);
      writes.push(chunk);
      return failure || success(chunk);
    });
    assert.deepEqual((await catalogManager.createInBatches('test', ids, scope)).data.successful_ids, ids);
    assert.deepEqual(writes.map(c => c.length), [500, 500, 2]);
    for (failure of [{ok: false}, {ok: true, data: {pending_operation_ids: ['pending']}},
      {ok: true, data: {unknown_items: [{id: 502}], successful_ids: [501]}},
      {ok: true, data: {failed_items: [{id: 501}]}}, {ok: true, data: {cooldown_sec: 60}}]) {
      writes.length = 0;
      const result = await catalogManager.createInBatches('test', ids, scope);
      assert.deepEqual(writes.map(c => c.length), [500, 500]);
      assert.equal(result.data.successful_ids.length, 500 + (failure.data?.successful_ids?.length || 0));
    }

    const lost = {ok:false, status:503, error:{code:'NETWORK_ERROR',message:'lost'}};
    failure = lost;
    const late = await catalogManager.createInBatches('test', ids, scope);
    assert.equal(late.ok, false);
    assert.equal(late.ambiguous, true);
    assert.equal(late.data.playlist_id, 25);
    assert.deepEqual(late.data.successful_ids, ids.slice(0,500));
    assert.deepEqual(late.data.unknown_items.map(item => item.id), ids.slice(500,1000));
    stub(api, 'createPlaylist', async () => lost);
    const first = await catalogManager.createInBatches('test', ids, scope);
    assert.equal(first.ambiguous, true);
    assert.deepEqual(first.data.successful_ids, []);
    assert.deepEqual(first.remainingIds, ids);

    let query;
    stub(api, 'request', async path => {query = new URL(path, 'http://localhost').searchParams;});
    await api.getDustyGems({excludeKeys: ['local:A,B', 'local:C&D']});
    assert.deepEqual(query.getAll('exclude_key'), ['local:A,B', 'local:C&D']);

    const queue = [catalog, local, catalog];
    state.set({playQueue: queue, playQueueIndex: 2, playMode: 'smart_radio', user: {user_id: 7}});
    stub(player, 'playSong', () => {});
    buildSongContextMenuItems(catalog, {source: 'queue', index: 2})[0].action();
    assert.equal(state.get('playQueueIndex'), 2);
    const calls = [];
    player.resetRadioSession();
    stub(player, 'scheduleSavePlaybackSession', () => {});
    stub(api, 'postSmartRadio', payload => {
      const gate = deferred();
      calls.push({gate, payload});
      return gate.promise;
    });
    const refill = player.requestRadio('refill');
    const rearrange = player.requestRadio('rearrange');
    const duplicate = player.requestRadio('refill');
    assert.equal(calls.length, 2);
    assert.deepEqual(calls[1].payload.superseded_request_ids, [calls[0].payload.request_id]);
    const last = {id: 32, title: 'new'};
    calls[1].gate.resolve({ok: true, data: {...calls[1].payload, songs: [last]}});
    await Promise.all([rearrange, duplicate]);
    calls[0].gate.resolve({ok: true, data: {...calls[0].payload, songs: [], exhausted: true}});
    assert.equal(await refill, false);
    assert.deepEqual(state.get('playQueue'), [...queue, last]);
    assert.equal(state.get('radioExhausted'), false);

    const binding = deferred(), events = [];
    stub(api, 'selectOfflineAccount', id => {assert.equal(id, '17'); return binding.promise;});
    stub(auth, 'updateLoginShell', () => {});
    stub(auth, 'updateUserUI', () => {});
    stub(toast, 'info', () => {});
    stub(state, 'emit', (event) => {if (event === 'auth:offline') events.push(state.get('sessionGeneration'));});
    const entering = auth.enterOfflineMode({account_id: '17'});
    assert.deepEqual(events, []);
    binding.resolve({ok: true, data: {session_generation: 83}});
    await entering;
    assert.deepEqual(events, [83]);
    if (!globalThis.window) stub(globalThis, 'window', {addEventListener() {}});
    const {App} = await import('../../web/js/app.js');
    stub(api, 'getCatalog', async () => ({ok: true, data: {account_id: '42', tracks: [foreign]}}));
    state.set({catalog: []});
    await new App().loadOfflineCatalog('17');
    assert.deepEqual(state.get('catalog'), []);
    assert.ok(state.get('catalogError'));
    stub(api, 'getCatalog', async () => ({ok:true, data:{tracks:[foreign],playlists:[{id:9}]}}));
    await new App().loadOfflineCatalog('17');
    assert.deepEqual(state.get('catalog'), []);
    assert.ok(state.get('catalogError'));
  } finally {
    for (const restore of cleanup.reverse()) restore();
    player.resetRadioSession();
    state.set(savedState);
  }
}
