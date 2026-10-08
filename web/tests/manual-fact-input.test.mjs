import assert from 'node:assert/strict';
import test from 'node:test';

import { buildManualFactItems, manualFactRowKey, manualFactSaveScope } from '../src/manualFactInput.js';

const rows = [
  { company_id: 1, staff_id: 2, value: 5 },
  { company_id: 1, staff_id: 3, value: null },
];

function raw(entries) {
  return new Map(entries.map(([company, staff, value]) => [manualFactRowKey(company, staff), value]));
}

test('previous_value comes from the last response, not from the cell', () => {
  const built = buildManualFactItems(rows, raw([[1, 2, '7'], [1, 3, '']]));
  assert.deepEqual(built.items, [
    { company_id: 1, staff_id: 2, value: 7, previous_value: 5 },
    { company_id: 1, staff_id: 3, value: null, previous_value: null },
  ]);
});

test('an untouched row posts value equal to previous_value', () => {
  // This is what lets the server leave a row alone that someone else changed meanwhile.
  const built = buildManualFactItems(rows, raw([[1, 2, '5'], [1, 3, '']]));
  for (const item of built.items) assert.equal(item.value, item.previous_value);
});

test('an empty cell is null, not zero, and a typed zero stays zero', () => {
  const built = buildManualFactItems(rows, raw([[1, 2, ' '], [1, 3, '0']]));
  assert.equal(built.items[0].value, null);
  assert.equal(built.items[1].value, 0);
  assert.equal(built.items[1].previous_value, null);
});

test('a comma is a decimal separator', () => {
  const built = buildManualFactItems(rows, raw([[1, 2, '2,5'], [1, 3, '']]));
  assert.equal(built.items[0].value, 2.5);
});

test('a negative or non-numeric cell stops the whole payload', () => {
  assert.equal(buildManualFactItems(rows, raw([[1, 2, '-1'], [1, 3, '']])).error, 'invalid');
  const built = buildManualFactItems(rows, raw([[1, 2, '5'], [1, 3, 'abc']]));
  assert.equal(built.error, 'invalid');
  assert.equal(built.row.staff_id, 3);
});

test('a row without a rendered cell is not posted', () => {
  const built = buildManualFactItems(rows, raw([[1, 2, '5']]));
  assert.deepEqual(built.items.map((item) => item.staff_id), [2]);
});

test('the save goes to the scope the rows were loaded for, not to the picker', () => {
  // The picker may have moved on while the reload is in flight or has failed; the rows and
  // their previous_value still belong to the loaded month.
  const loaded = { month: '2025-01', branch: '1', staff: '' };
  const live = { month: '2025-02', branch: '2', staff: '5' };
  assert.deepEqual(manualFactSaveScope(loaded, live), { month: '2025-01', company_id: 1, staff_id: null });
  assert.deepEqual(manualFactSaveScope(null, live), { month: '2025-02', company_id: 2, staff_id: 5 });
});
