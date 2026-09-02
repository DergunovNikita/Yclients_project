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
export function chartRenderType(spec) {
  const type = spec?.type || 'bar';
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
