import assert from 'node:assert/strict';
import test from 'node:test';

import { canViewBranchPayments } from '../src/branchPaymentsAccess.js';

test('only an explicit true from the server opens the Yandex Pay tab', () => {
  assert.equal(canViewBranchPayments({ role: 'owner', branch_payments_access: true }), true);
  assert.equal(canViewBranchPayments({ role: 'owner', branch_payments_access: false }), false);
});

test('a role alone never opens the tab', () => {
  assert.equal(canViewBranchPayments({ role: 'platform_admin' }), false);
  assert.equal(canViewBranchPayments({ role: 'manager', manual_fact_scope: 'branch' }), false);
});

test('a missing or loosely truthy flag keeps the tab hidden', () => {
  assert.equal(canViewBranchPayments({ branch_payments_access: undefined }), false);
  assert.equal(canViewBranchPayments({ branch_payments_access: null }), false);
  assert.equal(canViewBranchPayments({ branch_payments_access: 'true' }), false);
  assert.equal(canViewBranchPayments({ branch_payments_access: 1 }), false);
});

test('an api-key build without a portal user keeps full access', () => {
  assert.equal(canViewBranchPayments(null), false);
  assert.equal(canViewBranchPayments(null, { hasApiKey: true }), true);
  assert.equal(canViewBranchPayments({ branch_payments_access: false }, { hasApiKey: true }), false);
});
