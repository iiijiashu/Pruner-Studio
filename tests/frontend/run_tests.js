/**
 * Pruner Studio - Frontend Test Runner
 * Executes dependency-free unit tests for all frontend modules.
 */

import { runUtilsTests } from './utils.test.js';
import { runLyricsTests } from './lyrics.test.js';
import { runRulesTests } from './rules.test.js';
import { runStateTests } from './state.test.js';
import { runApiTests } from './api.test.js';
import { runDeckSwipeTests } from './deck_swipe.test.js';
import { runCatalogTests } from './catalog.test.js';
import { runIntegrationContractTests } from './integration_contract.test.js';
import { runRegressionFixesTests } from './regression_fixes.test.js';

async function main() {
  console.log('====================================================');
  console.log('  Running Pruner Studio Frontend Test Suite (Node.js)');
  console.log('====================================================\n');

  const startTime = Date.now();
  let passed = 0;
  let failed = 0;

  const suites = [
    { name: 'Utils Suite', fn: runUtilsTests },
    { name: 'Lyrics Suite', fn: runLyricsTests },
    { name: 'Rules Suite', fn: runRulesTests },
    { name: 'State Suite', fn: runStateTests },
    { name: 'API Client Suite', fn: runApiTests },
    { name: 'Deck Review & State Distinction Suite', fn: runDeckSwipeTests },
    { name: 'Catalog Shared State & Pagination Suite', fn: runCatalogTests },
    { name: 'Cross-layer Frontend Contract Suite', fn: runIntegrationContractTests },
    { name: 'Phase 2 Regression & Root Cause Fixes Suite', fn: runRegressionFixesTests },
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

main().catch(err => {
  console.error('Test runner fatal error:', err);
  process.exit(1);
});
