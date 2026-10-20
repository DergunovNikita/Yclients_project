import assert from 'node:assert/strict';
import test from 'node:test';

import {
  canPickLevel,
  chosenLevel,
  levelOptions,
  levelReportParams,
  levelRequestParam,
  levelSelection,
  selectableLevelOptions,
} from '../src/staffLevels.js';

const barber = (id, positionId, key, title, category = 'barber') => ({
  id,
  category,
  position_id: positionId,
  level: key ? { key, title } : null,
});

test('two position ids of one level become a single option with sorted, unique ids', () => {
  const options = levelOptions([
    barber(1, 12, 'топбарбер', 'ТОП-барбер'),
    barber(2, 7, 'топбарбер', 'ТОП-барбер'),
    barber(3, 12, 'топбарбер', 'ТОП-барбер'),
  ]);
  assert.deepEqual(options, [{ key: 'топбарбер', value: '7,12', label: 'ТОП-барбер' }]);
});

test('ids sort numerically, not as text', () => {
  const options = levelOptions([barber(1, 100, 'a', 'A'), barber(2, 9, 'a', 'A')]);
  assert.equal(options[0].value, '9,100');
});

test('the label is the most common title of the group; a tie goes to the alphabetical first', () => {
  const majority = levelOptions([
    barber(1, 1, 'топбарбер', 'ТОП Барбер'),
    barber(2, 2, 'топбарбер', 'ТОП-барбер'),
    barber(3, 2, 'топбарбер', 'ТОП-барбер'),
  ]);
  assert.equal(majority[0].label, 'ТОП-барбер');
  const tie = levelOptions([barber(1, 1, 'топбарбер', 'ТОП-барбер'), barber(2, 2, 'топбарбер', 'ТОП Барбер')]);
  assert.equal(tie[0].label, 'ТОП Барбер');
});

test('items without a level or a position id, and administrators, are ignored', () => {
  const options = levelOptions([
    barber(1, 5, null, null),
    barber(2, null, 'топбарбер', 'ТОП-барбер'),
    barber(3, 6, 'премиумбарбер', 'ПРЕМИУМ-барбер', 'administrator'),
    barber(4, 7, 'премиумбарбер', 'ПРЕМИУМ-барбер', 'mixed'),
    { id: 5, category: null, position_id: 8, level: { key: 'бренд', title: 'БРЕНД' } },
  ]);
  assert.deepEqual(options, [
    { key: 'бренд', value: '8', label: 'БРЕНД' },
    { key: 'премиумбарбер', value: '7', label: 'ПРЕМИУМ-барбер' },
  ]);
  assert.deepEqual(levelOptions([]), []);
  assert.deepEqual(levelOptions(undefined), []);
});

test('options are sorted by label, whatever the order of the list', () => {
  const items = [
    barber(1, 3, 'премиумбарбер', 'ПРЕМИУМ-барбер'),
    barber(2, 1, 'барбер', 'Барбер'),
    barber(3, 2, 'топбарбер', 'ТОП-барбер'),
  ];
  const labels = (list) => levelOptions(list).map((option) => option.label);
  assert.deepEqual(labels(items), ['Барбер', 'ПРЕМИУМ-барбер', 'ТОП-барбер']);
  assert.deepEqual(labels([...items].reverse()), labels(items));
});

test('a list of one person offers no level to choose', () => {
  assert.deepEqual(selectableLevelOptions([barber(1, 1, 'топбарбер', 'ТОП-барбер')]), []);
  assert.equal(selectableLevelOptions([barber(1, 1, 'a', 'A'), barber(2, 2, 'b', 'B')]).length, 2);
});

test('a chosen level that the list no longer offers falls back to all levels', () => {
  const options = [{ key: 'топбарбер', value: '7,12', label: 'ТОП-барбер' }];
  assert.equal(levelSelection(options, { value: '7,12' }), '7,12');
  assert.equal(levelSelection(options, { value: '9' }), '');
  assert.equal(levelSelection(options, { key: 'бренд', value: '7,12' }), '');
  assert.equal(levelSelection(options, { value: '' }), '');
  assert.equal(levelSelection(options, {}), '');
  assert.equal(levelSelection(options, undefined), '');
});

