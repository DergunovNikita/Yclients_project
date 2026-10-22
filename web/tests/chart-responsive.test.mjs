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
  const { canvas } = stubCanvas({ width, height: 260 });
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
  const expected = narrowChartOverrides(321);
  try {
    // The chart's own config never mentions scales.x at all — it only exists via the
    // line controller's defaults — so finding it here proves the plugin discovers scales
    // a chart never configured itself, not just the ones already sitting in its options.
    const ticks = chart.options.scales.x.ticks;
    assert.equal(ticks.maxRotation, expected.ticks.maxRotation);
    assert.equal(ticks.minRotation, expected.ticks.minRotation);
    assert.equal(ticks.autoSkip, expected.ticks.autoSkip);
    assert.equal(ticks.autoSkipPadding, expected.ticks.autoSkipPadding);
    // The y axis's own formatter must survive untouched.
    assert.equal(typeof chart.options.scales.y.ticks.callback, 'function');
    const labels = chart.options.plugins.legend.labels;
    assert.equal(labels.boxWidth, expected.legend.boxWidth);
    assert.equal(labels.padding, expected.legend.padding);
    assert.equal(labels.font.size, expected.legend.fontSize);
  } finally {
    chart.destroy();
  }
});

test('resizing the same instance follows, with no sticky state either way', () => {
  const wide = buildDateLineChart(1146);
  const wideTicks = wide.options.scales.x.ticks.maxRotation;
  const wideBox = wide.options.plugins.legend.labels.boxWidth;
  wide.destroy();
  const narrow = narrowChartOverrides(321);
  const chart = buildDateLineChart(321);
  try {
    assert.equal(chart.options.scales.x.ticks.maxRotation, narrow.ticks.maxRotation, 'starts narrow');
    assert.notEqual(wideTicks, narrow.ticks.maxRotation);

    chart.resize(1280, 260);
    assert.equal(chart.options.scales.x.ticks.maxRotation, wideTicks, 'widening restores the default');
    assert.equal(chart.options.plugins.legend.labels.boxWidth, wideBox);

    chart.resize(360, 812);
    assert.equal(chart.options.scales.x.ticks.maxRotation, narrow.ticks.maxRotation, 'narrowing again re-applies the override');
    assert.equal(chart.options.plugins.legend.labels.boxWidth, narrow.legend.boxWidth);

    chart.resize(1280, 900);
    assert.equal(chart.options.scales.x.ticks.maxRotation, wideTicks, 'and widening again restores it a second time');
  } finally {
    chart.destroy();
  }
});

test('a chart that set its own tick options on purpose gets those back, not the library default', () => {
  const { canvas } = stubCanvas({ width: 321, height: 260 });
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
    assert.equal(
      chart.options.scales.x.ticks.autoSkipPadding,
      narrowChartOverrides(321).ticks.autoSkipPadding,
      'narrow overrides it same as any other chart',
    );
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
  const { canvas } = stubCanvas({ width: 300, height: 260 });
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
  const { canvas } = stubCanvas({ width: 280, height: 260 });
  const chart = new Chart(canvas, {
    type: 'doughnut',
    data: { labels: ['Новые', 'Повторные'], datasets: [{ label: 'Клиенты', data: [40, 60] }] },
    options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: 'bottom' } } },
  });
  try {
    const narrowBox = narrowChartOverrides(280).legend.boxWidth;
    assert.equal(chart.options.plugins.legend.labels.boxWidth, narrowBox);
    assert.equal(chart.options.plugins.legend.position, 'bottom', 'unrelated legend options are untouched');
    chart.resize(900, 260);
    assert.ok(chart.options.plugins.legend.labels.boxWidth > narrowBox, 'widening restores a larger legend box');
  } finally {
    chart.destroy();
  }
});
