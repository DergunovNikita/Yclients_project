process.env.TZ = 'Europe/Moscow';

import test from 'node:test';
import assert from 'node:assert/strict';

import { stubCanvas, stubLocaleStorage } from './stub-canvas.mjs';

stubLocaleStorage();

const { formatDate } = await import('../src/reports/format.js');
const { cellDelta, matchComparisonRows, pairedRows } = await import('../src/reports/comparison.js');
const { withComparisonDatasets, mutedColor } = await import('../src/reports/chartSpec.js');
const { ReportChartManager } = await import('../src/reports/charts.js');
const { renderReportData } = await import('../src/reports/renderers/generic.js');
const { decodePathSegment, migrateFavorites, resolveReportRoute } = await import('../src/reports/routing.js');
const {
  reportComparisonRequest,
  reportDataParams,
  reportFiltersFromParams,
  reportSearchParams,
} = await import('../src/dashboardRequestState.js');

const money = { key: 'revenue', label: 'Выручка', format: 'money' };
const share = { key: 'share', label: 'Доля', format: 'percent' };
const text = { key: 'title', label: 'Услуга', format: 'text' };

function fakeContainer() {
  return { innerHTML: '', querySelectorAll: () => [], querySelector: () => null };
}

const fakeCharts = () => ({ clear() {}, render() {} });

test('rows are matched by row_key, and an unmatched row has no previous', () => {
  const table = { id: 't', row_key: 'title' };
  const previous = { id: 't', rows: [{ title: 'Стрижка', revenue: 100 }, { title: 'Борода', revenue: 50 }] };
  const rows = [{ title: 'Борода', revenue: 60 }, { title: 'Укладка', revenue: 10 }];
  const matched = matchComparisonRows(table, previous)(rows);
  assert.equal(matched[0].revenue, 50);
  assert.equal(matched[1], undefined);
});

test('a table without row_key or without a comparison table is not compared', () => {
  assert.equal(matchComparisonRows({ id: 't' }, { id: 't', rows: [] }), null);
  assert.equal(matchComparisonRows({ id: 't', row_key: 'title' }, undefined), null);
  const [pair] = pairedRows({ id: 't' }, [{ title: 'a' }], { id: 't', rows: [{ title: 'a' }] });
  assert.equal(pair.compared, false);
});

test('time tables are paired by position, since every window has its own period labels', () => {
  const table = { id: 't', row_key: 'period', x_kind: 'time' };
  const previous = { id: 't', rows: [{ period: '2026-07-01', revenue: 100 }, { period: '2026-07-02', revenue: 200 }] };
  const pairs = pairedRows(table, [{ period: '2026-08-01', revenue: 150 }, { period: '2026-08-02', revenue: 100 }], previous);
  assert.deepEqual(pairs.map((pair) => pair.previous.revenue), [100, 200]);
});

test('a time table is paired by position whatever its row_key is called, and only a time table', () => {
  const rows = [{ month: 'Август 2026', clients: 5 }];
  const previous = { id: 't', rows: [{ month: 'Июль 2026', clients: 4 }] };
  const byPosition = pairedRows({ id: 't', row_key: 'month', x_kind: 'time' }, rows, previous);
  assert.equal(byPosition[0].previous.clients, 4);
  // The column name `period` guesses nothing: without x_kind the labels are matched, and these differ.
  const byLabel = pairedRows({ id: 't', row_key: 'period' }, [{ period: 'b' }], { id: 't', rows: [{ period: 'a' }] });
  assert.equal(byLabel[0].previous, undefined);
});

test('money and counts change in percent of the base; a zero or missing base has no percent', () => {
  assert.deepEqual(cellDelta(money, { revenue: 110 }, { revenue: 100 }), { value: 10, format: 'money' });
  assert.deepEqual(cellDelta(money, { revenue: 50 }, { revenue: 100 }), { value: -50, format: 'money' });
  assert.equal(cellDelta(money, { revenue: 50 }, { revenue: 0 }).value, null);
  assert.equal(cellDelta(money, { revenue: 50 }, undefined).value, null);
  assert.equal(cellDelta(money, { revenue: null }, { revenue: 100 }), null);
  assert.equal(cellDelta(text, { title: 'a' }, { title: 'a' }), null);
});

