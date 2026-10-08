import { shouldRenderReportDataLabel } from '../dashboardRequestState.js';

export const SERIES_PALETTE = [
  '#0f766e',
  '#2563eb',
  '#b45309',
  '#16a34a',
  '#9333ea',
  '#db2777',
  '#0891b2',
  '#64748b',
  '#4d7c0f',
  '#dc2626',
  '#a21caf',
  '#4338ca',
];

// Past the palette there is nothing curated left to pick, so the hue simply keeps
// rotating. The palette is sized so year-over-year never gets there: it plots one
// series per year of history, which reaches twelve only in 2029.
const GENERATED_SATURATION = 62;
const GENERATED_LIGHTNESS = 38;

function seriesIndex(index) {
  return Number.isInteger(index) && index >= 0 ? index : 0;
}

function generatedHue(index) {
  return (index * 137) % 360;
}

export function chartSeriesColor(index) {
  const position = seriesIndex(index);
  if (position < SERIES_PALETTE.length) return SERIES_PALETTE[position];
  return `hsl(${generatedHue(position)}, ${GENERATED_SATURATION}%, ${GENERATED_LIGHTNESS}%)`;
}

function hasDrawableSegment(data) {
  const values = data || [];
  return values.some((value, index) => (
    index > 0
    && shouldRenderReportDataLabel(value)
    && shouldRenderReportDataLabel(values[index - 1])
  ));
}

/**
 * Chart type to actually render.
 *
 * A line needs two adjacent points to draw a segment. A period that collapses into a
 * single bucket, or a series whose months never touch, leaves bare markers that read as
 * a broken chart — those are shown as bars instead.
 */
// Types charts.js actually registers. Chart.js no longer auto-registers everything (the
// bundle imports controllers explicitly), so a type outside this set throws
// `"x" is not a registered controller` in the browser — with no server-side signal, because
// the type is chosen in Python by dashboard_reports._chart(). Falling back to 'bar' keeps a
// new backend chart type rendering something real instead of blanking the report; widen the
// set and the registration together when one is genuinely wanted.
const RENDERABLE_TYPES = new Set(['bar', 'line', 'doughnut', 'pie']);

export function chartRenderType(spec) {
  const requested = spec?.type || 'bar';
  const type = RENDERABLE_TYPES.has(requested) ? requested : 'bar';
  if (type !== 'line') return type;
  return (spec.datasets || []).some((dataset) => hasDrawableSegment(dataset.data))
    ? 'line'
    : 'bar';
}

// Labels collide along the axis once the points outrun the width, and on top of each
// other once the series stack up at the same point. The old guard watched only the
// first: it hid a month of daily values while still stacking 108 across nine years.
const MAX_LABELLED_POINTS = 31;
const MAX_LABELLED_VALUES = 62;

export function shouldRenderChartDataLabels({ type, pointCount = 0, datasetCount = 1 }) {
  if (type === 'doughnut' || type === 'pie') return true;
  const series = Math.max(datasetCount, 1);
  return pointCount <= MAX_LABELLED_POINTS && pointCount * series <= MAX_LABELLED_VALUES;
}

/**
 * Value format of the first series measuring on an axis.
 *
 * Never falls back across axes: the right-hand axis had no formatter at all, and the
 * money format of the series on the left is not the one its counts want.
 */
export function axisValueFormat(spec, axisId = 'y') {
  const datasets = spec?.datasets || [];
  const onAxis = datasets.find((dataset) => (dataset.axis || 'y') === axisId);
  return onAxis?.format || 'number';
}

function datasetsOnAxis(spec, axisId) {
  return (spec?.datasets || []).filter((dataset) => (dataset.axis || 'y') === axisId);
}

// Rounded shares of one column may add up to a hair over 100 (33.3 + 33.3 + 33.4 = 100.0,
// 33.4 + 33.4 + 33.3 = 100.1); that is rounding, not a value the axis should make room for.
const STACKED_ROUNDING_TOLERANCE = 1;

/**
 * Fixed upper bound of a stacked axis whose every series is a share, or undefined to auto-scale.
 *
 * Only a stack of shares is pinned: its columns are parts of one whole, so the axis ending at
 * 100 is what makes them comparable. A plain percent chart (ОПЗ %, growth) keeps auto-scaling —
 * pinning it would flatten a series that lives between 5 and 30 — and a column that passes the
 * ceiling unpins the axis rather than clip the very bar the reader came to see.
 */
export function axisMax(spec, axisId = 'y') {
  if (spec?.stacked !== true) return undefined;
  const onAxis = datasetsOnAxis(spec, axisId);
  if (!onAxis.length || !onAxis.every((dataset) => dataset.format === 'percent')) return undefined;
  const columnCount = Math.max(...onAxis.map((dataset) => (dataset.data || []).length));
  for (let index = 0; index < columnCount; index += 1) {
    const column = onAxis
      .map((dataset) => dataset.data?.[index])
      .filter((value) => shouldRenderReportDataLabel(value))
      .map(Number);
    const height = column.filter((value) => value > 0).reduce((sum, value) => sum + value, 0);
    if (height > 100 + STACKED_ROUNDING_TOLERANCE) return undefined;
  }
  return 100;
}

/**
 * Name shown in front of a tooltip value.
 *
 * An arc chart has one dataset and one segment per label, so the dataset's name would be
 * the same on every segment ("Доля: 40%") and say nothing about which one is hovered.
 */
export function tooltipSeriesLabel(spec, type, { datasetIndex = 0, dataIndex = 0 } = {}) {
  const dataset = spec?.datasets?.[datasetIndex] || {};
  if (type === 'doughnut' || type === 'pie') return spec?.labels?.[dataIndex] ?? dataset.label;
  return dataset.label;
}
