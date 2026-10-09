import {
  ArcElement,
  BarController,
  BarElement,
  CategoryScale,
  Chart,
  DoughnutController,
  Legend,
  LinearScale,
  LineController,
  LineElement,
  PieController,
  PointElement,
  Tooltip,
} from 'chart.js';

import { formatValue } from './format.js';
import {
  COMPARE_DASH,
  axisMax,
  axisValueFormat,
  chartRenderType,
  chartSeriesColor,
  mutedColor,
  shouldRenderChartDataLabels,
  tooltipSeriesLabel,
  withComparisonDatasets,
} from './chartSpec.js';
import { t } from '../i18n.js';
import { chartTooltipValue, shouldRenderReportDataLabel } from '../dashboardRequestState.js';
// Side-effect only: registers the global responsive tick/legend plugin (Chart.js dedupes
// the module, so main.js importing it too does not register it twice) — see that file for
// why it has to be a plugin rather than options set here.
import '../chartResponsive.js';

const dataLabelsPlugin = {
  id: 'reportDataLabels',
  afterDatasetsDraw(chart) {
    const options = chart.options.plugins?.reportDataLabels;
    if (!options?.display) return;

    if (!shouldRenderChartDataLabels({
      type: chart.config.type,
      pointCount: chart.data.labels?.length || 0,
      datasetCount: (chart.data.datasets || [])
        .filter((_, index) => chart.isDatasetVisible(index)).length,
    })) return;

    const { ctx } = chart;
    ctx.save();
    ctx.font = '600 11px Inter, ui-sans-serif, system-ui, sans-serif';
    ctx.fillStyle = '#334155';
    ctx.textAlign = 'center';

    chart.data.datasets.forEach((dataset, datasetIndex) => {
      const meta = chart.getDatasetMeta(datasetIndex);
      if (meta.hidden || dataset.reportCompare) return;

      meta.data.forEach((element, index) => {
        const raw = dataset.data?.[index];
        if (!shouldRenderReportDataLabel(raw)) return;
        const numericValue = Number(raw);

        const position = element.tooltipPosition();
        const isArcChart = chart.config.type === 'doughnut' || chart.config.type === 'pie';
        ctx.textBaseline = isArcChart ? 'middle' : 'bottom';
        ctx.fillText(
          formatValue(numericValue, dataset.reportFormat || 'number').replace(' ₽', ''),
          position.x,
          isArcChart ? position.y : position.y - 6,
        );
      });
    });
    ctx.restore();
  },
};

// Report specs render as bar, line, or doughnut (dashboard_reports.py's _chart() calls) — see
// main.js for the smaller bar/line-only registration its own hand-built charts need. Pie is
// registered alongside doughnut: the arc-chart branches throughout this file and chartSpec.js
// already treat the two as interchangeable, and ArcElement is shared by both.
Chart.register(
  BarController,
  LineController,
  DoughnutController,
  PieController,
  BarElement,
  LineElement,
  PointElement,
  ArcElement,
  CategoryScale,
  LinearScale,
  Legend,
  Tooltip,
);
Chart.register(dataLabelsPlugin);
Chart.defaults.animation = false;
Chart.defaults.font.family = 'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
Chart.defaults.color = '#64748b';
Chart.defaults.borderColor = '#e2e8f0';

function compareStyle(colorIndex) {
  const color = chartSeriesColor(colorIndex);
  return {
    borderColor: mutedColor(color),
    backgroundColor: mutedColor(color, 0.3),
    borderDash: COMPARE_DASH,
    pointRadius: 2,
    pointStyle: 'rectRot',
    borderWidth: 1.5,
  };
}

export class ReportChartManager {
  constructor() {
    this.instances = new Map();
    this.dataLabels = false;
  }

  clear() {
    this.instances.forEach((chart) => chart.destroy());
    this.instances.clear();
  }

  setDataLabels(enabled) {
    this.dataLabels = enabled;
    this.instances.forEach((chart) => {
      // Written to the plain options object optionsFor() built, never through
      // chart.options. That one is Chart.js's resolved *view* of the options, and
      // reading a branch of it hands back a proxy — so the guard line this replaces,
      // `chart.options.plugins = chart.options.plugins || {}`, stored a proxy back into
      // the config, and the write right after it then bounced between two proxy set
      // traps until the stack blew. Every click of the toggle threw, on every 4.x.
      // update() re-reads the config, so the resolved view is not needed here at all.
      chart.config.options.plugins.reportDataLabels.display = enabled;
      chart.update();
    });
  }

  render(canvas, baseSpec, comparisonChart = null) {
    if (!canvas || !baseSpec) return;
    const previous = this.instances.get(baseSpec.id);
    if (previous) previous.destroy();

    // The type follows the current window alone: a comparison series must not turn bars into a line.
    const type = chartRenderType(baseSpec);
    const spec = withComparisonDatasets(baseSpec, comparisonChart, t('reports.compareSuffix'));
    const isArc = type === 'doughnut' || type === 'pie';
    const chart = new Chart(canvas, {
      type,
      data: {
        labels: spec.labels || [],
        datasets: (spec.datasets || []).map((dataset, index) => ({
          label: dataset.label,
          data: dataset.data || [],
          // Arc charts colour each segment individually; other charts colour per series.
          borderColor: isArc ? '#ffffff' : chartSeriesColor(dataset.colorIndex ?? index),
          backgroundColor: isArc
            ? (dataset.data || []).map((_, i) => chartSeriesColor(i))
            : chartSeriesColor(dataset.colorIndex ?? index),
          ...(dataset.compare ? compareStyle(dataset.colorIndex) : {}),
          borderWidth: isArc ? 2 : undefined,
          tension: 0.28,
          // Never filled: stacked areas hid the series drawn under them, and an area
          // needs a translucent colour this code does not derive. The backend agrees —
          // every spec that mentions fill sets it to false.
          fill: false,
          borderRadius: type === 'bar' ? 4 : 0,
          yAxisID: dataset.axis || 'y',
          reportFormat: dataset.format || 'number',
          reportCompare: dataset.compare === true,
        })),
      },
      options: this.optionsFor(spec, type),
    });
    this.instances.set(spec.id, chart);
  }

  optionsFor(spec, type) {
    const axisTicks = (axisId) => ({
      callback: (value) => formatValue(value, axisValueFormat(spec, axisId)).replace(' ₽', ''),
    });
    const scales = {};
    if (type !== 'doughnut' && type !== 'pie') {
      const stacked = spec.stacked === true;
      if (stacked) scales.x = { stacked };
      scales.y = {
        beginAtZero: true,
        ticks: axisTicks('y'),
      };
      if (stacked) scales.y.stacked = true;
      const yMax = axisMax(spec, 'y');
      if (yMax !== undefined) scales.y.max = yMax;
      if ((spec.datasets || []).some((dataset) => dataset.axis === 'y1')) {
        scales.y1 = {
          beginAtZero: true,
          position: 'right',
          grid: { drawOnChartArea: false },
          ticks: axisTicks('y1'),
        };
      }
    }
    return {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales,
      plugins: {
        legend: { position: 'bottom' },
        reportDataLabels: { display: this.dataLabels },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const dataset = spec.datasets?.[ctx.datasetIndex] || {};
              const value = chartTooltipValue(ctx.parsed, ctx.chart?.options?.indexAxis);
              const format = dataset.format || axisValueFormat(spec, dataset.axis || 'y');
              const name = tooltipSeriesLabel(spec, type, ctx);
              return ` ${name}: ${formatValue(value, format)}`;
            },
          },
        },
      },
    };
  }
}
