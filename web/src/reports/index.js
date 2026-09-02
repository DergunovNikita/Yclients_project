import {
  createLatestRequestScope,
  fetchJson,
  isSupersededRequest,
  reportDataCacheKey,
  reportDataState,
  reportRefreshPresentation,
  reportScopedFilterAllowsLoad,
  staffRefreshAllowsDataLoad,
} from '../dashboardApi.js';
import { ReportChartManager } from './charts.js';
import { escapeHtml, formatDate } from './format.js';
import { comparePeriodOnLoad, defaultReportDates, nextComparePeriod } from '../period.js';
import { GROUP_LABELS, STATUS_LABELS, sourceLabel } from './registry.js';
import { renderReportData } from './renderers/generic.js';
import { intlLocale, t } from '../i18n.js';
import {
  DEFAULT_GRANULARITY,
  REPORT_FILTER_KEYS,
  reportCompareParams,
  reportFilterVisibility,
  reportFiltersFromParams,
  reportHistoryAction,
  reportLinkSearch,
  reportPeriodIsValid,
  reportRequestFilters,
  staffSelectionForOptions,
} from '../dashboardRequestState.js';

// The one place a filter key is tied to its input: everything else walks
// REPORT_FILTER_KEYS, so a key added without an input here fails loudly instead of
// silently missing the form.
const FILTER_INPUTS = {
  start_date: 'start',
  end_date: 'end',
  company_id: 'branch',
  staff_id: 'staff',
  granularity: 'granularity',
};

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
  const filters = reportFiltersFromParams(new URLSearchParams(window.location.search));
  REPORT_FILTER_KEYS.forEach((key) => { els[FILTER_INPUTS[key]].value = filters[key]; });
  els.compareStart.value = filters.compare_start_date;
  els.compareEnd.value = filters.compare_end_date;
  els.compareEnabled.checked = filters.compare_enabled;
  return filters;
}

function periodSubtitle(data) {
  const period = data?.period;
  if (!period) return '';
  return `${formatDate(period.start)} .. ${formatDate(period.end)} · ${period.granularity}`;
}

