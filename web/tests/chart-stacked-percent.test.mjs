process.env.TZ = 'Europe/Moscow';

import test from 'node:test';
import assert from 'node:assert/strict';

import { stubCanvas, stubLocaleStorage } from './stub-canvas.mjs';
import { axisMax, tooltipSeriesLabel } from '../src/reports/chartSpec.js';

stubLocaleStorage();

const { ReportChartManager } = await import('../src/reports/charts.js');

const share = (label, data) => ({ label, data, format: 'percent' });

const SHARES = {
  id: 'payment_shares_by_branch',
  type: 'bar',
  stacked: true,
  labels: ['Арбат', 'Тверская', 'Все филиалы'],
  datasets: [
    share('Наличные', [30, 20, 25]),
    share('Безналичные', [50, 55, 52.5]),
    share('Яндекс Пэй', [20, 25, 22.5]),
  ],
};

test('a stacked spec stacks both axes and a plain one leaves them alone', () => {
  const manager = new ReportChartManager();
  const stacked = manager.optionsFor(SHARES, 'bar').scales;
  assert.equal(stacked.x.stacked, true);
  assert.equal(stacked.y.stacked, true);

  const plain = manager.optionsFor({ ...SHARES, stacked: undefined }, 'bar').scales;
  assert.equal(plain.x, undefined);
  assert.equal(plain.y.stacked, undefined);
  // Only a literal true stacks: a stray truthy value from the backend must not.
  assert.equal(manager.optionsFor({ ...SHARES, stacked: 'yes' }, 'bar').scales.x, undefined);
});

test('an axis made only of shares ends at 100', () => {
  assert.equal(axisMax(SHARES), 100);
  assert.equal(new ReportChartManager().optionsFor(SHARES, 'bar').scales.y.max, 100);
  // Rounded shares may add up to a hair over a hundred; that is not a reason to unpin the axis.
  assert.equal(axisMax({ ...SHARES, datasets: [share('a', [33.4]), share('b', [33.4]), share('c', [33.3])] }), 100);
});

test('a stacked axis is not pinned where the data would be clipped', () => {
  assert.equal(axisMax({ type: 'bar', stacked: true, datasets: [share('a', [70]), share('b', [70])] }), undefined);
});

test('a plain percent chart keeps auto-scaling', () => {
  // ОПЗ % by year lives between 5 and 30: pinning it to 100 would flatten the whole series.
  assert.equal(axisMax({ type: 'bar', datasets: [share('ОПЗ %', [8, 21, 30])] }), undefined);
  assert.equal(axisMax({ type: 'line', datasets: [share('2025', [12, 18]), share('2026', [20, 25])] }), undefined);
  assert.equal(axisMax({ type: 'bar', datasets: [share('YoY', [40, 180])] }), undefined);
  assert.equal(new ReportChartManager().optionsFor({ type: 'bar', datasets: [share('ОПЗ %', [8, 21])] }, 'bar').scales.y.max, undefined);
});

test('a negative share lifts the positive part past 100 and frees the axis', () => {
  // Yandex Pay above cashless: the shares still add up to 100, so the positive bars reach 130.
  assert.equal(axisMax({ stacked: true, datasets: [share('a', [80]), share('b', [-30]), share('c', [50])] }), undefined);
  // The negative bar alone does not count towards the height.
  assert.equal(axisMax({ stacked: true, datasets: [share('a', [60]), share('b', [-30]), share('c', [40])] }), 100);
});

test('mixed or non-percent stacked axes keep auto-scaling', () => {
  assert.equal(axisMax({ stacked: true, datasets: [{ label: 'Выручка', data: [1, 2], format: 'money' }] }), undefined);
  assert.equal(axisMax({ stacked: true, datasets: [share('a', [10]), { label: 'b', data: [5], format: 'number' }] }), undefined);
  assert.equal(axisMax({ stacked: true, datasets: [] }), undefined);
});

test('an arc chart names the hovered segment, a bar chart names the series', () => {
  const doughnut = {
    type: 'doughnut',
    labels: ['Наличные', 'Безналичные', 'Яндекс Пэй'],
    datasets: [{ label: 'Доля', data: [30, 50, 20], format: 'percent' }],
  };
  assert.equal(tooltipSeriesLabel(doughnut, 'doughnut', { datasetIndex: 0, dataIndex: 2 }), 'Яндекс Пэй');
  assert.equal(tooltipSeriesLabel(doughnut, 'pie', { datasetIndex: 0, dataIndex: 1 }), 'Безналичные');
  assert.equal(tooltipSeriesLabel(SHARES, 'bar', { datasetIndex: 1, dataIndex: 0 }), 'Безналичные');
  // No label for the segment: the dataset's own name is better than "undefined".
  assert.equal(tooltipSeriesLabel({ ...doughnut, labels: [] }, 'doughnut', { dataIndex: 0 }), 'Доля');
});

test('the tooltip line carries the segment label and the formatted value', () => {
  const manager = new ReportChartManager();
  const doughnut = {
    type: 'doughnut',
    labels: ['Наличные', 'Безналичные'],
    datasets: [{ label: 'Доля', data: [30, 70], format: 'percent' }],
  };
  const { label } = manager.optionsFor(doughnut, 'doughnut').plugins.tooltip.callbacks;
  const line = label({ datasetIndex: 0, dataIndex: 1, parsed: 70, chart: { options: {} } });
  assert.match(line, /Безналичные/);
  assert.match(line, /70/);
  assert.doesNotMatch(line, /Доля/);

  const bar = manager.optionsFor(SHARES, 'bar').plugins.tooltip.callbacks.label({
    datasetIndex: 2,
    dataIndex: 0,
    parsed: { x: 0, y: 20 },
    chart: { options: {} },
  });
  assert.match(bar, /Яндекс Пэй/);
});

test('a stacked spec builds a live chart with stacked scales and a 100 ceiling', () => {
  const manager = new ReportChartManager();
  const { canvas } = stubCanvas();
  manager.render(canvas, SHARES);
  const chart = manager.instances.get(SHARES.id);
  assert.equal(chart.scales.x.options.stacked, true);
  assert.equal(chart.scales.y.options.stacked, true);
  assert.equal(chart.scales.y.max, 100);
  manager.clear();
});
