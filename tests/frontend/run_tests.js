import { pathToFileURL } from 'node:url';
import { runStageTwoTests } from './stage2_ux_polish.test.js';
import { runStageOneTests } from './stage1_interactions.test.js';
import { runShipGate } from './ship_gate.test.js';
import { runPhase6B } from './phase6b_discover_and_controls.test.js';
import { runPhase5Fixes } from './phase5_fixes.test.js';
import { runPhase2BTests } from './phase2b.test.js';
/**
 * Pruner Studio - Frontend Test Runner
 * Executes dependency-free unit tests for all frontend modules.
 */

import { runPhase4Tests } from './phase4.test.js';
import { runPhase3Tests } from './phase3.test.js';
import { runPhase1Tests } from './phase1.test.js';
import { runUtilsTests } from './utils.test.js';
import { runLyricsTests } from './lyrics.test.js';
import { runRulesTests } from './rules.test.js';
import { runStateTests } from './state.test.js';
import { runApiTests } from './api.test.js';
import { runDeckSwipeTests } from './deck_swipe.test.js';
import { runCatalogTests } from './catalog.test.js';
import { runIntegrationContractTests } from './integration_contract.test.js';
import { runRegressionFixesTests } from './regression_fixes.test.js';
import { runV03UpgradeTests } from './v03_upgrade.test.js';
import { runWave4ExperienceTests } from './wave4_experience.test.js';
import { runPhase7fAndPhase8Tests } from './phase7f_and_phase8.test.js';
import { runPhase9Tests } from './phase9_lyrics_and_tactile.test.js';
import { runPlaybackResumeFixesTests } from './playback_resume_fixes.test.js';
import { runVisualHygieneTests } from './visual_hygiene.test.js';

export async function runSuites(extraSuites = []) {
  console.log('====================================================');
  console.log('  Running Pruner Studio Frontend Test Suite (Node.js)');
  console.log('====================================================\n');

  const startTime = Date.now();
  let passed = 0;
  let failed = 0;

  const suites = [
    ...extraSuites,
    { name: 'Stage 2 UX polish', fn: runStageTwoTests },
    { name: 'Stage 1 interactions', fn: runStageOneTests },
    { name: "Release account, queue and playlist regressions", fn: runShipGate },
    { name: 'Phase 6B discovery and controls', fn: runPhase6B },
    { name: "Phase 4 accounts, channel status, cursors and updates", fn: runPhase4Tests },
    { name: 'Phase 3 desktop lyrics, compositor and audio mixer', fn: runPhase3Tests },
    { name: 'Utils Suite', fn: runUtilsTests },
    { name: 'Lyrics Suite', fn: runLyricsTests },
    { name: 'Rules Suite', fn: runRulesTests },
    { name: 'State Suite', fn: runStateTests },
    { name: 'API Client Suite', fn: runApiTests },
    { name: 'Deck Review & State Distinction Suite', fn: runDeckSwipeTests },
    { name: 'Catalog Shared State & Pagination Suite', fn: runCatalogTests },
    { name: 'Cross-layer Frontend Contract Suite', fn: runIntegrationContractTests },
    { name: 'Phase 2 Regression & Root Cause Fixes Suite', fn: runRegressionFixesTests },
    { name: 'v0.3 Upgrade Architecture & Safety Suite', fn: runV03UpgradeTests },
    { name: 'Wave 4 Theme, Search, Context Menu & Desktop Lyrics Suite', fn: runWave4ExperienceTests },
    { name: 'Phase 1 write safety and playback races', fn: runPhase1Tests },
    { name: 'Phase 2B playback, history and desktop controls', fn: runPhase2BTests },
    { name: 'Phase 5 session, media retry and progress races', fn: runPhase5Fixes },
    { name: 'Phase 7F Ship-Gate fixes & Phase 8 listening/game-escort experience', fn: runPhase7fAndPhase8Tests },
    { name: 'Phase 9 desktop lyrics overhaul and tactile UI', fn: runPhase9Tests },
    { name: 'Playback resume, AudioContext sync unlock & stream recovery fixes', fn: runPlaybackResumeFixesTests },
    { name: 'Phase 3 light-priority visual hygiene & VAC-01..VAC-08 contracts', fn: runVisualHygieneTests },
  ];

  for (const suite of suites) {
    try {
      await suite.fn();
      passed++;
    } catch (err) {
      console.error(`\n❌ FAILED: ${suite.name}`);
      console.error(err);
      failed++;
    }
  }

  const duration = Date.now() - startTime;
  console.log('\n====================================================');
  console.log(`  Test Summary: ${passed} Passed, ${failed} Failed (${duration}ms)`);
  console.log('====================================================');

  if (failed > 0) {
    process.exit(1);
  }
}

if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) await runSuites();
