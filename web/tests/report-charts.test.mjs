// The timezone is pinned before any Date work: the compare window used to be built with
// toISOString(), which moved it a day back for every zone ahead of UTC.
process.env.TZ = 'Europe/Moscow';

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  SERIES_PALETTE,
  axisValueFormat,
  chartRenderType,
  chartSeriesColor,
  shouldRenderChartDataLabels,
} from '../src/reports/chartSpec.js';
import {
  comparePeriodOnLoad,
  defaultComparePeriod,
  nextComparePeriod,
  shouldAdoptComparePeriod,
} from '../src/period.js';

// Chart.js resolves colours with @kurkle/color, which accepts hex and the legacy comma
// forms but not CSS Color 4 slash-alpha — an unparseable colour is dropped silently.
const CSS_COLOR = /^(#[0-9a-f]{6}([0-9a-f]{2})?|hsla?\(\d{1,3}, \d{1,3}%, \d{1,3}%(, [\d.]+)?\))$/;

// Chart.js's own parser is the authority, but it is only a transitive dependency here,
// so the regex above stands on its own where a flat node_modules cannot be assumed.
const { Color } = await import('@kurkle/color').catch(() => ({ Color: null }));

const lineSpec = (datasets, labels = []) => ({ type: 'line', labels, datasets });

test('a line with nothing to connect is rendered as bars', () => {
  // granularity=month over a single calendar month: one bucket, one bare marker.
  assert.equal(chartRenderType(lineSpec([
    { label: 'Выручка', data: [2026206] },
    { label: 'Записи', data: [526], axis: 'y1' },
  ], ['2026-08-01'])), 'bar');

  // Each year holding exactly one month draws isolated dots, never a segment.
  assert.equal(chartRenderType(lineSpec([
    { label: '2023', data: [null, null, null, null, 1000, null, null, null, null, null, null, null] },
    { label: '2024', data: [null, null, null, null, null, 2000, null, null, null, null, null, null] },
  ])), 'bar');

  assert.equal(chartRenderType(lineSpec([{ label: 'Выручка', data: [] }])), 'bar');
  assert.equal(chartRenderType(lineSpec([{ label: 'Выручка', data: [null, null, null] }])), 'bar');
});

test('a line with a drawable segment stays a line', () => {
  assert.equal(chartRenderType(lineSpec([
    { label: 'Выручка', data: [470190, 542790, 243660] },
  ])), 'line');

  // Year-over-year: the opening year starts in August, the current one stops in September.
  assert.equal(chartRenderType(lineSpec([
    { label: '2018', data: [null, null, null, null, null, null, null, 60170, 369765, 441615, 424510, 649550] },
    { label: '2026', data: [7248070, 8423880, 9553350, null, null, null, null, null, null, null, null, null] },
  ])), 'line');

  // One series without a segment does not downgrade the whole chart.
  assert.equal(chartRenderType(lineSpec([
    { label: 'Единиц', data: [5, null, 7] },
    { label: 'Выручка', data: [100, 200, 300] },
  ])), 'line');
});

test('non-line specs pass through untouched', () => {
  assert.equal(chartRenderType({ type: 'bar', datasets: [{ data: [1] }] }), 'bar');
  assert.equal(chartRenderType({ type: 'doughnut', datasets: [{ data: [1, 2] }] }), 'doughnut');
  assert.equal(chartRenderType(undefined), 'bar');
});

test('series colours never repeat inside one chart', () => {
  // year_over_year plots 2018..2026: the ninth series used to wrap onto the first colour.
  assert.equal(chartSeriesColor(0), SERIES_PALETTE[0]);
  assert.equal(chartSeriesColor(7), SERIES_PALETTE[7]);
  assert.notEqual(chartSeriesColor(12), chartSeriesColor(0));
  const colors = Array.from({ length: 24 }, (_, index) => chartSeriesColor(index));
  assert.equal(new Set(colors).size, colors.length);
});

test('every series colour is written in a form Chart.js parses', () => {
  for (const index of [0, 7, 8, 12, 100]) {
    assert.match(chartSeriesColor(index), CSS_COLOR);
    if (!Color) continue;
    assert.ok(new Color(chartSeriesColor(index)).valid, `series ${index}: ${chartSeriesColor(index)}`);
  }
  // The trap the comma form avoids, pinned only where the real parser is reachable.
  if (Color) assert.equal(new Color('hsl(204 62% 38%)').valid, true);
  if (Color) assert.equal(new Color('hsl(204 62% 38% / 13%)').valid, false);
});

test('a year-over-year chart is coloured from the curated palette alone', () => {
  // Rotated hues land wherever they land — index 9 falls 11 degrees from the palette
  // green. The palette is therefore sized past the charts that exist: year_over_year
  // plots one series per year of history, and the data starts in 2018.
  assert.ok(SERIES_PALETTE.length >= 12, `palette shrank to ${SERIES_PALETTE.length}`);
  for (let index = 0; index < SERIES_PALETTE.length; index += 1) {
    assert.equal(chartSeriesColor(index), SERIES_PALETTE[index]);
  }
});

test('value labels are hidden by width and by stacking, not by their product', () => {
  // A month of daily revenue is labellable; the old points-only guard hid it at 24.
  assert.equal(shouldRenderChartDataLabels({ type: 'bar', pointCount: 31, datasetCount: 1 }), true);
  assert.equal(shouldRenderChartDataLabels({ type: 'line', pointCount: 31, datasetCount: 2 }), true);
  // Records by period carries three series and keeps its labels over a short range.
  assert.equal(shouldRenderChartDataLabels({ type: 'line', pointCount: 20, datasetCount: 3 }), true);
  // The same chart over a full month is 93 values and goes back to being unlabelled.
  assert.equal(shouldRenderChartDataLabels({ type: 'line', pointCount: 31, datasetCount: 3 }), false);
  // A quarter of daily points outruns the axis width whatever the series count.
  assert.equal(shouldRenderChartDataLabels({ type: 'bar', pointCount: 92, datasetCount: 1 }), false);
  // 12 months across 9 years is 108 labels stacked on top of each other.
  assert.equal(shouldRenderChartDataLabels({ type: 'line', pointCount: 12, datasetCount: 9 }), false);
  assert.equal(shouldRenderChartDataLabels({ type: 'doughnut', pointCount: 300, datasetCount: 9 }), true);
});

test('each axis is formatted by the series that measures on it', () => {
  const spec = {
    type: 'line',
    datasets: [
      { label: 'Выручка', data: [1], format: 'money' },
      { label: 'Записи', data: [1], format: 'number', axis: 'y1' },
    ],
  };
  assert.equal(axisValueFormat(spec, 'y'), 'money');
  assert.equal(axisValueFormat(spec, 'y1'), 'number');
  // A count series that omits its format must not inherit roubles from the money series.
  assert.equal(axisValueFormat({ datasets: [{ format: 'money' }, { axis: 'y1' }] }, 'y1'), 'number');
  assert.equal(axisValueFormat({ datasets: [{ format: 'decimal' }] }, 'y1'), 'number');
  assert.equal(axisValueFormat({}, 'y'), 'number');
});

test('the default compare window is the previous window of the same length', () => {
  assert.deepEqual(
    defaultComparePeriod('2026-08-01', '2026-08-31'),
    { start: '2026-07-01', end: '2026-07-31' },
  );
  assert.deepEqual(
    defaultComparePeriod('2026-09-01', '2026-09-01'),
    { start: '2026-08-31', end: '2026-08-31' },
  );
  assert.deepEqual(
    defaultComparePeriod('2026-04-01', '2026-04-30'),
    { start: '2026-03-02', end: '2026-03-31' },
  );
  assert.equal(defaultComparePeriod('', '2026-08-31'), null);
  assert.equal(defaultComparePeriod('2026-08-31', '2026-08-01'), null);
});

test('a period crossing a DST switch keeps its calendar length', () => {
  const original = process.env.TZ;
  try {
    // Moscow has had no DST since 2014, so the switch has to be looked for elsewhere.
    process.env.TZ = 'America/Los_Angeles';
    // March springs forward: 31 calendar days, but an hour short of 31 elapsed days.
    assert.deepEqual(
      defaultComparePeriod('2026-03-01', '2026-03-31'),
      { start: '2026-01-29', end: '2026-02-28' },
    );
    // November falls back and gains one.
    assert.deepEqual(
      defaultComparePeriod('2026-11-01', '2026-11-30'),
      { start: '2026-10-02', end: '2026-10-31' },
    );
  } finally {
    process.env.TZ = original;
  }
});

test('the compare window follows the period only until the user picks one', () => {
  assert.equal(shouldAdoptComparePeriod({ compareStart: '', compareEnd: '', autoPeriod: null }), true);

  // A window that arrived with the link belongs to whoever built the link, so the caller
  // hands the predicate ours:false and it stays out of the way — including once those
  // fields are emptied, which is why the flag is not inferred from the values.
  assert.equal(shouldAdoptComparePeriod({
    compareStart: '2025-08-01', compareEnd: '2025-08-31', autoPeriod: null, ours: false,
  }), false);
  assert.equal(shouldAdoptComparePeriod({
    compareStart: '', compareEnd: '', autoPeriod: null, ours: false,
  }), false);

  const auto = { start: '2026-07-01', end: '2026-07-31' };
  assert.equal(shouldAdoptComparePeriod({
    compareStart: '2026-07-01', compareEnd: '2026-07-31', autoPeriod: auto,
  }), true);
  // Typing a window by hand takes it over; the caller latches ours:false on this answer.
  assert.equal(shouldAdoptComparePeriod({
    compareStart: '2025-08-01', compareEnd: '2025-08-31', autoPeriod: auto,
  }), false);
  // Clearing both fields drops the comparison on purpose: a new period must not revive it.
  assert.equal(shouldAdoptComparePeriod({ compareStart: '', compareEnd: '', autoPeriod: auto }), false);
});

test('the pre-filled compare window matches the backend baseline when no preset is in play', () => {
  // The reports page renders two deltas for the same metric: the segments table computes
  // its own against `DateRange.previous_period(None)`, and the comparison block uses the
  // window pre-filled here. A month shorter than its predecessor is where a calendar-based
  // rule and this day-based one part ways, so those are the cases worth pinning.
  // Mirrors tests/test_dashboard_api.py::test_previous_period_steps_back_by_the_presets_own_unit.
  assert.deepEqual(
    defaultComparePeriod('2025-11-01', '2025-11-30'),
    { start: '2025-10-02', end: '2025-10-31' },
  );
  assert.deepEqual(
    defaultComparePeriod('2026-02-01', '2026-02-28'),
    { start: '2026-01-04', end: '2026-01-31' },
  );
  assert.deepEqual(
    defaultComparePeriod('2026-09-01', '2026-09-05'),
    { start: '2026-08-27', end: '2026-08-31' },
  );
});

test('the compare window does not depend on the viewer timezone', () => {
  const original = process.env.TZ;
  try {
    for (const zone of ['Europe/Moscow', 'UTC', 'America/Los_Angeles', 'Pacific/Kiritimati']) {
      process.env.TZ = zone;
      assert.deepEqual(
        defaultComparePeriod('2026-08-01', '2026-08-31'),
        { start: '2026-07-01', end: '2026-07-31' },
        `compare window drifted in ${zone}`,
      );
    }
  } finally {
    process.env.TZ = original;
  }
});

test('a link tells us whether its compare window is ours to move', () => {
  const period = { start: '2026-09-01', end: '2026-09-30' };
  const auto = defaultComparePeriod(period.start, period.end);

  // The window an earlier filter change wrote into the URL keeps following the period.
  assert.deepEqual(
    comparePeriodOnLoad({ ...period, compareStart: auto.start, compareEnd: auto.end }),
    { autoPeriod: { start: auto.start, end: auto.end }, ours: true },
  );
  // A year-over-year window nobody would have written for this period is the sender's.
  assert.deepEqual(
    comparePeriodOnLoad({ ...period, compareStart: '2025-09-01', compareEnd: '2025-09-30' }),
    { autoPeriod: null, ours: false },
  );
  // No window at all leaves us free to offer the default.
  assert.deepEqual(
    comparePeriodOnLoad({ ...period, compareStart: '', compareEnd: '' }),
    { autoPeriod: null, ours: true },
  );
});

test('the compare window survives a reload and keeps following the period', () => {
  // Walks the states the reports view goes through, through the rules it actually uses.
  const move = (state, period, shown) => {
    const next = nextComparePeriod(state, {
      ...period,
      compareStart: shown.start,
      compareEnd: shown.end,
    });
    return [
      { autoPeriod: next.autoPeriod, ours: next.ours },
      next.window ? { start: next.window.start, end: next.window.end } : shown,
    ];
  };

  const august = { start: '2026-08-01', end: '2026-08-31' };
  const september = { start: '2026-09-01', end: '2026-09-30' };
  const october = { start: '2026-10-01', end: '2026-10-31' };

  // Fresh load, comparison ticked: the default window is offered and follows the period.
  let [state, shown] = move(comparePeriodOnLoad({ ...august, compareStart: '', compareEnd: '' }), august, { start: '', end: '' });
  assert.deepEqual(shown, { start: '2026-07-01', end: '2026-07-31' });
  [state, shown] = move(state, september, shown);
  assert.deepEqual(shown, { start: '2026-08-02', end: '2026-08-31' });

  // Reloading the link that filter change wrote must not freeze the window.
  [state, shown] = move(
    comparePeriodOnLoad({ ...september, compareStart: shown.start, compareEnd: shown.end }),
    september,
    shown,
  );
  assert.deepEqual(shown, { start: '2026-08-02', end: '2026-08-31' });
  [state, shown] = move(state, october, shown);
  // 31 days back from the day before October, so the window straddles two months.
  assert.deepEqual(shown, { start: '2026-08-31', end: '2026-09-30' });

  // A window chosen by hand survives the same reload and every later period change.
  const chosen = { start: '2025-10-01', end: '2025-10-31' };
  let [ownState, ownShown] = move(
    comparePeriodOnLoad({ ...october, compareStart: chosen.start, compareEnd: chosen.end }),
    october,
    chosen,
  );
  assert.deepEqual(ownShown, chosen);
  [ownState, ownShown] = move(ownState, september, ownShown);
  assert.deepEqual(ownShown, chosen);
});
