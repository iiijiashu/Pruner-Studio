/**
 * Pruner Studio - Phase 3 Light-Priority Visual Hygiene & VAC-01..VAC-08 Contract Suite
 * Verifies opaque chrome, soft recessed inputs, toolbar weight budget, zero noise pills,
 * scrollbarless carousel steppers, player bar geometry, hero/cross-view parity, and WCAG contrast.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createMockDom } from './wave4_experience.test.js';
import { discoverManager } from '../../web/js/discover.js';
import { deckManager } from '../../web/js/deck.js';
import { auth } from '../../web/js/auth.js';
import { api } from '../../web/js/api.js';
import { getSafeImageUrl } from '../../web/js/utils.js';
import { state } from '../../web/js/state.js';
import {
  THEME_OPTIONS,
  ACCENT_PRESETS,
  parseColorToRgb,
  relativeLuminance,
  computeContrastRatio,
  computeReadableAccentText,
} from '../../web/js/theme.js';

const read = (p) => fs.readFileSync(new URL('../../' + p, import.meta.url), 'utf8');

function extractBlock(css, selector) {
  const idx = css.indexOf(selector + ' {');
  assert.ok(idx >= 0, `Missing CSS block: ${selector}`);
  return css.slice(idx + selector.length + 2).split('}')[0];
}

function testOpaqueChromeAndPaletteContracts() {
  const baseCss = read('web/css/base.css');

  // VAC-01: Opaque chrome across all themes (zero rgba translucent header/rail/player)
  assert.doesNotMatch(baseCss, /--bg-(?:header|rail|player):\s*rgba\(/i, 'Chrome surfaces must be 100% opaque');

  const fixedThemes = [
    { id: 'dark', selector: ':root' },
    { id: 'midnight', selector: 'html[data-theme="midnight"]' },
    { id: 'light', selector: 'html[data-theme="light"]' },
    { id: 'celadon', selector: 'html[data-theme="celadon"]' },
    { id: 'parchment', selector: 'html[data-theme="parchment"]' },
    { id: 'nordic', selector: 'html[data-theme="nordic"]' },
    { id: 'espresso', selector: 'html[data-theme="espresso"]' },
    { id: 'emerald', selector: 'html[data-theme="emerald"]' },
  ];
  for (const { id, selector } of fixedThemes) {
    const block = extractBlock(baseCss, selector);
    for (const token of ['--bg-rail', '--bg-header', '--bg-player', '--bg-well']) {
      const m = block.match(new RegExp(`${token}:\\s*(#[0-9a-f]{6})`));
      assert.ok(m, `${id} must declare opaque lowercase hex for ${token}`);
    }
  }

  // VAC-01: Auto light-mode media query mirrors opaque light chrome
  const autoBlock = baseCss.split('@media (prefers-color-scheme: light)')[1]?.split('html[data-theme="auto"] {')[1]?.split('}')[0] || '';
  for (const token of ['--bg-rail', '--bg-header', '--bg-player', '--bg-well']) {
    assert.match(autoBlock, new RegExp(`${token}:\\s*#[0-9a-f]{6}`), `auto light mode must declare opaque ${token}`);
  }

  // VAC-03 & VAC-06: Softened light-theme primary CTA luminance (> 0.025, >= 7.0:1 contrast) & harmonized play circle
  for (const theme of ['parchment', 'light', 'celadon']) {
    const block = extractBlock(baseCss, `html[data-theme="${theme}"]`);
    const btnBg = block.match(/--btn-primary-bg:\s*(#[0-9a-f]{6})/)?.[1];
    const btnFg = block.match(/--btn-primary-text:\s*(#[0-9a-f]{6})/)?.[1];
    assert.ok(btnBg && btnFg, `${theme} must define --btn-primary-bg and --btn-primary-text`);
    assert.ok(relativeLuminance(parseColorToRgb(btnBg)) > 0.025, `${theme} primary button ${btnBg} must not be harsh pitch black`);
    assert.ok(computeContrastRatio(btnFg, btnBg) >= 7.0, `${theme} primary button contrast must be >= 7.0:1`);
    assert.match(block, /--surface-sheen:\s*none;/, `${theme} must use matte paper surface without plastic sheen`);

    const playFg = block.match(/--player-play-fg:\s*(#[0-9a-f]{6})/)?.[1];
    assert.ok(playFg, `${theme} must define --player-play-fg`);
    assert.match(block, /--player-play-bg:\s*var\(--accent-text\);/, `${theme} play circle must harmonize with --accent-text`);
    for (const preset of ACCENT_PRESETS) {
      const accentText = computeReadableAccentText(preset.hex, theme);
      assert.ok(computeContrastRatio(playFg, accentText) >= 4.6, `${theme} play circle ${preset.id} (${accentText}) vs ${playFg} must be >= 4.6:1`);
    }
  }

  // VAC-08: THEME_OPTIONS exact order & updated swatch sync
  assert.deepEqual(
    THEME_OPTIONS.map((t) => t.id),
    ['dark', 'midnight', 'light', 'celadon', 'parchment', 'nordic', 'espresso', 'emerald', 'dynamic', 'auto'],
  );
  const parchmentOpt = THEME_OPTIONS.find((t) => t.id === 'parchment');
  const lightOpt = THEME_OPTIONS.find((t) => t.id === 'light');
  assert.equal(parchmentOpt.bg, '#f3ede2');
  assert.equal(parchmentOpt.surface, '#fbf8f2');
  assert.equal(lightOpt.bg, '#f5f3ee');
  assert.equal(lightOpt.surface, '#fdfcfa');
}

function testHtmlToolbarAndCrossViewStructure() {
  const indexHtml = read('web/index.html').replace(/\r\n/g, '\n');
  const discoverCss = read('web/css/discover.css');
  const catalogCss = read('web/css/catalog.css');
  const deckCss = read('web/css/deck.css');
  const cloudCss = read('web/css/cloud.css');
  const searchCss = read('web/css/search.css');
  const componentsCss = read('web/css/components.css');

  // VAC-03: Strict toolbar weight budget (<= 1 primary, <= 1 secondary) on Daily Recommend & Dusty Gems
  const dustyActions = indexHtml.split('id="discover-dusty-section"')[1].split('class="discover-section-actions">')[1].split('id="discover-dusty-carousel"')[0];
  const dailyActions = indexHtml.split('<!-- Daily Recommend 30 Tracks -->')[1].split('class="discover-section-actions">')[1].split('id="discover-extra"')[0];
  for (const [label, html] of [['Dusty Gems', dustyActions], ['Daily Recommend', dailyActions]]) {
    const primaryCount = (html.match(/\bbtn-primary\b/g) || []).length;
    const secondaryCount = (html.match(/\bbtn-secondary\b/g) || []).length;
    assert.ok(primaryCount <= 1, `${label} must have <= 1 .btn-primary (found ${primaryCount})`);
    assert.ok(secondaryCount <= 1, `${label} must have <= 1 .btn-secondary (found ${secondaryCount})`);
  }
  // Daily Recommend exact order: Play All (primary) -> Shuffle (secondary) -> Divider -> Ghosts
  const playIdx = dailyActions.indexOf('id="discover-daily-play-all"');
  const shuffleIdx = dailyActions.indexOf('id="discover-shuffle"');
  const dividerIdx = dailyActions.indexOf('toolbar-divider');
  const historyIdx = dailyActions.indexOf('id="discover-history"');
  assert.ok(playIdx < shuffleIdx && shuffleIdx < dividerIdx && dividerIdx < historyIdx, 'Daily Recommend toolbar order must follow 1-primary / 1-secondary / divider / ghosts');

  // Quiet archival colophon + bottom status footer (without forbidden rail-footer class)
  assert.ok(indexHtml.includes('class="rail-status-bar"') && indexHtml.includes('class="brand-badge">本机运行</span>'));
  assert.equal(indexHtml.includes('rail-footer'), false);
  const railHeader = indexHtml.split('<div class="rail-header">')[1].split('</div>\n\n      <nav')[0];
  assert.equal(railHeader.includes('brand-badge'), false, 'brand-badge must be relocated out of cramped rail-header');

  // Settings shortcuts button tucked cleanly into .cloud-subtab-bar
  const subtabBar = indexHtml.split('<div class="cloud-subtab-bar">')[1].split('<!-- SUB-PANEL A')[0];
  assert.ok(subtabBar.includes('id="settings-shortcuts-btn"'), '#settings-shortcuts-btn must live inside .cloud-subtab-bar');
  const settingsPanel = indexHtml.split('id="cloud-panel-settings"')[1].split('<!-- SUB-PANEL B')[0];
  assert.equal(settingsPanel.includes('id="settings-shortcuts-btn"'), false, '#settings-shortcuts-btn must not float inside #cloud-panel-settings');

  // VAC-07: Cross-view radius, hero parity, recessed wells, zero orphan top borders
  assert.match(discoverCss, /\.discover-fm-cover\s*\{[^}]*border-radius:\s*var\(--radius-control\)/s);
  assert.match(catalogCss, /\.catalog-song-card\s*\{[^}]*border-radius:\s*8px/s);
  assert.match(deckCss, /\.deck-lyrics-wrapper\s*\{[^}]*background-color:\s*var\(--bg-well\)/s);
  assert.match(cloudCss, /\.settings-block\s*\{[^}]*background-color:\s*var\(--bg-well\)[^}]*box-shadow:\s*none/s);
  assert.match(searchCss, /\.search-view-header-card\s*\{[^}]*padding:\s*0;[^}]*border-top:\s*none/s);
  assert.match(componentsCss, /\.header-search-trigger\s*\{[^}]*background-color:\s*var\(--bg-well\)[^}]*border:\s*1px solid var\(--border-subtle\)/s);
}

function testDiscoverNoiseSuppressionAndCarouselSteppers() {
  const dom = createMockDom();
  const origDoc = globalThis.document;
  const origRaf = globalThis.requestAnimationFrame;
  const savedState = {
    dailyRecommend: state.get('dailyRecommend'),
    dailyRecommendMeta: state.get('dailyRecommendMeta'),
    dustyGems: state.get('dustyGems'),
    likedSongIds: state.get('likedSongIds'),
    offlineMode: state.get('offlineMode'),
  };

  try {
    globalThis.document = dom.mockDoc;
    globalThis.requestAnimationFrame = (fn) => { fn(); return 1; };

    // VAC-04: Suppress generic 网易云每日推荐 / 每日推荐 badges, preserve distinct reasons, icon-only + .sr-only 不再推荐
    const dailyList = dom.registerId('discover-daily-list');
    dom.registerId('discover-daily-play-all', 'button');
    dom.registerId('discover-daily-notice');

    state.set({
      offlineMode: false,
      likedSongIds: new Set([102]),
      dailyRecommendMeta: null,
      dailyRecommend: [
        { id: 101, title: 'Song Default A', artists: ['Artist A'], album: 'Album A', duration: 210000, recommend_reason: '网易云每日推荐' },
        { id: 102, title: 'Song Default B', artists: ['Artist B'], album: 'Album B', duration: 195000, recommend_reason: '每日推荐' },
        { id: 103, title: 'Song Distinctive', artists: ['Artist C'], album: 'Album C', duration: 240000, recommend_reason: '超 85% 人播放' },
      ],
    });
    discoverManager.renderDailyList();

    const badges = dailyList.querySelectorAll('.discover-reason-badge');
    assert.equal(badges.length, 1, 'Only distinct informative recommend_reason badges should render');
    assert.equal(badges[0].textContent, '超 85% 人播放');

    const banBtns = dailyList.querySelectorAll('.discover-not-interested-btn');
    assert.equal(banBtns.length, 3);
    for (const btn of banBtns) {
      assert.equal(btn.classList.contains('discover-row-reveal-action'), true, 'Ban button must use progressive reveal');
      const srLabel = btn.querySelector('.sr-only');
      assert.ok(srLabel && srLabel.textContent === '不再推荐', 'Ban button must keep accessible .sr-only label');
    }

    // VAC-05: Dusty Gems carousel steppers toggle disabled & edge fade flags honestly at bounds
    const carousel = dom.registerId('discover-dusty-carousel');
    const strip = dom.registerId('discover-dusty-grid');
    const prevBtn = dom.registerId('discover-dusty-prev', 'button');
    const nextBtn = dom.registerId('discover-dusty-next', 'button');
    dom.registerId('discover-dusty-play-all', 'button');

    strip.scrollLeft = 0;
    strip.scrollWidth = 1500;
    strip.clientWidth = 800;
    strip.scrollBy = ({ left }) => {
      strip.scrollLeft = Math.max(0, Math.min(strip.scrollWidth - strip.clientWidth, strip.scrollLeft + left));
    };

    discoverManager.bindDustyCarouselControls();
    state.set({
      dustyGems: Array.from({ length: 6 }, (_, i) => ({
        id: 200 + i,
        title: `Gem ${i + 1}`,
        artists: ['Singer'],
        recommend_reason: '沉睡宝藏 · 尚未播放',
      })),
    });
    discoverManager.renderDustyGems();

    assert.equal(prevBtn.disabled, true, 'At left edge prev must be disabled');
    assert.equal(nextBtn.disabled, false, 'With overflow next must be enabled');
    assert.equal(carousel.dataset.canScrollLeft, 'false');
    assert.equal(carousel.dataset.canScrollRight, 'true');

    nextBtn.click();
    assert.ok(strip.scrollLeft > 0, 'Clicking next must scroll strip right');
    assert.equal(prevBtn.disabled, false, 'After scrolling right prev must be enabled');
    assert.equal(carousel.dataset.canScrollLeft, 'true');

    // Scroll to far right bound
    nextBtn.click();
    assert.equal(strip.scrollLeft, 700);
    assert.equal(nextBtn.disabled, true, 'At right edge next must be disabled');
    assert.equal(carousel.dataset.canScrollRight, 'false');

    // Scroll back to start
    prevBtn.click();
    prevBtn.click();
    assert.equal(strip.scrollLeft, 0);
    assert.equal(prevBtn.disabled, true);
    assert.equal(nextBtn.disabled, false);
  } finally {
    globalThis.document = origDoc;
    globalThis.requestAnimationFrame = origRaf;
    state.set(savedState);
  }
}

async function testRound2ResponsiveAndUiContracts() {
  const indexHtml = read('web/index.html').replace(/\r\n/g, '\n');
  const baseCss = read('web/css/base.css');
  const componentsCss = read('web/css/components.css');
  const discoverCss = read('web/css/discover.css');
  const stageCss = read('web/css/stage.css');
  const deckCss = read('web/css/deck.css');

  // Phase A: Fluid rail, 3-axis aligned header inner, rail playlists, compact 76px player, column-major daily list
  assert.match(baseCss, /--rail-width:\s*216px;/);
  assert.match(baseCss, /@media\s*\(min-width:\s*1440px\)\s*\{\s*:root\s*\{\s*--rail-width:\s*244px;/);
  assert.match(baseCss, /@media\s*\(min-width:\s*1920px\)\s*\{\s*:root\s*\{\s*--rail-width:\s*260px;/);
  assert.match(baseCss, /--max-content-width:\s*min\(1960px,\s*100%\);/);
  assert.match(baseCss, /html\[data-density="compact"\][^}]*--player-height:\s*76px;/s);
  assert.ok(indexHtml.includes('class="app-header-inner"'), 'Header must wrap contents in .app-header-inner for 3-axis alignment');
  assert.match(componentsCss, /\.app-header-inner\s*\{[^}]*max-width:\s*var\(--max-content-width\)[^}]*padding:\s*0 clamp\(20px,\s*3vw,\s*56px\)/s);
  assert.ok(indexHtml.includes('id="rail-playlists-section"') && indexHtml.includes('id="rail-playlists-list"'));
  assert.match(discoverCss, /grid-auto-flow:\s*column;/, 'Wide daily list must use column-major reading order');

  // Phase A.3: Single-line zero-wrap stage topbar, overflow popover, top-unmasked scroll-aware lyrics, 5-ratio stack
  assert.match(stageCss, /\.stage-top-bar\s*\{[^}]*max-height:\s*48px;[^}]*flex-wrap:\s*nowrap;/s);
  assert.match(stageCss, /\.stage-lyrics-scroller\s*\{[^}]*mask-image:\s*linear-gradient\(to bottom,\s*black 0%,\s*black 86%,\s*transparent 100%\)/s);
  assert.match(stageCss, /\.stage-lyrics-scroller\.has-top-scroll\s*\{[^}]*mask-image:\s*linear-gradient\(to bottom,\s*transparent 0%,\s*black 11%,\s*black 86%,\s*transparent 100%\)/s);
  assert.match(stageCss, /@media\s*\(max-aspect-ratio:\s*115\/100\),\s*\(max-width:\s*1024px\)/);
  const stageMore = indexHtml.split('id="stage-more-popover"')[1]?.split('</div>\n          </div>\n        </div>')[0] || '';
  for (const needle of ['id="stage-candidates-btn"', 'id="stage-similar-btn"', 'data-copy-line', 'data-copy-full']) {
    assert.ok(stageMore.includes(needle), `Stage overflow popover must house ${needle}`);
  }
  const stageRightCol = indexHtml.split('<div class="stage-right-column">')[1]?.split('</div>\n      </div>\n    </div>')[0] || '';
  assert.equal(stageRightCol.includes('lyrics-copy-actions'), false, 'Stage right column must not show redundant floating copy bar above lyrics');

  // Phase B: 0%-100% Smart Radio slider + Braun SVG Analog Radio Tuner Dial + zero-blur switch + clickable insights
  assert.ok(indexHtml.includes('id="discover-explore-slider"') && indexHtml.includes('min="0" max="100"'), 'Explore slider must cover full 0..100 range');
  assert.ok(indexHtml.includes('<svg id="discover-acoustic-orb"'), 'Smart Radio hero must use pure-vector SVG Braun Analog Radio Tuner Dial');
  assert.equal(indexHtml.includes('<canvas id="discover-acoustic-orb"'), false, 'Legacy cold-gradient canvas orb must be removed');
  assert.ok(indexHtml.includes('id="discover-tuner-needle"') && indexHtml.includes('id="discover-tuner-arc"') && indexHtml.includes('id="discover-tuner-value"'), 'Tuner dial must expose needle, arc, and monospace FM readout');
  assert.equal(/@keyframes\s+viewEnter\b/.test(baseCss), false, 'base.css must not animate full view opacity on tab switch / cold boot');
  assert.equal(discoverManager.formatExploreLabel(0), '0% · 纯熟歌回味');
  assert.equal(discoverManager.formatExploreLabel(20), '20% · 偏爱熟歌');
  assert.equal(discoverManager.formatExploreLabel(50), '50% · 熟歌与新声均衡');
  assert.equal(discoverManager.formatExploreLabel(85), '85% · 偏爱新声探索');
  assert.equal(discoverManager.formatExploreLabel(100), '100% · 纯新歌探索');
  const prevSettings = state.get('settings');
  try {
    state.updateSettings({ visual_mode: 'quiet_eco', gpu_performance_mode: 'balanced', game_mode: false });
    assert.equal(discoverManager.shouldPauseOrb(), true, 'Tuner dial kinetic meter must pause in quiet_eco');
    state.updateSettings({ visual_mode: 'soft_dynamic', gpu_performance_mode: 'eco', game_mode: false });
    assert.equal(discoverManager.shouldPauseOrb(), true, 'Tuner dial kinetic meter must pause in gpu eco mode');
    state.updateSettings({ visual_mode: 'soft_dynamic', gpu_performance_mode: 'balanced', game_mode: true });
    assert.equal(discoverManager.shouldPauseOrb(), true, 'Tuner dial kinetic meter must pause in game_mode');
  } finally {
    state.set({ settings: prevSettings });
  }

  // Verify clickable Discover insights bar & Braun tuner dial live slider sync
  {
    const insightDom = createMockDom();
    insightDom.mockDoc.createElementNS = (_ns, tag) => insightDom.makeEl(tag);
    const prevDoc = globalThis.document;
    const prevWin = globalThis.window;
    const prevNode = globalThis.Node;
    const prevCustomEvent = globalThis.CustomEvent;
    const dispatched = [];
    try {
      globalThis.document = insightDom.mockDoc;
      globalThis.Node = insightDom.MockNode;
      globalThis.CustomEvent = class MockCustomEvent { constructor(type, init = {}) { this.type = type; this.detail = init.detail; } };
      globalThis.window = {
        dispatchEvent: (ev) => dispatched.push(ev),
      };
      const bar = insightDom.registerId('discover-insights-bar', 'div');
      const dustySection = insightDom.registerId('discover-dusty-section', 'div');
      let scrolled = false;
      dustySection.scrollIntoView = () => { scrolled = true; };

      const needle = insightDom.registerId('discover-tuner-needle', 'g');
      insightDom.registerId('discover-tuner-arc', 'path');
      const readout = insightDom.registerId('discover-tuner-value', 'text');
      insightDom.registerId('discover-tuner-stage', 'div');

      discoverManager.renderAcousticOrbFrame(80);
      assert.equal(readout.textContent, 'FM · 80%', 'Tuner dial readout must mirror 80%');
      assert.equal(needle.attributes.transform, 'rotate(81.0 80 80)', 'Tuner needle angle must rotate to +81 deg at 80%');

      state.set({
        listeningInsights: {
          catalog_track_count: 1122,
          total_plays: 259,
          total_completed: 155,
          total_listen_minutes: 602.6,
          dusty_gems_count: 1104,
          cloud_count: 0,
          local_count: 0,
          top_artists: ['林俊杰', '宇多田ヒカル', 'Vicky宣宣'],
        },
        dustyGems: [{ id: 1, title: 'Gem', artists: ['A'] }],
      });
      discoverManager.renderInsightsBar();
      const pills = bar.querySelectorAll('.discover-insight-pill');
      assert.equal(pills.length, 4, 'All 4 Discover insight blocks must render');
      assert.ok(pills.every((p) => p.classList.contains('is-interactive')), 'Every insight block must be interactive');
      pills[0].click();
      pills[1].click();
      pills[2].click();
      const chips = bar.querySelectorAll('.discover-insight-artist-chip');
      assert.equal(chips.length, 3, 'Top artists block must render 3 individual clickable artist chips');
      chips[1].click();
      assert.equal(scrolled, true, 'Clicking 沉睡宝藏 insight must scroll to dusty section');
      assert.equal(dustySection.classList.contains('is-spotlight'), true, 'Clicking 沉睡宝藏 must spotlight section');
      assert.deepEqual(
        dispatched.map((e) => e.detail),
        ['history', 'catalog', { tab: 'search', query: '宇多田ヒカル' }],
        'Clicking insights and artist chips must dispatch exact navigation targets',
      );
    } finally {
      if (discoverManager.dustySpotlightTimer) {
        clearTimeout(discoverManager.dustySpotlightTimer);
        discoverManager.dustySpotlightTimer = null;
      }
      globalThis.document = prevDoc;
      globalThis.window = prevWin;
      globalThis.Node = prevNode;
      globalThis.CustomEvent = prevCustomEvent;
    }
  }

  // Phase C: Redesigned .account-hub has EXACTLY ONE probe-all button, zero per-row 探测连通, Bilibili QR modal & quick auth
  assert.ok(indexHtml.includes('id="bilibili-qr-modal"') && indexHtml.includes('id="cloud-channels-test-all-btn"'));
  const dom = createMockDom();
  const origDoc = globalThis.document;
  const origGetAccounts = api.getAccounts;
  const origGetChannels = api.getChannels;
  try {
    globalThis.document = dom.mockDoc;
    const panel = dom.makeEl('section');
    panel.isConnected = true;
    panel.append = (...kids) => kids.forEach(k => panel.appendChild(typeof k === 'string' ? dom.mockDoc.createTextNode(k) : k));
    panel.replaceChildren = () => { panel.children.length = 0; };
    const origCreate = document.createElement;
    document.createElement = (tag) => {
      const el = origCreate(tag);
      el.append = (...kids) => kids.forEach(k => el.appendChild(typeof k === 'string' ? dom.mockDoc.createTextNode(k) : k));
      el.replaceChildren = () => { el.children.length = 0; };
      return el;
    };
    api.getAccounts = async () => ({ ok: true, data: { active_account_id: '1', saved_accounts: [{ account_id: '1', display_name: 'Alice' }] } });
    api.getChannels = async () => ({
      ok: true,
      data: {
        channels: [
          { channel: 'bilibili', display_name: 'B站音频', connection_state: 'unconfigured' },
          { channel: 'qq', display_name: 'QQ音乐', connection_state: 'configured' },
          { channel: 'kugou_lite', display_name: '酷狗概念版', connection_state: 'unconfigured' },
          { channel: 'gdstudio', display_name: 'GDStudio', connection_state: 'reachable', zero_credential: true },
        ],
      },
    });
    await auth.renderHub(panel);
    const buttons = panel.querySelectorAll('button');
    const probeBtns = buttons.filter(b => b.id === 'hub-probe-all-btn' || b.textContent === '一键探测全部');
    const legacyProbeBtns = buttons.filter(b => b.textContent === '探测连通');
    assert.equal(probeBtns.length, 1, 'Account Hub must render exactly one 一键探测全部 button');
    assert.equal(legacyProbeBtns.length, 0, 'Account Hub must not render stacked per-row 探测连通 buttons');
    assert.ok(buttons.some(b => b.textContent === '扫码登录'), 'Bilibili/NetEase must offer inline 扫码登录');
    assert.ok(buttons.some(b => b.textContent === '快捷登录'), 'QQ/Kugou/Migu/Kuwo must offer inline 快捷登录');
  } finally {
    api.getAccounts = origGetAccounts;
    api.getChannels = origGetChannels;
    globalThis.document = origDoc;
  }

  // Phase D: Zero-scroll Deck cockpit, default random order prioritizing healthy tracks over stubs, vinyl SVG fallback
  assert.match(deckCss, /\.main-viewport:has\(#view-deck\.active\)\s*\{[^}]*overflow:\s*hidden;/s);
  assert.ok(indexHtml.includes('<option value="random" selected>排序: 随机抽选</option>'));
  assert.ok(indexHtml.includes('id="deck-random-next-btn"'));
  const fallbackSvg = getSafeImageUrl('', '音');
  assert.ok(fallbackSvg.startsWith('data:image/svg+xml') && !fallbackSvg.includes('%23161713'), 'Cover fallback must use warm minimalist vinyl SVG instead of dark slab');
  assert.equal(deckManager.isStubTrack({ id: 1, title: '歌曲 #10086', artists: ['未知歌手'], duration: 0 }), true);
  assert.equal(deckManager.isStubTrack({ id: 2, title: '晴天', artists: ['周杰伦'], duration: 269000, cover_url: 'https://p1.music.126.net/a.jpg' }), false);
  const mixed = [
    { id: 1, title: '歌曲 #1', artists: ['未知歌手'], duration: 0, source_playlist_id: 10 },
    { id: 2, title: 'Healthy A', artists: ['Singer A'], duration: 210000, cover_url: 'https://p1.music.126.net/a.jpg', source_playlist_id: 10 },
    { id: 3, title: 'Healthy B', artists: ['Singer B'], duration: 220000, cover_url: 'https://p1.music.126.net/b.jpg', source_playlist_id: 10 },
  ];
  const orderedRandom = deckManager.filterAndOrderTracks(mixed, 10, 'all', 'random');
  assert.equal(orderedRandom.length, 3);
  assert.equal(orderedRandom[2].id, 1, 'Random Deck order must prioritize hydrated healthy tracks ahead of stub tracks');
}

export async function runVisualHygieneTests() {
  testOpaqueChromeAndPaletteContracts();
  testHtmlToolbarAndCrossViewStructure();
  testDiscoverNoiseSuppressionAndCarouselSteppers();
  await testRound2ResponsiveAndUiContracts();
  console.log('✓ Phase 3 & Round 2 visual hygiene, 5-ratio responsive & UI contract tests passed!');
}
