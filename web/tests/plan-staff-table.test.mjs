import assert from 'node:assert/strict';
import test from 'node:test';

import { mergedStaffTable } from '../src/planStaffTable.js';

const metrics = ['revenue', 'clients', 'wax_qty', 'opz_pct', 'extra_services_qty', 'reviews_qty'].map((code) => ({ code }));
const cell = (code, scope = 'personal') => ({ code, fact: 1, calculation_scope: scope });
const metricSets = {
  barber: metrics.filter((metric) => !['extra_services_qty', 'reviews_qty'].includes(metric.code)),
  administrator: metrics.filter((metric) => metric.code !== 'wax_qty'),
};
const barber = (id) => ({
  id,
  category: 'barber',
  metrics: ['revenue', 'clients', 'wax_qty', 'opz_pct'].map((code) => cell(code)),
});
const admin = (id) => ({
  id,
  category: 'administrator',
  metrics: [
    cell('revenue'),
    cell('clients', 'administrator_records'),
    cell('opz_pct', 'administrator_records'),
    cell('extra_services_qty', 'administrator_records'),
    cell('reviews_qty'),
  ],
});

test('columns are the union of barber and administrator metrics, in payload order', () => {
  const { columns } = mergedStaffTable({ groups: [barber(1), admin(2)], metrics, metricSets });
  assert.deepEqual(
    columns.map((column) => column.code),
    ['revenue', 'clients', 'wax_qty', 'opz_pct', 'extra_services_qty', 'reviews_qty'],
  );
});

test('administrators keep records-scope cells in the shared columns; barber KPIs stay barbers\'', () => {
  const { blocks } = mergedStaffTable({ groups: [barber(1), admin(2)], metrics, metricSets });
  assert.deepEqual(blocks.map((block) => block.category), ['barber', 'administrator']);
  const [barberRow] = blocks[0].groups;
  const [adminRow] = blocks[1].groups;
  assert.deepEqual([...adminRow.cells.keys()], ['revenue', 'clients', 'opz_pct', 'extra_services_qty', 'reviews_qty']);
  assert.equal(adminRow.cells.get('clients').calculation_scope, 'administrator_records');
  // Wax is not an administrator metric; reviews are not a barber one: both render as an empty cell.
  assert.equal(adminRow.cells.has('wax_qty'), false);
  assert.deepEqual([...barberRow.cells.keys()], ['revenue', 'clients', 'wax_qty', 'opz_pct']);
  assert.equal(barberRow.cells.has('reviews_qty'), false);
});

test('hasRecordsCells is set only on a block that shows a records-scope cell', () => {
  const personalAdmin = { id: 5, category: 'administrator', metrics: [cell('revenue')] };
  const withRecords = mergedStaffTable({ groups: [barber(1), admin(2)], metrics, metricSets });
  assert.deepEqual(withRecords.blocks.map((block) => block.hasRecordsCells), [false, true]);
  const without = mergedStaffTable({ groups: [barber(1), personalAdmin], metrics, metricSets });
  assert.deepEqual(without.blocks.map((block) => block.hasRecordsCells), [false, false]);
});

test('a metric hidden for a category by the display filter stays out of its rows', () => {
  const filtered = { ...metricSets, administrator: metricSets.administrator.filter((metric) => metric.code !== 'clients') };
  const visible = metrics.filter((metric) => metric.code !== 'clients');
  const { columns, blocks } = mergedStaffTable({ groups: [admin(2)], metrics: visible, metricSets: filtered });
  assert.equal(blocks[0].groups[0].cells.has('clients'), false);
  assert.equal(columns.some((column) => column.code === 'clients'), false);
});

test('a money cell the server stripped makes no column unless another row has it', () => {
  const stripped = { ...admin(2), metrics: admin(2).metrics.filter((c) => c.code !== 'revenue') };
  const noMoney = { ...barber(1), metrics: barber(1).metrics.filter((c) => c.code !== 'revenue') };
  const { columns } = mergedStaffTable({ groups: [noMoney, stripped], metrics, metricSets });
  assert.equal(columns.some((column) => column.code === 'revenue'), false);
});

test('rows without a category form their own trailing block', () => {
  const { blocks } = mergedStaffTable({
    groups: [{ id: 4, metrics: [cell('revenue')] }, barber(1)],
    metrics,
    metricSets,
  });
  assert.deepEqual(blocks.map((block) => block.category), ['barber', 'unknown']);
});

test('no staff rows give no blocks', () => {
  assert.deepEqual(mergedStaffTable({ groups: [], metrics, metricSets }), { columns: [], blocks: [] });
});