export function initReports({
  clearError,
  showError,
  setApiState,
  pushHistory = (state, url) => history.pushState(state, '', url),
  replaceHistory = (state, url) => history.replaceState(state, '', url),
}) {
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
    startField: document.getElementById('report-start')?.closest('label'),
    endField: document.getElementById('report-end')?.closest('label'),
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
    periodApplies: true,
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

  function reloadActiveReport() {
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
  }

  // What the compare inputs were last filled with automatically, and whether they are
  // still ours to move. Once they hold something we did not put there, they are the
  // user's and a new period leaves them alone.
  let autoComparePeriod = null;
  let compareWindowIsOurs = true;

  function syncCompareDefaults() {
    const next = nextComparePeriod(
      { autoPeriod: autoComparePeriod, ours: compareWindowIsOurs },
      {
        start: els.start.value,
        end: els.end.value,
        compareStart: els.compareStart.value,
        compareEnd: els.compareEnd.value,
      },
    );
    autoComparePeriod = next.autoPeriod;
    compareWindowIsOurs = next.ours;
    if (!next.window) return;
    els.compareStart.value = next.window.start;
    els.compareEnd.value = next.window.end;
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

  // The selection is passed in rather than read back off the select: a staff id that
  // belongs to another branch matches no option yet and the select reports '' for it,
  // which silently widened a per-employee link to the whole branch.
  async function loadStaff(desiredStaffId = els.staff.value) {
    state.staffLoaded = false;
    state.staffIds = [];
    const request = staffRequests.start();
    try {
      const payload = await fetchJson('/dashboard/staff', { company_id: els.branch.value }, {
        signal: request.signal,
        slowState: false,
      });
      if (!request.isCurrent()) return 'superseded';
      const staff = payload.data || [];
      const selected = staffSelectionForOptions(desiredStaffId, staff.map((person) => person.id));
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
      showFilterWarning(error.message, () => loadStaff(desiredStaffId));
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
          <span>${reports.length.toLocaleString(intlLocale())}</span>
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
    state.periodApplies = true;
    setCatalogVisible(true);
    els.viewer.classList.remove('visible');
    if (push) pushHistory({ view: 'reports' }, reportPath('', reportSearch()));
    renderCatalog();
  }

  function currentFilters() {
    const filters = {};
    REPORT_FILTER_KEYS.forEach((key) => { filters[key] = els[FILTER_INPUTS[key]].value; });
    filters.granularity = filters.granularity || DEFAULT_GRANULARITY;
    return {
      ...filters,
      compare_start_date: els.compareStart.value,
      compare_end_date: els.compareEnd.value,
      compare_enabled: els.compareEnabled.checked,
    };
  }

  function requestFilters() {
    return reportRequestFilters({
      filters: currentFilters(),
      periodApplies: state.periodApplies,
      fallbackPeriod: defaultReportDates(),
    });
  }

  function reportParams() {
    const filters = requestFilters();
    const params = { report_id: state.activeReportId };
    REPORT_FILTER_KEYS.forEach((key) => { params[key] = filters[key]; });
    // The same rule that builds the link decides what the request asks for.
    return Object.assign(params, reportCompareParams(filters));
  }

  // The link carries the period the form actually holds — a report that ignores the
  // period must not overwrite the one the user picked for every other report.
  function reportSearch() {
    return reportLinkSearch({
      filters: currentFilters(),
      currentSearch: window.location.search.replace(/^\?/, ''),
      periodApplies: state.periodApplies,
    });
  }

  function applyReportFilterVisibility(meta) {
    const filters = meta.filters || {};
    const visibility = reportFilterVisibility(filters);
    state.periodApplies = visibility.dateRange;
    if (els.startField) els.startField.hidden = !visibility.dateRange;
    if (els.endField) els.endField.hidden = !visibility.dateRange;
    if (els.granularityField) els.granularityField.hidden = !visibility.granularity;
    const canCompare = visibility.compare;
    if (els.compareRow) els.compareRow.hidden = !canCompare;
    // Only the checkbox gates the request, so the window itself is kept. Blanking it
    // here would read as the user clearing it and freeze it for the rest of the session.
    if (!canCompare) els.compareEnabled.checked = false;
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
    const historyState = { view: 'reports', report: reportId };
    const historyUrl = reportPath(reportId, reportSearch());
    const historyAction = reportHistoryAction({
      push,
      historyUrl,
      currentUrl: window.location.pathname + window.location.search,
    });
    if (historyAction === 'push') pushHistory(historyState, historyUrl);
    if (historyAction === 'replace') replaceHistory(historyState, historyUrl);
    setCatalogVisible(false);
    els.viewer.classList.add('visible');
    els.viewerTitle.textContent = meta.title;
    if (!reportPeriodIsValid(requestFilters())) {
      // The new start was typed before the new end. The API rejects that range, so say so
      // rather than spending a request on it — and never leave the previous report's
      // numbers standing under this report's title.
      charts.clear();
      els.viewerSubtitle.textContent = `${formatDate(els.start.value)} .. ${formatDate(els.end.value)}`;
      els.content.innerHTML = `<div class="empty compact">${t('reports.periodInvertedMessage')}</div>`;
      els.refresh.disabled = false;
      clearError();
      setApiState(t('reports.periodInverted'), 'warn');
      request.finish();
      return;
    }
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
        retry: reloadActiveReport,
        onSlow: () => showError(t('dash.apiSlowMessage'), { apiStatus: 'slow', retry: reloadActiveReport }),
      });
      if (!request.isCurrent() || state.activeReportId !== reportId) return;
      const data = payload.data;
      state.reportData.set(cacheKey, data);
      els.viewerTitle.textContent = data.title || meta.title;
      els.viewerSubtitle.textContent = periodSubtitle(data);
      renderReportData(els.content, data, charts);
      clearError();
      const dataState = reportDataState(data);
      const asked = currentFilters();
      const compareDropped = Boolean(asked.compare_enabled && !reportCompareParams(asked));
      const compareInverted = Boolean(asked.compare_start_date && asked.compare_end_date);
      if (compareDropped) {
        // The report is fine; only its comparison was dropped, and dropping it silently
        // would leave the ticked checkbox claiming a comparison that is not on screen.
        setApiState(t('reports.compareDropped'), 'warn');
      } else if (dataState === 'partial') {
        setApiState(t('dash.apiPartial'), 'warn');
      } else if (dataState === 'empty') {
        setApiState(t('dash.apiEmpty'), 'warn');
      } else {
        setApiState(t('dash.apiConnected'), 'ok');
      }
    } catch (error) {
      if (isSupersededRequest(error) || !request.isCurrent()) return;
      charts.clear();
      showError(error.message, { apiStatus: error.apiStatus, retry: reloadActiveReport });
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
      showError(error.message, {
        apiStatus: error.apiStatus,
        retry: async () => {
          state.loaded = false;
          await loadFromLocation();
        },
      });
      showCatalog(false);
      return;
    }
    const requested = applyReportParamsFromLocation(els);
    setDefaultDates();
    const loaded = comparePeriodOnLoad({
      start: els.start.value,
      end: els.end.value,
      compareStart: requested.compare_start_date,
      compareEnd: requested.compare_end_date,
    });
    autoComparePeriod = loaded.autoPeriod;
    compareWindowIsOurs = loaded.ours;
    syncCompareDefaults();
    const requestedCompanyId = requested.company_id;
    const requestedStaffId = requested.staff_id;
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
    const staffStatus = await loadStaff(requestedStaffId);
    if (staffStatus === 'superseded') return;
    if (!reportScopedFilterAllowsLoad(requestedStaffId, state.staffLoaded, state.staffIds)) {
      showBlockedScope();
      return;
    }
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
  els.viewerBack.addEventListener('click', () => {
    // Walking out of a report drops its error and its status; the catalog is not it.
    // The pill only claims a healthy API when the catalog on screen really did load.
    clearError();
    if (state.loaded) setApiState(t('dash.apiConnected'), 'ok');
    showCatalog();
  });
  els.refresh.addEventListener('click', reloadActiveReport);
  els.branch.addEventListener('change', async () => {
    const expectedBranch = els.branch.value;
    els.staff.value = '';
    const staffStatus = await loadStaff();
    if (!staffRefreshAllowsDataLoad(staffStatus, expectedBranch, els.branch.value)) return;
    reloadActiveReport();
  });
  [els.start, els.end].forEach((input) => {
    input.addEventListener('change', () => {
      syncCompareDefaults();
      reloadActiveReport();
    });
  });
  // A half-written window asks for nothing until its other bound is typed in;
  // reportCompareParams is the only gate, so the checkbox is left as the user set it.
  [els.staff, els.granularity, els.compareEnabled, els.compareStart, els.compareEnd].forEach((input) => {
    input.addEventListener('change', reloadActiveReport);
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
