import Chart from 'chart.js/auto';

const apiBase = import.meta.env.VITE_API_BASE || '';

const els = {
  kpi: document.getElementById('kpi'),
  visitMetrics: document.getElementById('visit-metrics'),
  error: document.getElementById('error'),
  apiState: document.getElementById('api-state'),
  syncState: document.getElementById('sync-state'),
  periodLabel: document.getElementById('period-label'),
  revenueMeta: document.getElementById('revenue-meta'),
  appointmentsMeta: document.getElementById('appointments-meta'),
  servicesMeta: document.getElementById('services-meta'),
  extraServicesMeta: document.getElementById('extra-services-meta'),
  planMeta: document.getElementById('plan-meta'),
  tableMeta: document.getElementById('table-meta'),
  planFactTable: document.getElementById('plan-fact-table'),
  servicesTable: document.getElementById('services-table'),
  extraServicesTable: document.getElementById('extra-services-table'),
  revenueChart: document.getElementById('revenue-chart'),
  appointmentsChart: document.getElementById('appointments-chart'),
  servicesChart: document.getElementById('services-chart'),
  overviewView: document.getElementById('overview-view'),
  planView: document.getElementById('plan-view'),
  viewLinks: [...document.querySelectorAll('[data-view-link]')],
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
};

const charts = {
  revenue: null,
  appointments: null,
  services: null,
};

let activeView = 'overview';
let branchOptions = [];

const ADMIN_HIDDEN_METRIC_CODES = new Set(['revenue', 'avg_check_total']);

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
      response = await fetch(url);
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

function defaultDates(filter) {
  const now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), 1);
  filter.end.value = formatInputDate(now);
  filter.start.value = formatInputDate(start);
}

function formatInputDate(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
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
      label: 'Средний чек общий',
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
            </tr>
          </thead>
          <tbody>
            ${staffPlan.metrics
              .map(
                (metric) => `
                  <tr>
                    <td>${escapeHtml(metric.label)}</td>
                    <td class="number">${escapeHtml(formatMetricValue(metric.plan, metric.format))}</td>
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
    els.planFactTable.innerHTML = `${diagnosticsHtml}<div class="empty">Нет плана за выбранный период</div>`;
    els.planMeta.textContent = '';
    return;
  }

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

function renderBundle(bundle) {
  const {
    summary,
    revenue_daily: daily = [],
    top_services: services = [],
    extra_services: extraServices = [],
  } = bundle;
  renderKpi(summary);
  renderVisitMetrics(summary);
  renderRevenueChart(daily);
  renderAppointmentsChart(daily);
  renderServicesChart(services.slice(0, 8));
  renderServicesTable(services);
  renderExtraServicesTable(extraServices);

  els.periodLabel.textContent = `${summary.period.start} .. ${summary.period.end}`;
  els.revenueMeta.textContent = `${daily.length} дней`;
  els.appointmentsMeta.textContent = `${formatNumber(summary.revenue.appointments)} записей`;
  els.servicesMeta.textContent = `${services.length} услуг`;
  els.extraServicesMeta.textContent = `${formatNumber(summary.revenue.extra_service_count)} оказано`;
  els.tableMeta.textContent = `${formatMoney(summary.revenue.total)} всего`;
}

async function loadBranches() {
  try {
    const payload = await fetchJson('/dashboard/branches');
    branchOptions = payload.data || [];
    Object.values(filterEls).forEach((filter) => renderBranchOptions(filter));
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
}

async function loadStaff(filter) {
  const selected = filter.staff.value;
  try {
    const payload = await fetchJson('/dashboard/staff', {
      company_id: filter.branch.value,
    });
    const staffOptions = payload.data || [];
    filter.staff.innerHTML = '<option value="">Все работники</option>';
    staffOptions.forEach((staff) => {
      const option = document.createElement('option');
      option.value = staff.id;
      option.textContent = filter.branch.value
        ? staff.name
        : `${staff.name} · ${staff.company_title || `Филиал ${staff.company_id}`}`;
      filter.staff.appendChild(option);
    });
    filter.staff.value = staffOptions.some((staff) => String(staff.id) === selected) ? selected : '';
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

function viewFromHash() {
  return window.location.hash === '#plan-fact' ? 'plan' : 'overview';
}

function setActiveView(view) {
  activeView = view;
  els.overviewView.classList.toggle('active', view === 'overview');
  els.planView.classList.toggle('active', view === 'plan');
  els.viewLinks.forEach((link) => {
    link.classList.toggle('active', link.dataset.viewLink === view);
  });
  els.periodLabel.textContent = view === 'plan'
    ? 'План/факт по филиалам и сотрудникам'
    : 'Метрики по филиалам и услугам';
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
  } else {
    await loadDashboard();
  }
}

async function init() {
  Object.values(filterEls).forEach((filter) => defaultDates(filter));
  renderServicesTable([]);
  renderExtraServicesTable([]);
  await loadBranches();
  await Promise.all(Object.values(filterEls).map((filter) => loadStaff(filter)));
  setActiveView(viewFromHash());
  await loadCurrentView();
}

filterEls.overview.load.addEventListener('click', () => loadDashboard());
filterEls.overview.branch.addEventListener('change', async () => {
  await loadStaff(filterEls.overview);
  await loadDashboard();
});
filterEls.overview.staff.addEventListener('change', () => loadDashboard());

filterEls.plan.load.addEventListener('click', () => loadPlanFact());
filterEls.plan.branch.addEventListener('change', async () => {
  await loadStaff(filterEls.plan);
  await loadPlanFact();
});
filterEls.plan.staff.addEventListener('change', () => loadPlanFact());

window.addEventListener('hashchange', async () => {
  const nextView = viewFromHash();
  if (nextView === activeView) return;
  setActiveView(nextView);
  await loadCurrentView();
});
init();
