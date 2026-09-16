import assert from 'node:assert/strict';
import test from 'node:test';

import {
  canEnterManualFacts,
  entersManualFactsForSelf,
  manualFactScope,
} from '../src/manualFactAccess.js';

test('the server decides who may open the manual fact tabs', () => {
  assert.equal(manualFactScope({ role: 'manager', manual_fact_scope: 'branch' }), 'branch');
  assert.equal(manualFactScope({ role: 'barber', manual_fact_scope: 'self' }), 'self');
  assert.equal(manualFactScope({ role: 'barber', manual_fact_scope: 'none' }), 'none');
});

test('a role alone never opens the tabs', () => {
  // An owner whose account owns no editable row still gets what the server said.
  assert.equal(canEnterManualFacts({ role: 'owner', manual_fact_scope: 'none' }), false);
  assert.equal(canEnterManualFacts({ role: 'barber', manual_fact_scope: 'self' }), true);
});

test('an unknown or missing scope keeps the tabs shut', () => {
  assert.equal(manualFactScope({ role: 'barber' }), 'none');
  assert.equal(manualFactScope({ role: 'barber', manual_fact_scope: 'everything' }), 'none');
  assert.equal(manualFactScope(null), 'none');
});

test('an api-key build without a portal user keeps full access', () => {
  assert.equal(manualFactScope(null, { hasApiKey: true }), 'branch');
  assert.equal(canEnterManualFacts(null, { hasApiKey: true }), true);
  assert.equal(entersManualFactsForSelf(null, { hasApiKey: true }), false);
});

test('only a self scope hides the worker filter', () => {
  assert.equal(entersManualFactsForSelf({ manual_fact_scope: 'self' }), true);
  assert.equal(entersManualFactsForSelf({ manual_fact_scope: 'branch' }), false);
  assert.equal(entersManualFactsForSelf({ manual_fact_scope: 'none' }), false);
});
