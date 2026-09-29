import { Chart } from 'chart.js';

/**
 * CSS-pixel canvas width below which a chart is treated as "phone-narrow".
 *
 * Measured live: phone canvases render 300-330px wide; the narrowest desktop canvas (a
 * Plan/actual grid cell) is 548px+. 480 sits between the two, so this only fires for an
 * actually-narrow canvas — phone or a squeezed desktop grid column — never today's desktop.
 */
export const NARROW_CHART_WIDTH = 480;

/**
 * Pure width -> override decision. Null means "wide": restore the chart's own config.
 * Otherwise: tick rotation/skip only (never `display`, so a chart that hid its legend or
 * ticks on purpose stays hidden) plus a smaller legend so it stops eating the plot area.
 */
export function narrowChartOverrides(width) {
  if (typeof width !== 'number' || !(width > 0) || width >= NARROW_CHART_WIDTH) return null;
  return {
    ticks: { maxRotation: 0, minRotation: 0, autoSkip: true, autoSkipPadding: 12 },
    legend: { boxWidth: 10, boxHeight: 10, padding: 8, fontSize: 10 },
  };
}

const TICK_KEYS = ['maxRotation', 'minRotation', 'autoSkip', 'autoSkipPadding'];
const LEGEND_KEYS = ['boxWidth', 'boxHeight', 'padding'];

// Per-chart memory of each value's pre-override state, so "restore" puts back this chart's
// own config rather than Chart.js's built-in defaults. Keyed by the Chart instance, so a
// destroyed-and-rebuilt chart (destroyChart() in main.js) always starts with a clean slate.
const originalsByChart = new WeakMap();

function stashFor(chart) {
  let entry = originalsByChart.get(chart);
  if (!entry) {
    entry = { scales: {}, legend: null };
    originalsByChart.set(chart, entry);
  }
  return entry;
}

function applyLeafKeys(target, keys, source) {
  keys.forEach((key) => {
    target[key] = source[key];
  });
}

/**
 * `chart.options` is a read-only resolved Proxy view of `chart.config.options` merged with
 * defaults; writing a value read from it back into the config recurses through two proxy
 * set traps until the stack blows (see ReportChartManager.setDataLabels in reports/charts.js,
 * which hit exactly this). So this plugin only reads `chart.options` to discover which scale
 * ids exist — including ones a chart's config never mentions, e.g. renderRevenueChart in
 * main.js sets `scales.y` alone but still gets a category "x" scale from the defaults — and
 * only ever writes plain literals into `chart.config.options`. Writing `undefined` there for
 * "wide" lets the resolver's merge chain fall through to the chart's original value again.
 *
 * `beforeLayout` fires at the start of every update(), before scales/legend read their tick
 * and label options for that pass, and a resize's debounced update() goes through it too —
 * so one hook covers first render and every later resize with no sticky state, just the
 * chart's current `chart.width`.
 */
const responsiveChartOptionsPlugin = {
  id: 'responsiveChartOptions',
  beforeLayout(chart) {
    const rawOptions = chart.config?.options;
    if (!rawOptions) return;

    const overrides = narrowChartOverrides(chart.width);
    const stash = stashFor(chart);

    // chart.options.scales (not chart.config.options.scales) picks up an axis the chart
    // never configured itself, like the implicit "x" scale above. Rotation/autoSkip only
    // affect the horizontal-position scale in Chart.js, so applying them to every id is a
    // safe no-op on ones where it doesn't apply (a horizontal bar's category "y", a "y1").
    const resolvedScaleIds = Object.keys(chart.options?.scales || {});
    resolvedScaleIds.forEach((axisId) => {
      rawOptions.scales = rawOptions.scales || {};
      const rawScale = rawOptions.scales[axisId] = rawOptions.scales[axisId] || {};
      rawScale.ticks = rawScale.ticks || {};
      if (!stash.scales[axisId]) {
        stash.scales[axisId] = {};
        applyLeafKeys(stash.scales[axisId], TICK_KEYS, rawScale.ticks);
      }
      applyLeafKeys(rawScale.ticks, TICK_KEYS, overrides ? overrides.ticks : stash.scales[axisId]);
    });

    // Doughnut/pie charts have no scales (resolvedScaleIds is empty) but still carry a
    // legend, so this runs unconditionally rather than being folded into the loop above.
    rawOptions.plugins = rawOptions.plugins || {};
    rawOptions.plugins.legend = rawOptions.plugins.legend || {};
    const rawLegend = rawOptions.plugins.legend;
    rawLegend.labels = rawLegend.labels || {};
    rawLegend.labels.font = rawLegend.labels.font || {};
    if (!stash.legend) {
      stash.legend = { fontSize: rawLegend.labels.font.size };
      applyLeafKeys(stash.legend, LEGEND_KEYS, rawLegend.labels);
    }
    const legendValues = overrides ? overrides.legend : stash.legend;
    applyLeafKeys(rawLegend.labels, LEGEND_KEYS, legendValues);
    rawLegend.labels.font.size = legendValues.fontSize;
  },
};

Chart.register(responsiveChartOptionsPlugin);