test('a share changes in percentage points, and a zero share is a real base', () => {
  assert.deepEqual(cellDelta(share, { share: 30 }, { share: 20 }), { value: 10, format: 'percent' });
  assert.deepEqual(cellDelta(share, { share: 5 }, { share: 0 }), { value: 5, format: 'percent' });
  assert.equal(cellDelta(share, { share: 5 }, { share: null }).value, null);
});

function reportWithTable(extra = {}) {
  return {
    cards: [],
    charts: [],
    tables: [{
      id: 'services',
      title: 'Услуги',
      row_key: 'title',
      columns: [text, money, share],
      rows: [{ title: 'Стрижка', revenue: 110, share: 30 }, { title: 'Новая', revenue: 5, share: 1 }],
    }],
    comparison: {
      period: { start: '2026-07-01', end: '2026-07-31' },
      source_status: 'ready',
      cards: [],
      rows: [],
      charts: [],
      tables: [{ id: 'services', rows: [{ title: 'Стрижка', revenue: 100, share: 20 }] }],
    },
    ...extra,
  };
}

test('table cells carry a signed delta under the value, with a dash for an unmatched row', () => {
  const container = fakeContainer();
  renderReportData(container, reportWithTable(), fakeCharts());
  const deltas = [...container.innerHTML.matchAll(/reports-cell-delta delta ([a-z]*)"[^>]*>([^<]*)</g)]
    .map((match) => [match[1], match[2]]);
  assert.equal(deltas.length, 4);
  assert.deepEqual(deltas[0], ['up', '+10%']);
  assert.equal(deltas[1][0], 'up');
  assert.match(deltas[1][1], /^\+10/);
  assert.match(deltas[1][1], /pp|п\.п\.|p\.p\./);
  assert.deepEqual(deltas[2], ['', '—']);
  assert.deepEqual(deltas[3], ['', '—']);
});

test('without a comparison no delta is drawn', () => {
  const container = fakeContainer();
  renderReportData(container, reportWithTable({ comparison: undefined }), fakeCharts());
  assert.equal(container.innerHTML.includes('reports-cell-delta'), false);
});

test('the key column never gets a delta', () => {
  const data = reportWithTable();
  data.tables[0].columns = [{ key: 'staff_id', label: 'ID', format: 'number' }, money];
  data.tables[0].row_key = 'staff_id';
  data.tables[0].rows = [{ staff_id: 7, revenue: 110 }];
  data.comparison.tables[0].rows = [{ staff_id: 7, revenue: 100 }];
  const container = fakeContainer();
  renderReportData(container, data, fakeCharts());
  assert.equal((container.innerHTML.match(/reports-cell-delta/g) || []).length, 1);
});

test('the summary table prints its dates, signs the delta and dashes a zero base', () => {
  const data = {
    period: { start: '2026-08-01', end: '2026-08-31' },
    cards: [],
    charts: [],
    tables: [],
    comparison: {
      period: { start: '2026-07-01', end: '2026-07-31' },
      source_status: 'ready',
      cards: [],
      rows: [
        { label: 'Выручка', format: 'money', current: 110, compare: 100, delta: 10, delta_pct: 10 },
        { label: 'Новые', format: 'number', current: 4, compare: 0, delta: 4, delta_pct: null },
        { label: 'ОПЗ', format: 'percent', current: 30, compare: 20, delta: 10, delta_pct: 50 },
      ],
    },
  };
  const container = fakeContainer();
  renderReportData(container, data, fakeCharts());
  const html = container.innerHTML;
  assert.equal(html.includes('2026-07-01'), false, 'raw ISO dates must not be printed');
  assert.ok(html.includes(formatDate('2026-07-01')));
  assert.ok(html.includes(formatDate('2026-08-31')));
  assert.match(html, /delta up">\+10%</);
  assert.match(html, /delta ">—</);
  // a share has no percent of a percent
  assert.equal(html.includes('+50%'), false);
});

test('a comparison that is not ready says so, even when the report has no cards', () => {
  const data = reportWithTable();
  data.comparison.source_status = 'partial';
  const container = fakeContainer();
  renderReportData(container, data, fakeCharts());
  assert.match(container.innerHTML, /reports-note--warning/);
});

const timeSpec = {
  id: 'c', type: 'line', x_kind: 'time', labels: ['d1', 'd2', 'd3'],
  datasets: [{ label: 'Выручка', data: [1, 2, 3], format: 'money' }],
};

test('a time chart aligns the comparison series by index and pads with gaps', () => {
  const merged = withComparisonDatasets(
    timeSpec,
    { id: 'c', labels: ['p1', 'p2'], datasets: [{ label: 'Выручка', data: [10, 20] }] },
    'сравнение',
  );
  assert.equal(merged.datasets.length, 2);
  assert.deepEqual(merged.datasets[1].data, [10, 20, null]);
  assert.equal(merged.datasets[1].label, 'Выручка (сравнение)');
  assert.equal(merged.datasets[1].compare, true);
  assert.equal(merged.datasets[1].format, 'money');
  assert.equal(timeSpec.datasets.length, 1, 'the spec is not mutated');
});

test('a category chart aligns the comparison series by label', () => {
  const spec = { ...timeSpec, type: 'bar', x_kind: 'category', labels: ['A', 'B', 'C'] };
  const merged = withComparisonDatasets(
    spec,
    { id: 'c', labels: ['C', 'A'], datasets: [{ label: 'Выручка', data: [30, 10] }] },
    'сравнение',
  );
  assert.deepEqual(merged.datasets[1].data, [10, null, 30]);
});

test('arcs and stacks get no comparison series', () => {
  const comparison = { id: 'c', labels: ['d1'], datasets: [{ label: 'Выручка', data: [9] }] };
  assert.equal(withComparisonDatasets({ ...timeSpec, type: 'doughnut' }, comparison, 'x').datasets.length, 1);
  assert.equal(withComparisonDatasets({ ...timeSpec, stacked: true }, comparison, 'x').datasets.length, 1);
  assert.equal(withComparisonDatasets(timeSpec, undefined, 'x'), timeSpec);
});

test('muted colours stay parseable', () => {
  assert.equal(mutedColor('#0f766e', 0.5), '#0f766e80');
  assert.equal(mutedColor('hsl(10, 62%, 38%)', 0.5), 'hsla(10, 62%, 38%, 0.5)');
});

test('the manager draws the comparison series dashed in the colour of its original, without data labels', () => {
  const manager = new ReportChartManager();
  const { canvas } = stubCanvas();
  manager.render(
    canvas,
    timeSpec,
    { id: 'c', labels: ['p1', 'p2', 'p3'], datasets: [{ label: 'Выручка', data: [10, 20, 30] }] },
  );
  const [current, previous] = manager.instances.get('c').data.datasets;
  assert.equal(current.borderDash, undefined);
  assert.deepEqual(previous.borderDash, [6, 4]);
  assert.equal(previous.borderColor, `${current.borderColor}8c`);
  assert.equal(previous.reportCompare, true);
  manager.clear();
});

test('an id from the URL resolves to the catalog, an alias, or nothing', () => {
  const reports = [{ id: 'financial_overview', aliases: ['revenue_dynamics'] }, { id: 'goods_dynamics', aliases: [] }];
  assert.deepEqual(resolveReportRoute('goods_dynamics', reports), { id: 'goods_dynamics', renamed: false, unknown: false });
  assert.deepEqual(resolveReportRoute('revenue_dynamics', reports), { id: 'financial_overview', renamed: true, unknown: false });
  assert.deepEqual(resolveReportRoute('gone', reports), { id: '', renamed: false, unknown: true });
  assert.deepEqual(resolveReportRoute('', reports), { id: '', renamed: false, unknown: false });
});

test('a path segment that is not valid percent-encoding does not throw', () => {
  assert.equal(decodePathSegment('%D0%BE%D1%82'), 'от');
  assert.equal(decodePathSegment('100%'), '100%');
});

test('favourites follow aliases, collapse duplicates and keep ids this role does not see', () => {
  const reports = [{ id: 'financial_overview', aliases: ['revenue_dynamics', 'day_overview'] }];
  const migrated = migrateFavorites(['revenue_dynamics', 'day_overview', 'financial_overview', 'hidden_for_me'], reports);
  assert.deepEqual(migrated.favorites, ['financial_overview', 'hidden_for_me']);
  assert.equal(migrated.changed, true);
  assert.equal(migrateFavorites(['financial_overview'], reports).changed, false);
  assert.deepEqual(migrateFavorites('garbage', reports), { favorites: [], changed: false });
});

const filters = {
  start_date: '2026-08-01', end_date: '2026-08-31', company_id: '', staff_id: '', granularity: 'week',
  compare_start_date: '', compare_end_date: '', compare_enabled: true, period_preset: '',
};

test('granularity is sent only to reports that offer it', () => {
  assert.equal(reportDataParams({ reportId: 'r', filters, meta: { granularity: false } }).granularity, undefined);
  assert.equal(reportDataParams({ reportId: 'r', filters, meta: { granularity: true } }).granularity, 'week');
});

test('a ticked box under a preset with no window asks for the preset baseline', () => {
  const preset = { ...filters, period_preset: 'month' };
  const params = reportDataParams({ reportId: 'r', filters: preset, meta: {} });
  assert.equal(params.compare_previous, 'true');
  assert.equal(params.compare_start_date, undefined);
  assert.equal(params.period_preset, 'month');
});

test('a typed window wins over the baseline; no preset or no tick asks for nothing', () => {
  const typed = { ...filters, period_preset: 'month', compare_start_date: '2026-06-01', compare_end_date: '2026-06-30' };
  const params = reportDataParams({ reportId: 'r', filters: typed, meta: {} });
  assert.equal(params.compare_previous, undefined);
  assert.equal(params.compare_start_date, '2026-06-01');
  assert.equal(reportComparisonRequest(filters), null);
  assert.equal(reportComparisonRequest({ ...filters, period_preset: 'month', compare_enabled: false }), null);
  // half a window is a window still being typed
  assert.equal(reportComparisonRequest({ ...typed, compare_end_date: '' }), null);
});

test('a report that cannot compare is sent no comparison, and the box keeps its state', () => {
  const preset = { ...filters, period_preset: 'month' };
  const params = reportDataParams({ reportId: 'r', filters: preset, meta: { compare: false } });
  assert.equal(params.compare_previous, undefined);
  assert.equal(preset.compare_enabled, true);
});

test('the baseline comparison survives a reload: the link carries it and the parser ticks the box again', () => {
  const preset = { ...filters, period_preset: 'month' };
  const link = reportSearchParams(preset);
  assert.equal(link.get('compare_previous'), 'true');
  assert.equal(link.get('compare_start_date'), null);
  const restored = reportFiltersFromParams(new URLSearchParams(link.toString()));
  assert.equal(restored.compare_enabled, true);
  assert.equal(restored.period_preset, 'month');
  assert.deepEqual(reportComparisonRequest(restored), { compare_previous: 'true' });
});

test('a link without a tick, or without a preset, does not claim the baseline comparison', () => {
  assert.equal(reportSearchParams({ ...filters, period_preset: 'month', compare_enabled: false }).has('compare_previous'), false);
  assert.equal(reportSearchParams(filters).has('compare_previous'), false);
  // a hand-edited link naming the baseline without the preset it belongs to asks for nothing
  const stray = reportFiltersFromParams(new URLSearchParams('compare_previous=true&start_date=2026-08-01'));
  assert.equal(stray.compare_enabled, false);
  // a typed window stays the link's comparison, and drops the baseline flag
  const typed = reportSearchParams({
    ...filters, period_preset: 'month', compare_start_date: '2026-06-01', compare_end_date: '2026-06-30',
  });
  assert.equal(typed.has('compare_previous'), false);
  assert.equal(typed.get('compare_start_date'), '2026-06-01');
});

test('the viewer subtitle names a granularity only for reports that have one', async () => {
  const { periodSubtitle } = await import('../src/reports/format.js');
  const data = { period: { start: '2026-08-01', end: '2026-08-31', granularity: 'day' } };
  const plain = periodSubtitle(data, { filters: { granularity: false } });
  const bucketed = periodSubtitle(data, { filters: { granularity: true } });
  assert.doesNotMatch(plain, /·/);
  assert.match(bucketed, /·/);
  assert.ok(bucketed.startsWith(plain));
  assert.doesNotMatch(bucketed, /\bday\b/, 'the id is localised, not printed raw');
  assert.equal(periodSubtitle(null, {}), '');
});
