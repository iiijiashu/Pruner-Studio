/**
 * Pruner Studio Frontend Tests - Rule Engine & Simulator
 */
import assert from 'node:assert/strict';
import { RuleEngine } from '../../web/js/rules.js';

export async function runRulesTests() {
  console.log('Testing rules.js...');

  const mockSong1 = {
    id: 101,
    title: 'Intro (Live at Arena)',
    artists: ['Rock Band', 'Guest Artist'],
    album: 'Live Album 2024',
    duration: 45,
    availability: 'available',
    liked: true,
  };

  const mockSong2 = {
    id: 102,
    title: 'Full Symphonic Anthem',
    artists: ['Classical Orchestra'],
    album: 'Symphony No. 5',
    duration: 320,
    availability: 'unavailable',
    liked: true,
  };

  const mockSong3 = {
    id: 103,
    title: 'VIP Exclusive Single',
    artists: ['Pop Star'],
    album: 'Pop Gold',
    duration: 190,
    availability: 'vip',
    liked: false,
  };

  // 1. Clause Evaluations
  assert.equal(RuleEngine.evaluateClause(mockSong1, { field: 'duration', operator: 'lt', value: 60 }), true);
  assert.equal(RuleEngine.evaluateClause(mockSong2, { field: 'duration', operator: 'lt', value: 60 }), false);
  assert.equal(RuleEngine.evaluateClause(mockSong2, { field: 'duration', operator: 'gt', value: 300 }), true);
  assert.equal(RuleEngine.evaluateClause({ ...mockSong1, duration: 45000 }, { field: 'duration', operator: 'lt', value: 60 }), true);

  assert.equal(RuleEngine.evaluateClause(mockSong1, { field: 'title', operator: 'contains', value: 'live' }), true);
  assert.equal(RuleEngine.evaluateClause(mockSong1, { field: 'title', operator: 'regex', value: '\\(live' }), true);
  assert.equal(RuleEngine.evaluateClause(mockSong1, { field: 'artist', operator: 'contains', value: 'guest' }), true);

  assert.equal(RuleEngine.evaluateClause(mockSong2, { field: 'availability', operator: 'equals', value: 'unavailable' }), true);
  assert.equal(RuleEngine.evaluateClause(mockSong3, { field: 'availability', operator: 'equals', value: 'vip' }), true);

  // 2. Rule Evaluation
  const shortLiveRule = {
    id: 'r1',
    name: 'Short Live Songs',
    enabled: true,
    match_clauses: [
      { field: 'duration', operator: 'lt', value: 60 },
      { field: 'title', operator: 'contains', value: 'live' }
    ]
  };

  assert.equal(RuleEngine.evaluateRule(mockSong1, shortLiveRule), true);
  assert.equal(RuleEngine.evaluateRule(mockSong2, shortLiveRule), false);

  // Disabled rule should return false
  const disabledRule = { ...shortLiveRule, enabled: false };
  assert.equal(RuleEngine.evaluateRule(mockSong1, disabledRule), false);

  // 3. Simulation across full catalog
  const catalog = [mockSong1, mockSong2, mockSong3];
  const rules = [
    shortLiveRule,
    {
      id: 'r2',
      name: 'Unavailable Songs',
      enabled: true,
      match_clauses: [{ field: 'availability', operator: 'equals', value: 'unavailable' }]
    }
  ];

  const sim = RuleEngine.runSimulation(catalog, rules);
  assert.equal(sim.totalCount, 2, 'Should match song 1 and song 2');
  assert.equal(sim.ruleMatches.get('r1').length, 1);
  assert.equal(sim.ruleMatches.get('r1')[0].id, 101);
  assert.equal(sim.ruleMatches.get('r2').length, 1);
  assert.equal(sim.ruleMatches.get('r2')[0].id, 102);

  console.log('✓ rules.js tests passed!');
}
