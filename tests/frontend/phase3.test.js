import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { state } from '../../web/js/state.js';
import { player } from '../../web/js/player.js';
import { stageManager } from '../../web/js/stage.js';
import { THEME_OPTIONS, ACCENT_PRESETS, normalizeAppearance, applyAppearance, computeContrastRatio, computeReadableAccentText } from '../../web/js/theme.js';

const read = path => fs.readFileSync(path, 'utf8');

async function testDesktop() {
  const html = read('web/desktop_lyrics.html');
  assert.doesNotMatch(html, /pywebview-drag-region/);
  const controls = [...html.matchAll(/<(?:button|input)\b[^>]*>/g)];
  assert.ok(controls.length >= 14);
  assert.ok(controls.every(match => match[0].includes('dl-no-drag')));
  for (const id of [
    'dl-prev-btn', 'dl-play-btn', 'dl-next-btn', 'dl-heart-btn', 'dl-lock-btn', 'dl-more-btn', 'dl-close-btn',
    'dl-font-minus-btn', 'dl-font-plus-btn', 'dl-minus-btn', 'dl-plus-btn', 'dl-mute-btn', 'dl-volume-slider',
    'dl-preset-btn', 'dl-surface-btn', 'dl-center-btn', 'dl-settings-btn',
  ]) {
    assert.ok(html.includes(`id="${id}"`), `Missing control id=${id}`);
  }
  for (const preset of ['cinema_white','emerald_flow','vinyl_gold','light_desktop_ink','sakura_mist','game_contrast','apple_glass','neon_duo']) assert.ok(html.includes(preset));
  const elements = new Map(), events = new Map(), calls = [];
  const element = id => {
    if (elements.has(id)) return elements.get(id);
    const node = {
      style: {setProperty(k,v) { this[k] = v; }}, dataset: {}, parentElement: {},
      classList: {add(){}, remove(){}, toggle(){}}, children: [],
      appendChild(n) { this.children.push(n); }, setAttribute(k,v) { this[k] = v; },
      addEventListener(k,fn) { this[k] = fn; },
    };
    elements.set(id,node);
    return node;
  };
  let frame = null, clock = 0, releaseClose, closed = false;
  const page = {performance: {now: () => clock}, window: {close: () => { closed = true; }},
    document: {getElementById: element, documentElement: element('root'), addEventListener(){},
      createElement: () => element(Symbol()), createElementNS: () => element(Symbol())},
    requestAnimationFrame: fn => {frame = fn; return 1;}, cancelAnimationFrame: () => {frame = null;},
    EventSource: class {addEventListener(k,fn) {events.set(k,fn);}},
    fetch: async (path, opts) => {
      const body = JSON.parse(opts?.body || '{}');
      calls.push([path,body]);
      if (body.open === false) await new Promise(resolve => {releaseClose = resolve;});
      return {ok: true, json: async () => ({ok:true,data:{}})};
    },
  };
  vm.runInNewContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],page);
  const emit = (key,payload) => events.get(key)({data:JSON.stringify(payload)});
  const packet = {line:'one', words:[{start:1,dur:2,text:'one'}], current_time:1, offset_sec:.5,
    playback_rate:2, playback_seq:4, liked:true, is_playing:true};
  emit('desktop_lyrics', packet);
  const word = element('dl-row-a').children[0];
  assert.equal(word.style['--progress'],'25.0%');
  clock = 250; frame(clock);
  assert.equal(word.style['--progress'],'50.0%');
  emit('desktop_lyrics',{playback_seq:3,liked:false});
  assert.equal(element('dl-heart-btn')['aria-pressed'],'true');
  emit('desktop_lyrics',{...packet,buffering:true});
  assert.equal(frame,null);
  assert.equal(word.style['--progress'],'25.0%');
  emit('desktop_lyrics_style',{gpu_performance_mode:'eco',style:{preset:'light_desktop_ink'}});
  emit('desktop_lyrics',{...packet,buffering:false});
  assert.equal(frame,null);
  assert.equal(element('root').style['--dl-played'],'#181a1f');
  for (const [instance, seq, trackSeq, liked, expected] of [
    ['old', 12, 4, true, 'true'], ['new', 1, 0, false, 'false'],
    ['new', 0, 0, true, 'false'], ['old', 13, 5, true, 'false'], ['new', 2, 1, true, 'true'],
  ]) {
    emit('desktop_lyrics', { player_instance_id: instance, msg_seq: seq, playback_seq: trackSeq, liked });
    assert.equal(element('dl-heart-btn')['aria-pressed'], expected);
  }
  const drag = element('dl-shell').pointerdown;
  const before = calls.length;
  await drag({button:0,target:{closest:()=>true}});
  await drag({button:2,target:{closest:()=>false}});
  assert.equal(calls.length,before);
  await drag({button:0,target:{closest:()=>false}});
  assert.equal(calls.at(-1)[0],'/api/desktop/lyrics-window/drag');
  await element('dl-settings-btn').click({});
  assert.equal(calls.at(-1)[1].action,'open_lyrics_settings');
  const closing = element('dl-close-btn').click({});
  assert.equal(closed,false);
  releaseClose(); await closing;
  assert.equal(closed,true);
  page.fetch = async () => ({ok:false,status:503,json:async () => ({error:{message:'播放器未连接'}})});
  await element('dl-next-btn').click({});
  assert.equal(element('dl-error').textContent,'播放器未连接');
}

