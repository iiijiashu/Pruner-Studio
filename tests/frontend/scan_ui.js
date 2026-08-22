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

if (errors === 0) {
  console.log('✓ UI Scan Passed: Zero emojis, zero forbidden English nav labels, and all required deck controls found in web/**.');
  process.exit(0);
} else {
  console.error(`❌ UI Scan Failed: Found ${errors} violation(s).`);
  process.exit(1);
}
