/**
 * Pruner Studio Frontend Verification - Scan UI HTML/JS/CSS
 * Verifies that primary UI contains zero emojis and zero obsolete English nav terms.
 */
import fs from 'node:fs';
import path from 'node:path';

// Regex for emoji characters
const emojiRegex = /[\u{1F300}-\u{1F9FF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{1F000}-\u{1F02F}\u{1F0A0}-\u{1F0FF}\u{1F100}-\u{1F64F}\u{1F680}-\u{1F6FF}]/u;

const forbiddenTerms = [
  'Review Deck',
  'Rules & Workshop',
  'Cloud & Sync',
  'Instant Unlike'
];

let errors = 0;

function checkFile(filePath) {
  const content = fs.readFileSync(filePath, 'utf8');
  const lines = content.split('\n');
  lines.forEach((line, idx) => {
    if (/\.innerHTML\s*(?:\+?=)|\.insertAdjacentHTML\s*\(/.test(line)) {
      console.error(`[UNSAFE_HTML] ${filePath}:${idx + 1}`);
      errors++;
    }
    if (emojiRegex.test(line)) {
      console.error(`[EMOJI] Found in ${filePath}:${idx + 1} -> ${line.trim()}`);
      errors++;
    }
    for (const term of forbiddenTerms) {
      if (line.toLowerCase().includes(term.toLowerCase())) {
        console.error(`[FORBIDDEN_TERM] '${term}' found in ${filePath}:${idx + 1} -> ${line.trim()}`);
        errors++;
      }
    }
  });
}

function scanDir(dir) {
  const files = fs.readdirSync(dir);
  for (const file of files) {
    const fullPath = path.join(dir, file);
    const stat = fs.statSync(fullPath);
    if (stat.isDirectory()) {
      scanDir(fullPath);
    } else if (file.endsWith('.html') || file.endsWith('.js') || file.endsWith('.css')) {
      checkFile(fullPath);
    }
  }
}

console.log('Scanning web/** for emojis and obsolete English nav labels...');
scanDir('web');

// Structural contract check: the deck bottom bar must keep the instant/batch
// commit mode toggle reachable (DESIGN.md review workspace layout).
const indexHtml = fs.readFileSync(path.join('web', 'index.html'), 'utf8');
const requiredDeckControls = [
  'deck-mode-toggle',
  'deck-dislike-btn',
  'deck-undo-btn',
  'deck-skip-btn',
  'deck-play-btn',
  'deck-chorus-jump-btn',
  'deck-like-btn',
];
for (const controlId of requiredDeckControls) {
  if (!indexHtml.includes(`id="${controlId}"`)) {
    console.error(`[MISSING_CONTROL] #${controlId} must exist in web/index.html (deck action bar contract)`);
    errors++;
  }
}

// Visual Acceptance Contract (VAC-01..VAC-08) static guardrails
const readCss = (name) => fs.readFileSync(path.join('web', 'css', name), 'utf8');
const cssFiles = fs.readdirSync(path.join('web', 'css')).filter((f) => f.endsWith('.css'));

// VAC-01 & VAC-08: Zero backdrop-filter and zero 10px/11px font sizes across all web/css/*.css
for (const file of cssFiles) {
  const text = readCss(file);
  if (/backdrop-filter\s*:/i.test(text)) {
    console.error(`[VAC-01_GLASSMORPHISM] Forbidden backdrop-filter in web/css/${file}`);
    errors++;
  }
  if (/font-size:\s*(?:10|11)px\b/i.test(text)) {
    console.error(`[VAC-08_MICRO_FONT] Forbidden 10px/11px font-size in web/css/${file}`);
    errors++;
  }
}

// VAC-01 & VAC-08: Opaque chrome tokens (--bg-header, --bg-rail, --bg-player) & lowercase hexes in base.css
const baseCss = readCss('base.css');
const chromeRgba = baseCss.match(/--bg-(?:header|rail|player):\s*rgba\([^)]+\)/gi);
if (chromeRgba) {
  console.error(`[VAC-01_OPAQUE_CHROME] Translucent chrome token forbidden in base.css -> ${chromeRgba.join(', ')}`);
  errors++;
}
const uppercaseHex = baseCss.match(/#[0-9a-fA-F]{6}\b/g)?.filter((h) => h !== h.toLowerCase()) || [];
if (uppercaseHex.length > 0) {
  console.error(`[VAC-08_HEX_CASE] Uppercase hex colors forbidden in base.css -> ${uppercaseHex.join(', ')}`);
  errors++;
}

// VAC-02: Recessed hairline .header-search-trigger (must not use --border-control)
const componentsCss = readCss('components.css');
const searchTriggerBlock = componentsCss.match(/\.header-search-trigger\s*\{([^}]*)\}/)?.[1] || '';
if (/--border-control/.test(searchTriggerBlock) || !/--bg-well/.test(searchTriggerBlock)) {
  console.error('[VAC-02_SEARCH_TRIGGER] .header-search-trigger must use recessed --bg-well and hairline border (not --border-control)');
  errors++;
}

// VAC-05: Dusty Gems 2-row 8-card Showcase Grid, dedicated #view-dusty & Smart Radio vinyl console
const discoverCss = readCss('discover.css');
const mainCss = readCss('main.css');
const discoverJs = fs.readFileSync(path.join('web', 'js', 'discover.js'), 'utf8');
if (!/\.discover-dusty-grid\s*\{[^}]*display:\s*grid[^}]*grid-template-columns:\s*repeat\(4,\s*minmax\(0,\s*1fr\)\)/s.test(discoverCss)) {
  console.error('[VAC-05_DUSTY_GRID] .discover-dusty-grid must render as a responsive 4-column showcase grid');
  errors++;
}
for (const reqId of ['discover-dusty-prev', 'discover-dusty-refresh', 'discover-dusty-view-all', 'view-dusty', 'discover-radio-stage', 'discover-radio-next-btn']) {
  if (!indexHtml.includes(`id="${reqId}"`)) {
    console.error(`[VAC-05_DISCOVER_HOOK] #${reqId} must exist in web/index.html`);
    errors++;
  }
}
if (!mainCss.includes('./dusty_view.css')) {
  console.error('[VAC-05_DUSTY_CSS] web/css/main.css must import ./dusty_view.css');
  errors++;
}
for (const legacyText of ['重新编排心动电台', '想听多少新歌']) {
  if (indexHtml.includes(legacyText) || discoverJs.includes(legacyText)) {
    console.error(`[VAC-05_LEGACY_RADIO_COPY] Obsolete copy '${legacyText}' must not remain in Discover Smart Radio`);
    errors++;
  }
}

