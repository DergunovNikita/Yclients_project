import Chart from 'chart.js/auto';
import { enhanceSelect } from './customSelect.js';
import {
  authFetch,
  authHeaders,
  getSelectedPortalAccountId,
  getToken,
  loadCurrentUser,
  logout,
  setSelectedPortalAccountId,
} from './auth.js';

if (!getToken() && !import.meta.env.VITE_API_KEY) {
  window.location.href = '/login.html';
}

import { initReports } from './reports/index.js';

const apiBase = import.meta.env.VITE_API_BASE || '';
const apiKey = import.meta.env.VITE_API_KEY || '';
let currentUser = null;

const els = {
  kpi: document.getElementById('kpi'),
  visitMetrics: document.getElementById('visit-metrics'),
  error: document.getElementById('error'),
  apiState: document.getElementById('api-state'),
  syncState: document.getElementById('sync-state'),
  periodLabel: document.getElementById('period-label'),
  revenueMeta: document.getElementById('revenue-meta'),
  appointmentsMeta: document.getElementById('appointments-meta'),
  appointmentsMetrics: document.getElementById('appointments-metrics'),
  appointmentsWarning: document.getElementById('appointments-warning'),
  servicesMeta: document.getElementById('services-meta'),
  extraServicesMeta: document.getElementById('extra-services-meta'),
  planMeta: document.getElementById('plan-meta'),
  tableMeta: document.getElementById('table-meta'),
  planInsights: document.getElementById('plan-insights'),
  planFactTable: document.getElementById('plan-fact-table'),
  reviewFactEditor: document.getElementById('review-fact-editor'),
  reviewFactMeta: document.getElementById('review-fact-meta'),
  reviewFactSave: document.getElementById('review-fact-save'),
  servicesTable: document.getElementById('services-table'),
  extraServicesTable: document.getElementById('extra-services-table'),
  revenueChart: document.getElementById('revenue-chart'),
  appointmentsChart: document.getElementById('appointments-chart'),
  opzChart: document.getElementById('opz-chart'),
  servicesChart: document.getElementById('services-chart'),
  overviewView: document.getElementById('overview-view'),
  planView: document.getElementById('plan-view'),
  planSettingsView: document.getElementById('plan-settings-view'),
  serviceManagementView: document.getElementById('service-management-view'),
  reviewFactsView: document.getElementById('review-facts-view'),
  reportsView: document.getElementById('reports-view'),
  viewLinks: [...document.querySelectorAll('[data-view-link]')],
  planSettingsMonth: document.getElementById('plan-settings-month'),
  planSettingsLoad: document.getElementById('plan-settings-load'),
  planSettingsCopy: document.getElementById('plan-settings-copy'),
  planSettingsReset: document.getElementById('plan-settings-reset'),
  planSettingsSave: document.getElementById('plan-settings-save'),
  planSettingsDirty: document.getElementById('plan-settings-dirty'),
  planSettingsSaved: document.getElementById('plan-settings-saved'),
  planSettingsBranchMeta: document.getElementById('plan-settings-branch-meta'),
  planSettingsStaffMeta: document.getElementById('plan-settings-staff-meta'),
  planSettingsBranches: document.getElementById('plan-settings-branches'),
  planSettingsStaff: document.getElementById('plan-settings-staff'),
  serviceFilterBranch: document.getElementById('service-filter-branch'),
  serviceFilterCategory: document.getElementById('service-filter-category'),
  serviceFilterGroup: document.getElementById('service-filter-group'),
  serviceFilterQuery: document.getElementById('service-filter-query'),
  serviceFilterExtra: document.getElementById('service-filter-extra'),
  serviceFilterLoad: document.getElementById('service-filter-load'),
  serviceCatalogMeta: document.getElementById('service-catalog-meta'),
  serviceCatalogTable: document.getElementById('service-catalog-table'),
  serviceManagementDirty: document.getElementById('service-management-dirty'),
  serviceManagementReset: document.getElementById('service-management-reset'),
  serviceManagementSave: document.getElementById('service-management-save'),
  serviceKpiGroupsMeta: document.getElementById('service-kpi-groups-meta'),
  serviceKpiGroupsTable: document.getElementById('service-kpi-groups-table'),
  serviceGroupTitle: document.getElementById('service-group-title'),
  serviceGroupCode: document.getElementById('service-group-code'),
  serviceGroupDescription: document.getElementById('service-group-description'),
  serviceGroupAdd: document.getElementById('service-group-add'),
  tenantSwitcher: document.getElementById('tenant-switcher'),
  tenantSelect: document.getElementById('tenant-select'),
  tenantMeta: document.getElementById('tenant-meta'),
  overviewPresetButtons: [...document.querySelectorAll('[data-overview-preset]')],
  overviewJumpButtons: [...document.querySelectorAll('[data-overview-jump]')],
};

const filterEls = {
  overview: {
    start: document.getElementById('overview-start'),
    end: document.getElementById('overview-end'),
    branch: document.getElementById('overview-branch'),
    staff: document.getElementById('overview-staff'),
    load: document.getElementById('overview-load'),
  },
  plan: {
    start: document.getElementById('plan-start'),
    end: document.getElementById('plan-end'),
    branch: document.getElementById('plan-branch'),
    staff: document.getElementById('plan-staff'),
    load: document.getElementById('plan-load'),
  },
  reviewFacts: {
    start: document.getElementById('review-fact-start'),
    end: document.getElementById('review-fact-end'),
    branch: document.getElementById('review-fact-branch'),
    staff: document.getElementById('review-fact-staff'),
    load: document.getElementById('review-fact-load'),
  },
};

const customFilterDropdowns = {};
Object.values(filterEls).forEach((filter) => {
  customFilterDropdowns[filter.branch.id] = enhanceSelect(filter.branch, { placeholder: 'Все филиалы' });
  customFilterDropdowns[filter.staff.id] = enhanceSelect(filter.staff, { placeholder: 'Все работники' });
});

const charts = {
  revenue: null,
  appointments: null,
  opz: null,
  services: null,
  selectedStaffPlan: null,
  goodsKpi: null,
};

const pageOpenedAt = new Date();
let activeView = 'overview';
let branchOptions = [];
let reviewFactRows = [];
let planSettingsData = null;
let planSettingsSavedData = null;
let planSettingsSavedSnapshot = '';
let planSettingsDirty = false;
let planSettingsLoadedMonth = '';
let allowDirtyPlanSettingsNavigation = false;
let serviceManagementData = { rows: [], groups: [], categories: [] };
let serviceManagementSavedData = null;
let serviceManagementSavedSnapshot = '';
let serviceManagementDirty = false;
let reportsController = null;
let selectedTenant = null;

const ADMIN_HIDDEN_METRIC_CODES = new Set(['revenue', 'avg_check_total']);

function headers() {
  const extra = {};
  if (apiKey) extra['X-API-Key'] = apiKey;
  return authHeaders(extra);
}

function apiUrl(path, params = {}) {
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') {
      qs.set(key, value);
    }
  });
  const suffix = qs.toString() ? `?${qs}` : '';
  const normalizedPath = `${path}${suffix}`;
  if (!apiBase) return normalizedPath;
  return `${apiBase.replace(/\/$/, '')}${normalizedPath}`;
}

function apiUrlCandidates(path, params = {}) {
  const primary = apiUrl(path, params);
  const candidates = [primary];
  if (apiBase.includes('127.0.0.1')) {
    candidates.push(primary.replace('127.0.0.1', 'localhost'));
  } else if (apiBase.includes('localhost')) {
    candidates.push(primary.replace('localhost', '127.0.0.1'));
  }
  return [...new Set(candidates)];
}

function formatMoney(value) {
  return `${Math.round(Number(value || 0)).toLocaleString('ru-RU')} ₽`;
}

function formatNumber(value) {
  return Number(value || 0).toLocaleString('ru-RU');
}

function formatDecimal(value) {
  return Number(value || 0).toLocaleString('ru-RU', { maximumFractionDigits: 2 });
}

function formatPct(value) {
  if (value === null || value === undefined) return 'нет базы';
  const sign = value > 0 ? '+' : '';
  return `${sign}${Number(value).toLocaleString('ru-RU')}% к прошлому периоду`;
}

function formatMetricValue(value, format) {
  if (value === null || value === undefined) return '—';
  if (format === 'money') return formatMoney(value);
  if (format === 'percent') return `${Number(value).toLocaleString('ru-RU', { maximumFractionDigits: 2 })}%`;
  return formatNumber(value);
}

function formatInputNumber(value) {
  if (value === null || value === undefined || value === '') return '';
  const number = Number(value);
  if (Number.isNaN(number)) return '';
  return Number.isInteger(number) ? String(number) : String(number);
}

