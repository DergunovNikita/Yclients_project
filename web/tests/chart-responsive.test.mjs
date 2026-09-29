// Registers the plugin as a side effect, same as charts.js/main.js do — so this file also
// stands in for "does importing it twice (as both of those do) blow up", since Node's ESM
// cache means the module body only actually runs once.
import test from 'node:test';
import assert from 'node:assert/strict';

import { stubCanvas } from './stub-canvas.mjs';

const { NARROW_CHART_WIDTH, narrowChartOverrides } = await import('../src/chartResponsive.js');
const { Chart, BarController, LineController, DoughnutController, BarElement, LineElement, PointElement,
  ArcElement, CategoryScale, LinearScale, Legend, Tooltip } = await import('chart.js');

Chart.register(
  BarController, LineController, DoughnutController,
  BarElement, LineElement, PointElement, ArcElement,
  CategoryScale, LinearScale, Legend, Tooltip,
);

test('narrowChartOverrides is a pure width -> decision function', () => {
  assert.equal(narrowChartOverrides(NARROW_CHART_WIDTH), null, 'the threshold itself is wide');
  assert.equal(narrowChartOverrides(NARROW_CHART_WIDTH + 1), null);
  assert.equal(narrowChartOverrides(1280), null);
  assert.equal(narrowChartOverrides(0), null, 'a not-yet-measured canvas must not read as narrow');
  assert.equal(narrowChartOverrides(-10), null);
  assert.equal(narrowChartOverrides(NaN), null);
  assert.equal(narrowChartOverrides(undefined), null);

  const narrow = narrowChartOverrides(NARROW_CHART_WIDTH - 1);
  assert.equal(narrow.ticks.maxRotation, 0);
  assert.equal(narrow.ticks.autoSkip, true);
  assert.ok(narrow.legend.boxWidth < 40, 'legend boxes should shrink below the Chart.js default of 40');

  const phone = narrowChartOverrides(321);
  assert.deepEqual(phone, narrow, 'the decision only depends on width, not on how narrow');
});

/** A line chart shaped like renderRevenueChart in main.js: only `scales.y` in its own config. */
function buildDateLineChart(width) {
  const canvas = stubCanvas({ width, height: 260 });
  return new Chart(canvas, {
    type: 'line',
    data: {
      labels: ['2026-06-01', '2026-06-02', '2026-06-03'],
      datasets: [{ label: 'Выручка', data: [100, 200, 150] }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: { y: { beginAtZero: true, ticks: { callback: (v) => v } } },
    },
  });
}

test('a narrow canvas gets a flat, auto-skipped x axis and a smaller legend', () => {
  const chart = buildDateLineChart(321);
  try {
    // The chart's own config never mentions scales.x at all — it only exists via the
    // line controller's defaults — so finding it here proves the plugin discovers scales
    // a chart never configured itself, not just the ones already sitting in its options.
    assert.equal(chart.options.scales.x.ticks.maxRotation, 0);
    assert.equal(chart.options.scales.x.ticks.minRotation, 0);
    assert.equal(chart.options.scales.x.ticks.autoSkip, true);
    assert.equal(chart.options.scales.x.ticks.autoSkipPadding, 12);
    // The y axis's own formatter must survive untouched.
    assert.equal(typeof chart.options.scales.y.ticks.callback, 'function');
    assert.equal(chart.options.plugins.legend.labels.boxWidth, 10);
    assert.equal(chart.options.plugins.legend.labels.padding, 8);
    assert.equal(chart.options.plugins.legend.labels.font.size, 10);
  } finally {
    chart.destroy();
  }
});

test('a wide canvas keeps Chart.js defaults untouched (desktop stays desktop)', () => {
  const chart = buildDateLineChart(1146);
  try {
    assert.equal(chart.options.scales.x.ticks.maxRotation, 50, 'Chart.js default category rotation');
    assert.equal(chart.options.scales.x.ticks.autoSkipPadding, 3, 'Chart.js default autoSkipPadding');
    assert.equal(chart.options.plugins.legend.labels.boxWidth, 40, 'Chart.js default legend boxWidth');
    assert.equal(chart.options.plugins.legend.labels.padding, 10, 'Chart.js default legend padding');
  } finally {
    chart.destroy();
  }
});

test('resizing the same instance follows, with no sticky state either way', () => {
  const chart = buildDateLineChart(321);
  try {
    assert.equal(chart.options.scales.x.ticks.maxRotation, 0, 'starts narrow');

    chart.resize(1280, 260);
    assert.equal(chart.options.scales.x.ticks.maxRotation, 50, 'widening restores the default');
    assert.equal(chart.options.plugins.legend.labels.boxWidth, 40);

    chart.resize(360, 812);
    assert.equal(chart.options.scales.x.ticks.maxRotation, 0, 'narrowing again re-applies the override');
    assert.equal(chart.options.plugins.legend.labels.boxWidth, 10);

    chart.resize(1280, 900);
    assert.equal(chart.options.scales.x.ticks.maxRotation, 50, 'and widening again restores it a second time');
  } finally {
    chart.destroy();
  }
});

test('a chart that set its own tick options on purpose gets those back, not the library default', () => {
  const canvas = stubCanvas({ width: 321, height: 260 });
  const chart = new Chart(canvas, {
    type: 'bar',
    data: { labels: ['a', 'b'], datasets: [{ label: 'x', data: [1, 2] }] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: { x: { ticks: { autoSkipPadding: 7 } }, y: { beginAtZero: true } },
    },
  });
  try {
    assert.equal(chart.options.scales.x.ticks.autoSkipPadding, 12, 'narrow overrides it same as any other chart');
    chart.resize(1280, 260);
    assert.equal(
      chart.options.scales.x.ticks.autoSkipPadding,
      7,
      'restoring must bring back this chart\'s own value, not autoSkipPadding: 3',
    );
  } finally {
    chart.destroy();
  }
});

test('a horizontal bar chart with its legend deliberately off is not broken', () => {
  // Shaped like renderServicesChart: indexAxis 'y', legend.display explicitly false.
  const canvas = stubCanvas({ width: 300, height: 260 });
  const chart = new Chart(canvas, {
    type: 'bar',
    data: {
      labels: ['Уход голова, шт', 'Камуфляж, шт'],
      datasets: [{ label: 'Выручка', data: [500, 300] }],
    },
    options: {
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      scales: { x: { beginAtZero: true } },
      plugins: { legend: { display: false } },
    },
  });
  try {
    // The plugin must never flip `display` back on for a chart that turned its legend off.
    assert.equal(chart.options.plugins.legend.display, false);
    chart.resize(1280, 260);
    assert.equal(chart.options.plugins.legend.display, false, 'still off after widening');
  } finally {
    chart.destroy();
  }
});

test('a doughnut chart (no scales at all) still gets a smaller legend and does not crash', () => {
  const canvas = stubCanvas({ width: 280, height: 260 });
  const chart = new Chart(canvas, {
    type: 'doughnut',
    data: { labels: ['Новые', 'Повторные'], datasets: [{ label: 'Клиенты', data: [40, 60] }] },
    options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: 'bottom' } } },
  });
  try {
    assert.equal(chart.options.plugins.legend.labels.boxWidth, 10);
    assert.equal(chart.options.plugins.legend.position, 'bottom', 'unrelated legend options are untouched');
    chart.resize(900, 260);
    assert.equal(chart.options.plugins.legend.labels.boxWidth, 40);
  } finally {
    chart.destroy();
  }
});
