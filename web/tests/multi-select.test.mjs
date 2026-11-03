import assert from 'node:assert/strict';
import test from 'node:test';

import { idListParam, keepListed, parseIdList, selectedValues, setSelectedValues } from '../src/multiSelect.js';

const makeSelect = (entries, multiple = true) => ({
  multiple,
  options: entries.map(([value, selected = false]) => ({ value, selected })),
});

test('a selection is the chosen option values, and the empty placeholder is never one', () => {
  const select = makeSelect([['', true], ['1'], ['2', true], ['3', true]]);
  assert.deepEqual(selectedValues(select), ['2', '3']);
  assert.deepEqual(selectedValues(makeSelect([['1'], ['2']])), []);
  assert.deepEqual(selectedValues(null), []);
});

test('writing a selection chooses exactly those options', () => {
  const select = makeSelect([['1', true], ['2'], ['3']]);
  setSelectedValues(select, ['2', '3', '99']);
  assert.deepEqual(selectedValues(select), ['2', '3']);
  setSelectedValues(select, []);
  assert.deepEqual(selectedValues(select), []);
  // Ids arrive as numbers from the API.
  setSelectedValues(select, [1]);
  assert.deepEqual(selectedValues(select), ['1']);
});

test('a single select takes only the first value', () => {
  const select = makeSelect([['', true], ['1'], ['2']], false);
  setSelectedValues(select, ['2', '1']);
  assert.deepEqual(selectedValues(select), ['2']);
});

test('a stale choice drops out and the rest keeps its order', () => {
  assert.deepEqual(keepListed(['3', '1', '9'], [1, 2, 3]), ['3', '1']);
  assert.deepEqual(keepListed([5], ['5']), ['5']);
  assert.deepEqual(keepListed(['9'], [1]), []);
  assert.deepEqual(keepListed(undefined, [1]), []);
});

test('a list travels as one comma-separated param and parses back', () => {
  assert.equal(idListParam(['1', '2', '1']), '1,2');
  assert.equal(idListParam([]), '');
  assert.deepEqual(parseIdList('1, 2,,2'), ['1', '2']);
  assert.deepEqual(parseIdList(''), []);
  assert.deepEqual(parseIdList(null), []);
});
