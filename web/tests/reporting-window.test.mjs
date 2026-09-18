import test from 'node:test';
import assert from 'node:assert/strict';

import { branchesForPeriod } from '../src/reportingWindow.js';

const OPEN = { id: 1, title: 'Always ours', reporting_start_date: null, reporting_end_date: null };
const LEFT = { id: 2, title: 'Left', reporting_start_date: '2022-09-10', reporting_end_date: '2026-08-31' };
const YOUNG = { id: 3, title: 'Opened later', reporting_start_date: '2025-03-02', reporting_end_date: null };
const ALL = [OPEN, LEFT, YOUNG];

const ids = (start, end) => branchesForPeriod(ALL, start, end).map((branch) => branch.id);

test('a departed branch is gone from a period after it left', () => {
  assert.deepEqual(ids('2026-09-01', '2026-09-18'), [1, 3]);
});

test('a departed branch is still offered for the months it was ours', () => {
  assert.deepEqual(ids('2026-08-01', '2026-08-31'), [1, 2, 3]);
});

test('the closing day itself still counts — the bound is inclusive', () => {
  assert.deepEqual(ids('2026-08-31', '2026-08-31'), [1, 2, 3]);
  assert.deepEqual(ids('2026-09-01', '2026-09-01'), [1, 3]);
});

test('a period straddling the handover keeps the branch: part of it is still history', () => {
  assert.deepEqual(ids('2026-08-01', '2026-09-30'), [1, 2, 3]);
});

test('a branch that had not opened yet is not offered either', () => {
  assert.deepEqual(ids('2024-01-01', '2024-12-31'), [1, 2]);
});

test('an empty period filters nothing — the page has not picked dates yet', () => {
  assert.deepEqual(ids('', ''), [1, 2, 3]);
});