test('the level chosen in the network survives a narrower scope by its key, not by its ids', () => {
  const network = levelOptions([
    barber(1, 141895, 'топбарбер', 'ТОП-барбер'),
    barber(2, 443000, 'топбарбер', 'ТОП-барбер'),
    barber(3, 100, 'барбер', 'Барбер'),
  ]);
  const chosen = network.find((option) => option.key === 'топбарбер');
  assert.equal(chosen.value, '141895,443000');

  const oneBranch = levelOptions([
    barber(1, 443000, 'топбарбер', 'ТОП-барбер'),
    barber(3, 100, 'барбер', 'Барбер'),
  ]);
  // The ids differ in the branch; the selection follows the key and so sends the branch's own id set.
  assert.equal(levelSelection(oneBranch, { key: chosen.key, value: chosen.value }), '443000');
  // A branch without that level falls back to all levels.
  assert.equal(levelSelection(levelOptions([barber(3, 100, 'барбер', 'Барбер'), barber(4, 101, 'барбер', 'Барбер')]),
    { key: chosen.key, value: chosen.value }), '');
});

test('a link carries only ids: they map to the option with the same ids, else the one sharing most of them', () => {
  const options = levelOptions([
    barber(1, 1, 'a', 'A'), barber(2, 2, 'a', 'A'), barber(3, 3, 'b', 'B'), barber(4, 4, 'c', 'C'),
  ]);
  assert.equal(levelSelection(options, { value: '2,1' }), '1,2');
  assert.equal(levelSelection(options, { value: '2' }), '1,2');
  assert.equal(levelSelection(options, { value: '3,99' }), '3');
  assert.equal(levelSelection(options, { value: '99' }), '');
  assert.equal(levelSelection(options, { value: 'abc' }), '');
});

test('chosenLevel reads the key off the selected option', () => {
  const select = { value: '7', selectedOptions: [{ dataset: { key: 'топбарбер' } }] };
  assert.deepEqual(chosenLevel(select), { value: '7', key: 'топбарбер' });
  assert.deepEqual(chosenLevel({ value: '', selectedOptions: [{ dataset: { key: '' } }] }), { value: '', key: '' });
  assert.deepEqual(chosenLevel({ value: '', selectedOptions: [] }), { value: '', key: '' });
  assert.deepEqual(chosenLevel(null), { value: '', key: '' });
});

test('the level reaches only the plan/fact and Overview bundle requests, and only through levelRequestParam', async () => {
  const { readFileSync } = await import('node:fs');
  const main = readFileSync(new URL('../src/main.js', import.meta.url), 'utf8');
  const filterParams = main.match(/function filterParams\(filter\) \{[\s\S]*?\n\}/)[0];
  assert.doesNotMatch(filterParams, /staff_position_ids/);
  assert.match(main, /'\/dashboard\/widget\/plan_fact', \{\s*\.\.\.filterParams\(filter\),\s*staff_position_ids: levelRequestParam\(filter\)/);
  assert.match(main, /'\/dashboard\/bundle', \{\s*\.\.\.filterParams\(filter\),\s*staff_position_ids: levelRequestParam\(filter\)/);
  assert.equal(main.match(/staff_position_ids:/g).length, 2);
});

test('a chosen person wins over a level in the request', () => {
  assert.equal(levelRequestParam({ staff: { value: '' }, level: { value: '7,12' } }), '7,12');
  assert.equal(levelRequestParam({ staff: { value: '5' }, level: { value: '7,12' } }), '');
  assert.equal(levelRequestParam({ staff: { value: '' }, level: { value: '' } }), '');
  assert.equal(levelRequestParam({ staff: { value: '' } }), '');
});

test('under a level filter an Overview card links only to a report that filters by level, and carries it', () => {
  assert.deepEqual(levelReportParams('new_vs_returning', null), {});
  assert.deepEqual(levelReportParams('new_vs_returning', undefined), {});
  assert.equal(levelReportParams('new_vs_returning', { position_ids: [7, 12] }), null);
  assert.deepEqual(levelReportParams('staff_leaderboard', { position_ids: [12, 7] }), { staff_position_ids: '12,7' });
  assert.equal(levelReportParams('staff_leaderboard', { position_ids: [] }), null);
});

test('a personal role is offered no level: the server would ignore it', () => {
  const items = [barber(1, 1, 'a', 'A'), barber(2, 2, 'b', 'B')];
  ['admin', 'barber'].forEach((role) => {
    assert.equal(canPickLevel({ role }), false);
    assert.deepEqual(selectableLevelOptions(items, { role }), []);
  });
  ['platform_admin', 'owner', 'branch_admin', 'manager', undefined].forEach((role) => {
    assert.equal(canPickLevel({ role }), true);
    assert.equal(selectableLevelOptions(items, { role }).length, 2);
  });
  // An API-key session has no user and sees everything.
  assert.equal(selectableLevelOptions(items, null).length, 2);
});