function testGovernor() {
  const stage = new stageManager.constructor();
  const saved = {settings:state.get('settings'),isPlaying:state.get('isPlaying'),windowVisible:state.get('windowVisible'),
    windowMinimized:state.get('windowMinimized'),isStageOpen:state.get('isStageOpen')};
  const oldDoc = globalThis.document, oldCancel = globalThis.cancelAnimationFrame;
  let cancelled;
  try {
    globalThis.document = {documentElement:{dataset:{}},getElementById:()=>null,hidden:false};
    globalThis.cancelAnimationFrame = id => {cancelled = id;};
    for (const reason of ['hidden','minimized','tray','blur','paused','eco','reduced']) {
      state.set({settings:{visual_mode:'soft_dynamic', gpu_performance_mode:reason === 'eco' ? 'eco' : 'balanced'},
        windowVisible:reason !== 'tray', windowMinimized:reason === 'minimized',isPlaying:reason !== 'paused',isStageOpen:true});
      document.hidden = reason === 'hidden'; stage.windowBlurred = reason === 'blur';
      stage.motion = { matches:reason === 'reduced' };
      stage.fluidCtx = {}; stage.fluidRafId = 42; cancelled = null;
      stage.syncAmbientPauseState();
      assert.equal(cancelled,42,reason);
      assert.equal(document.documentElement.dataset.gpuPaused,'true',reason);
    }
    const backing = stage.fluidCanvas = {width:160,height:160};
    stage.unmountAmbientLayer();
    assert.deepEqual(backing,{width:0,height:0});
  } finally {
    globalThis.document = oldDoc; globalThis.cancelAnimationFrame = oldCancel;
    state.set(saved);
  }
}

function testDecks() {
  const p = new player.constructor();
  const params = () => ({value:1,cancelScheduledValues(){},setValueAtTime(v) {this.start=v;},
    linearRampToValueAtTime(v,t){this.end=v;this.until=t;}});
  const a = p.streamAudio, b = p.standbyAudio;
  const ga = params(), gb = params();
  p.audioCtx = {currentTime:2}; p.deckGains.set(a,ga); p.deckGains.set(b,gb);
  a.src='/api/stream/one'; a.paused=false;
  p.reserveOutgoing();
  assert.equal(p.audio,b);
  p.startFade();
  assert.deepEqual([ga.start,ga.end,gb.start,gb.end],[1,0,0,1]);
  assert.ok(ga.until > 2);
  p.finishFade();
  assert.equal(a.paused,true); assert.equal(a.getAttribute('src'),null); assert.equal(gb.value,1);
  p.reserveOutgoing();
  ga.linearRampToValueAtTime = () => {throw Error('automation unavailable');};
  p.startFade();
  assert.equal(p.fade,null);
  assert.equal(ga.value,1); assert.equal(b.paused,true);
  p.activeUsesWebAudio=true; p.activeSrc='/api/stream/two';
  p.userPausedSequence=-1;
  p.fallbackToDirect(p.requestSequence,p.activeSrc);
  assert.equal(p.audio,p.directAudio); assert.equal(p.audio.src,p.activeSrc); assert.equal(p.audio.paused,false);
}

