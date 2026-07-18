import {
  createLatestRequestScope,
  fetchJson,
  isSupersededRequest,
  reportDataCacheKey,
  reportDataState,
  reportRefreshPresentation,
  reportScopedFilterAllowsLoad,
  staffRefreshAllowsDataLoad,
} from './api.js';
import { ReportChartManager } from './charts.js';
import { defaultReportDates, escapeHtml, formatDate } from './format.js';
import { GROUP_LABELS, STATUS_LABELS, sourceLabel } from './registry.js';
import { renderReportData } from './renderers/generic.js';
import { t } from '../i18n.js';

const FAVORITES_KEY = 'yclients_reports_favorites';

function getFavorites() {
  try {
    return new Set(JSON.parse(localStorage.getItem(FAVORITES_KEY) || '[]'));
  } catch {
    return new Set();
  }
}

function saveFavorites(favorites) {
  localStorage.setItem(FAVORITES_KEY, JSON.stringify([...favorites]));
}

function uniqueSorted(values) {
  return [...new Set(values.filter(Boolean))].sort((a, b) => a.localeCompare(b, 'ru'));
}

function optionHtml(value, label, selectedValue) {
  return `<option value="${escapeHtml(value)}"${String(value) === String(selectedValue) ? ' selected' : ''}>${escapeHtml(label)}</option>`;
}

function statusText(report) {
  return STATUS_LABELS[report.status] || report.status;
}

function sourceText(report) {
  return (report.required_sources || []).map(sourceLabel).join(', ');
}

function reportMatches(report, filters, favorites) {
  const q = filters.search.trim().toLowerCase();
  if (q) {
    const hay = `${report.title} ${report.description} ${report.id}`.toLowerCase();
    if (!hay.includes(q)) return false;
  }
  if (filters.group && report.group !== filters.group) return false;
  if (filters.status && report.status !== filters.status) return false;
  if (filters.role && !(report.roles || []).includes(filters.role)) return false;
  if (filters.theme && !(report.themes || []).includes(filters.theme)) return false;
  if (filters.favoritesOnly && !favorites.has(report.id)) return false;
  return true;
}

function reportPath(reportId = '', search = '') {
  const query = search ? (search.startsWith('?') ? search : `?${search}`) : '';
  return reportId ? `/reports/${encodeURIComponent(reportId)}${query}` : `/reports${query}`;
}

function reportIdFromLocation() {
  const legacyReportId = new URLSearchParams(window.location.search).get('report');
  if (legacyReportId) return legacyReportId;
  const path = window.location.pathname.replace(/\/+$/, '');
  if (path === '/reports') return '';
  if (!path.startsWith('/reports/')) return '';
  return decodeURIComponent(path.slice('/reports/'.length).split('/')[0] || '');
}

function applyReportParamsFromLocation(els) {
  const params = new URLSearchParams(window.location.search);
  const valueMap = {
    start_date: els.start,
    end_date: els.end,
    company_id: els.branch,
    staff_id: els.staff,
    granularity: els.granularity,
    compare_start_date: els.compareStart,
    compare_end_date: els.compareEnd,
  };
  Object.entries(valueMap).forEach(([key, input]) => {
    const value = params.get(key);
    if (value !== null && input) input.value = value;
  });
  if (params.has('compare_start_date') || params.has('compare_end_date')) {
    els.compareEnabled.checked = true;
  }
}

function periodSubtitle(data) {
  const period = data?.period;
  if (!period) return '';
  return `${formatDate(period.start)} .. ${formatDate(period.end)} · ${period.granularity}`;
}

