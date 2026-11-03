import assert from 'node:assert/strict';
import test from 'node:test';

import { defaultPersonalBranch, isMultiBranchAdministrator, personalBranchSelection } from '../src/personalScope.js';

const branches = [{ id: 7 }, { id: 3 }, { id: 9 }];
const row = (companyId, extra = {}) => ({ company_id: companyId, is_active: true, category: 'administrator', ...extra });

test('a personal account on administrator rows in two branches opens on the first of them in branch order', () => {
  assert.equal(defaultPersonalBranch({ role: 'barber' }, [row(9), row(3)], branches), '3');
});

test('one branch of rows leaves «all branches»: the clamp already gives a single row', () => {
  assert.equal(defaultPersonalBranch({ role: 'barber' }, [row(3)], branches), '');
});

test('a barber keeps the group of own rows: it sums their numbers over the branches', () => {
  const barberRows = [row(3, { category: 'barber' }), row(9, { category: 'barber' })];
  assert.equal(defaultPersonalBranch({ role: 'barber' }, barberRows, branches), '');
});

test('the admin role is not personal: it picks its branch itself, whatever rows it has', () => {
  assert.equal(defaultPersonalBranch({ role: 'admin' }, [row(9), row(3)], branches), '');
  assert.equal(isMultiBranchAdministrator({ role: 'admin' }, [row(3), row(9)]), false);
  assert.deepEqual(personalBranchSelection({ role: 'admin' }, [row(3), row(9)], branches, ['3', '9']), ['3', '9']);
});

test('an account labelled barber whose rows are administrators is treated as one', () => {
  assert.equal(defaultPersonalBranch({ role: 'barber' }, [row(9), row(3, { category: 'mixed' })], branches), '3');
});

test('branch roles are never preselected', () => {
  assert.equal(defaultPersonalBranch({ role: 'owner' }, [row(3), row(9)], branches), '');
});

test('fired rows do not make a second branch, and nothing offered means no default', () => {
  assert.equal(defaultPersonalBranch({ role: 'barber' }, [row(3), row(9, { is_active: false })], branches), '');
  assert.equal(defaultPersonalBranch({ role: 'barber' }, [row(3), row(9)], []), '');
});

test('the first branch the view offers wins, not the first row', () => {
  assert.equal(defaultPersonalBranch({ role: 'barber' }, [row(3), row(100)], [{ id: 100 }, { id: 3 }]), '100');
});

test('the branch of the latest past shift wins over branch order', () => {
  const rows = [row(7, { last_shift_date: '2026-10-29' }), row(3, { last_shift_date: '2026-11-02' })];
  assert.equal(defaultPersonalBranch({ role: 'barber' }, rows, branches), '3');
  const reversed = [row(7, { last_shift_date: '2026-11-02' }), row(3, { last_shift_date: '2026-10-29' })];
  assert.equal(defaultPersonalBranch({ role: 'barber' }, reversed, branches), '7');
});

test('a branch with a shift beats one without, equal days keep branch order', () => {
  assert.equal(defaultPersonalBranch({ role: 'barber' }, [row(7), row(9, { last_shift_date: '2026-10-01' })], branches), '9');
  const tied = [row(9, { last_shift_date: '2026-10-01' }), row(3, { last_shift_date: '2026-10-01' })];
  assert.equal(defaultPersonalBranch({ role: 'barber' }, tied, branches), '3');
});

test('only a personal account with living administrator rows in two branches is a multi-branch administrator', () => {
  assert.equal(isMultiBranchAdministrator({ role: 'barber' }, [row(3), row(9)]), true);
  assert.equal(isMultiBranchAdministrator({ role: 'barber' }, [row(3), row(9, { category: 'mixed' })]), true);
  assert.equal(isMultiBranchAdministrator({ role: 'barber' }, [row(3), row(3)]), false);
  assert.equal(isMultiBranchAdministrator({ role: 'barber' }, [row(3), row(9, { is_active: false })]), false);
  assert.equal(isMultiBranchAdministrator({ role: 'barber' }, [row(3, { category: 'barber' }), row(9, { category: 'barber' })]), false);
  assert.equal(isMultiBranchAdministrator({ role: 'owner' }, [row(3), row(9)]), false);
});

test('a multi-branch administrator cannot hold two branches or none: one choice, the default branch for none', () => {
  const rows = [row(7, { last_shift_date: '2026-10-29' }), row(3, { last_shift_date: '2026-11-02' })];
  const admin = { role: 'barber' };
  assert.deepEqual(personalBranchSelection(admin, rows, branches, []), ['3']);
  assert.deepEqual(personalBranchSelection(admin, rows, branches, ['7']), ['7']);
  assert.deepEqual(personalBranchSelection(admin, rows, branches, ['7', '3']), ['7']);
});

test('nothing to default to leaves the selection as it is', () => {
  assert.deepEqual(personalBranchSelection({ role: 'barber' }, [row(3), row(9)], [], []), []);
});

test('everyone else keeps any selection, a group of branches included', () => {
  const barberRows = [row(3, { category: 'barber' }), row(9, { category: 'barber' })];
  assert.deepEqual(personalBranchSelection({ role: 'barber' }, barberRows, branches, []), []);
  assert.deepEqual(personalBranchSelection({ role: 'barber' }, barberRows, branches, ['3', '9']), ['3', '9']);
  assert.deepEqual(personalBranchSelection({ role: 'owner' }, [row(3), row(9)], branches, ['3', '9']), ['3', '9']);
  assert.deepEqual(personalBranchSelection({ role: 'barber' }, [row(3)], branches, []), []);
});
