import Chart from 'chart.js/auto';

import { formatValue } from './format.js';
import {
  axisValueFormat,
  chartRenderType,
  chartSeriesColor,
  shouldRenderChartDataLabels,
} from './chartSpec.js';
import { chartTooltipValue, shouldRenderReportDataLabel } from '../dashboardRequestState.js';

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
      if (meta.hidden) return;

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

Chart.register(dataLabelsPlugin);
Chart.defaults.animation = false;
Chart.defaults.font.family = 'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
Chart.defaults.color = '#64748b';
Chart.defaults.borderColor = '#e2e8f0';

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
      chart.options.plugins = chart.options.plugins || {};
      chart.options.plugins.reportDataLabels = { display: enabled };
      chart.update();
    });
  }

  render(canvas, spec) {
    if (!canvas || !spec) return;
    const previous = this.instances.get(spec.id);
    if (previous) previous.destroy();

    const type = chartRenderType(spec);
    const isArc = type === 'doughnut' || type === 'pie';
    const chart = new Chart(canvas, {
      type,
      data: {
        labels: spec.labels || [],
        datasets: (spec.datasets || []).map((dataset, index) => ({
          label: dataset.label,
          data: dataset.data || [],
          // Arc charts colour each segment individually; other charts colour per series.
          borderColor: isArc ? '#ffffff' : chartSeriesColor(index),
          backgroundColor: isArc
            ? (dataset.data || []).map((_, i) => chartSeriesColor(i))
            : chartSeriesColor(index),
          borderWidth: isArc ? 2 : undefined,
          tension: 0.28,
          // Never filled: stacked areas hid the series drawn under them, and an area
          // needs a translucent colour this code does not derive. The backend agrees —
          // every spec that mentions fill sets it to false.
          fill: false,
          borderRadius: type === 'bar' ? 4 : 0,
          yAxisID: dataset.axis || 'y',
          reportFormat: dataset.format || 'number',
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
      scales.y = {
        beginAtZero: true,
        ticks: axisTicks('y'),
      };
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
              return ` ${dataset.label}: ${formatValue(value, format)}`;
            },
          },
        },
      },
    };
  }
}
