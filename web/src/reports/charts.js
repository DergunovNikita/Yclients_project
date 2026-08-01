import Chart from 'chart.js/auto';

import { formatValue } from './format.js';
import { shouldRenderReportDataLabel } from '../dashboardRequestState.js';

const PALETTE = [
  '#0f766e',
  '#2563eb',
  '#b45309',
  '#16a34a',
  '#9333ea',
  '#db2777',
  '#0891b2',
  '#64748b',
];

const dataLabelsPlugin = {
  id: 'reportDataLabels',
  afterDatasetsDraw(chart) {
    const options = chart.options.plugins?.reportDataLabels;
    if (!options?.display) return;

    const pointCount = chart.data.labels?.length || 0;
    if (chart.config.type !== 'doughnut' && pointCount > 24) return;

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
        const isDoughnut = chart.config.type === 'doughnut';
        ctx.textBaseline = isDoughnut ? 'middle' : 'bottom';
        ctx.fillText(
          formatValue(numericValue, dataset.reportFormat || 'number').replace(' ₽', ''),
          position.x,
          isDoughnut ? position.y : position.y - 6,
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

    const isArc = spec.type === 'doughnut' || spec.type === 'pie';
    const chart = new Chart(canvas, {
      type: spec.type || 'bar',
      data: {
        labels: spec.labels || [],
        datasets: (spec.datasets || []).map((dataset, index) => ({
          label: dataset.label,
          data: dataset.data || [],
          // Arc charts colour each segment individually; other charts colour per series.
          borderColor: isArc ? '#ffffff' : PALETTE[index % PALETTE.length],
          backgroundColor: isArc
            ? (dataset.data || []).map((_, i) => PALETTE[i % PALETTE.length])
            : spec.type === 'line'
              ? `${PALETTE[index % PALETTE.length]}22`
              : PALETTE[index % PALETTE.length],
          borderWidth: isArc ? 2 : undefined,
          tension: 0.28,
          fill: dataset.fill ?? (spec.type === 'line'),
          borderRadius: spec.type === 'bar' ? 4 : 0,
          yAxisID: dataset.axis || 'y',
          reportFormat: dataset.format || 'number',
        })),
      },
      options: this.optionsFor(spec),
    });
    this.instances.set(spec.id, chart);
  }

  optionsFor(spec) {
    const firstFormat = spec.datasets?.[0]?.format || 'number';
    const hasSecondAxis = (spec.datasets || []).some((dataset) => dataset.axis === 'y1');
    const scales = {};
    if (spec.type !== 'doughnut') {
      scales.y = {
        beginAtZero: true,
        ticks: {
          callback: (value) => formatValue(value, firstFormat).replace(' ₽', ''),
        },
      };
      if (hasSecondAxis) {
        scales.y1 = {
          beginAtZero: true,
          position: 'right',
          grid: { drawOnChartArea: false },
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
              const parsed = ctx.parsed;
              const value = typeof parsed === 'number'
                ? parsed
                : parsed?.x !== undefined ? parsed.x : parsed?.y;
              return ` ${dataset.label}: ${formatValue(value, dataset.format || firstFormat)}`;
            },
          },
        },
      },
    };
  }
}
