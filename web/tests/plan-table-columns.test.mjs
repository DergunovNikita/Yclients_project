import assert from 'node:assert/strict';
import test from 'node:test';

import { alignedColumnWidths, unionColumnKeys } from '../src/planTableColumns.js';

test('tables on one plan screen get one width per column key, spare room going to the metric columns', () => {
  const minWidths = new Map([['col0', 128], ['col1', 90], ['revenue', 80], ['visits', 40], ['reviews', 60]]);
  const branch = ['col0', 'col1', 'revenue', 'visits', 'reviews'];
  const staff = ['col0', 'col1', 'revenue', 'visits'];
  const all = unionColumnKeys([branch, staff]);
  assert.deepEqual(all, branch);
  assert.deepEqual(unionColumnKeys([staff, branch]), branch);
  assert.deepEqual(alignedColumnWidths(branch, all, minWidths, 578), [128, 90, 160, 80, 120]);
  // The staff table has no reviews column: its last column covers the gap, every other edge lines up.
  assert.deepEqual(alignedColumnWidths(staff, all, minWidths, 578), [128, 90, 160, 200]);
  // Too narrow: everything at min-content, the box scrolls.
  assert.deepEqual(alignedColumnWidths(branch, all, minWidths, 300), [128, 90, 80, 40, 60]);
});
