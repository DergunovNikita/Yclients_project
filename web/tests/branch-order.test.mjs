import assert from 'node:assert/strict';
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