export function initReports({ clearError, showError, setApiState }) {
  const els = {
    view: document.getElementById('reports-view'),
    count: document.getElementById('reports-count'),
    search: document.getElementById('reports-search'),
    group: document.getElementById('reports-group'),
    status: document.getElementById('reports-status'),
    role: document.getElementById('reports-role'),
    theme: document.getElementById('reports-theme'),
    favoritesOnly: document.getElementById('reports-favorites'),
    reset: document.getElementById('reports-reset'),
    catalogToolbar: document.getElementById('reports-catalog-toolbar'),
    catalogPanel: document.getElementById('reports-catalog-panel'),
    catalog: document.getElementById('reports-catalog'),
    viewer: document.getElementById('reports-viewer'),
    viewerTitle: document.getElementById('reports-viewer-title'),
    viewerSubtitle: document.getElementById('reports-viewer-subtitle'),
    viewerBack: document.getElementById('reports-viewer-back'),
    content: document.getElementById('reports-report-content'),
    start: document.getElementById('report-start'),
    end: document.getElementById('report-end'),
    branch: document.getElementById('report-branch'),
    staff: document.getElementById('report-staff'),
    granularity: document.getElementById('report-granularity'),
    compareEnabled: document.getElementById('report-compare-enabled'),
    compareStart: document.getElementById('report-compare-start'),
    compareEnd: document.getElementById('report-compare-end'),
    compareRow: document.querySelector('.report-compare-row'),
    granularityField: document.getElementById('report-granularity')?.closest('label'),
    refresh: document.getElementById('report-refresh'),
    dataLabels: document.getElementById('report-data-labels'),
  };
  if (!els.view) return null;

  const charts = new ReportChartManager();
  const reportRequests = createLatestRequestScope();
  const staffRequests = createLatestRequestScope();
  const branchRequests = createLatestRequestScope();
  const catalogRequests = createLatestRequestScope();
  const state = {
    loaded: false,
    branchesLoaded: false,
    staffLoaded: false,
    staffIds: [],
    reports: [],
    branches: [],
    activeReportId: '',
    reportData: new Map(),
    filters: {
      search: '',
      group: '',
      status: '',
      role: '',
      theme: '',
      favoritesOnly: false,
    },
  };

  function reportRetry() {
    if (state.activeReportId) openReport(state.activeReportId, false);
  }

  function ensureFilterWarning() {
    let warning = document.getElementById('reports-filter-warning');
    if (warning) return warning;
    warning = document.createElement('div');
    warning.id = 'reports-filter-warning';
    warning.className = 'reports-note reports-note--warning reports-filter-warning';
    warning.hidden = true;
    els.catalogPanel?.before(warning);
    return warning;
  }

  function showFilterWarning(message, retry) {
    const warning = ensureFilterWarning();
    warning.hidden = false;
    warning.innerHTML = `<strong>${escapeHtml(t('reports.filtersUnavailable'))}</strong><span>${escapeHtml(message)}</span>`;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'alert__retry';
    button.textContent = t('dash.retry');
    button.addEventListener('click', retry);
    warning.appendChild(button);
  }

  function clearFilterWarning() {
    const warning = document.getElementById('reports-filter-warning');
    if (warning) warning.hidden = true;
  }

  function setCatalogVisible(visible) {
    if (els.catalogToolbar) els.catalogToolbar.hidden = !visible;
    if (els.catalogPanel) els.catalogPanel.hidden = !visible;
  }

  function setDefaultDates() {
    const dates = defaultReportDates();
    els.start.value = els.start.value || dates.start;
    els.end.value = els.end.value || dates.end;
    const start = new Date(`${dates.start}T00:00:00`);
    const end = new Date(`${dates.end}T00:00:00`);
    const span = Math.max(0, Math.round((end - start) / 86400000));
    const cmpEnd = new Date(start);
    cmpEnd.setDate(cmpEnd.getDate() - 1);
    const cmpStart = new Date(cmpEnd);
    cmpStart.setDate(cmpStart.getDate() - span);
    els.compareStart.value = els.compareStart.value || cmpStart.toISOString().slice(0, 10);
    els.compareEnd.value = els.compareEnd.value || cmpEnd.toISOString().slice(0, 10);
  }

  async function ensureLoaded() {
    if (state.loaded) return;
    const catalogRequest = catalogRequests.start();
    state.branchesLoaded = false;
    setDefaultDates();
    try {
      const reportsPayload = await fetchJson('/dashboard/reports', {}, {
        signal: catalogRequest.signal,
      });
      if (!catalogRequest.isCurrent()) return;
      state.reports = reportsPayload.data || [];
      renderFilterOptions();
      const branchRequest = branchRequests.start();
      try {
        const branchesPayload = await fetchJson('/dashboard/branches', {}, {
          signal: branchRequest.signal,
          slowState: false,
        });
        if (!branchRequest.isCurrent() || !catalogRequest.isCurrent()) return;
        state.branches = branchesPayload.data || [];
        renderBranches();
        state.branchesLoaded = true;
        clearFilterWarning();
        await loadStaff();
      } catch (error) {
        if (
          !isSupersededRequest(error)
          && branchRequest.isCurrent()
          && catalogRequest.isCurrent()
        ) {
          showFilterWarning(error.message, async () => {
            state.loaded = false;
            await loadFromLocation();
          });
        }
      } finally {
        if (branchRequest.isCurrent()) branchRequest.finish();
      }
      if (!catalogRequest.isCurrent()) return;
      state.loaded = true;
    } catch (error) {
      if (isSupersededRequest(error) || !catalogRequest.isCurrent()) return;
      throw error;
    } finally {
      if (catalogRequest.isCurrent()) catalogRequest.finish();
    }
  }

  function renderFilterOptions() {
    const selected = {
      group: els.group.value,
      status: els.status.value,
      role: els.role.value,
      theme: els.theme.value,
    };
    const groups = uniqueSorted(state.reports.map((report) => report.group));
    const statuses = uniqueSorted(state.reports.map((report) => report.status));
    const roles = uniqueSorted(state.reports.flatMap((report) => report.roles || []));
    const themes = uniqueSorted(state.reports.flatMap((report) => report.themes || []));
    els.group.innerHTML = optionHtml('', t('dash.allGroups'), selected.group)
      + groups.map((group) => optionHtml(group, GROUP_LABELS[group] || group, selected.group)).join('');
    els.status.innerHTML = optionHtml('', t('dash.allStatuses'), selected.status)
      + statuses.map((status) => optionHtml(status, STATUS_LABELS[status] || status, selected.status)).join('');
    els.role.innerHTML = optionHtml('', t('dash.allRoles'), selected.role)
      + roles.map((role) => optionHtml(role, role, selected.role)).join('');
    els.theme.innerHTML = optionHtml('', t('dash.allThemes'), selected.theme)
      + themes.map((theme) => optionHtml(theme, theme, selected.theme)).join('');
  }

  function renderBranches() {
    const selected = els.branch.value;
    els.branch.innerHTML = optionHtml('', t('dash.allBranches'), selected)
      + state.branches.map((branch) => optionHtml(branch.id, branch.title, selected)).join('');
  }

  async function loadStaff() {
    state.staffLoaded = false;
    state.staffIds = [];
    const request = staffRequests.start();
    const selected = els.staff.value;
    try {
      const payload = await fetchJson('/dashboard/staff', { company_id: els.branch.value }, {
        signal: request.signal,
        slowState: false,
      });
      if (!request.isCurrent()) return 'superseded';
      const staff = payload.data || [];
      els.staff.innerHTML = optionHtml('', t('dash.allStaff'), selected)
        + staff.map((person) => optionHtml(
          person.id,
          els.branch.value ? person.name : `${person.name} · ${person.company_title || person.company_id}`,
          selected,
        )).join('');
      state.staffLoaded = true;
      state.staffIds = staff.map((person) => person.id);
      clearFilterWarning();
      return 'ready';
    } catch (error) {
      if (isSupersededRequest(error) || !request.isCurrent()) return 'superseded';
      showFilterWarning(error.message, () => loadStaff());
      return 'failed';
    } finally {
      if (request.isCurrent()) request.finish();
    }
  }

  function collectCatalogFilters() {
    state.filters.search = els.search.value;
    state.filters.group = els.group.value;
    state.filters.status = els.status.value;
    state.filters.role = els.role.value;
    state.filters.theme = els.theme.value;
    state.filters.favoritesOnly = els.favoritesOnly.checked;
  }

  function renderCatalog() {
    collectCatalogFilters();
    setCatalogVisible(true);
    const favorites = getFavorites();
    const filtered = state.reports.filter((report) => reportMatches(report, state.filters, favorites));
    els.count.textContent = t('reports.catalogCount', { total: state.reports.length, shown: filtered.length });
    els.viewer.classList.remove('visible');
    charts.clear();
    if (!filtered.length) {
      els.catalog.innerHTML = `<div class="empty compact">${t('reports.notFound')}</div>`;
      return;
    }
    const grouped = filtered.reduce((acc, report) => {
      (acc[report.group] = acc[report.group] || []).push(report);
      return acc;
    }, {});
    els.catalog.innerHTML = Object.entries(grouped).map(([group, reports]) => `
      <section class="reports-section">
        <div class="reports-section__head">
          <h3>${escapeHtml(GROUP_LABELS[group] || group)}</h3>
          <span>${reports.length.toLocaleString('ru-RU')}</span>
        </div>
        <div class="reports-grid">
          ${reports.map((report) => `
            <article class="report-card report-card--${escapeHtml(report.status)}" data-report-id="${escapeHtml(report.id)}">
              <button class="report-card__pin${favorites.has(report.id) ? ' active' : ''}" type="button" data-report-pin="${escapeHtml(report.id)}" title="${favorites.has(report.id) ? t('reports.removeFavorite') : t('reports.addFavorite')}">★</button>
              <div class="report-card__status">${escapeHtml(statusText(report))}</div>
              <h4>${escapeHtml(report.title)}</h4>
              <p>${escapeHtml(report.description)}</p>
              <div class="report-card__tags">
                ${(report.themes || []).slice(0, 3).map((theme) => `<span>${escapeHtml(theme)}</span>`).join('')}
              </div>
              <div class="report-card__source">${escapeHtml(sourceText(report))}</div>
            </article>
          `).join('')}
        </div>
      </section>
    `).join('');
  }

  function showCatalog(push = true) {
    reportRequests.abort();
    state.activeReportId = '';
    setCatalogVisible(true);
    els.viewer.classList.remove('visible');
    if (push) history.pushState({ view: 'reports' }, '', reportPath());
    renderCatalog();
  }

  function reportParams() {
    const params = {
      report_id: state.activeReportId,
      start_date: els.start.value,
      end_date: els.end.value,
      company_id: els.branch.value,
      staff_id: els.staff.value,
      granularity: els.granularity.value || 'day',
    };
    if (els.compareEnabled.checked) {
      params.compare_start_date = els.compareStart.value;
      params.compare_end_date = els.compareEnd.value;
    }
    if (state.activeReportId === 'year_over_year') {
      params.start_year = 2022;
      params.end_year = 2026;
    }
    return params;
  }

  function reportSearch() {
    const params = new URLSearchParams();
    if (els.start.value) params.set('start_date', els.start.value);
    if (els.end.value) params.set('end_date', els.end.value);
    if (els.branch.value) params.set('company_id', els.branch.value);
    if (els.staff.value) params.set('staff_id', els.staff.value);
    if (els.granularity.value) params.set('granularity', els.granularity.value);
    if (els.compareEnabled.checked) {
      if (els.compareStart.value) params.set('compare_start_date', els.compareStart.value);
      if (els.compareEnd.value) params.set('compare_end_date', els.compareEnd.value);
    }
    return params.toString();
  }

  function applyReportFilterVisibility(meta) {
    const filters = meta.filters || {};
    if (els.granularityField) els.granularityField.hidden = filters.granularity === false;
    const canCompare = filters.compare !== false;
    if (els.compareRow) els.compareRow.hidden = !canCompare;
    if (!canCompare) {
      els.compareEnabled.checked = false;
      els.compareStart.value = '';
      els.compareEnd.value = '';
    }
  }

  async function openReport(reportId, push = true) {
    state.activeReportId = reportId;
    const meta = state.reports.find((report) => report.id === reportId);
    if (!meta) {
      showCatalog(push);
      return;
    }
    const request = reportRequests.start();
    applyReportFilterVisibility(meta);
    if (push) history.pushState({ view: 'reports', report: reportId }, '', reportPath(reportId, reportSearch()));
    setCatalogVisible(false);
    els.viewer.classList.add('visible');
    els.viewerTitle.textContent = meta.title;
    const requestParams = reportParams();
    const cacheKey = reportDataCacheKey(requestParams);
    const previousData = state.reportData.get(cacheKey);
    const refreshPresentation = reportRefreshPresentation(previousData);
    if (refreshPresentation.retainedData) {
      els.viewerSubtitle.textContent = periodSubtitle(refreshPresentation.retainedData);
      renderReportData(els.content, refreshPresentation.retainedData, charts);
    } else {
      els.viewerSubtitle.textContent = t('common.loadingShort');
      els.content.innerHTML = `<div class="empty compact">${t('reports.loadingReport')}</div>`;
    }
    els.refresh.disabled = true;
    clearError();
    setApiState(
      refreshPresentation.state === 'refreshing' ? t('dash.apiRefreshing') : t('dash.apiLoading'),
      'warn',
    );
    try {
      const payload = await fetchJson('/dashboard/reports/data', requestParams, {
        signal: request.signal,
        retry: reportRetry,
        onSlow: () => showError(t('dash.apiSlowMessage'), { apiStatus: 'slow', retry: reportRetry }),
      });
      if (!request.isCurrent() || state.activeReportId !== reportId) return;
      const data = payload.data;
      state.reportData.set(cacheKey, data);
      els.viewerTitle.textContent = data.title || meta.title;
      els.viewerSubtitle.textContent = periodSubtitle(data);
      renderReportData(els.content, data, charts);
      clearError();
      const dataState = reportDataState(data);
      if (dataState === 'partial') {
        setApiState(t('dash.apiPartial'), 'warn');
      } else if (dataState === 'empty') {
        setApiState(t('dash.apiEmpty'), 'warn');
      } else {
        setApiState(t('dash.apiConnected'), 'ok');
      }
    } catch (error) {
      if (isSupersededRequest(error) || !request.isCurrent()) return;
      charts.clear();
      showError(error.message, { apiStatus: error.apiStatus, retry: reportRetry });
      if (!previousData) {
        els.viewerSubtitle.textContent = t('common.errorPrefix');
        els.content.innerHTML = `<div class="empty compact">${t('reports.loadFailed')}</div>`;
      } else {
        renderReportData(els.content, previousData, charts);
      }
    } finally {
      if (request.isCurrent()) {
        els.refresh.disabled = false;
        request.finish();
      }
    }
  }

  async function loadFromLocation() {
    try {
      await ensureLoaded();
    } catch (error) {
      showError(error.message);
      showCatalog(false);
      return;
    }
    applyReportParamsFromLocation(els);
    const locationParams = new URLSearchParams(window.location.search);
    const requestedCompanyId = locationParams.get('company_id') || '';
    const requestedStaffId = locationParams.get('staff_id') || '';
    const showBlockedScope = () => {
      const retry = async () => {
        state.loaded = false;
        await loadFromLocation();
      };
      showError(t('reports.filtersUnavailable'), { apiStatus: 'error', retry });
      const reportId = reportIdFromLocation();
      const meta = state.reports.find((report) => report.id === reportId);
      if (meta) {
        state.activeReportId = reportId;
        setCatalogVisible(false);
        els.viewer.classList.add('visible');
        els.viewerTitle.textContent = meta.title;
        els.viewerSubtitle.textContent = t('common.errorPrefix');
        els.content.innerHTML = `<div class="empty compact">${t('reports.loadFailed')}</div>`;
      } else {
        showCatalog(false);
      }
    };
    if (!reportScopedFilterAllowsLoad(
      requestedCompanyId,
      state.branchesLoaded,
      state.branches.map((branch) => branch.id),
    )) {
      showBlockedScope();
      return;
    }
    const expectedBranch = els.branch.value;
    const staffStatus = await loadStaff();
    if (staffStatus === 'superseded') return;
    if (!reportScopedFilterAllowsLoad(requestedStaffId, state.staffLoaded, state.staffIds)) {
      showBlockedScope();
      return;
    }
    if (staffStatus === 'failed') els.staff.value = '';
    if (!staffRefreshAllowsDataLoad(staffStatus, expectedBranch, els.branch.value)) return;
    const reportId = reportIdFromLocation();
    if (reportId) {
      await openReport(reportId, false);
    } else {
      showCatalog(false);
    }
  }

  function resetFilters() {
    els.search.value = '';
    els.group.value = '';
    els.status.value = '';
    els.role.value = '';
    els.theme.value = '';
    els.favoritesOnly.checked = false;
    renderCatalog();
  }

  els.search.addEventListener('input', renderCatalog);
  [els.group, els.status, els.role, els.theme].forEach((select) => {
    select.addEventListener('change', renderCatalog);
  });
  els.favoritesOnly.addEventListener('change', renderCatalog);
  els.reset.addEventListener('click', resetFilters);
  els.catalog.addEventListener('click', async (event) => {
    const pin = event.target.closest('[data-report-pin]');
    if (pin) {
      const favorites = getFavorites();
      favorites.has(pin.dataset.reportPin) ? favorites.delete(pin.dataset.reportPin) : favorites.add(pin.dataset.reportPin);
      saveFavorites(favorites);
      renderCatalog();
      return;
    }
    const card = event.target.closest('[data-report-id]');
    if (!card) return;
    await openReport(card.dataset.reportId);
  });
  els.viewerBack.addEventListener('click', () => showCatalog());
  els.refresh.addEventListener('click', () => {
    if (state.activeReportId) openReport(state.activeReportId, false);
  });
  els.branch.addEventListener('change', async () => {
    const expectedBranch = els.branch.value;
    els.staff.value = '';
    const staffStatus = await loadStaff();
    if (!staffRefreshAllowsDataLoad(staffStatus, expectedBranch, els.branch.value)) return;
    if (state.activeReportId) openReport(state.activeReportId, false);
  });
  [els.start, els.end, els.staff, els.granularity, els.compareEnabled, els.compareStart, els.compareEnd].forEach((input) => {
    input.addEventListener('change', () => {
      if (state.activeReportId) openReport(state.activeReportId, false);
    });
  });
  els.dataLabels.addEventListener('click', () => {
    const enabled = els.dataLabels.getAttribute('aria-pressed') !== 'true';
    els.dataLabels.setAttribute('aria-pressed', enabled ? 'true' : 'false');
    charts.setDataLabels(enabled);
  });

  return {
    loadFromLocation,
    clear: () => {
      charts.clear();
      state.loaded = false;
      state.branchesLoaded = false;
      state.staffLoaded = false;
      state.staffIds = [];
      state.activeReportId = '';
      reportRequests.abort();
      staffRequests.abort();
      branchRequests.abort();
      catalogRequests.abort();
    },
  };
}
