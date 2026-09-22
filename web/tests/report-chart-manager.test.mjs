// chartSpec.js is covered by pure unit tests next door. This file covers what only a
// live Chart can answer: the manager builds real instances and the data-label toggle
// reaches them. Replacing an object on chart.options used to blow the stack here —
// Chart.js writes a resolved option through to its first scope, which is another
// proxy, which writes through again — and no pure test could see it.
process.env.TZ = 'Europe/Moscow';

import test from 'node:test';
import assert from 'node:assert/strict';

import { stubCanvas, stubLocaleStorage } from './stub-canvas.mjs';

stubLocaleStorage();

const { ReportChartManager } = await import('../src/reports/charts.js');

const SPECS = [
  {
    id: 'bar',
    type: 'bar',
    labels: ['2026-07', '2026-08'],
    datasets: [{ label: 'Выручка', data: [1200, 3400], format: 'money' }],
  },
  {
    // Two axes: the y1 branch of optionsFor() only runs for a spec that asks for it.
    id: 'line',
    type: 'line',
    labels: ['2026-07', '2026-08', '2026-09'],
    datasets: [
      { label: 'Записи', data: [12, 18, 25], format: 'number' },
      { label: 'Средний чек', data: [900, 950, 1010], format: 'money', axis: 'y1' },
    ],
  },
  {
    id: 'doughnut',
    type: 'doughnut',
    labels: ['Новые', 'Повторные'],
    datasets: [{ label: 'Клиенты', data: [40, 60], format: 'number' }],
  },
];

function renderAll() {
  const manager = new ReportChartManager();
  const drawnBySpec = new Map();
  for (const spec of SPECS) {
    const { canvas, drawn } = stubCanvas();
    drawnBySpec.set(spec.id, drawn);
    manager.render(canvas, spec);
  }
  return { manager, drawnBySpec };
}

const pointCount = (spec) => spec.datasets.reduce((total, set) => total + set.data.length, 0);

/**
 * Text the chart paints on one redraw, keyed by spec id.
 *
 * Axis ticks and the legend go through fillText too, so a data label is not recognisable
 * on its own — what the toggle is worth is the difference between a redraw with it and
 * a redraw without.
 */
function textsPerRedraw(manager, drawnBySpec, enabled) {
  for (const drawn of drawnBySpec.values()) drawn.length = 0;
  manager.setDataLabels(enabled);
  return new Map([...drawnBySpec].map(([id, drawn]) => [id, drawn.length]));
}

test('every report chart type builds a live instance', () => {
  const { manager } = renderAll();
  assert.deepEqual([...manager.instances.keys()], ['bar', 'line', 'doughnut']);
  manager.clear();
  assert.equal(manager.instances.size, 0);
});

test('toggling data labels reaches charts that are already on screen', () => {
  const { manager, drawnBySpec } = renderAll();

  const off = textsPerRedraw(manager, drawnBySpec, false);
  const on = textsPerRedraw(manager, drawnBySpec, true);

  assert.equal(manager.dataLabels, true);
  for (const spec of SPECS) {
    // The plugin reads chart.options, so that is where the flag has to resolve.
    assert.equal(manager.instances.get(spec.id).options.plugins.reportDataLabels.display, true);
    assert.equal(
      on.get(spec.id) - off.get(spec.id),
      pointCount(spec),
      `${spec.id}: one value per point should have been painted`,
    );
  }

  manager.clear();
});

test('toggling data labels off stops painting them', () => {
  const { manager, drawnBySpec } = renderAll();

  const on = textsPerRedraw(manager, drawnBySpec, true);
  const off = textsPerRedraw(manager, drawnBySpec, false);

  assert.equal(manager.dataLabels, false);
  for (const spec of SPECS) {
    assert.equal(manager.instances.get(spec.id).options.plugins.reportDataLabels.display, false);
    assert.equal(
      on.get(spec.id) - off.get(spec.id),
      pointCount(spec),
      `${spec.id}: values kept being painted after the toggle went off`,
    );
  }

  manager.clear();
});

test('a chart rendered while the toggle is on opens with labels', () => {
  const manager = new ReportChartManager();
  manager.setDataLabels(true);

  const { canvas, drawn } = stubCanvas();
  manager.render(canvas, SPECS[0]);
  const withLabels = drawn.length;

  manager.setDataLabels(false);
  drawn.length = 0;
  manager.setDataLabels(false);

  assert.equal(manager.instances.get('bar').options.plugins.reportDataLabels.display, false);
  assert.ok(
    withLabels > drawn.length,
    'a chart built while the toggle was on should already carry its values',
  );
  manager.clear();
});