function formatMoscowDateTime(value) {
  if (!value) return null;
  const isoValue = /(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? value : `${value}Z`;
  const date = new Date(isoValue);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('ru-RU', {
    timeZone: 'Europe/Moscow',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(date);
}

function deltaClass(value) {
  if (value === null || value === undefined || value === 0) return '';
  return value > 0 ? 'up' : 'down';
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function setApiState(text, kind = 'warn') {
  els.apiState.textContent = text;
  els.apiState.className = `pill ${kind}`;
}

function showError(message) {
  els.error.textContent = message;
  els.error.classList.add('visible');
  setApiState('API: ошибка', 'error');
}

function clearError() {
  els.error.textContent = '';
  els.error.classList.remove('visible');
}

async function fetchJson(path, params) {
  const errors = [];
  for (const url of apiUrlCandidates(path, params)) {
    let response;
    try {
      response = await fetch(url, { headers: headers() });
    } catch (error) {
      errors.push(`${url}\n${error.message}`);
      continue;
    }

    if (!response.ok) {
      const body = await response.text();
      throw new Error(`API вернул ${response.status} для ${url}\n\n${body.slice(0, 1000)}`);
    }

    const payload = await response.json();
    if (payload.success === false) {
      throw new Error(`API вернул success=false для ${url}`);
    }
    return payload;
  }

  throw new Error(
    `Не удалось подключиться к API.\n\n${errors.join('\n\n')}\n\nПроверь, что локальный API открыт в браузере по http://127.0.0.1:8000/health или http://localhost:8000/health.`,
  );
}

async function requestJson(path, { method = 'GET', body = null } = {}) {
  const errors = [];
  for (const url of apiUrlCandidates(path)) {
    let response;
    try {
      response = await fetch(url, {
        method,
        headers: { ...headers(), 'Content-Type': 'application/json' },
        body: body === null ? undefined : JSON.stringify(body),
      });
    } catch (error) {
      errors.push(`${url}\n${error.message}`);
      continue;
    }

    if (!response.ok) {
      const responseBody = await response.text();
      throw new Error(`API вернул ${response.status} для ${url}\n\n${responseBody.slice(0, 1000)}`);
    }

    const payload = await response.json();
    if (payload.success === false) {
      throw new Error(`API вернул success=false для ${url}`);
    }
    return payload;
  }

  throw new Error(
    `Не удалось подключиться к API.\n\n${errors.join('\n\n')}\n\nПроверь, что локальный API открыт в браузере по http://127.0.0.1:8000/health или http://localhost:8000/health.`,
  );
}

async function postJson(path, body) {
  return requestJson(path, { method: 'POST', body });
}

async function patchJson(path, body) {
  return requestJson(path, { method: 'PATCH', body });
}

function defaultDates(filter) {
  const now = new Date(pageOpenedAt);
  const start = new Date(now.getFullYear(), now.getMonth(), 1);
  filter.end.value = formatInputDate(now);
  filter.start.value = formatInputDate(start);
}

function setReviewFactDefaultDates() {
  const now = new Date(pageOpenedAt);
  filterEls.reviewFacts.start.value = formatInputDate(new Date(now.getFullYear(), now.getMonth(), 1));
  filterEls.reviewFacts.end.value = formatInputDate(now);
}

function overviewPresetRange(preset) {
  const end = new Date(pageOpenedAt);
  const start = new Date(end);
  if (preset === 'week') {
    const day = start.getDay() || 7;
    start.setDate(start.getDate() - day + 1);
  } else if (preset === 'month') {
    start.setDate(1);
  } else if (preset === 'quarter') {
    start.setMonth(Math.floor(start.getMonth() / 3) * 3, 1);
  } else if (preset === 'year') {
    start.setMonth(0, 1);
  }
  return { start, end };
}

function setOverviewPreset(preset) {
  const range = overviewPresetRange(preset);
  filterEls.overview.start.value = formatInputDate(range.start);
  filterEls.overview.end.value = formatInputDate(range.end);
  els.overviewPresetButtons.forEach((button) => {
    button.classList.toggle('active', button.dataset.overviewPreset === preset);
  });
}

function currentMonthValue() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
}

function previousMonthValue(month) {
  const [year, monthNumber] = String(month || currentMonthValue()).split('-').map(Number);
  const date = new Date(year, monthNumber - 2, 1);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`;
}

function formatInputDate(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function formatShortDate(value) {
  const date = new Date(`${value}T00:00:00`);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' });
}

function renderCards(target, cards) {
  target.innerHTML = cards
    .map(
      (card) => `
        <article class="card">
          <div class="label">${escapeHtml(card.label)}</div>
          <div class="value">${escapeHtml(card.value)}</div>
          ${card.delta ? `<div class="delta ${deltaClass(card.deltaValue)}">${escapeHtml(card.delta)}</div>` : ''}
        </article>
      `,
    )
    .join('');
}

function renderKpi(summary) {
  const revenue = summary.revenue || {};
  const averageCheck = summary.average_check || {};
  const cards = [
    {
      label: 'Общая выручка',
      value: formatMoney(revenue.total),
      delta: formatPct(revenue.change_pct),
      deltaValue: revenue.change_pct,
    },
    {
      label: 'Посещенные записи',
      value: formatNumber(revenue.appointments),
      delta: formatPct(revenue.appointments_change_pct),
      deltaValue: revenue.appointments_change_pct,
    },
    {
      label: averageCheck.source_status === 'partial'
        ? 'Средний чек общий (предварительно)'
        : 'Средний чек общий',
      value: formatMoney(averageCheck.total),
      delta: formatPct(averageCheck.total_change_pct),
      deltaValue: averageCheck.total_change_pct,
    },
    {
      label: 'Выручка по услугам',
      value: formatMoney(revenue.service_revenue),
      delta: formatPct(revenue.service_revenue_change_pct),
      deltaValue: revenue.service_revenue_change_pct,
    },
    {
      label: 'Кол-во оказанных услуг',
      value: formatNumber(revenue.service_count),
      delta: formatPct(revenue.service_count_change_pct),
      deltaValue: revenue.service_count_change_pct,
    },
    {
      label: 'Средний чек по услугам',
      value: formatMoney(averageCheck.services),
      delta: formatPct(averageCheck.services_change_pct),
      deltaValue: averageCheck.services_change_pct,
    },
    {
      label: 'Выручка по товарам',
      value: formatMoney(revenue.goods_revenue),
      delta: formatPct(revenue.goods_revenue_change_pct),
      deltaValue: revenue.goods_revenue_change_pct,
    },
    {
      label: 'Кол-во проданных товаров',
      value: formatNumber(revenue.goods_count),
      delta: formatPct(revenue.goods_count_change_pct),
      deltaValue: revenue.goods_count_change_pct,
    },
    {
      label: 'Средний чек по товарам',
      value: formatMoney(averageCheck.goods),
      delta: formatPct(averageCheck.goods_change_pct),
      deltaValue: averageCheck.goods_change_pct,
    },
    {
      label: 'Выручка по доп. услугам',
      value: formatMoney(revenue.extra_service_revenue),
      delta: formatPct(revenue.extra_service_revenue_change_pct),
      deltaValue: revenue.extra_service_revenue_change_pct,
    },
    {
      label: 'Кол-во оказанных доп. услуг',
      value: formatNumber(revenue.extra_service_count),
      delta: formatPct(revenue.extra_service_count_change_pct),
      deltaValue: revenue.extra_service_count_change_pct,
    },
    {
      label: 'Средний чек по доп. услугам',
      value: formatMoney(averageCheck.extra_services),
      delta: formatPct(averageCheck.extra_services_change_pct),
      deltaValue: averageCheck.extra_services_change_pct,
    },
  ];

  renderCards(els.kpi, cards);
}

function renderVisitMetrics(summary) {
  const visitMetrics = summary.visit_metrics || {};
  const cards = [
    {
      label: 'Количество ОПЗ',
      value: formatNumber(visitMetrics.opz_qty),
      delta: formatPct(visitMetrics.opz_qty_change_pct),
      deltaValue: visitMetrics.opz_qty_change_pct,
    },
    {
      label: 'Доля ОПЗ от визитов',
      value: formatMetricValue(visitMetrics.opz_pct, 'percent'),
      delta: formatPct(visitMetrics.opz_pct_change_pct),
      deltaValue: visitMetrics.opz_pct_change_pct,
    },
    {
      label: 'Доп. услуги от посещений',
      value: formatMetricValue(visitMetrics.extra_services_per_appointment_pct, 'percent'),
      delta: formatPct(visitMetrics.extra_services_per_appointment_pct_change_pct),
      deltaValue: visitMetrics.extra_services_per_appointment_pct_change_pct,
    },
    {
      label: 'Уникальные клиенты',
      value: formatNumber(visitMetrics.unique_clients),
      delta: formatPct(visitMetrics.unique_clients_change_pct),
      deltaValue: visitMetrics.unique_clients_change_pct,
    },
    {
      label: 'Визитов на клиента',
      value: formatDecimal(visitMetrics.visits_per_client),
      delta: formatPct(visitMetrics.visits_per_client_change_pct),
      deltaValue: visitMetrics.visits_per_client_change_pct,
    },
    {
      label: 'Уникальные клиенты с доп. услугами',
      value: formatMetricValue(visitMetrics.extra_service_clients_pct, 'percent'),
      delta: `${formatNumber(visitMetrics.extra_service_clients)} уник. клиентов`,
      deltaValue: null,
    },
  ];

  renderCards(els.visitMetrics, cards);
}

function renderAppointmentsMetrics(summary) {
  const breakdown = summary.appointments_breakdown || {};
  const ready = breakdown.source_status === 'ready';
  const metricValue = (value) => (ready ? formatNumber(value) : 'Нет данных');
  const metricShare = (value) => (ready ? `${formatNumber(value)}% от общего` : '');
  const cards = [
    {
      label: 'Всего записей',
      value: metricValue(breakdown.total),
      delta: metricShare(breakdown.total_share_pct),
      deltaValue: null,
    },
    {
      label: 'Отменённые записи',
      value: metricValue(breakdown.cancelled),
      delta: metricShare(breakdown.cancelled_share_pct),
      deltaValue: null,
    },
    {
      label: 'Завершённые записи',
      value: metricValue(breakdown.completed),
      delta: metricShare(breakdown.completed_share_pct),
      deltaValue: null,
    },
    {
      label: 'Незавершённые записи',
      value: metricValue(breakdown.incomplete),
      delta: metricShare(breakdown.incomplete_share_pct),
      deltaValue: null,
    },
  ];

  renderCards(els.appointmentsMetrics, cards);
  els.appointmentsWarning.textContent = ready
    ? ''
    : 'Точные метрики записей временно недоступны в YCLIENTS.';
  els.appointmentsWarning.classList.toggle('visible', !ready);
}

function destroyChart(name) {
  if (charts[name]) {
    charts[name].destroy();
    charts[name] = null;
  }
}

function renderRevenueChart(daily) {
  destroyChart('revenue');
  charts.revenue = new Chart(els.revenueChart, {
    type: 'line',
    data: {
      labels: daily.map((item) => item.date),
      datasets: [
        {
          label: 'Выручка',
          data: daily.map((item) => item.revenue),
          borderColor: '#0f766e',
          backgroundColor: 'rgba(15, 118, 110, 0.12)',
          fill: true,
          tension: 0.28,
          pointRadius: 2,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        y: {
          beginAtZero: true,
          ticks: { callback: (value) => formatMoney(value).replace(' ₽', '') },
        },
      },
      plugins: {
        tooltip: {
          callbacks: { label: (ctx) => ` ${formatMoney(ctx.parsed.y)}` },
        },
      },
    },
  });
}

function renderAppointmentsChart(daily) {
  destroyChart('appointments');
  charts.appointments = new Chart(els.appointmentsChart, {
    type: 'bar',
    data: {
      labels: daily.map((item) => item.date),
      datasets: [
        {
          label: 'Записи',
          data: daily.map((item) => item.appointments),
          backgroundColor: '#2563eb',
          borderRadius: 4,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: { y: { beginAtZero: true } },
    },
  });
}

function renderOpzChart(daily) {
  destroyChart('opz');
  charts.opz = new Chart(els.opzChart, {
    type: 'bar',
    data: {
      labels: daily.map((item) => item.date),
      datasets: [
        {
          label: 'Количество ОПЗ',
          data: daily.map((item) => item.opz_qty || 0),
          backgroundColor: '#7c3aed',
          borderRadius: 4,
          yAxisID: 'y',
        },
        {
          type: 'line',
          label: 'Доля от завершённых визитов',
          data: daily.map((item) => item.opz_pct || 0),
          borderColor: '#ea580c',
          backgroundColor: '#ea580c',
          tension: 0.25,
          pointRadius: 2,
          yAxisID: 'y1',
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        y: { beginAtZero: true, title: { display: true, text: 'ОПЗ' } },
        y1: {
          beginAtZero: true,
          position: 'right',
          grid: { drawOnChartArea: false },
          ticks: { callback: (value) => `${formatNumber(value)}%` },
          title: { display: true, text: '%' },
        },
      },
    },
  });
}

function renderServicesChart(services) {
  destroyChart('services');
  charts.services = new Chart(els.servicesChart, {
    type: 'bar',
    data: {
      labels: services.map((item) => item.title || `Услуга ${item.service_id || ''}`),
      datasets: [
        {
          label: 'Выручка',
          data: services.map((item) => item.revenue),
          backgroundColor: '#b45309',
          borderRadius: 4,
        },
      ],
    },
    options: {
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: {
          beginAtZero: true,
          ticks: { callback: (value) => formatMoney(value).replace(' ₽', '') },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: { label: (ctx) => ` ${formatMoney(ctx.parsed.x)}` },
        },
      },
    },
  });
}

function renderServicesTable(services) {
  if (!services.length) {
    els.servicesTable.innerHTML = '<div class="empty">Нет услуг за выбранный период</div>';
    return;
  }

  els.servicesTable.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Услуга</th>
          <th class="number">Продано</th>
          <th class="number">Выручка</th>
        </tr>
      </thead>
      <tbody>
        ${services
          .map(
            (item) => `
              <tr>
                <td>${escapeHtml(item.title || `Услуга ${item.service_id || ''}`)}</td>
                <td class="number">${formatNumber(item.sold)}</td>
                <td class="number">${formatMoney(item.revenue)}</td>
              </tr>
            `,
          )
          .join('')}
      </tbody>
    </table>
  `;
}

function renderExtraServicesTable(services) {
  if (!services.length) {
    els.extraServicesTable.innerHTML = '<div class="empty">Нет доп. услуг за выбранный период</div>';
    return;
  }

  els.extraServicesTable.innerHTML = `
    <div class="table-box-scroll extra-services-scroll">
    <table>
      <thead>
        <tr>
          <th>Доп. услуга</th>
          <th class="number">Сделано</th>
          <th class="number">Филиалов</th>
          <th class="number">Выручка</th>
        </tr>
      </thead>
      <tbody>
        ${services
          .map(
            (item) => `
              <tr>
                <td>${escapeHtml(item.title || `Услуга ${item.service_id || ''}`)}</td>
                <td class="number">${formatNumber(item.sold)}</td>
                <td class="number">${formatNumber(item.branch_count)}</td>
                <td class="number">${formatMoney(item.revenue)}</td>
              </tr>
            `,
          )
          .join('')}
      </tbody>
    </table>
    </div>
  `;
}

function renderPlanTable(groups, metrics) {
  const rowTypes = [
    ['plan', 'План'],
    ['fact', 'Факт'],
    ['remaining', 'Осталось'],
    ['completion_pct', '% выполнения'],
  ];

  return `
    <div class="table-scroll">
      <table class="plan-table">
        <thead>
          <tr>
            <th>Разрез</th>
            <th>Показатель</th>
            ${metrics.map((metric) => `<th class="number" data-metric="${escapeHtml(metric.code)}">${escapeHtml(metric.label)}</th>`).join('')}
          </tr>
        </thead>
        <tbody>
          ${groups
            .map((group) =>
              rowTypes
                .map(([field, label], index) => `
                  <tr>
                    ${
                      index === 0
                        ? `<th class="branch-cell" rowspan="${rowTypes.length}">${escapeHtml(group.title)}</th>`
                        : ''
                    }
                    <td class="row-label">${escapeHtml(label)}</td>
                    ${metrics
                      .map((metric) => {
                        const cellsByCode = Object.fromEntries((group.metrics || []).map((cell) => [cell.code, cell]));
                        const cell = cellsByCode[metric.code] || {};
                        const format = field === 'completion_pct' ? 'percent' : metric.format;
                        const statusClass = field === 'completion_pct' ? ` metric-status ${cell.status || 'no-plan'}` : '';
                        return `<td class="number${statusClass}" data-metric="${escapeHtml(metric.code)}">${escapeHtml(formatMetricValue(cell[field], format))}</td>`;
                      })
                      .join('')}
                  </tr>
                `)
                .join(''),
            )
            .join('')}
        </tbody>
      </table>
    </div>
  `;
}

function renderPlanSection(title, groups, metrics, meta = '') {
  if (!groups.length || !metrics.length) return '';
  return `
    <section class="plan-section">
      <div class="plan-section-title">
        <h3>${escapeHtml(title)}</h3>
        <span class="meta">${escapeHtml(meta)}</span>
      </div>
      ${renderPlanTable(groups, metrics)}
    </section>
  `;
}

function metricsForDisplay(category, metrics) {
  if (category !== 'administrator') return metrics;
  return metrics.filter((metric) => !ADMIN_HIDDEN_METRIC_CODES.has(metric.code));
}

function renderStaffCategorySections(prefix, groups, metricSets, metrics) {
  const sections = [];
  const categoryOrder = ['barber', 'administrator', 'unknown'];
  categoryOrder.forEach((category) => {
    const categoryGroups = groups.filter((group) => (group.category || 'unknown') === category);
    if (!categoryGroups.length) return;
    const categoryMetrics = metricsForDisplay(category, metricSets[category] || metrics);
    const label = categoryGroups[0].category_label || category;
    const title = prefix ? `${prefix} · ${label}` : label;
    sections.push(renderPlanSection(title, categoryGroups, categoryMetrics, `${categoryGroups.length} сотрудников`));
  });
  return sections;
}

function renderSelectedStaffPlanTable(staffPlan) {
  if (!staffPlan?.metrics?.length) return '';
  return `
    <section class="plan-section selected-staff-plan">
      <div class="plan-section-title">
        <h3>План сотрудника: ${escapeHtml(staffPlan.title || 'сотрудник')}</h3>
        <span class="meta">${escapeHtml(staffPlan.category_label || '')}</span>
      </div>
      <div class="table-scroll staff-plan-scroll">
        <table class="staff-plan-table">
          <thead>
            <tr>
              <th>KPI</th>
              <th class="number">План</th>
              <th class="number">Факт</th>
              <th class="number">% выполнения</th>
            </tr>
          </thead>
          <tbody>
            ${staffPlan.metrics
              .map(
                (metric) => `
                  <tr>
                    <td>${escapeHtml(metric.label)}</td>
                    <td class="number">${escapeHtml(formatMetricValue(metric.plan, metric.format))}</td>
                    <td class="number">${escapeHtml(formatMetricValue(metric.fact, metric.format))}</td>
                    <td class="number metric-status ${escapeHtml(metric.status || 'no-plan')}">
                      ${escapeHtml(formatMetricValue(metric.completion_pct, 'percent'))}
                    </td>
                  </tr>
                `,
              )
              .join('')}
          </tbody>
        </table>
      </div>
    </section>
  `;
}

function renderRankingList(rows, format) {
  if (!rows?.length) return '<div class="empty compact">Нет сотрудников за выбранный период</div>';
  return `
    <ol class="ranking-list">
      ${rows
        .map((row, index) => `
          <li>
            <span class="rank">${index + 1}</span>
            <span class="name">${escapeHtml(row.title || `Сотрудник ${row.staff_id || ''}`)}</span>
            <span class="score">${escapeHtml(formatMetricValue(row.value, format))}</span>
          </li>
        `)
        .join('')}
    </ol>
  `;
}

function chartValue(value) {
  return Number(value || 0);
}

function renderPlanInsights(planFact) {
  destroyChart('selectedStaffPlan');
  destroyChart('goodsKpi');
  if (!els.planInsights) return;

  const rankings = planFact?.staff_rankings || {};
  const goodsKpis = planFact?.goods_kpi_execution || [];
  const selectedStaffPlan = planFact?.selected_staff_plan;
  const hasSelectedStaff = Boolean(planFact?.selected_staff);
  const panels = [];

  if (selectedStaffPlan?.metrics?.length) {
    panels.push(`
      <div class="panel wide">
        <div class="panel-title">
          <h2>План vs факт: ${escapeHtml(selectedStaffPlan.title || 'сотрудник')}</h2>
          <span class="meta">${escapeHtml(selectedStaffPlan.category_label || '')}</span>
        </div>
        <div class="chart-box short"><canvas id="selected-staff-plan-chart"></canvas></div>
      </div>
    `);
  }

  if (!hasSelectedStaff) {
    panels.push(`
      <div class="panel">
        <div class="panel-title">
          <h2>Топ-5 по выручке</h2>
          <span class="meta">${formatNumber(rankings.revenue_top?.length || 0)} сотрудников</span>
        </div>
        ${renderRankingList(rankings.revenue_top || [], 'money')}
      </div>
    `);
    panels.push(`
      <div class="panel">
        <div class="panel-title">
          <h2>Топ-5 по СЧ</h2>
          <span class="meta">${formatNumber(rankings.avg_check_top?.length || 0)} сотрудников</span>
        </div>
        ${renderRankingList(rankings.avg_check_top || [], 'money')}
      </div>
    `);
  }

  if (goodsKpis.length) {
    panels.push(`
      <div class="panel wide">
        <div class="panel-title">
          <h2>Выполнение товарных KPI</h2>
          <span class="meta">${goodsKpis.length} KPI</span>
        </div>
        <div class="chart-box short"><canvas id="goods-kpi-chart"></canvas></div>
      </div>
    `);
  }

  els.planInsights.innerHTML = panels.join('');

  const selectedCanvas = document.getElementById('selected-staff-plan-chart');
  if (selectedCanvas && selectedStaffPlan?.metrics?.length) {
    const visibleMetrics = selectedStaffPlan.metrics.filter(
      (metric) => (metric.plan !== null && metric.plan !== undefined) || chartValue(metric.fact) !== 0,
    );
    charts.selectedStaffPlan = new Chart(selectedCanvas, {
      type: 'bar',
      data: {
        labels: visibleMetrics.map((metric) => metric.label),
        datasets: [
          {
            label: 'План',
            data: visibleMetrics.map((metric) => chartValue(metric.plan)),
            backgroundColor: '#94a3b8',
            borderRadius: 4,
          },
          {
            label: 'Факт',
            data: visibleMetrics.map((metric) => chartValue(metric.fact)),
            backgroundColor: '#0f766e',
            borderRadius: 4,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: { y: { beginAtZero: true } },
      },
    });
  }

  const goodsCanvas = document.getElementById('goods-kpi-chart');
  if (goodsCanvas && goodsKpis.length) {
    charts.goodsKpi = new Chart(goodsCanvas, {
      type: 'bar',
      data: {
        labels: goodsKpis.map((metric) => metric.label),
        datasets: [
          {
            label: 'План',
            data: goodsKpis.map((metric) => chartValue(metric.plan)),
            backgroundColor: '#94a3b8',
            borderRadius: 4,
          },
          {
            label: 'Факт',
            data: goodsKpis.map((metric) => chartValue(metric.fact)),
            backgroundColor: '#b45309',
            borderRadius: 4,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: { y: { beginAtZero: true } },
        plugins: {
          tooltip: {
            callbacks: {
              afterBody: (items) => {
                const item = goodsKpis[items[0]?.dataIndex];
                return item ? [`Выполнение: ${formatMetricValue(item.completion_pct, 'percent')}`] : [];
              },
            },
          },
        },
      },
    });
  }
}

function renderPlanDiagnostics(diagnostics) {
  if (!diagnostics?.length) return '';
  return `
    <div class="plan-diagnostics">
      ${diagnostics
        .map((item) => {
          const details = [
            `барберы: ${formatNumber(item.barber_clients_fact)}`,
            `администраторы: ${formatNumber(item.administrator_clients_fact)}`,
            `записей без корректного администратора: ${formatNumber(item.unassigned_records_count)}`,
          ];
          return `
            <div class="diagnostic warning">
              <strong>${escapeHtml(item.message || 'Проверка данных')}</strong>
              <span>${escapeHtml(details.join(' · '))}</span>
            </div>
          `;
        })
        .join('')}
    </div>
  `;
}

function renderPlanFact(planFact) {
  const groups = planFact?.groups || [];
  const metrics = planFact?.metrics || [];
  const diagnosticsHtml = renderPlanDiagnostics(planFact?.diagnostics || []);
  if (!groups.length && !planFact?.parent_group) {
    renderPlanInsights(null);
    if (els.planInsights) els.planInsights.innerHTML = '';
    els.planFactTable.innerHTML = `${diagnosticsHtml}<div class="empty">Нет плана за выбранный период</div>`;
    els.planMeta.textContent = '';
    return;
  }
  renderPlanInsights(planFact);

  const metricSets = planFact?.metric_sets || {};
  if (planFact?.view_scope === 'staff') {
    const sections = [];
    if (planFact.selected_staff_plan) {
      sections.push(renderSelectedStaffPlanTable(planFact.selected_staff_plan));
    }

    if (planFact.parent_group) {
      const branchTitle = planFact.branch?.title || planFact.parent_group.title || 'Филиал';
      sections.push(renderPlanSection(branchTitle, [planFact.parent_group], metricSets.branch || metrics));
    }

    sections.push(...renderStaffCategorySections('', groups, metricSets, metrics));

    els.planFactTable.innerHTML = diagnosticsHtml + (
      sections.join('') || '<div class="empty">Нет сотрудников для выбранного филиала</div>'
    );
  } else {
    els.planFactTable.innerHTML = diagnosticsHtml + renderPlanTable(groups, metrics);
  }

  const planPeriod = planFact?.plan_period;
  const planPeriodText = planPeriod ? ` · план ${planPeriod.start} .. ${planPeriod.end}` : '';
  const selectedStaff = planFact?.selected_staff;
  const scopeText = planFact?.view_scope === 'staff'
    ? `${planFact.branch?.title || 'Филиал'} · ${selectedStaff?.name || 'сотрудники'}`
    : 'сеть и филиалы';
  els.planMeta.textContent = `${scopeText} · ${groups.length} строк${planPeriodText}`;
}

function renderPlanSettingInput(scope, row, field) {
  return `
    <input
      type="text"
      inputmode="decimal"
      data-plan-${scope}
      data-company-id="${escapeHtml(row.company_id)}"
      ${row.staff_id ? `data-staff-id="${escapeHtml(row.staff_id)}"` : ''}
      data-field="${escapeHtml(field)}"
      value="${escapeHtml(formatInputNumber(row[field]))}"
    />
  `;
}

function renderPlanSettingsBranches(rows) {
  if (!rows.length) {
    els.planSettingsBranches.innerHTML = '<div class="empty compact">Нет филиалов</div>';
    return;
  }
  const fields = [
    ['wax_pct', 'Воск, %'],
    ['head_care_pct', 'Уход голова, %'],
    ['face_care_pct', 'Уход лицо, %'],
    ['camouflage_pct', 'Камуфляж, %'],
    ['cosmo_pct', 'Космо, %'],
    ['opz_pct', 'ОПЗ, %'],
    ['cosmo_price', 'Цена космо'],
  ];
  els.planSettingsBranchMeta.textContent = `${rows.length} филиалов`;
  els.planSettingsBranches.innerHTML = `
    <div class="table-scroll">
      <table class="plan-settings-table">
        <thead>
          <tr>
            <th>Филиал</th>
            ${fields.map(([, label]) => `<th class="number">${escapeHtml(label)}</th>`).join('')}
          </tr>
        </thead>
        <tbody>
          ${rows
            .map((row) => `
              <tr>
                <td>${escapeHtml(row.company_title || `Филиал ${row.company_id}`)}</td>
                ${fields.map(([field]) => `<td class="number">${renderPlanSettingInput('branch', row, field)}</td>`).join('')}
              </tr>
            `)
            .join('')}
        </tbody>
      </table>
    </div>
  `;
}

function renderPlanSettingsStaffSection(title, rows, fields) {
  if (!rows.length) return '';
  return `
    <section class="plan-section">
      <div class="plan-section-title">
        <h3>${escapeHtml(title)}</h3>
        <span class="meta">${rows.length} сотрудников</span>
      </div>
      <div class="table-scroll">
        <table class="plan-settings-table">
          <thead>
            <tr>
              <th>Филиал</th>
              <th>Имя</th>
              <th class="number">staff_id</th>
              <th class="number">user_id</th>
              ${fields.map(([, label]) => `<th class="number">${escapeHtml(label)}</th>`).join('')}
            </tr>
          </thead>
          <tbody>
            ${rows
              .map((row) => `
                <tr>
                  <td>${escapeHtml(row.company_title || `Филиал ${row.company_id}`)}</td>
                  <td>${escapeHtml(row.staff_name || `Сотрудник ${row.staff_id}`)}</td>
                  <td class="number readonly">${escapeHtml(row.staff_id)}</td>
                  <td class="number readonly">${escapeHtml(row.user_id || '')}</td>
                  ${fields.map(([field]) => `<td class="number">${renderPlanSettingInput('staff', row, field)}</td>`).join('')}
                </tr>
              `)
              .join('')}
          </tbody>
        </table>
      </div>
    </section>
  `;
}

function renderPlanSettingsStaff(rows) {
  const barbers = rows.filter((row) => row.staff_category === 'barber');
  const admins = rows.filter((row) => row.staff_category === 'administrator');
  els.planSettingsStaffMeta.textContent = `${barbers.length} барберов · ${admins.length} администраторов`;
  els.planSettingsStaff.innerHTML = [
    renderPlanSettingsStaffSection('Барберы', barbers, [
      ['clients', 'Клиентов'],
      ['avg_check_total', 'СЧ общий'],
    ]),
    renderPlanSettingsStaffSection('Администраторы', admins, [
      ['clients', 'Клиентов'],
      ['reviews_qty', 'Отзывы'],
      ['cosmo_qty', 'Космо шт'],
    ]),
  ].join('') || '<div class="empty compact">Нет активных сотрудников</div>';
}

function setPlanSettingsDirty(isDirty) {
  planSettingsDirty = isDirty;
  els.planSettingsDirty.classList.toggle('visible', isDirty);
  els.planSettingsSave.disabled = !planSettingsData;
  els.planSettingsReset.disabled = !isDirty || !planSettingsSavedData;
}

function planSettingsInputValue(selector) {
  const input = document.querySelector(selector);
  const value = input?.value.trim() || '';
  return value === '' ? null : value;
}

function collectPlanSettingsPayload() {
  const branches = (planSettingsData?.branches || []).map((row) => ({
    company_id: Number(row.company_id),
    wax_pct: planSettingsInputValue(`input[data-plan-branch][data-company-id="${row.company_id}"][data-field="wax_pct"]`),
    head_care_pct: planSettingsInputValue(`input[data-plan-branch][data-company-id="${row.company_id}"][data-field="head_care_pct"]`),
    face_care_pct: planSettingsInputValue(`input[data-plan-branch][data-company-id="${row.company_id}"][data-field="face_care_pct"]`),
    camouflage_pct: planSettingsInputValue(`input[data-plan-branch][data-company-id="${row.company_id}"][data-field="camouflage_pct"]`),
    cosmo_pct: planSettingsInputValue(`input[data-plan-branch][data-company-id="${row.company_id}"][data-field="cosmo_pct"]`),
    opz_pct: planSettingsInputValue(`input[data-plan-branch][data-company-id="${row.company_id}"][data-field="opz_pct"]`),
    cosmo_price: planSettingsInputValue(`input[data-plan-branch][data-company-id="${row.company_id}"][data-field="cosmo_price"]`),
  }));
  const staff = (planSettingsData?.staff || []).map((row) => ({
    company_id: Number(row.company_id),
    staff_id: Number(row.staff_id),
    staff_category: row.staff_category,
    clients: planSettingsInputValue(`input[data-plan-staff][data-staff-id="${row.staff_id}"][data-field="clients"]`),
    avg_check_total: planSettingsInputValue(`input[data-plan-staff][data-staff-id="${row.staff_id}"][data-field="avg_check_total"]`),
    reviews_qty: planSettingsInputValue(`input[data-plan-staff][data-staff-id="${row.staff_id}"][data-field="reviews_qty"]`),
    cosmo_qty: planSettingsInputValue(`input[data-plan-staff][data-staff-id="${row.staff_id}"][data-field="cosmo_qty"]`),
  }));
  return {
    month: els.planSettingsMonth.value,
    branches,
    staff,
  };
}

function updatePlanSettingsDirtyFromForm() {
  if (!planSettingsData) return;
  setPlanSettingsDirty(JSON.stringify(collectPlanSettingsPayload()) !== planSettingsSavedSnapshot);
}

function renderPlanSettings(data, { updateSnapshot = true, dirty = false } = {}) {
  planSettingsData = data;
  els.planSettingsMonth.value = data.month;
  planSettingsLoadedMonth = data.month;
  renderPlanSettingsBranches(data.branches || []);
  renderPlanSettingsStaff(data.staff || []);
  els.planSettingsSaved.textContent = data.last_saved_at
    ? `Последнее сохранение: ${formatMoscowDateTime(data.last_saved_at)}`
    : 'Последнее сохранение: нет';

  if (updateSnapshot) {
    planSettingsSavedData = JSON.parse(JSON.stringify(data));
    planSettingsSavedSnapshot = JSON.stringify(collectPlanSettingsPayload());
    setPlanSettingsDirty(false);
  } else {
    setPlanSettingsDirty(dirty);
  }
}

function confirmDiscardPlanSettings() {
  return !planSettingsDirty || window.confirm('Есть несохранённые изменения. Перейти без сохранения?');
}

function confirmDiscardServiceManagement() {
  return !serviceManagementDirty || window.confirm('Есть несохранённые изменения в услугах. Перейти без сохранения?');
}

function setPlanSettingsLoading(isLoading) {
  els.planSettingsLoad.disabled = isLoading;
  els.planSettingsCopy.disabled = isLoading;
  els.planSettingsSave.disabled = isLoading || !planSettingsData;
  els.planSettingsReset.disabled = isLoading || !planSettingsDirty || !planSettingsSavedData;
  els.planSettingsLoad.textContent = isLoading ? 'Загрузка' : 'Загрузить';
  els.planSettingsSave.textContent = isLoading ? 'Сохранение' : 'Сохранить';
}

async function loadPlanSettings({ month = els.planSettingsMonth.value, copyFrom = null, dirty = false } = {}) {
  clearError();
  setPlanSettingsLoading(true);
  setApiState('API: загрузка', 'warn');
  try {
    const params = { month };
    if (copyFrom) params.copy_from = copyFrom;
    const payload = await fetchJson('/dashboard/plan/settings', params);
    renderPlanSettings(payload.data, { updateSnapshot: !copyFrom, dirty });
    setApiState('API: подключен', 'ok');
    await loadSyncStatus();
  } catch (error) {
    showError(error.message);
  } finally {
    setPlanSettingsLoading(false);
  }
}

async function savePlanSettings() {
  clearError();
  setPlanSettingsLoading(true);
  setApiState('API: сохранение', 'warn');
  try {
    const payload = await postJson('/dashboard/plan/settings', collectPlanSettingsPayload());
    renderPlanSettings(payload.data);
    setApiState('API: подключен', 'ok');
  } catch (error) {
    showError(error.message);
  } finally {
    setPlanSettingsLoading(false);
  }
}

async function copyPreviousPlanSettings() {
  if (!confirmDiscardPlanSettings()) return;
  const month = els.planSettingsMonth.value || currentMonthValue();
  await loadPlanSettings({ month, copyFrom: previousMonthValue(month), dirty: true });
}

async function reloadPlanSettingsMonth() {
  if (!confirmDiscardPlanSettings()) {
    els.planSettingsMonth.value = planSettingsLoadedMonth || currentMonthValue();
    return;
  }
  await loadPlanSettings({ month: els.planSettingsMonth.value || currentMonthValue() });
}

function renderServiceBranchOptions() {
  const selected = els.serviceFilterBranch.value;
  els.serviceFilterBranch.innerHTML = '<option value="">Все филиалы</option>';
  branchOptions.forEach((branch) => {
    const option = document.createElement('option');
    option.value = branch.id;
    option.textContent = branch.title;
    els.serviceFilterBranch.appendChild(option);
  });
  els.serviceFilterBranch.value = branchOptions.some((branch) => String(branch.id) === selected) ? selected : '';
}

function renderServiceFilterOptions(data) {
  const selectedCategory = els.serviceFilterCategory.value;
  els.serviceFilterCategory.innerHTML = '<option value="">Все категории</option>';
  (data.categories || []).forEach((category) => {
    const option = document.createElement('option');
    option.value = category;
    option.textContent = category;
    els.serviceFilterCategory.appendChild(option);
  });
  els.serviceFilterCategory.value = (data.categories || []).includes(selectedCategory) ? selectedCategory : '';

  const selectedGroup = els.serviceFilterGroup.value;
  els.serviceFilterGroup.innerHTML = '<option value="">Все группы</option>';
  (data.groups || []).forEach((group) => {
    const option = document.createElement('option');
    option.value = group.id;
    option.textContent = group.is_active ? group.title : `${group.title} (архив)`;
    els.serviceFilterGroup.appendChild(option);
  });
  els.serviceFilterGroup.value = (data.groups || []).some((group) => String(group.id) === selectedGroup) ? selectedGroup : '';
}

function serviceGroupOptionsHtml(selectedGroupId) {
  const groups = serviceManagementData.groups || [];
  const selected = selectedGroupId === null || selectedGroupId === undefined ? '' : String(selectedGroupId);
  const assignedInactive = groups.find((group) => String(group.id) === selected && !group.is_active);
  const activeGroups = groups.filter((group) => group.is_active);
  const options = ['<option value="">Без группы</option>'];
  activeGroups.forEach((group) => {
    options.push(`<option value="${escapeHtml(group.id)}" ${String(group.id) === selected ? 'selected' : ''}>${escapeHtml(group.title)}</option>`);
  });
  if (assignedInactive) {
    options.push(`<option value="${escapeHtml(assignedInactive.id)}" selected disabled>${escapeHtml(assignedInactive.title)} (архив)</option>`);
  }
  return options.join('');
}

function renderServiceCatalog(rows) {
  els.serviceCatalogMeta.textContent = `${rows.length} услуг`;
  if (!rows.length) {
    els.serviceCatalogTable.innerHTML = '<div class="empty compact">Нет услуг по выбранным фильтрам</div>';
    return;
  }
  els.serviceCatalogTable.innerHTML = `
    <div class="table-scroll">
      <table class="service-table">
        <thead>
          <tr>
            <th>Филиал</th>
            <th>Категория</th>
            <th class="number">ID</th>
            <th>Название</th>
            <th>Доп услуга</th>
            <th>KPI-группа</th>
            <th>Обновлено</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((row) => `
            <tr>
              <td>${escapeHtml(row.company_title || `Филиал ${row.company_id}`)}</td>
              <td>${escapeHtml(row.category_title || '')}</td>
              <td class="number readonly">${escapeHtml(row.service_id)}</td>
              <td>${escapeHtml(row.title)}</td>
              <td>
                <input
                  class="service-extra-input"
                  type="checkbox"
                  data-company-id="${escapeHtml(row.company_id)}"
                  data-service-id="${escapeHtml(row.service_id)}"
                  ${row.is_extra ? 'checked' : ''}
                />
              </td>
              <td>
                <select
                  class="service-group-select"
                  data-company-id="${escapeHtml(row.company_id)}"
                  data-service-id="${escapeHtml(row.service_id)}"
                >
                  ${serviceGroupOptionsHtml(row.kpi_group_id)}
                </select>
              </td>
              <td>${escapeHtml(formatMoscowDateTime(row.label_updated_at || row.kpi_assignment_updated_at || row.updated_at) || '')}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function renderServiceKpiGroups(groups) {
  els.serviceKpiGroupsMeta.textContent = `${groups.length} групп`;
  if (!groups.length) {
    els.serviceKpiGroupsTable.innerHTML = '<div class="empty compact">Нет KPI-групп</div>';
    return;
  }
  els.serviceKpiGroupsTable.innerHTML = `
    <div class="table-scroll">
      <table class="service-group-table">
        <thead>
          <tr>
            <th>Название</th>
            <th>Код</th>
            <th>Описание</th>
            <th class="number">Порядок</th>
            <th>Активна</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          ${groups.map((group) => `
            <tr data-service-group-row data-group-id="${escapeHtml(group.id)}">
              <td><input type="text" data-group-field="title" value="${escapeHtml(group.title)}" /></td>
              <td><input type="text" data-group-field="code" value="${escapeHtml(group.code)}" /></td>
              <td><input type="text" data-group-field="description" value="${escapeHtml(group.description || '')}" /></td>
              <td class="number"><input type="number" data-group-field="sort_order" value="${escapeHtml(group.sort_order || 0)}" /></td>
              <td><input class="service-group-active" type="checkbox" data-group-field="is_active" ${group.is_active ? 'checked' : ''} /></td>
              <td class="number"><button type="button" class="secondary" data-service-group-archive>В архив</button></td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function collectServiceManagementPayload() {
  const groupSelects = new Map(
    [...els.serviceCatalogTable.querySelectorAll('.service-group-select')].map((select) => [
      `${select.dataset.companyId}:${select.dataset.serviceId}`,
      select.value ? Number(select.value) : null,
    ]),
  );
  const rows = [...els.serviceCatalogTable.querySelectorAll('.service-extra-input')].map((input) => {
    const key = `${input.dataset.companyId}:${input.dataset.serviceId}`;
    return {
      company_id: Number(input.dataset.companyId),
      service_id: Number(input.dataset.serviceId),
      is_extra: input.checked,
      kpi_group_id: groupSelects.get(key) ?? null,
    };
  });
  const groups = [...els.serviceKpiGroupsTable.querySelectorAll('[data-service-group-row]')].map((row) => {
    const value = (field) => row.querySelector(`[data-group-field="${field}"]`);
    return {
      id: Number(row.dataset.groupId),
      title: value('title')?.value.trim() || '',
      code: value('code')?.value.trim() || '',
      description: value('description')?.value.trim() || '',
      sort_order: Number(value('sort_order')?.value || 0),
      is_active: Boolean(value('is_active')?.checked),
    };
  });
  return { rows, groups };
}

function setServiceManagementDirty(isDirty) {
  serviceManagementDirty = isDirty;
  els.serviceManagementDirty.classList.toggle('visible', isDirty);
  els.serviceManagementSave.disabled = !serviceManagementData;
  els.serviceManagementReset.disabled = !isDirty || !serviceManagementSavedData;
}

function updateServiceManagementDirtyFromForm() {
  setServiceManagementDirty(JSON.stringify(collectServiceManagementPayload()) !== serviceManagementSavedSnapshot);
}

function renderServiceManagement(data, { updateSnapshot = true } = {}) {
  serviceManagementData = data || { rows: [], groups: [], categories: [] };
  renderServiceFilterOptions(serviceManagementData);
  renderServiceCatalog(serviceManagementData.rows || []);
  renderServiceKpiGroups(serviceManagementData.groups || []);
  if (updateSnapshot) {
    serviceManagementSavedData = JSON.parse(JSON.stringify(serviceManagementData));
    serviceManagementSavedSnapshot = JSON.stringify(collectServiceManagementPayload());
    setServiceManagementDirty(false);
  } else {
    updateServiceManagementDirtyFromForm();
  }
}

function serviceManagementParams() {
  return {
    company_id: els.serviceFilterBranch.value,
    category: els.serviceFilterCategory.value,
    kpi_group_id: els.serviceFilterGroup.value,
    q: els.serviceFilterQuery.value.trim(),
    is_extra: els.serviceFilterExtra.checked ? true : undefined,
  };
}

function setServiceManagementLoading(isLoading) {
  els.serviceFilterLoad.disabled = isLoading;
  els.serviceManagementSave.disabled = isLoading || !serviceManagementData;
  els.serviceManagementReset.disabled = isLoading || !serviceManagementDirty || !serviceManagementSavedData;
  els.serviceGroupAdd.disabled = isLoading;
  els.serviceFilterLoad.textContent = isLoading ? 'Загрузка' : 'Обновить';
  els.serviceManagementSave.textContent = isLoading ? 'Сохранение' : 'Сохранить';
}

async function loadServiceManagement() {
  clearError();
  setServiceManagementLoading(true);
  setApiState('API: загрузка', 'warn');
  try {
    const payload = await fetchJson('/dashboard/services', serviceManagementParams());
    renderServiceManagement(payload.data);
    setApiState('API: подключен', 'ok');
    await loadSyncStatus();
  } catch (error) {
    showError(error.message);
  } finally {
    setServiceManagementLoading(false);
  }
}

async function saveServiceManagement() {
  clearError();
  setServiceManagementLoading(true);
  setApiState('API: сохранение', 'warn');
  const current = collectServiceManagementPayload();
  const saved = serviceManagementSavedSnapshot ? JSON.parse(serviceManagementSavedSnapshot) : { rows: [], groups: [] };
  const savedRows = new Map(saved.rows.map((row) => [`${row.company_id}:${row.service_id}`, row]));
  const savedGroups = new Map(saved.groups.map((group) => [group.id, group]));
  try {
    for (const row of current.rows) {
      const previous = savedRows.get(`${row.company_id}:${row.service_id}`);
      if (!previous || previous.is_extra !== row.is_extra) {
        await patchJson(`/dashboard/services/${row.company_id}/${row.service_id}/labels`, { is_extra: row.is_extra });
      }
      if (!previous || previous.kpi_group_id !== row.kpi_group_id) {
        await patchJson(`/dashboard/services/${row.company_id}/${row.service_id}/kpi_group`, { group_id: row.kpi_group_id });
      }
    }
    for (const group of current.groups) {
      const previous = savedGroups.get(group.id);
      if (!previous || JSON.stringify(previous) !== JSON.stringify(group)) {
        await patchJson(`/dashboard/services/kpi_groups/${group.id}`, group);
      }
    }
    await loadServiceManagement();
    setApiState('API: подключен', 'ok');
  } catch (error) {
    showError(error.message);
  } finally {
    setServiceManagementLoading(false);
  }
}

async function addServiceKpiGroup() {
  clearError();
  const title = els.serviceGroupTitle.value.trim();
  if (!title) {
    showError('Название KPI-группы обязательно');
    return;
  }
  setServiceManagementLoading(true);
  setApiState('API: сохранение', 'warn');
  try {
    await postJson('/dashboard/services/kpi_groups', {
      title,
      code: els.serviceGroupCode.value.trim() || null,
      description: els.serviceGroupDescription.value.trim() || null,
      is_active: true,
    });
    els.serviceGroupTitle.value = '';
    els.serviceGroupCode.value = '';
    els.serviceGroupDescription.value = '';
    await loadServiceManagement();
    setApiState('API: подключен', 'ok');
  } catch (error) {
    showError(error.message);
  } finally {
    setServiceManagementLoading(false);
  }
}

function renderReviewFactEditor(data) {
  reviewFactRows = data?.rows || [];
  const totalValue = data?.total_value || 0;
  els.reviewFactMeta.textContent = `${reviewFactRows.length} администраторов · ${formatNumber(totalValue)} отзывов`;

  if (!reviewFactRows.length) {
    els.reviewFactEditor.innerHTML = '<div class="empty compact">Нет активных администраторов</div>';
    els.reviewFactSave.disabled = true;
    return;
  }

  els.reviewFactSave.disabled = false;
  els.reviewFactEditor.innerHTML = `
    <div class="table-scroll review-fact-scroll">
      <table class="review-fact-table">
        <thead>
          <tr>
            <th>Филиал</th>
            <th>Администратор</th>
            <th class="number">Отзывы факт</th>
          </tr>
        </thead>
        <tbody>
          ${reviewFactRows
            .map((row) => {
              return `
                <tr>
                  <td>${escapeHtml(row.company_title || `Филиал ${row.company_id}`)}</td>
                  <td>${escapeHtml(row.staff_name)}</td>
                  <td class="number">
                    <input
                      type="number"
                      min="0"
                      step="1"
                      inputmode="numeric"
                      data-company-id="${escapeHtml(row.company_id)}"
                      data-staff-id="${escapeHtml(row.staff_id)}"
                      value="${escapeHtml(formatInputNumber(row.value))}"
                    />
                  </td>
                </tr>
              `;
            })
            .join('')}
        </tbody>
      </table>
    </div>
  `;
}

async function loadReviewFactEditor() {
  const filter = filterEls.reviewFacts;
  const payload = await fetchJson('/dashboard/plan/reviews_fact', filterParams(filter));
  renderReviewFactEditor(payload.data);
}

function reviewFactPayload() {
  const filter = filterEls.reviewFacts;
  const items = [...els.reviewFactEditor.querySelectorAll('input[data-staff-id]')].map((input) => {
    const rawValue = input.value.trim().replace(',', '.');
    if (rawValue === '') {
      return {
        company_id: Number(input.dataset.companyId),
        staff_id: Number(input.dataset.staffId),
        value: null,
      };
    }
    const value = Number(rawValue);
    if (!Number.isFinite(value) || value < 0) {
      throw new Error('Отзывы факт должны быть неотрицательным числом');
    }
    return {
      company_id: Number(input.dataset.companyId),
      staff_id: Number(input.dataset.staffId),
      value,
    };
  });

  return {
    start_date: filter.start.value,
    end_date: filter.end.value,
    company_id: filter.branch.value ? Number(filter.branch.value) : null,
    staff_id: filter.staff.value ? Number(filter.staff.value) : null,
    items,
  };
}

async function saveReviewFactEditor() {
  clearError();
  els.reviewFactSave.disabled = true;
  els.reviewFactSave.textContent = 'Сохранение';
  setApiState('API: сохранение', 'warn');

  try {
    const payload = await postJson('/dashboard/plan/reviews_fact', reviewFactPayload());
    renderReviewFactEditor(payload.data);
    setApiState('API: подключен', 'ok');
  } catch (error) {
    showError(error.message);
  } finally {
    els.reviewFactSave.textContent = 'Сохранить факт';
    els.reviewFactSave.disabled = !reviewFactRows.length;
  }
}

function renderBundle(bundle) {
  const {
    summary,
    revenue_daily: daily = [],
    top_services: services = [],
    extra_services: extraServices = [],
  } = bundle;
  renderKpi(summary);
  renderVisitMetrics(summary);
  renderAppointmentsMetrics(summary);
  renderRevenueChart(daily);
  renderAppointmentsChart(daily);
  renderOpzChart(daily);
  renderServicesChart(services.slice(0, 8));
  renderServicesTable(services);
  renderExtraServicesTable(extraServices);

  els.periodLabel.textContent = `${summary.period.start} .. ${summary.period.end}`;
  els.revenueMeta.textContent = `${daily.length} дней`;
  const appointmentsBreakdown = summary.appointments_breakdown || {};
  els.appointmentsMeta.textContent = appointmentsBreakdown.source_status === 'ready'
    ? `${formatNumber(appointmentsBreakdown.total)} записей`
    : 'Нет точных данных';
  els.servicesMeta.textContent = `${services.length} услуг`;
  els.extraServicesMeta.textContent = `${formatNumber(summary.revenue.extra_service_count)} оказано`;
  els.tableMeta.textContent = `${formatMoney(summary.revenue.total)} всего`;
}

async function loadBranches() {
  try {
    const payload = await fetchJson('/dashboard/branches');
    branchOptions = payload.data || [];
    Object.values(filterEls).forEach((filter) => renderBranchOptions(filter));
    renderServiceBranchOptions();
  } catch (error) {
    showError(error.message);
  }
}

function renderBranchOptions(filter) {
  const selected = filter.branch.value;
  filter.branch.innerHTML = '<option value="">Все филиалы</option>';
  branchOptions.forEach((branch) => {
    const option = document.createElement('option');
    option.value = branch.id;
    option.textContent = branch.title;
    filter.branch.appendChild(option);
  });
  filter.branch.value = branchOptions.some((branch) => String(branch.id) === selected) ? selected : '';
  customFilterDropdowns[filter.branch.id]?.refresh();
}

async function loadStaff(filter) {
  const selected = filter.staff.value;
  try {
    const payload = await fetchJson('/dashboard/staff', {
      company_id: filter.branch.value,
    });
    const staffOptions = payload.data || [];
    const defaultLabel = filter === filterEls.reviewFacts ? 'Все сотрудники' : 'Все работники';
    filter.staff.innerHTML = `<option value="">${defaultLabel}</option>`;
    staffOptions.forEach((staff) => {
      const option = document.createElement('option');
      option.value = staff.id;
      option.textContent = filter.branch.value
        ? staff.name
        : `${staff.name} · ${staff.company_title || `Филиал ${staff.company_id}`}`;
      filter.staff.appendChild(option);
    });
    filter.staff.value = staffOptions.some((staff) => String(staff.id) === selected) ? selected : '';
    customFilterDropdowns[filter.staff.id]?.refresh();
  } catch (error) {
    showError(error.message);
  }
}

function filterParams(filter) {
  return {
    start_date: filter.start.value,
    end_date: filter.end.value,
    company_id: filter.branch.value,
    staff_id: filter.staff.value,
  };
}

function setFilterLoading(filter, isLoading) {
  filter.load.disabled = isLoading;
  filter.load.textContent = isLoading ? 'Загрузка' : 'Обновить';
}

async function loadSyncStatus() {
  try {
    const payload = await fetchJson('/dashboard/widget/sync_status');
    const sync = payload.data?.sync || {};
    const lastRun = sync.last_run;
    const lastSuccessfulAt = sync.last_successful_sync_at
      || (lastRun?.status === 'success' ? lastRun.finished_at : null);
    els.syncState.textContent = lastSuccessfulAt
      ? `данные актуальны на ${formatMoscowDateTime(lastSuccessfulAt)}`
      : 'данные актуальны: нет успешных обновлений';
  } catch {
    els.syncState.textContent = 'данные актуальны: статус недоступен';
  }
}

function viewFromLocation() {
  if (window.location.pathname.replace(/\/+$/, '') === '/reports' || window.location.pathname.startsWith('/reports/')) return 'reports';
  if (window.location.hash === '#plan-fact') return 'plan';
  if (window.location.hash === '#plan-settings') return 'planSettings';
  if (window.location.hash === '#services') return 'serviceManagement';
  if (window.location.hash === '#review-facts') return 'reviewFacts';
  return 'overview';
}

function setActiveView(view) {
  activeView = view;
  els.overviewView.classList.toggle('active', view === 'overview');
  els.planView.classList.toggle('active', view === 'plan');
  els.planSettingsView.classList.toggle('active', view === 'planSettings');
  els.serviceManagementView.classList.toggle('active', view === 'serviceManagement');
  els.reviewFactsView.classList.toggle('active', view === 'reviewFacts');
  els.reportsView.classList.toggle('active', view === 'reports');
  els.viewLinks.forEach((link) => {
    link.classList.toggle('active', link.dataset.viewLink === view);
  });
  const labels = {
    overview: 'Метрики по филиалам и услугам',
    plan: 'План/факт по филиалам и сотрудникам',
    planSettings: 'Установка планов по месяцам',
    serviceManagement: 'Актуальные услуги и KPI-группы',
    reviewFacts: 'Ручной факт отзывов по администраторам',
    reports: 'Каталог отчетов и аналитика',
  };
  els.periodLabel.textContent = labels[view] || labels.overview;
}

async function loadPlanFact() {
  const filter = filterEls.plan;
  clearError();
  setFilterLoading(filter, true);
  setApiState('API: загрузка', 'warn');

  try {
    const payload = await fetchJson('/dashboard/widget/plan_fact', filterParams(filter));
    renderPlanFact(payload.data);
    setApiState('API: подключен', 'ok');
    await loadSyncStatus();
  } catch (error) {
    showError(error.message);
  } finally {
    setFilterLoading(filter, false);
  }
}

async function loadReviewFacts() {
  const filter = filterEls.reviewFacts;
  clearError();
  setFilterLoading(filter, true);
  setApiState('API: загрузка', 'warn');

  try {
    await loadReviewFactEditor();
    setApiState('API: подключен', 'ok');
    await loadSyncStatus();
  } catch (error) {
    showError(error.message);
  } finally {
    setFilterLoading(filter, false);
  }
}

async function loadDashboard() {
  const filter = filterEls.overview;
  clearError();
  setFilterLoading(filter, true);
  setApiState('API: загрузка', 'warn');

  try {
    const payload = await fetchJson('/dashboard/bundle', filterParams(filter));
    renderBundle(payload.data);
    setApiState('API: подключен', 'ok');
    await loadSyncStatus();
  } catch (error) {
    showError(error.message);
  } finally {
    setFilterLoading(filter, false);
  }
}

async function loadCurrentView() {
  if (activeView === 'plan') {
    await loadPlanFact();
  } else if (activeView === 'planSettings') {
    await loadPlanSettings();
  } else if (activeView === 'serviceManagement') {
    await loadServiceManagement();
  } else if (activeView === 'reviewFacts') {
    await loadReviewFacts();
  } else if (activeView === 'reports') {
    await reportsController?.loadFromLocation();
  } else {
    await loadDashboard();
  }
}

const ROLE_LABELS = {
  platform_admin: 'Platform Admin — платформа',
  owner: 'Owner — владелец сети',
  branch_admin: 'Branch Admin — админ филиала',
  manager: 'Manager — метрики филиала',
  viewer: 'Viewer — только просмотр',
};

function accountDisplayName(user) {
  const fullName = user?.full_name?.trim();
  if (fullName) return fullName;
  return user?.email?.split('@')[0] || '';
}

function tenantOptionLabel(tenant) {
  const branchText = tenant.branch_count === 1 ? '1 филиал' : `${tenant.branch_count || 0} филиалов`;
  return `${tenant.label || `Tenant ${tenant.id}`} · ${branchText}`;
}

async function setupPlatformTenantSelector() {
  selectedTenant = null;
  if (els.tenantSwitcher) {
    els.tenantSwitcher.hidden = true;
  }

  if (currentUser?.role !== 'platform_admin') {
    setSelectedPortalAccountId('');
    return true;
  }

  const payload = await authFetch('/auth/admin/portal-accounts');
  const tenants = payload.data || [];
  if (!els.tenantSwitcher || !els.tenantSelect) {
    return tenants.length > 0;
  }

  els.tenantSwitcher.hidden = false;
  els.tenantSelect.innerHTML = '';
  tenants.forEach((tenant) => {
    const option = document.createElement('option');
    option.value = tenant.id;
    option.textContent = tenantOptionLabel(tenant);
    els.tenantSelect.appendChild(option);
  });

  if (!tenants.length) {
    setSelectedPortalAccountId('');
    els.tenantSelect.disabled = true;
    if (els.tenantMeta) {
      els.tenantMeta.textContent = 'Нет созданных бизнес-сетей';
    }
    showError('Для platform admin нет доступных business tenants.');
    return false;
  }

  const storedTenantId = getSelectedPortalAccountId();
  selectedTenant = tenants.find((tenant) => String(tenant.id) === storedTenantId)
    || tenants.find((tenant) => Number(tenant.branch_count || 0) > 0)
    || tenants[0];
  setSelectedPortalAccountId(selectedTenant.id);
  els.tenantSelect.disabled = false;
  els.tenantSelect.value = String(selectedTenant.id);
  if (els.tenantMeta) {
    els.tenantMeta.textContent = selectedTenant.branch_count
      ? 'Dashboard показывает данные выбранной сети'
      : 'У выбранной сети пока нет филиалов';
  }

  if (!selectedTenant.branch_count) {
    showError('У выбранной business-сети нет подключенных филиалов.');
    return false;
  }
  return true;
}

async function init() {
  reportsController = initReports({ clearError, showError, setApiState });
  Object.values(filterEls).forEach((filter) => defaultDates(filter));
  setReviewFactDefaultDates();
  els.overviewPresetButtons.forEach((button) => {
    button.classList.toggle('active', button.dataset.overviewPreset === 'month');
  });
  els.planSettingsMonth.value = currentMonthValue();
  renderServicesTable([]);
  renderExtraServicesTable([]);
  renderServiceManagement({ rows: [], groups: [], categories: [] });
  if (getToken()) {
    let me;
    try {
      me = await loadCurrentUser();
    } catch {
      logout();
      return;
    }

    currentUser = me.data;
    const profileLink = document.getElementById('user-profile-link');
    const profileName = document.getElementById('user-profile-name');
    const profileRole = document.getElementById('user-profile-role');
    if (profileLink) {
      profileLink.hidden = false;
    }
    if (profileName) {
      profileName.textContent = accountDisplayName(me.data);
    }
    if (profileRole) {
      profileRole.textContent = ROLE_LABELS[me.data.role] || me.data.role;
    }

    try {
      const canLoadTenantData = await setupPlatformTenantSelector();
      if (!canLoadTenantData) {
        setApiState('API: нет tenant', 'warn');
        return;
      }
    } catch (error) {
      showError(error.message);
      setApiState('API: tenant ошибка', 'error');
      return;
    }
  } else if (!apiKey) {
    window.location.href = '/login.html';
    return;
  } else {
    setSelectedPortalAccountId('');
  }
  await loadBranches();
  await Promise.all(Object.values(filterEls).map((filter) => loadStaff(filter)));
  setActiveView(viewFromLocation());
  await loadCurrentView();
}

filterEls.overview.load.addEventListener('click', () => loadDashboard());
els.tenantSelect?.addEventListener('change', () => {
  setSelectedPortalAccountId(els.tenantSelect.value);
  window.location.reload();
});
filterEls.overview.start.addEventListener('change', () => {
  els.overviewPresetButtons.forEach((button) => button.classList.remove('active'));
});
filterEls.overview.end.addEventListener('change', () => {
  els.overviewPresetButtons.forEach((button) => button.classList.remove('active'));
});
filterEls.overview.branch.addEventListener('change', async () => {
  await loadStaff(filterEls.overview);
  await loadDashboard();
});
filterEls.overview.staff.addEventListener('change', () => loadDashboard());
els.overviewPresetButtons.forEach((button) => {
  button.addEventListener('click', () => {
    setOverviewPreset(button.dataset.overviewPreset || 'month');
    loadDashboard();
  });
});
els.overviewJumpButtons.forEach((button) => {
  button.addEventListener('click', () => {
    const section = button.dataset.overviewJump;
    const target = document.querySelector(`[data-overview-section="${section}"]`);
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
});

filterEls.plan.load.addEventListener('click', () => loadPlanFact());
filterEls.plan.branch.addEventListener('change', async () => {
  await loadStaff(filterEls.plan);
  await loadPlanFact();
});
filterEls.plan.staff.addEventListener('change', () => loadPlanFact());
filterEls.reviewFacts.load.addEventListener('click', () => loadReviewFacts());
filterEls.reviewFacts.branch.addEventListener('change', async () => {
  await loadStaff(filterEls.reviewFacts);
  await loadReviewFacts();
});
filterEls.reviewFacts.staff.addEventListener('change', () => loadReviewFacts());
els.reviewFactSave.addEventListener('click', () => saveReviewFactEditor());
els.planSettingsLoad.addEventListener('click', () => reloadPlanSettingsMonth());
els.planSettingsMonth.addEventListener('change', () => reloadPlanSettingsMonth());
els.planSettingsCopy.addEventListener('click', () => copyPreviousPlanSettings());
els.planSettingsReset.addEventListener('click', () => {
  if (planSettingsSavedData) {
    renderPlanSettings(JSON.parse(JSON.stringify(planSettingsSavedData)));
  }
});
els.planSettingsSave.addEventListener('click', () => savePlanSettings());
els.planSettingsBranches.addEventListener('input', () => updatePlanSettingsDirtyFromForm());
els.planSettingsStaff.addEventListener('input', () => updatePlanSettingsDirtyFromForm());
els.serviceFilterLoad.addEventListener('click', () => loadServiceManagement());
els.serviceFilterBranch.addEventListener('change', () => loadServiceManagement());
els.serviceFilterCategory.addEventListener('change', () => loadServiceManagement());
els.serviceFilterGroup.addEventListener('change', () => loadServiceManagement());
els.serviceFilterExtra.addEventListener('change', () => loadServiceManagement());
els.serviceFilterQuery.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') loadServiceManagement();
});
els.serviceCatalogTable.addEventListener('input', () => updateServiceManagementDirtyFromForm());
els.serviceCatalogTable.addEventListener('change', () => updateServiceManagementDirtyFromForm());
els.serviceKpiGroupsTable.addEventListener('input', () => updateServiceManagementDirtyFromForm());
els.serviceKpiGroupsTable.addEventListener('change', () => updateServiceManagementDirtyFromForm());
els.serviceKpiGroupsTable.addEventListener('click', (event) => {
  const button = event.target.closest('[data-service-group-archive]');
  if (!button) return;
  const row = button.closest('[data-service-group-row]');
  const active = row?.querySelector('[data-group-field="is_active"]');
  if (active) {
    active.checked = false;
    updateServiceManagementDirtyFromForm();
  }
});
els.serviceManagementSave.addEventListener('click', () => saveServiceManagement());
els.serviceManagementReset.addEventListener('click', () => {
  if (serviceManagementSavedData) {
    renderServiceManagement(JSON.parse(JSON.stringify(serviceManagementSavedData)));
  }
});
els.serviceGroupAdd.addEventListener('click', () => addServiceKpiGroup());

els.viewLinks.forEach((link) => {
  link.addEventListener('click', (event) => {
    const view = link.dataset.viewLink;
    if (!view) return;
    if (!confirmDiscardPlanSettings() || !confirmDiscardServiceManagement()) {
      event.preventDefault();
      return;
    }
    event.preventDefault();
    if (planSettingsDirty) {
      setPlanSettingsDirty(false);
      allowDirtyPlanSettingsNavigation = true;
    }
    const paths = {
      overview: '/#overview',
      plan: '/#plan-fact',
      planSettings: '/#plan-settings',
      serviceManagement: '/#services',
      reviewFacts: '/#review-facts',
      reports: '/reports',
    };
    history.pushState({ view }, '', paths[view] || '/#overview');
    setActiveView(view);
    loadCurrentView();
  });
});

window.addEventListener('hashchange', async () => {
  const nextView = viewFromLocation();
  if (nextView === activeView) return;
  if (planSettingsDirty && activeView === 'planSettings' && nextView !== 'planSettings') {
    if (!allowDirtyPlanSettingsNavigation && !confirmDiscardPlanSettings()) {
      window.location.hash = '#plan-settings';
      return;
    }
    setPlanSettingsDirty(false);
  }
  if (serviceManagementDirty && activeView === 'serviceManagement' && nextView !== 'serviceManagement') {
    if (!confirmDiscardServiceManagement()) {
      window.location.hash = '#services';
      return;
    }
    setServiceManagementDirty(false);
  }
  allowDirtyPlanSettingsNavigation = false;
  setActiveView(nextView);
  await loadCurrentView();
});
window.addEventListener('popstate', async () => {
  const nextView = viewFromLocation();
  if (nextView === activeView && nextView !== 'reports') return;
  setActiveView(nextView);
  await loadCurrentView();
});
window.addEventListener('beforeunload', (event) => {
  if (!planSettingsDirty && !serviceManagementDirty) return;
  event.preventDefault();
  event.returnValue = '';
});
init();