export async function runPhase3Tests() {
  assert.deepEqual(THEME_OPTIONS.map(x=>x.id),['dark','midnight','light','celadon','parchment','nordic','espresso','emerald','dynamic','auto']);
  for (const {id} of THEME_OPTIONS) assert.equal(normalizeAppearance({theme:id}).theme,id);
  const originalDocument = globalThis.document;
  const colors = new Map();
  try {
    globalThis.document = {documentElement:{dataset:{},style:{setProperty:(key,value) => colors.set(key,value)}}};
    for (const coverColor of ['#ffffff','#000000','#ff0000','#00ff00','#0000ff']) {
      applyAppearance({theme:'dynamic'},{coverColor});
      for (const i of [0,1,2]) assert.ok(computeContrastRatio('#8e8e98',colors.get('--album-'+i)) >= 4.5);
    }
  } finally { globalThis.document = originalDocument; }
  const components = read('web/css/components.css');
  for (const [button, background] of [['keep', '#62a368'], ['prune', '#df584c']]) {
    const block = components.split('.btn-' + button)[1].split('}')[0];
    assert.match(block, /color: #121214;/);
    assert.ok(computeContrastRatio('#121214', background) >= 4.5);
  }
  const css = read('web/css/base.css');
  for (const theme of ['light', 'celadon', 'parchment']) {
    const block = css.split('html[data-theme="' + theme + '"] {')[1].split('}')[0];
    const color = token => block.match(new RegExp('--' + token + ': (#[0-9a-f]+)'))[1];
    for (const surface of ['bg-night', 'bg-carbon', 'bg-card', 'bg-elevated']) {
      for (const ink of ['text-primary', 'text-secondary', 'text-muted']) {
        assert.ok(computeContrastRatio(color(ink), color(surface)) >= 4.5, theme + ' ' + ink + ' on ' + surface);
      }
    }
  }
  const rootBlock = css.split(':root {')[1].split('}')[0];
  const getToken = (block, token) => {
    const m = block.match(new RegExp('--' + token + ':\\s*(#[0-9a-f]+)', 'i'))
      || rootBlock.match(new RegExp('--' + token + ':\\s*(#[0-9a-f]+)', 'i'));
    assert.ok(m, 'Missing token --' + token);
    return m[1];
  };
  for (const theme of ['dark', 'midnight', 'light', 'celadon', 'parchment', 'nordic', 'espresso', 'emerald']) {
    const block = theme === 'dark' ? rootBlock : css.split('html[data-theme="' + theme + '"] {')[1].split('}')[0];
    const surfaces = ['bg-night', 'bg-carbon', 'bg-card', 'bg-elevated'].map(s => getToken(block, s));
    for (const inkToken of ['accent-text', 'border-control']) {
      const ink = getToken(block, inkToken);
      for (const surface of surfaces) {
        assert.ok(computeContrastRatio(ink, surface) >= 4.5, `${theme} --${inkToken} (${ink}) on ${surface}`);
      }
    }
    const parseHex = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));
    const toHex = rgb => '#' + rgb.map(v => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, '0')).join('');
    const blend = (fg, alpha, bg) => {
      const f = parseHex(fg), b = parseHex(bg);
      return toHex([0, 1, 2].map(i => f[i] * alpha + b[i] * (1 - alpha)));
    };
    for (const preset of ACCENT_PRESETS) {
      const readable = computeReadableAccentText(preset.hex, theme);
      for (const surface of surfaces) {
        assert.ok(computeContrastRatio(readable, surface) >= 4.5, `${theme} preset ${preset.id} (${readable}) on ${surface}`);
        const tinted = blend(preset.hex, 0.16, surface);
        assert.ok(computeContrastRatio(readable, tinted) >= 4.5, `${theme} preset ${preset.id} (${readable}) on tinted ${tinted} (${surface})`);
      }
    }
  }
  for (const mode of ['fluid','vinyl','poster','visualizer']) assert.ok(read('web/index.html').includes('value="'+mode+'"'));
  assert.ok(read('web/css/stage.css').includes('[data-theme="light"] .stage-ambient-scrim'));
  await testDesktop();
  testGovernor();
  testDecks();
}
