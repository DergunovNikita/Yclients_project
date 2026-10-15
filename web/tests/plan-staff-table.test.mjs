import assert from 'node:assert/strict';
import test from 'node:test';

import { mergedStaffTable } from '../src/planStaffTable.js';

const metrics = ['revenue', 'clients', 'wax_qty', 'opz_pct', 'extra_services_qty'].map((code) => ({ code }));
const cell = (code, scope = 'personal') => ({ code, fact: 1, calculation_scope: scope });
const metricSets = {
  barber: metrics.filter((metric) => metric.code !== 'extra_services_qty'),
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
    cell('opz_pct'),
    cell('extra_services_qty', 'administrator_records'),
  ],
});

test('columns are the union of barber and personal administrator metrics, in payload order', () => {
  const { columns } = mergedStaffTable({ groups: [barber(1), admin(2)], metrics, metricSets });
  assert.deepEqual(columns.map((column) => column.code), ['revenue', 'clients', 'wax_qty', 'opz_pct']);
});

test('cells outside the row category or personal scope are absent, and the other table keeps them', () => {
  const { blocks } = mergedStaffTable({ groups: [barber(1), admin(2)], metrics, metricSets });
  assert.deepEqual(blocks.map((block) => block.category), ['barber', 'administrator']);
  const [barberRow] = blocks[0].groups;
  const [adminRow] = blocks[1].groups;
  assert.deepEqual([...barberRow.cells.keys()], ['revenue', 'clients', 'wax_qty', 'opz_pct']);
  // "clients" is records-based and wax is not an administrator metric: both render as an empty cell.
  assert.deepEqual([...adminRow.cells.keys()], ['revenue', 'opz_pct']);
});

test('a metric hidden for a category by the display filter stays out of its rows', () => {
  const filtered = { ...metricSets, barber: metricSets.barber.filter((metric) => metric.code !== 'clients') };
  const { blocks } = mergedStaffTable({
    groups: [barber(1)],
    metrics: metrics.filter((metric) => metric.code !== 'clients'),
    metricSets: filtered,
  });
  assert.equal(blocks[0].groups[0].cells.has('clients'), false);
});

test('administrators with no personal metric get no block and add no columns', () => {
  const onlyRecords = { id: 3, category: 'administrator', metrics: [cell('clients', 'administrator_records')] };
  const { columns, blocks } = mergedStaffTable({ groups: [barber(1), onlyRecords], metrics, metricSets });
  assert.deepEqual(blocks.map((block) => block.category), ['barber']);
  assert.equal(columns.some((column) => column.code === 'extra_services_qty'), false);
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
