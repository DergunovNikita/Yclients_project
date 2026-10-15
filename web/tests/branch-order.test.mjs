import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { branchOrderChanged, moveBranch } from '../src/branchOrder.js';

const list = [{ id: 1 }, { id: 2 }, { id: 3 }];
const ids = (items) => items.map((item) => item.id);

test('moveBranch swaps with the neighbour and never mutates its input', () => {
  assert.deepEqual(ids(moveBranch(list, 1, -1)), [2, 1, 3]);
  assert.deepEqual(ids(moveBranch(list, 1, 1)), [1, 3, 2]);
  assert.deepEqual(ids(list), [1, 2, 3]);
});

test('moveBranch ignores moves past either end', () => {
  assert.equal(moveBranch(list, 0, -1), list);
  assert.equal(moveBranch(list, 2, 1), list);
  assert.equal(moveBranch(list, 7, -1), list);
});

test('branchOrderChanged compares positions, not membership', () => {
  assert.equal(branchOrderChanged(list, [...list]), false);
  assert.equal(branchOrderChanged(list, moveBranch(list, 0, 1)), true);
  assert.equal(branchOrderChanged(list, list.slice(1)), true);
});

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');

test('the branch order lives in the settings page, not in the admin page', () => {
  assert.match(read('../settings.html'), /data-panel="branches"/);
  assert.doesNotMatch(read('../admin.html'), /branch-order|branchOrder/);
  assert.doesNotMatch(read('../src/admin.js'), /branch-order|branchOrder/);
});

test('a successful save is announced only after the list was read again', () => {
  const settings = read('../src/settings.js');
  assert.match(settings, /if \(await loadBranchOrder\(\)\) \{\s*els\.branchOrderSuccess\.textContent/);
});
