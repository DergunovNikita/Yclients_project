import assert from 'node:assert/strict';
import test from 'node:test';

import {
  canPickLevel,
  chosenLevels,
  levelIdsParam,
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

test('a chosen level that the list no longer offers drops out of the selection', () => {
  const options = [
    { key: 'топбарбер', value: '7,12', label: 'ТОП-барбер' },
    { key: 'барбер', value: '3', label: 'Барбер' },
  ];
  assert.deepEqual(levelSelection(options, [{ value: '7,12' }]), ['7,12']);
  assert.deepEqual(levelSelection(options, [{ value: '9' }]), []);
  assert.deepEqual(levelSelection(options, [{ key: 'бренд', value: '7,12' }]), []);
  // The levels that are still listed stay chosen, in option order.
  assert.deepEqual(
    levelSelection(options, [{ key: 'барбер', value: '3' }, { key: 'бренд', value: '5' }, { key: 'топбарбер', value: '7,12' }]),
    ['7,12', '3'],
  );
  assert.deepEqual(levelSelection(options, [{ value: '' }]), []);
  assert.deepEqual(levelSelection(options, [{}]), []);
  assert.deepEqual(levelSelection(options, []), []);
  assert.deepEqual(levelSelection(options, undefined), []);
});

test('the levels chosen in the network survive a narrower scope by their keys, not by their ids', () => {
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
  assert.deepEqual(levelSelection(oneBranch, [{ key: chosen.key, value: chosen.value }]), ['443000']);
  // A branch without that level falls back to all levels.
  assert.deepEqual(levelSelection(levelOptions([barber(3, 100, 'барбер', 'Барбер'), barber(4, 101, 'барбер', 'Барбер')]),
    [{ key: chosen.key, value: chosen.value }]), []);
});

test('a link carries only ids: they map to the levels they cover, else to the one sharing most of them', () => {
  const options = levelOptions([
    barber(1, 1, 'a', 'A'), barber(2, 2, 'a', 'A'), barber(3, 3, 'b', 'B'), barber(4, 4, 'c', 'C'),
  ]);
  assert.deepEqual(levelSelection(options, [{ value: '2,1' }]), ['1,2']);
  assert.deepEqual(levelSelection(options, [{ value: '2' }]), ['1,2']);
  assert.deepEqual(levelSelection(options, [{ value: '3,99' }]), ['3']);
  assert.deepEqual(levelSelection(options, [{ value: '99' }]), []);
  assert.deepEqual(levelSelection(options, [{ value: 'abc' }]), []);
  // Several levels in one link: the union of their ids selects each of them.
  assert.deepEqual(levelSelection(options, [{ value: '1,2,4' }]), ['1,2', '4']);
});

const option = (value, selected, key = '') => ({ value, selected, dataset: { key } });

test('chosenLevels reads the key off every selected option and skips the rest', () => {
  const select = { options: [option('7', true, 'топбарбер'), option('3', false, 'барбер'), option('9', true, 'премиум')] };
  assert.deepEqual(chosenLevels(select), [{ value: '7', key: 'топбарбер' }, { value: '9', key: 'премиум' }]);
  assert.deepEqual(chosenLevels({ options: [option('7', false, 'топбарбер')] }), []);
  assert.deepEqual(chosenLevels(null), []);
});

test('chosen levels ask for the union of their id sets, sorted numerically', () => {
  assert.equal(levelIdsParam(['100,9', '12', '9']), '9,12,100');
  assert.equal(levelIdsParam(['7,12']), '7,12');
  assert.equal(levelIdsParam([]), '');
  assert.equal(levelIdsParam(undefined), '');
});

test('chosen people win over levels in the request', () => {
  const select = (...values) => ({ options: values.map((value) => option(value, true)) });
  const none = { options: [option('5', false)] };
  assert.equal(levelRequestParam({ staff: none, level: select('7,12') }), '7,12');
  assert.equal(levelRequestParam({ staff: none, level: select('7,12', '3') }), '3,7,12');
  assert.equal(levelRequestParam({ staff: select('5'), level: select('7,12') }), '');
  assert.equal(levelRequestParam({ staff: select('5', '6'), level: select('7,12') }), '');
  assert.equal(levelRequestParam({ staff: none, level: none }), '');
  assert.equal(levelRequestParam({ staff: none }), '');
});

test('under a level filter an Overview card links only to a report that filters by level, and carries it', () => {
  assert.deepEqual(levelReportParams('new_vs_returning', null), {});
  assert.deepEqual(levelReportParams('new_vs_returning', undefined), {});
  assert.equal(levelReportParams('new_vs_returning', { position_ids: [7, 12] }), null);
  assert.deepEqual(levelReportParams('staff_leaderboard', { position_ids: [12, 7] }), { staff_position_ids: '12,7' });
  assert.equal(levelReportParams('staff_leaderboard', { position_ids: [] }), null);
});

test('the barber role is offered no level: the server would ignore it; the administrator picks one like a manager', () => {
  const items = [barber(1, 1, 'a', 'A'), barber(2, 2, 'b', 'B')];
  assert.equal(canPickLevel({ role: 'barber' }), false);
  assert.deepEqual(selectableLevelOptions(items, { role: 'barber' }), []);
  ['platform_admin', 'owner', 'branch_admin', 'manager', 'admin', undefined].forEach((role) => {
    assert.equal(canPickLevel({ role }), true);
    assert.equal(selectableLevelOptions(items, { role }).length, 2);
  });
  // An API-key session has no user and sees everything.
  assert.equal(selectableLevelOptions(items, null).length, 2);
});