// VAC-06: Rebalanced player bar height & non-overlapping inline queue pill
const comfortablePlayerH = Number(baseCss.match(/:root\s*\{[\s\S]*?--player-height:\s*(\d+)px/)?.[1] || 0);
const compactPlayerH = Number(baseCss.match(/html\[data-density="compact"\]\s*\{[\s\S]*?--player-height:\s*(\d+)px/)?.[1] || 0);
if (comfortablePlayerH < 82 || compactPlayerH < 72) {
  console.error(`[VAC-06_PLAYER_HEIGHT] --player-height must be >=82px comfortable (${comfortablePlayerH}) and >=72px compact (${compactPlayerH})`);
  errors++;
}
const playerCss = readCss('player.css');
if (!/\.queue-count-dot\s*\{[^}]*position:\s*static/s.test(playerCss)) {
  console.error('[VAC-06_QUEUE_BADGE] .queue-count-dot must use inline position: static (zero overlap with queue SVG)');
  errors++;
}

// VAC-07: Hero parity, recessed settings-block without shadow, zero orphan top borders
const fmCardBlock = discoverCss.match(/\.discover-fm-card\s*\{([^}]*)\}/)?.[1] || '';
if (/box-shadow:\s*none/i.test(fmCardBlock)) {
  console.error('[VAC-07_HERO_PARITY] .discover-fm-card must not disable box-shadow (must match .discover-action-card)');
  errors++;
}
const cloudCss = readCss('cloud.css');
const settingsBlock = cloudCss.match(/\.settings-block\s*\{([^}]*)\}/)?.[1] || '';
if (!/box-shadow:\s*none/i.test(settingsBlock) || !/--bg-well/.test(settingsBlock)) {
  console.error('[VAC-07_NESTED_SHADOW] .settings-block must use recessed --bg-well and box-shadow: none');
  errors++;
}
const catalogCss = readCss('catalog.css');
const searchCss = readCss('search.css');
const catalogNavBlock = catalogCss.match(/\.catalog-section-nav\s*\{([^}]*)\}/)?.[1] || '';
const searchHeaderBlock = searchCss.match(/\.search-view-header-card\s*\{([^}]*)\}/)?.[1] || '';
if (!/border-top:\s*none/i.test(catalogNavBlock) || !/border-top:\s*none/i.test(searchHeaderBlock)) {
  console.error('[VAC-07_ORPHAN_BORDER] First child of #view-catalog and #view-search must have border-top: none');
  errors++;
}

if (errors === 0) {
  console.log('✓ UI Scan Passed: Zero emojis, zero forbidden English nav labels, all required deck controls, and VAC-01..VAC-08 static invariants verified.');
  process.exit(0);
} else {
  console.error(`❌ UI Scan Failed: Found ${errors} violation(s).`);
  process.exit(1);
}
