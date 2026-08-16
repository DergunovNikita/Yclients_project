import assert from 'node:assert/strict';
import test from 'node:test';

import {
  filterPlanFactForDisplay,
  hideMoneyPlanMetrics,
  moneyPlanMetricCodes,
  normalizeHiddenPlanMetricCodes,
  setPlanMetricHidden,
  visiblePlanMetrics,
} from '../src/planMetricVisibility.js';

const metrics = [
  { code: 'revenue', label: 'Revenue', format: 'money' },
  { code: 'avg_check_total', label: 'Average check', format: 'money' },
  { code: 'clients', label: 'Clients', format: 'number' },
];

test('plan metric visibility exposes only server-provided metrics', () => {
  const normalized = normalizeHiddenPlanMetricCodes(metrics, ['revenue', 'forbidden_metric']);

  assert.deepEqual([...normalized], ['revenue']);
  assert.deepEqual(
    visiblePlanMetrics(metrics, normalized).map((metric) => metric.code),
    ['avg_check_total', 'clients'],
  );
});

test('normalization restores one KPI when a refreshed server metric set would hide every option', () => {
  const refreshedMetrics = [metrics[1]];
  const normalized = normalizeHiddenPlanMetricCodes(
    refreshedMetrics,
    new Set(['revenue', 'avg_check_total']),
  );

  assert.deepEqual([...normalized], []);
  assert.deepEqual(visiblePlanMetrics(refreshedMetrics, normalized), refreshedMetrics);
});

test('a user cannot hide the last visible plan metric', () => {
  let hidden = setPlanMetricHidden(metrics, new Set(), 'revenue', true);
  hidden = setPlanMetricHidden(metrics, hidden, 'avg_check_total', true);
  const unchanged = setPlanMetricHidden(metrics, hidden, 'clients', true);

  assert.deepEqual([...unchanged], ['revenue', 'avg_check_total']);
  assert.deepEqual(
    visiblePlanMetrics(metrics, unchanged).map((metric) => metric.code),
    ['clients'],
  );
});

test('money preset hides every available money metric and show-all reset restores them', () => {
  assert.deepEqual([...moneyPlanMetricCodes(metrics)], ['revenue', 'avg_check_total']);

  const hidden = hideMoneyPlanMetrics(metrics, new Set());
  assert.deepEqual([...hidden], ['revenue', 'avg_check_total']);
  assert.deepEqual(visiblePlanMetrics(metrics, hidden).map((metric) => metric.code), ['clients']);
  assert.deepEqual(visiblePlanMetrics(metrics, new Set()), metrics);
});

test('money preset restores a hidden non-money KPI so every money metric can be hidden', () => {
  const hidden = hideMoneyPlanMetrics(metrics, new Set(['clients']));

  assert.deepEqual([...hidden], ['revenue', 'avg_check_total']);
  assert.deepEqual(visiblePlanMetrics(metrics, hidden).map((metric) => metric.code), ['clients']);
});

test('money preset preserves the last visible KPI when only money metrics are available', () => {
  const moneyMetrics = metrics.slice(0, 2);
  const hidden = hideMoneyPlanMetrics(moneyMetrics, new Set(['revenue']));

  assert.deepEqual([...hidden], ['revenue']);
  assert.deepEqual(visiblePlanMetrics(moneyMetrics, hidden), [moneyMetrics[1]]);
});

test('display filtering covers every plan/fact surface without mutating the API payload', () => {
  const metricCells = metrics.map((metric) => ({ code: metric.code, fact: 1 }));
  const payload = {
    metrics,
    metric_sets: {
      branch: metrics,
      barber: metrics.slice(0, 2),
    },
    parent_group: { title: 'Branch', metrics: metricCells },
    groups: [{ title: 'Employee', metrics: metricCells }],
    selected_staff_plan: { title: 'Employee', metrics: metricCells },
    goods_kpi_execution: [
      { code: 'clients', fact: 1 },
      { code: 'revenue', fact: 100 },
    ],
  };
  const original = structuredClone(payload);

  const filtered = filterPlanFactForDisplay(payload, new Set(['revenue']));

  assert.deepEqual(filtered.metrics.map((metric) => metric.code), ['avg_check_total', 'clients']);
  assert.deepEqual(filtered.metric_sets.branch.map((metric) => metric.code), ['avg_check_total', 'clients']);
  assert.deepEqual(filtered.metric_sets.barber.map((metric) => metric.code), ['avg_check_total']);
  assert.deepEqual(filtered.parent_group.metrics.map((metric) => metric.code), ['avg_check_total', 'clients']);
  assert.deepEqual(filtered.groups[0].metrics.map((metric) => metric.code), ['avg_check_total', 'clients']);
  assert.deepEqual(filtered.selected_staff_plan.metrics.map((metric) => metric.code), ['avg_check_total', 'clients']);
  assert.deepEqual(filtered.goods_kpi_execution.map((metric) => metric.code), ['clients']);
  assert.deepEqual(payload, original);
});

test('a page-level selection may intentionally leave a narrower staff category empty', () => {
  const reviews = { code: 'reviews_qty', label: 'Reviews', format: 'number' };
  const payload = {
    metrics: [...metrics, reviews],
    metric_sets: { barber: metrics },
    selected_staff_plan: { title: 'Barber', metrics },
    groups: [],
  };

  const filtered = filterPlanFactForDisplay(
    payload,
    new Set(metrics.map((metric) => metric.code)),
  );

  assert.deepEqual(filtered.metrics, [reviews]);
  assert.deepEqual(filtered.metric_sets.barber, []);
  assert.deepEqual(filtered.selected_staff_plan.metrics, []);
});
