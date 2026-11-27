import { testStageThree } from './stage3_lan_sync.test.js';
import { testMilestone1Upgrades } from './milestone1_upgrade.test.js';

import { runSuites } from './run_tests.js';

await runSuites([
  { name: 'Stage 3 LAN sync', fn: testStageThree },
  { name: 'Milestone 0 & 1 upgrades', fn: testMilestone1Upgrades },
]);
