import Chart from 'chart.js/auto';
import { enhanceSelect } from './customSelect.js';
import {
  acquireStartupSession,
  authFetch,
  ensureOnboardingComplete,
  getSelectedPortalAccountId,
  loadCurrentUserQuietly,
  redirectToLogin,
  requiresLogin,
  setSelectedPortalAccountId,
} from './auth.js';
import {
  createLatestRequestScope,
  fetchJson as sharedFetchJson,
  filterServiceManagementResult,
  isSupersededRequest,
  latestServiceManagementTimestamp,
  mergeServiceManagementResult,
  patchJson as sharedPatchJson,
  postJson as sharedPostJson,
  serviceManagementChanges,
  serviceManagementControls,
  serviceManagementLoadAllowed,
  serviceManagementNavigationAllowed,
  runServiceManagementMutation,
  settleServiceManagementLoad,
  staffRefreshAllowsDataLoad,
} from './dashboardApi.js';

import { escapeHtml } from './html.js';
import {
  BRANCH_PLAN_SETTING_FIELDS,
  STAFF_PLAN_SETTING_FIELDS_BY_CATEGORY,
  buildPlanSettingsPayload,
  isScheduleAttributionDiagnostic,
} from './planSettings.js';
import {
  filterPlanFactForDisplay,
  hideMoneyPlanMetrics,
  normalizeHiddenPlanMetricCodes,
  setPlanMetricHidden,
} from './planMetricVisibility.js';
import { initReports } from './reports/index.js';
import { applyTranslations, getLocale, intlLocale, mountLanguageSwitcher, t } from './i18n.js';
import {
  createUnsavedChangesGuard,
  editorSaveDockState,
  historyNavigationDecision,
  shouldHandleSameTabNavigation,
} from './unsavedChanges.js';

document.documentElement.lang = getLocale();
applyTranslations();

const apiKey = import.meta.env?.VITE_API_KEY || '';
const DEMO_AUTOLOGIN = import.meta.env.VITE_DEMO_AUTOLOGIN;
let currentUser = null;

const els = {
  kpi: document.getElementById('kpi'),
  visitMetrics: document.getElementById('visit-metrics'),
  clientsMetrics: document.getElementById('clients-metrics'),
  revenueMetrics: document.getElementById('revenue-metrics'),
  servicesMetrics: document.getElementById('services-metrics'),
  staffPersonalScope: document.getElementById('staff-personal-scope'),
  staffPersonalTitle: document.getElementById('staff-personal-title'),
  staffBranchScope: document.getElementById('staff-branch-scope'),
  staffBranchTitle: document.getElementById('staff-branch-title'),
  staffBranchMetrics: document.getElementById('staff-branch-metrics'),
  staffBranchDetails: document.getElementById('staff-branch-details'),
  servicesSection: document.getElementById('services-overview-section'),
  servicesDetails: document.getElementById('services-details'),
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
  planColumnPicker: document.getElementById('plan-column-picker'),
  planColumnToggle: document.getElementById('plan-column-toggle'),
  planColumnMenu: document.getElementById('plan-column-menu'),
  planColumnList: document.getElementById('plan-column-list'),
  planColumnCount: document.getElementById('plan-column-count'),
  planColumnsHideMoney: document.getElementById('plan-columns-hide-money'),
  planColumnsShowAll: document.getElementById('plan-columns-show-all'),
  reviewFactEditor: document.getElementById('review-fact-editor'),
  reviewFactMeta: document.getElementById('review-fact-meta'),
  reviewFactSave: document.getElementById('review-fact-save'),
  opzFactEditor: document.getElementById('opz-fact-editor'),
  opzFactMeta: document.getElementById('opz-fact-meta'),
  opzFactSave: document.getElementById('opz-fact-save'),
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
  opzFactsView: document.getElementById('opz-facts-view'),
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
  floatingEditorSave: document.getElementById('floating-editor-save'),
  floatingEditorSaveStatus: document.getElementById('floating-editor-save-status'),
  floatingEditorSaveButton: document.getElementById('floating-editor-save-button'),
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
  profileSettingsLink: document.querySelector('#user-profile-link a[href]'),
  unsavedChangesDialog: document.getElementById('unsaved-changes-dialog'),
  unsavedChangesStay: document.getElementById('unsaved-changes-stay'),
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
    month: document.getElementById('review-fact-month'),
    branch: document.getElementById('review-fact-branch'),
    staff: document.getElementById('review-fact-staff'),
    load: document.getElementById('review-fact-load'),
  },
  opzFacts: {
    month: document.getElementById('opz-fact-month'),
    branch: document.getElementById('opz-fact-branch'),
    staff: document.getElementById('opz-fact-staff'),
    load: document.getElementById('opz-fact-load'),
  },
};

const customFilterDropdowns = {};
Object.values(filterEls).forEach((filter) => {
  customFilterDropdowns[filter.branch.id] = enhanceSelect(filter.branch, { placeholder: t('dash.allBranches') });
  customFilterDropdowns[filter.staff.id] = enhanceSelect(filter.staff, { placeholder: t('dash.allWorkers') });
});

const charts = {
  revenue: null,
  appointments: null,
  opz: null,
  services: null,
  staffPlanGroups: [],
  goodsKpi: null,
};

// Plan/fact metrics live on different scales (money vs counts vs %), so each
// scale gets its own chart to stay readable.
const PLAN_SCALE_GROUPS = [
  // Money metrics differ in magnitude between themselves (revenue vs avg check),
  // so each gets its own chart, laid out in a single row.
  { format: 'money', labelKey: 'dash.scaleMoney', color: '#0f766e', perMetric: true },
  { format: 'number', labelKey: 'dash.scaleCount', color: '#2563eb' },
  { format: 'percent', labelKey: 'dash.scalePercent', color: '#b45309' },
];

const pageOpenedAt = new Date();
let activeView = 'overview';
let branchOptions = [];
let reviewFactRows = [];
let opzFactRows = [];
let planSettingsData = null;
let planSettingsSavedData = null;
let planSettingsSavedSnapshot = '';
let planSettingsDirty = false;
let planSettingsLoadedMonth = '';
let planSettingsSaving = false;
let reviewFactSavedSnapshot = '';
let reviewFactSavedData = null;
let reviewFactDirty = false;
let reviewFactLoadedFilters = null;
let reviewFactSaving = false;
let opzFactSavedSnapshot = '';
let opzFactSavedData = null;
let opzFactDirty = false;
let opzFactLoadedFilters = null;
let opzFactSaving = false;
let serviceManagementData = { rows: [], groups: [], categories: [] };
let serviceManagementSavedData = null;
let serviceManagementSavedSnapshot = '';
let serviceManagementDirty = false;
let serviceManagementLoading = false;
let serviceManagementMutationPending = false;
let reportsController = null;
let selectedTenant = null;
let retryCurrentView = null;
let currentPlanFactPayload = null;
let hiddenPlanMetricCodes = new Set();
const loadedStaffFilters = new WeakSet();
const staffRequestScopes = new WeakMap();
const viewRequestScopes = {
  overview: createLatestRequestScope(),
  plan: createLatestRequestScope(),
  planSettings: createLatestRequestScope(),
  serviceManagement: createLatestRequestScope(),
  reviewFacts: createLatestRequestScope(),
  opzFacts: createLatestRequestScope(),
  branches: createLatestRequestScope(),
};
const viewsWithData = new Set();
const HISTORY_POSITION_KEY = 'dashboardPosition';
let historyPosition = 0;
let currentHistoryUrl = window.location.href;
let suppressedHistoryTraversal = null;
let handlingHistoryNavigation = false;
let historyCorrectionPromise = null;
let unsavedDialogResolve = null;

function showUnsavedChangesDialog() {
  if (!els.unsavedChangesDialog || typeof els.unsavedChangesDialog.showModal !== 'function') {
    return Promise.resolve(window.confirm(t('dash.unsavedChangesMessage')));
  }
  const trigger = document.activeElement;
  els.unsavedChangesDialog.returnValue = 'stay';
  els.unsavedChangesDialog.showModal();
  queueMicrotask(() => els.unsavedChangesStay?.focus());
  return new Promise((resolve) => {
    unsavedDialogResolve = (shouldLeave) => {
      if (!shouldLeave && trigger instanceof HTMLElement) trigger.focus();
      resolve(shouldLeave);
    };
  });
}

function finishUnsavedChangesDialog() {
  const resolve = unsavedDialogResolve;
  unsavedDialogResolve = null;
  resolve?.(els.unsavedChangesDialog.returnValue === 'leave');
}

function hasProtectedDirtyChanges() {
  return (
    (activeView === 'planSettings' && planSettingsDirty)
    || (activeView === 'reviewFacts' && reviewFactDirty)
    || (activeView === 'opzFacts' && opzFactDirty)
  );
}

function protectedSavePending() {
  return (
    (activeView === 'planSettings' && planSettingsSaving)
    || (activeView === 'reviewFacts' && reviewFactSaving)
    || (activeView === 'opzFacts' && opzFactSaving)
  );
}

function discardProtectedChanges() {
  if (activeView === 'planSettings') {
    if (planSettingsSavedData) renderPlanSettings(JSON.parse(JSON.stringify(planSettingsSavedData)));
    else setPlanSettingsDirty(false);
  }
  if (activeView === 'reviewFacts') {
    restoreReviewFactFilters();
    if (reviewFactSavedData) renderReviewFactEditor(JSON.parse(JSON.stringify(reviewFactSavedData)));
    else setReviewFactDirty(false);
  }
  if (activeView === 'opzFacts') {
    restoreOpzFactFilters();
    if (opzFactSavedData) renderOpzFactEditor(JSON.parse(JSON.stringify(opzFactSavedData)));
    else setOpzFactDirty(false);
  }
}

const protectedChangesGuard = createUnsavedChangesGuard({
  isDirty: hasProtectedDirtyChanges,
  isBlocked: protectedSavePending,
  confirmLeave: showUnsavedChangesDialog,
  onDiscard: discardProtectedChanges,
});

function updateFloatingEditorSave() {
  const state = editorSaveDockState({
    activeView,
    planSettingsDirty,
    planSettingsSaving,
    reviewFactDirty,
    reviewFactSaving,
    opzFactDirty,
    opzFactSaving,
    isDemo: document.body.classList.contains('demo-mode'),
  });
  els.floatingEditorSave.hidden = !state.visible;
  document.body.classList.toggle('floating-editor-save-visible', state.visible);
  if (!state.visible) return;
  els.floatingEditorSaveStatus.textContent = t('dash.unsavedChanges');
  els.floatingEditorSaveButton.disabled = state.saving;
  els.floatingEditorSaveButton.textContent = state.saving ? t('common.saving') : t('dash.saveChanges');
}

async function runDashboardNavigation(action) {
  if (!serviceManagementNavigationAllowed({
    mutationPending: serviceManagementMutationPending,
    activeView,
  })) return false;
  if (!confirmDiscardServiceManagement()) return false;
  if (serviceManagementDirty) setServiceManagementDirty(false);
  return protectedChangesGuard.run(action);
}

function dashboardPath(view) {
  const paths = {
    overview: '/#overview',
    plan: '/#plan-fact',
    planSettings: '/#plan-settings',
    serviceManagement: '/#services',
    reviewFacts: '/#review-facts',
    opzFacts: '/#opz-facts',
    reports: '/reports',
  };
  return paths[view] || paths.overview;
}

function replaceDashboardHistory(state, url = window.location.href) {
  history.replaceState({
    ...(history.state || {}),
    ...state,
    [HISTORY_POSITION_KEY]: historyPosition,
  }, '', url);
  currentHistoryUrl = new URL(url, window.location.href).href;
}

function pushDashboardHistory(state, url) {
  historyPosition += 1;
  history.pushState({ ...state, [HISTORY_POSITION_KEY]: historyPosition }, '', url);
  currentHistoryUrl = new URL(url, window.location.href).href;
}

function restoreHistoryPosition(delta) {
  if (!delta) return Promise.resolve();
  return new Promise((resolve) => {
    suppressedHistoryTraversal = {
      expectedPosition: historyPosition,
      resolve,
    };
    history.go(delta);
  });
}

els.unsavedChangesDialog?.addEventListener('close', finishUnsavedChangesDialog);
els.unsavedChangesDialog?.addEventListener('cancel', (event) => {
  event.preventDefault();
  els.unsavedChangesDialog.close('stay');
});
els.unsavedChangesDialog?.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  event.preventDefault();
  event.stopPropagation();
  els.unsavedChangesDialog.close('stay');
});
els.unsavedChangesDialog?.addEventListener('click', (event) => {
  if (event.target === els.unsavedChangesDialog) els.unsavedChangesDialog.close('stay');
});

window.addEventListener('portal:session-transition', () => {
  Object.values(viewRequestScopes).forEach((scope) => scope.abort());
  reportsController?.clear();
  currentPlanFactPayload = null;
  hiddenPlanMetricCodes = new Set();
  setPlanColumnPickerOpen(false);
  renderPlanColumnPicker();
});

const SETTINGS_ADMIN_ROLES = new Set(['platform_admin', 'owner', 'branch_admin']);
const SETTINGS_VIEWS = new Set(['planSettings', 'serviceManagement', 'reviewFacts', 'opzFacts']);

function hasSettingsAdminAccess() {
  if (apiKey && !currentUser) return true;
  return SETTINGS_ADMIN_ROLES.has(currentUser?.role);
}

function canAccessView(view) {
  return !SETTINGS_VIEWS.has(view) || hasSettingsAdminAccess();
}

function accessibleView(view) {
  return canAccessView(view) ? view : 'overview';
}

function setViewLinksHidden(view, hidden) {
  els.viewLinks
    .filter((link) => link.dataset.viewLink === view)
    .forEach((link) => {
      link.hidden = hidden;
    });
}

function applyDashboardPermissions() {
  const hideSettings = !hasSettingsAdminAccess();
  SETTINGS_VIEWS.forEach((view) => setViewLinksHidden(view, hideSettings));
  els.planSettingsView.hidden = hideSettings;
  els.serviceManagementView.hidden = hideSettings;
  els.reviewFactsView.hidden = hideSettings;
  els.opzFactsView.hidden = hideSettings;
}

function setOverviewSectionHidden(section, hidden) {
  document.querySelectorAll(`[data-overview-section="${section}"]`).forEach((node) => {
    node.hidden = hidden;
  });
  els.overviewJumpButtons
    .filter((button) => button.dataset.overviewJump === section)
    .forEach((button) => {
      button.hidden = hidden;
    });
}

function applyFinancialVisibility(summary) {
  const financialsHidden = Boolean(summary?.financials_hidden);
  setOverviewSectionHidden('revenue', financialsHidden);
  setOverviewSectionHidden('services', financialsHidden);
}

function formatMoney(value) {
  return `${Math.round(Number(value || 0)).toLocaleString(intlLocale())} ₽`;
}

function formatNumber(value) {
  return Number(value || 0).toLocaleString(intlLocale());
}

function formatDecimal(value) {
  return Number(value || 0).toLocaleString(intlLocale(), { maximumFractionDigits: 2 });
}

function formatPct(value) {
  if (value === null || value === undefined) return t('dash.noBase');
  const sign = value > 0 ? '+' : '';
  return t('dash.changeVsPrevious', { value: `${sign}${Number(value).toLocaleString(intlLocale())}%` });
}

function formatMetricValue(value, format) {
  if (value === null || value === undefined) return '—';
  if (format === 'money') return formatMoney(value);
  if (format === 'percent') return `${Number(value).toLocaleString(intlLocale(), { maximumFractionDigits: 2 })}%`;
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

function setApiState(text, kind = 'warn') {
  els.apiState.textContent = text;
  els.apiState.className = `pill ${kind}`;
}

function setApiStatus(status) {
  const states = {
    loading: [t('dash.apiLoading'), 'warn'],
    refreshing: [t('dash.apiRefreshing'), 'warn'],
    slow: [t('dash.apiSlow'), 'warn'],
    partial: [t('dash.apiPartial'), 'warn'],
    empty: [t('dash.apiEmpty'), 'warn'],
    ready: [t('dash.apiConnected'), 'ok'],
    auth_required: [t('dash.apiAuthRequired'), 'warn'],
    forbidden: [t('dash.apiForbidden'), 'error'],
    timeout: [t('dash.apiTimeout'), 'error'],
    server_error: [t('dash.apiServerError'), 'error'],
    sync_problem: [t('dash.apiSyncProblem'), 'warn'],
    ok: [t('dash.apiConnected'), 'ok'],
    error: [t('dash.apiError'), 'error'],
  };
  const [label, kind] = states[status] || states.error;
  setApiState(label, kind);
}

function setErrorMessage(message, retry = null) {
  els.error.textContent = message;
  retryCurrentView = retry;
  if (retry) {
    const action = document.createElement('button');
    action.type = 'button';
    action.className = 'alert__retry';
    action.textContent = t('dash.retry');
    action.addEventListener('click', () => retry());
    els.error.appendChild(document.createTextNode(' '));
    els.error.appendChild(action);
  }
  els.error.classList.add('visible');
}

function showError(message, { apiStatus = 'error', retry = null } = {}) {
  setErrorMessage(message, retry);
  setApiStatus(apiStatus);
}

function showSlowNotice(retry = null) {
  setErrorMessage(t('dash.apiSlowMessage'), retry);
  setApiStatus('slow');
}

function clearError() {
  els.error.textContent = '';
  els.error.classList.remove('visible');
  retryCurrentView = null;
}

function requestScopeForStaff(filter) {
  if (!staffRequestScopes.has(filter)) staffRequestScopes.set(filter, createLatestRequestScope());
  return staffRequestScopes.get(filter);
}

function beginViewRequest(view) {
  const request = viewRequestScopes[view].start();
  setApiStatus(viewsWithData.has(view) ? 'refreshing' : 'loading');
  return request;
}

function setLoadedApiState(data, { empty = false } = {}) {
  const appointmentsStatus = data?.summary?.appointments_breakdown?.source_status;
  const summaryStatus = data?.summary?.source_status;
  if (data?.source_status === 'partial' || summaryStatus === 'partial' || appointmentsStatus === 'unavailable') {
    setApiStatus('partial');
  } else {
    setApiStatus(empty ? 'empty' : 'ready');
  }
}

function showFilterWarning(input, message, retry) {
  const host = input?.closest('label') || input?.parentElement;
  if (!host) return;
  let warning = host.querySelector(':scope > .filter-load-warning');
  if (!warning) {
    warning = document.createElement('span');
    warning.className = 'filter-load-warning';
    host.appendChild(warning);
  }
  warning.textContent = message;
  if (retry) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = t('dash.retry');
    button.addEventListener('click', retry);
    warning.appendChild(document.createTextNode(' '));
    warning.appendChild(button);
  }
}

function clearFilterWarning(input) {
  const host = input?.closest('label') || input?.parentElement;
  host?.querySelector(':scope > .filter-load-warning')?.remove();
}

function requestOptions(options = {}) {
  return {
    ...options,
    onSlow: () => showSlowNotice(options.retry || retryCurrentView),
  };
}

function fetchJson(path, params, options = {}) {
  return sharedFetchJson(path, params, requestOptions(options));
}

function postJson(path, body, options = {}) {
  return sharedPostJson(path, body, requestOptions(options));
}

function patchJson(path, body, options = {}) {
  return sharedPatchJson(path, body, requestOptions(options));
}

function defaultDates(filter) {
  const now = new Date(pageOpenedAt);
  const start = new Date(now.getFullYear(), now.getMonth(), 1);
  filter.end.value = formatInputDate(now);
  filter.start.value = formatInputDate(start);
}

function setManualFactDefaultMonths() {
  const now = new Date(pageOpenedAt);
  const month = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
  filterEls.reviewFacts.month.value = month;
  filterEls.opzFacts.month.value = month;
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

function overviewReportUrl(reportId) {
  const filter = filterEls.overview;
  const params = new URLSearchParams();
  if (filter.start.value) params.set('start_date', filter.start.value);
  if (filter.end.value) params.set('end_date', filter.end.value);
  if (filter.branch.value) params.set('company_id', filter.branch.value);
  if (filter.staff.value) params.set('staff_id', filter.staff.value);
  params.set('granularity', 'month');
  return `/reports/${encodeURIComponent(reportId)}?${params.toString()}`;
}

function renderCards(target, cards) {
  target.innerHTML = cards
    .map(
      (card) => {
        const tag = card.href ? 'a' : 'article';
        const actionLabel = card.href ? t('dash.openReportWithLabel', { label: card.label }) : '';
        const attrs = card.href
          ? ` href="${escapeHtml(card.href)}" data-report-link class="card card--link" aria-label="${escapeHtml(actionLabel)}" title="${escapeHtml(actionLabel)}"`
          : ' class="card"';
        return `
        <${tag}${attrs}>
          <div class="label">${escapeHtml(card.label)}</div>
          <div class="value">${escapeHtml(card.value)}</div>
          ${card.delta ? `<div class="delta ${deltaClass(card.deltaValue)}">${escapeHtml(card.delta)}</div>` : ''}
          ${card.action ? `<div class="card__action">${escapeHtml(card.action)}</div>` : ''}
        </${tag}>
      `;
      },
    )
    .join('');
}

function presentCards(cards) {
  return cards.filter((card) => card.present !== false);
}

function renderKpi(summary) {
  const revenueBlock = summary.revenue;
  const averageCheckBlock = summary.average_check;
  const revenue = revenueBlock || {};
  const averageCheck = averageCheckBlock || {};
  const cards = [
    {
      label: t('dash.cardTotalRevenue'),
      value: formatMoney(revenue.total),
      delta: formatPct(revenue.change_pct),
      deltaValue: revenue.change_pct,
      present: Boolean(revenueBlock),
    },
    {
      label: t('dash.cardVisitedAppointments'),
      value: formatNumber(revenue.appointments),
      delta: formatPct(revenue.appointments_change_pct),
      deltaValue: revenue.appointments_change_pct,
      present: Boolean(revenueBlock),
    },
    {
      label: averageCheck.source_status === 'partial'
        ? t('dash.cardAverageCheckTotalPartial')
        : t('dash.cardAverageCheckTotal'),
      value: formatMoney(averageCheck.total),
      delta: formatPct(averageCheck.total_change_pct),
      deltaValue: averageCheck.total_change_pct,
      present: Boolean(averageCheckBlock),
    },
  ];

  renderCards(els.kpi, presentCards(cards));
}

function goodsRevenueShare(revenue) {
  const total = Number(revenue.total || 0);
  if (!total) return null;
  return (Number(revenue.goods_revenue || 0) / total) * 100;
}

function usesAdministratorSchedule(summary) {
  return summary?.service_attribution?.mode === 'administrator_schedule';
}

function renderStaffPersonalScope() {
  const selectedOption = filterEls.overview.staff.selectedOptions[0];
  const hasSelectedStaff = Boolean(filterEls.overview.staff.value && selectedOption);
  els.staffPersonalScope.hidden = !hasSelectedStaff;
  if (!hasSelectedStaff) return;
  els.staffPersonalTitle.textContent = t('dash.personalMetricsTitle', {
    name: selectedOption.textContent?.trim() || t('dash.worker'),
  });
}

function renderStaffBranchMetrics(summary) {
  const isBranchScope = usesAdministratorSchedule(summary);
  const detailsParent = isBranchScope && !summary.financials_hidden
    ? els.staffBranchDetails
    : els.servicesSection;
  if (els.servicesDetails.parentElement !== detailsParent) {
    detailsParent.appendChild(els.servicesDetails);
  }
  els.staffBranchScope.hidden = !isBranchScope;
  if (!isBranchScope) {
    renderCards(els.staffBranchMetrics, []);
    return;
  }

  const selectedStaff = filterEls.overview.staff.selectedOptions[0]?.textContent?.trim()
    || t('dash.worker');
  els.staffBranchTitle.textContent = t('dash.branchShiftMetricsTitle', { name: selectedStaff });

  const attribution = summary.service_attribution || {};
  const revenue = summary.revenue || {};
  const averageCheck = summary.average_check || {};
  const visitMetrics = summary.visit_metrics || {};
  const cards = [
    {
      label: t('dash.cardBranchShiftAppointments'),
      value: formatMetricValue(attribution.appointment_count, 'number'),
    },
    {
      label: t('dash.cardBranchShiftClients'),
      value: formatMetricValue(attribution.unique_client_count, 'number'),
    },
    {
      label: t('dash.cardExtraServiceCount'),
      value: formatMetricValue(revenue.extra_service_count, 'number'),
      delta: formatPct(revenue.extra_service_count_change_pct),
      deltaValue: revenue.extra_service_count_change_pct,
    },
    {
      label: t('dash.cardExtraServicesPerAppointment'),
      value: formatMetricValue(visitMetrics.extra_services_per_appointment_pct, 'percent'),
      delta: formatPct(visitMetrics.extra_services_per_appointment_pct_change_pct),
      deltaValue: visitMetrics.extra_services_per_appointment_pct_change_pct,
    },
    {
      label: t('dash.cardExtraServiceClients'),
      value: formatMetricValue(visitMetrics.extra_service_clients_pct, 'percent'),
      delta: t('dash.uniqueClientsCount', { count: formatNumber(visitMetrics.extra_service_clients) }),
      deltaValue: null,
    },
    {
      label: t('dash.cardExtraServiceRevenue'),
      value: formatMetricValue(revenue.extra_service_revenue, 'money'),
      delta: formatPct(revenue.extra_service_revenue_change_pct),
      deltaValue: revenue.extra_service_revenue_change_pct,
      present: !summary.financials_hidden,
    },
    {
      label: t('dash.cardExtraServiceAverageCheck'),
      value: formatMetricValue(averageCheck.extra_services, 'money'),
      delta: formatPct(averageCheck.extra_services_change_pct),
      deltaValue: averageCheck.extra_services_change_pct,
      present: !summary.financials_hidden,
    },
  ];

  renderCards(els.staffBranchMetrics, presentCards(cards));
}

function renderRevenueMetrics(summary) {
  if (!summary.revenue) {
    renderCards(els.revenueMetrics, []);
    return;
  }
  const revenue = summary.revenue;
  const cards = [
    {
      label: t('dash.cardServiceRevenue'),
      value: formatMoney(revenue.service_revenue),
      delta: formatPct(revenue.service_revenue_change_pct),
      deltaValue: revenue.service_revenue_change_pct,
    },
    {
      label: t('dash.cardGoodsRevenue'),
      value: formatMoney(revenue.goods_revenue),
      delta: formatPct(revenue.goods_revenue_change_pct),
      deltaValue: revenue.goods_revenue_change_pct,
    },
    {
      label: t('dash.cardExtraServiceRevenue'),
      value: formatMoney(revenue.extra_service_revenue),
      delta: formatPct(revenue.extra_service_revenue_change_pct),
      deltaValue: revenue.extra_service_revenue_change_pct,
    },
    {
      label: t('dash.cardGoodsRevenueShare'),
      value: formatMetricValue(goodsRevenueShare(revenue), 'percent'),
      delta: t('dash.ofTotalRevenue'),
      deltaValue: null,
    },
  ];

  renderCards(
    els.revenueMetrics,
    usesAdministratorSchedule(summary) ? cards.filter((_, index) => index !== 2) : cards,
  );
}

function renderServicesMetrics(summary) {
  const revenueBlock = summary.revenue;
  const averageCheckBlock = summary.average_check;
  const revenue = revenueBlock || {};
  const averageCheck = averageCheckBlock || {};
  const revenuePresent = Boolean(revenueBlock);
  const averageCheckPresent = Boolean(averageCheckBlock);
  const cards = [
    {
      label: t('dash.cardServiceCount'),
      value: formatNumber(revenue.service_count),
      delta: formatPct(revenue.service_count_change_pct),
      deltaValue: revenue.service_count_change_pct,
      present: revenuePresent,
    },
    {
      label: t('dash.cardServiceAverageCheck'),
      value: formatMoney(averageCheck.services),
      delta: formatPct(averageCheck.services_change_pct),
      deltaValue: averageCheck.services_change_pct,
      present: averageCheckPresent,
    },
    {
      label: t('dash.cardGoodsCount'),
      value: formatNumber(revenue.goods_count),
      delta: formatPct(revenue.goods_count_change_pct),
      deltaValue: revenue.goods_count_change_pct,
      present: revenuePresent,
    },
    {
      label: t('dash.cardGoodsAverageCheck'),
      value: formatMoney(averageCheck.goods),
      delta: formatPct(averageCheck.goods_change_pct),
      deltaValue: averageCheck.goods_change_pct,
      present: averageCheckPresent,
    },
    {
      label: t('dash.cardExtraServiceCount'),
      value: formatNumber(revenue.extra_service_count),
      delta: formatPct(revenue.extra_service_count_change_pct),
      deltaValue: revenue.extra_service_count_change_pct,
      present: revenuePresent,
    },
    {
      label: t('dash.cardExtraServiceAverageCheck'),
      value: formatMoney(averageCheck.extra_services),
      delta: formatPct(averageCheck.extra_services_change_pct),
      deltaValue: averageCheck.extra_services_change_pct,
      present: averageCheckPresent,
    },
  ];

  const visibleCards = usesAdministratorSchedule(summary)
    ? cards.filter((_, index) => index < 4)
    : cards;
  renderCards(els.servicesMetrics, presentCards(visibleCards));
}

function renderVisitMetrics(summary) {
  const visitMetrics = summary.visit_metrics || {};
  const cards = [
    {
      label: t('dash.cardOpzQty'),
      value: formatNumber(visitMetrics.opz_qty),
      delta: formatPct(visitMetrics.opz_qty_change_pct),
      deltaValue: visitMetrics.opz_qty_change_pct,
    },
    {
      label: t('dash.cardOpzPct'),
      value: formatMetricValue(visitMetrics.opz_pct, 'percent'),
      delta: formatPct(visitMetrics.opz_pct_change_pct),
      deltaValue: visitMetrics.opz_pct_change_pct,
    },
    {
      label: t('dash.cardExtraServicesPerAppointment'),
      value: formatMetricValue(visitMetrics.extra_services_per_appointment_pct, 'percent'),
      delta: formatPct(visitMetrics.extra_services_per_appointment_pct_change_pct),
      deltaValue: visitMetrics.extra_services_per_appointment_pct_change_pct,
    },
  ];

  renderCards(
    els.visitMetrics,
    usesAdministratorSchedule(summary) ? cards.slice(0, 2) : cards,
  );
}

function renderClientsMetrics(summary) {
  const visitMetrics = summary.visit_metrics || {};
  const clientFrequency = visitMetrics.client_visit_frequency || {};
  const oneVisit = clientFrequency.one_visit || {};
  const twoToThreeVisits = clientFrequency.two_to_three_visits || {};
  const fourPlusVisits = clientFrequency.four_plus_visits || {};
  const cards = [
    {
      label: t('dash.cardUniqueClients'),
      value: formatNumber(visitMetrics.unique_clients),
      delta: formatPct(visitMetrics.unique_clients_change_pct),
      deltaValue: visitMetrics.unique_clients_change_pct,
    },
    {
      label: t('dash.cardVisitsPerClient'),
      value: formatDecimal(visitMetrics.visits_per_client),
      delta: formatPct(visitMetrics.visits_per_client_change_pct),
      deltaValue: visitMetrics.visits_per_client_change_pct,
    },
    {
      label: t('dash.cardExtraServiceClients'),
      value: formatMetricValue(visitMetrics.extra_service_clients_pct, 'percent'),
      delta: t('dash.uniqueClientsCount', { count: formatNumber(visitMetrics.extra_service_clients) }),
      deltaValue: null,
    },
    {
      label: t('dash.cardOneVisitClients'),
      value: formatNumber(oneVisit.count),
      delta: t('dash.ofClients', { value: formatMetricValue(oneVisit.pct, 'percent') }),
      deltaValue: null,
    },
    {
      label: t('dash.cardTwoThreeVisitClients'),
      value: formatNumber(twoToThreeVisits.count),
      delta: t('dash.ofClients', { value: formatMetricValue(twoToThreeVisits.pct, 'percent') }),
      deltaValue: null,
    },
    {
      label: t('dash.cardFourPlusVisitClients'),
      value: formatNumber(fourPlusVisits.count),
      delta: t('dash.ofClients', { value: formatMetricValue(fourPlusVisits.pct, 'percent') }),
      deltaValue: null,
    },
    {
      label: t('dash.cardNewClients'),
      value: formatNumber(visitMetrics.new_clients),
      delta: formatPct(visitMetrics.new_clients_change_pct),
      deltaValue: visitMetrics.new_clients_change_pct,
      href: overviewReportUrl('new_vs_returning_cross'),
      action: t('dash.openReport'),
    },
    {
      label: t('dash.cardNewClientsPct'),
      value: formatMetricValue(visitMetrics.new_clients_pct, 'percent'),
      delta: formatPct(visitMetrics.new_clients_pct_change_pct),
      deltaValue: visitMetrics.new_clients_pct_change_pct,
      href: overviewReportUrl('new_vs_returning_cross'),
      action: t('dash.openReport'),
    },
    {
      label: t('dash.cardRepeatClients'),
      value: formatNumber(visitMetrics.repeat_clients),
      delta: formatPct(visitMetrics.repeat_clients_change_pct),
      deltaValue: visitMetrics.repeat_clients_change_pct,
      href: overviewReportUrl('new_vs_returning_cross'),
      action: t('dash.openReport'),
    },
    {
      label: t('dash.cardRepeatClientsPct'),
      value: formatMetricValue(visitMetrics.repeat_clients_pct, 'percent'),
      delta: formatPct(visitMetrics.repeat_clients_pct_change_pct),
      deltaValue: visitMetrics.repeat_clients_pct_change_pct,
      href: overviewReportUrl('new_vs_returning_cross'),
      action: t('dash.openReport'),
    },
  ];

  renderCards(
    els.clientsMetrics,
    usesAdministratorSchedule(summary) ? cards.filter((_, index) => index !== 2) : cards,
  );
}

function renderAppointmentsMetrics(summary) {
  const breakdown = summary.appointments_breakdown || {};
  const exact = breakdown.source_status === 'ready';
  const ready = exact || breakdown.source_status === 'local';
  const metricValue = (value) => (ready ? formatNumber(value) : t('dash.noData'));
  const metricShare = (value) => (ready ? t('dash.ofTotal', { value: `${formatNumber(value)}%` }) : '');
  const cards = [
    {
      label: t('dash.cardTotalAppointments'),
      value: metricValue(breakdown.total),
      delta: metricShare(breakdown.total_share_pct),
      deltaValue: null,
    },
    {
      label: t('dash.cardCancelledAppointments'),
      value: metricValue(breakdown.cancelled),
      delta: metricShare(breakdown.cancelled_share_pct),
      deltaValue: null,
    },
    {
      label: t('dash.cardCompletedAppointments'),
      value: metricValue(breakdown.completed),
      delta: metricShare(breakdown.completed_share_pct),
      deltaValue: null,
    },
    {
      label: t('dash.cardIncompleteAppointments'),
      value: metricValue(breakdown.incomplete),
      delta: metricShare(breakdown.incomplete_share_pct),
      deltaValue: null,
    },
  ];

  renderCards(els.appointmentsMetrics, cards);
  els.appointmentsWarning.textContent = ready
    ? exact ? '' : t('dash.appointmentsLocalEstimate')
    : t('dash.appointmentsUnavailable');
  els.appointmentsWarning.classList.toggle('visible', !exact);
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
          label: t('dash.revenue'),
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
          label: t('dash.appointments'),
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
          label: t('dash.cardOpzQty'),
          data: daily.map((item) => item.opz_qty || 0),
          backgroundColor: '#7c3aed',
          borderRadius: 4,
          yAxisID: 'y',
        },
        {
          type: 'line',
          label: t('dash.completedVisitShare'),
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
        y: { beginAtZero: true, title: { display: true, text: t('dash.opzShort') } },
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
      labels: services.map((item) => item.title || t('dash.serviceFallback', { id: item.service_id || '' })),
      datasets: [
        {
          label: t('dash.revenue'),
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
    els.servicesTable.innerHTML = `<div class="empty">${escapeHtml(t('dash.noServicesForPeriod'))}</div>`;
    return;
  }

  els.servicesTable.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>${escapeHtml(t('dash.service'))}</th>
          <th class="number">${escapeHtml(t('dash.sold'))}</th>
          <th class="number">${escapeHtml(t('dash.revenue'))}</th>
        </tr>
      </thead>
      <tbody>
        ${services
          .map(
            (item) => `
              <tr>
                <td>${escapeHtml(item.title || t('dash.serviceFallback', { id: item.service_id || '' }))}</td>
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
    els.extraServicesTable.innerHTML = `<div class="empty">${escapeHtml(t('dash.noExtraServicesForPeriod'))}</div>`;
    return;
  }

  els.extraServicesTable.innerHTML = `
    <div class="table-box-scroll extra-services-scroll">
    <table>
      <thead>
        <tr>
          <th>${escapeHtml(t('dash.extraService'))}</th>
          <th class="number">${escapeHtml(t('dash.doneCount'))}</th>
          <th class="number">${escapeHtml(t('dash.branchesCount'))}</th>
          <th class="number">${escapeHtml(t('dash.revenue'))}</th>
        </tr>
      </thead>
      <tbody>
        ${services
          .map(
            (item) => `
              <tr>
                <td>${escapeHtml(item.title || t('dash.serviceFallback', { id: item.service_id || '' }))}</td>
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

function planMetricOptions() {
  return currentPlanFactPayload?.metrics || [];
}

function setPlanColumnPickerOpen(open, { restoreFocus = false } = {}) {
  if (!els.planColumnMenu || !els.planColumnToggle) return;
  const nextOpen = Boolean(open && !els.planColumnToggle.disabled);
  els.planColumnMenu.hidden = !nextOpen;
  els.planColumnToggle.setAttribute('aria-expanded', String(nextOpen));
  if (!nextOpen && restoreFocus) els.planColumnToggle.focus();
}

function updatePlanColumnPickerState() {
  const metrics = planMetricOptions();
  hiddenPlanMetricCodes = normalizeHiddenPlanMetricCodes(metrics, hiddenPlanMetricCodes);
  const hiddenCount = hiddenPlanMetricCodes.size;
  const visibleCount = metrics.length - hiddenCount;

  els.planColumnToggle.disabled = metrics.length === 0;
  els.planColumnToggle.setAttribute(
    'aria-label',
    t('dash.columnsButtonAria', { count: hiddenCount }),
  );
  els.planColumnCount.textContent = t('dash.columnsHiddenCount', { count: hiddenCount });
  els.planColumnCount.hidden = hiddenCount === 0;

  els.planColumnList.querySelectorAll('[data-plan-metric-code]').forEach((input) => {
    const hidden = hiddenPlanMetricCodes.has(input.dataset.planMetricCode);
    input.checked = !hidden;
    input.disabled = !hidden && visibleCount <= 1;
  });

  const hideMoneyResult = hideMoneyPlanMetrics(metrics, hiddenPlanMetricCodes);
  els.planColumnsHideMoney.disabled = hideMoneyResult.size === hiddenPlanMetricCodes.size
    && [...hideMoneyResult].every((code) => hiddenPlanMetricCodes.has(code));
  els.planColumnsShowAll.disabled = hiddenCount === 0;
  if (!metrics.length) setPlanColumnPickerOpen(false);
}

function renderPlanColumnPicker() {
  const metrics = planMetricOptions();
  hiddenPlanMetricCodes = normalizeHiddenPlanMetricCodes(metrics, hiddenPlanMetricCodes);
  els.planColumnList.innerHTML = metrics
    .map((metric) => `
      <label class="plan-column-option">
        <input
          type="checkbox"
          data-plan-metric-code="${escapeHtml(metric.code)}"
          ${hiddenPlanMetricCodes.has(metric.code) ? '' : 'checked'}
        />
        <span>${escapeHtml(metric.label)}</span>
      </label>
    `)
    .join('');
  updatePlanColumnPickerState();
}

function renderCurrentPlanFact() {
  renderPlanFact(filterPlanFactForDisplay(currentPlanFactPayload, hiddenPlanMetricCodes));
}

function applyPlanMetricVisibility(nextHiddenCodes) {
  hiddenPlanMetricCodes = nextHiddenCodes;
  renderCurrentPlanFact();
  updatePlanColumnPickerState();
}

function renderPlanTable(groups, metrics) {
  const rowTypes = [
    ['plan', t('dash.plan')],
    ['fact', t('dash.fact')],
    ['remaining', t('dash.remaining')],
    ['completion_pct', t('dash.completionPct')],
  ];

  return `
    <div class="table-scroll">
      <table class="plan-table">
        <thead>
          <tr>
            <th>${t('dash.dimension')}</th>
            <th>${t('dash.metric')}</th>
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

const PLAN_SCOPE_ORDER = ['personal', 'administrator_records', 'administrator_shift'];

function planScopeLabel(scope) {
  const labels = {
    personal: t('dash.scopePersonal'),
    administrator_records: t('dash.scopeAdministratorRecords'),
    administrator_shift: t('dash.scopeAdministratorShift'),
  };
  return labels[scope] || labels.personal;
}

function renderStaffCategorySections(prefix, groups, metricSets, metrics) {
  const sections = [];
  const categoryOrder = ['barber', 'administrator', 'unknown'];
  categoryOrder.forEach((category) => {
    const categoryGroups = groups.filter((group) => (group.category || 'unknown') === category);
    if (!categoryGroups.length) return;
    const categoryMetrics = metricSets[category] || metrics;
    const label = categoryGroups[0].category_label || category;
    const baseTitle = prefix ? `${prefix} · ${label}` : label;
    const scopeByCode = new Map(
      categoryGroups
        .flatMap((group) => group.metrics || [])
        .map((metric) => [metric.code, metric.calculation_scope || 'personal']),
    );
    const scopes = category === 'administrator' ? PLAN_SCOPE_ORDER : ['personal'];
    scopes.forEach((scope) => {
      const scopeMetrics = categoryMetrics.filter(
        (metric) => (scopeByCode.get(metric.code) || 'personal') === scope,
      );
      if (!scopeMetrics.length) return;
      const title = category === 'administrator'
        ? `${baseTitle} · ${planScopeLabel(scope)}`
        : baseTitle;
      sections.push(renderPlanSection(
        title,
        categoryGroups,
        scopeMetrics,
        t('dash.staffCount', { count: categoryGroups.length }),
      ));
    });
  });
  return sections;
}

function renderSelectedStaffPlanTable(staffPlan) {
  if (!staffPlan?.metrics?.length) return '';
  const title = t('dash.staffPlanTitle', {
    name: staffPlan.title || t('dash.staffFallbackLower'),
  });
  return PLAN_SCOPE_ORDER.map((scope) => {
    const metrics = staffPlan.metrics.filter(
      (metric) => (metric.calculation_scope || 'personal') === scope,
    );
    if (!metrics.length) return '';
    return `
      <section class="plan-section selected-staff-plan">
        <div class="plan-section-title">
          <h3>${escapeHtml(`${title} · ${planScopeLabel(scope)}`)}</h3>
          <span class="meta">${escapeHtml(staffPlan.category_label || '')}</span>
        </div>
        <div class="table-scroll staff-plan-scroll">
          <table class="staff-plan-table">
            <thead>
              <tr>
                <th>KPI</th>
                <th class="number">${t('dash.plan')}</th>
                <th class="number">${t('dash.fact')}</th>
                <th class="number">${t('dash.completionPct')}</th>
              </tr>
            </thead>
            <tbody>
              ${metrics.map((metric) => `
                <tr>
                  <td>${escapeHtml(metric.label)}</td>
                  <td class="number">${escapeHtml(formatMetricValue(metric.plan, metric.format))}</td>
                  <td class="number">${escapeHtml(formatMetricValue(metric.fact, metric.format))}</td>
                  <td class="number metric-status ${escapeHtml(metric.status || 'no-plan')}">
                    ${escapeHtml(formatMetricValue(metric.completion_pct, 'percent'))}
                  </td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>
      </section>
    `;
  }).join('');
}

function chartValue(value) {
  return Number(value || 0);
}

function buildStaffPlanChart(canvas, metrics, factColor) {
  const format = metrics[0]?.format || 'number';
  return new Chart(canvas, {
    type: 'bar',
    data: {
      labels: metrics.map((metric) => metric.label),
      datasets: [
        {
          label: t('dash.plan'),
          data: metrics.map((metric) => chartValue(metric.plan)),
          backgroundColor: '#94a3b8',
          borderRadius: 4,
        },
        {
          label: t('dash.fact'),
          data: metrics.map((metric) => chartValue(metric.fact)),
          backgroundColor: factColor,
          borderRadius: 4,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: { y: { beginAtZero: true, ticks: { callback: (value) => formatMetricValue(value, format) } } },
      plugins: {
        tooltip: {
          callbacks: { label: (ctx) => ` ${ctx.dataset.label}: ${formatMetricValue(ctx.parsed.y, format)}` },
        },
      },
    },
  });
}

function renderPlanInsights(planFact) {
  charts.staffPlanGroups.forEach((chart) => chart.destroy());
  charts.staffPlanGroups = [];
  destroyChart('goodsKpi');
  if (!els.planInsights) return;

  const goodsKpis = planFact?.goods_kpi_execution || [];
  const selectedStaffPlan = planFact?.selected_staff_plan;
  const panels = [];

  const hasSelectedStaffCharts = Boolean(selectedStaffPlan?.metrics?.length);
  const staffPlanScaleGroups = [];
  if (hasSelectedStaffCharts) {
    const visibleMetrics = selectedStaffPlan.metrics.filter(
      (metric) => (metric.plan !== null && metric.plan !== undefined) || chartValue(metric.fact) !== 0,
    );
    const title = t('dash.planVsFactTitle', { name: escapeHtml(selectedStaffPlan.title || t('dash.staffFallbackLower')) });
    const meta = escapeHtml(selectedStaffPlan.category_label || '');
    PLAN_SCOPE_ORDER.forEach((scope) => {
      const scopeMetrics = visibleMetrics.filter(
        (metric) => (metric.calculation_scope || 'personal') === scope,
      );
      PLAN_SCALE_GROUPS.forEach((scale) => {
        const groupMetrics = scopeMetrics.filter(
          (metric) => (metric.format || 'number') === scale.format,
        );
        if (!groupMetrics.length) return;
        const heading = `${title} · ${planScopeLabel(scope)} · ${t(scale.labelKey)}`;
        if (scale.perMetric) {
          const cells = groupMetrics
            .map((metric, index) => {
              const canvasId = `selected-staff-plan-${scope}-${scale.format}-${index}`;
              staffPlanScaleGroups.push({ canvasId, metrics: [metric], color: scale.color });
              return `<div class="chart-box short"><canvas id="${canvasId}"></canvas></div>`;
            })
            .join('');
          panels.push(`
            <div class="panel wide">
              <div class="panel-title"><h2>${heading}</h2><span class="meta">${meta}</span></div>
              <div class="plan-scale-row">${cells}</div>
            </div>
          `);
        } else {
          const canvasId = `selected-staff-plan-${scope}-${scale.format}`;
          staffPlanScaleGroups.push({ canvasId, metrics: groupMetrics, color: scale.color });
          panels.push(`
            <div class="panel wide">
              <div class="panel-title"><h2>${heading}</h2><span class="meta">${meta}</span></div>
              <div class="chart-box short"><canvas id="${canvasId}"></canvas></div>
            </div>
          `);
        }
      });
    });
  }

  // The goods KPI chart repeats the count chart's wax/camouflage/care metrics,
  // so only show it in the network/branch view, never for a selected employee.
  if (goodsKpis.length && !selectedStaffPlan) {
    panels.push(`
      <div class="panel wide">
        <div class="panel-title">
          <h2>${t('dash.goodsKpiCompletion')}</h2>
          <span class="meta">${goodsKpis.length} KPI</span>
        </div>
        <div class="chart-box short"><canvas id="goods-kpi-chart"></canvas></div>
      </div>
    `);
  }

  els.planInsights.innerHTML = panels.join('');

  staffPlanScaleGroups.forEach((group) => {
    const canvas = document.getElementById(group.canvasId);
    if (!canvas) return;
    charts.staffPlanGroups.push(buildStaffPlanChart(canvas, group.metrics, group.color));
  });

  const goodsCanvas = document.getElementById('goods-kpi-chart');
  if (goodsCanvas && goodsKpis.length) {
    charts.goodsKpi = new Chart(goodsCanvas, {
      type: 'bar',
      data: {
        labels: goodsKpis.map((metric) => metric.label),
        datasets: [
          {
            label: t('dash.plan'),
            data: goodsKpis.map((metric) => chartValue(metric.plan)),
            backgroundColor: '#94a3b8',
            borderRadius: 4,
          },
          {
            label: t('dash.fact'),
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
                return item ? [t('dash.completionTooltip', { value: formatMetricValue(item.completion_pct, 'percent') })] : [];
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
          const scheduleCoverage = isScheduleAttributionDiagnostic(item.code);
          const details = scheduleCoverage
            ? [
                t('dash.diagnosticRequiredPeriod', {
                  start: item.required_start,
                  end: item.required_end,
                }),
                item.covered_start && item.covered_end
                  ? t('dash.diagnosticCoveredPeriod', {
                      start: item.covered_start,
                      end: item.covered_end,
                    })
                  : t('dash.diagnosticNoCoverage'),
              ]
            : [
                t('dash.diagnosticBarbers', { count: formatNumber(item.barber_clients_fact) }),
                t('dash.diagnosticAdmins', { count: formatNumber(item.administrator_clients_fact) }),
                t('dash.diagnosticUnassigned', { count: formatNumber(item.unassigned_records_count) }),
              ];
          const message = scheduleCoverage
            ? t('dash.scheduleCoverageWarning', { branch: item.company_title || '' })
            : item.message || t('dash.dataCheck');
          return `
            <div class="diagnostic warning">
              <strong>${escapeHtml(message)}</strong>
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
    els.planFactTable.innerHTML = `${diagnosticsHtml}<div class="empty">${t('dash.noPlanForPeriod')}</div>`;
    els.planMeta.textContent = '';
    return;
  }
  renderPlanInsights(planFact);

  const metricSets = planFact?.metric_sets || {};
  if (planFact?.view_scope === 'staff') {
    const sections = [];
    if (planFact.selected_staff_plan) {
      sections.push(renderSelectedStaffPlanTable(planFact.selected_staff_plan));
    } else {
      if (planFact.parent_group) {
        const branchTitle = planFact.branch?.title || planFact.parent_group.title || t('dash.branch');
        sections.push(renderPlanSection(branchTitle, [planFact.parent_group], metricSets.branch || metrics));
      }
      sections.push(...renderStaffCategorySections('', groups, metricSets, metrics));
    }

    els.planFactTable.innerHTML = diagnosticsHtml + (
      sections.join('') || `<div class="empty">${t('dash.noStaffForBranch')}</div>`
    );
  } else {
    els.planFactTable.innerHTML = diagnosticsHtml + renderPlanTable(groups, metrics);
  }

  const planPeriod = planFact?.plan_period;
  const planPeriodText = planPeriod ? t('dash.planPeriodMeta', { start: planPeriod.start, end: planPeriod.end }) : '';
  const selectedStaff = planFact?.selected_staff;
  const scopeText = planFact?.view_scope === 'staff'
    ? `${planFact.branch?.title || t('dash.branch')} · ${selectedStaff?.name || t('dash.staffPlural')}`
    : t('dash.networkAndBranches');
  els.planMeta.textContent = t('dash.planMeta', { scope: scopeText, count: groups.length, period: planPeriodText });
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
    els.planSettingsBranches.innerHTML = `<div class="empty compact">${t('dash.noBranches')}</div>`;
    return;
  }
  const fields = BRANCH_PLAN_SETTING_FIELDS.map((field) => [field, planSettingFieldLabel(field)]);
  els.planSettingsBranchMeta.textContent = t('dash.branchesMeta', { count: rows.length });
  els.planSettingsBranches.innerHTML = `
    <div class="table-scroll plan-settings-scroll">
      <table class="plan-settings-table">
        <thead>
          <tr>
            <th>${t('dash.branch')}</th>
            ${fields.map(([, label]) => `<th class="number">${escapeHtml(label)}</th>`).join('')}
          </tr>
        </thead>
        <tbody>
          ${rows
            .map((row) => `
              <tr>
                <td>${escapeHtml(row.company_title || t('dash.branchFallbackWithId', { id: row.company_id }))}</td>
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
        <span class="meta">${t('dash.staffCount', { count: rows.length })}</span>
      </div>
      <div class="table-scroll plan-settings-scroll">
        <table class="plan-settings-table">
          <thead>
            <tr>
              <th>${t('dash.branch')}</th>
              <th>${t('dash.name')}</th>
              <th class="number">staff_id</th>
              <th class="number">user_id</th>
              ${fields.map(([, label]) => `<th class="number">${escapeHtml(label)}</th>`).join('')}
            </tr>
          </thead>
          <tbody>
            ${rows
              .map((row) => `
                <tr>
                  <td>${escapeHtml(row.company_title || t('dash.branchFallbackWithId', { id: row.company_id }))}</td>
                  <td>${escapeHtml(row.staff_name || t('dash.staffFallback', { id: row.staff_id }))}</td>
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
  els.planSettingsStaffMeta.textContent = t('dash.planSettingsStaffMeta', { barbers: barbers.length, admins: admins.length });
  els.planSettingsStaff.innerHTML = [
    renderPlanSettingsStaffSection(t('dash.barbers'), barbers, [
      ...STAFF_PLAN_SETTING_FIELDS_BY_CATEGORY.barber.map((field) => [field, planSettingFieldLabel(field)]),
    ]),
    renderPlanSettingsStaffSection(
      t('dash.administrators'),
      admins,
      STAFF_PLAN_SETTING_FIELDS_BY_CATEGORY.administrator.map((field) => [field, planSettingFieldLabel(field)]),
    ),
  ].join('') || `<div class="empty compact">${t('dash.noActiveStaff')}</div>`;
}

function setPlanSettingsDirty(isDirty) {
  planSettingsDirty = isDirty;
  els.planSettingsDirty.classList.toggle('visible', isDirty);
  els.planSettingsSave.disabled = planSettingsSaving || !planSettingsData;
  els.planSettingsReset.disabled = planSettingsSaving || !isDirty || !planSettingsSavedData;
  updateFloatingEditorSave();
}

function planSettingsInputValue(selector) {
  const input = document.querySelector(selector);
  const value = input?.value.trim() || '';
  return value === '' ? null : value;
}

function planSettingFieldLabel(field) {
  const translationKeys = {
    wax_pct: 'dash.waxPct',
    head_care_pct: 'dash.headCarePct',
    face_care_pct: 'dash.faceCarePct',
    camouflage_pct: 'dash.camouflagePct',
    cosmo_pct: 'dash.cosmoPct',
    opz_pct: 'dash.opzPct',
    cosmo_price: 'dash.cosmoPrice',
    clients: 'dash.clientsCount',
    avg_check_total: 'dash.averageCheckShort',
    reviews_qty: 'dash.reviews',
    cosmo_qty: 'dash.cosmoQty',
    extra_services_qty: 'dash.extraServicesQty',
    extra_services_pct: 'dash.extraServicesPct',
  };
  return t(translationKeys[field]);
}

function collectPlanSettingsPayload() {
  const readValue = (scope, row, field) => {
    const idSelector = scope === 'branch'
      ? `[data-company-id="${row.company_id}"]`
      : `[data-staff-id="${row.staff_id}"]`;
    return planSettingsInputValue(`input[data-plan-${scope}]${idSelector}[data-field="${field}"]`);
  };
  return buildPlanSettingsPayload(planSettingsData, els.planSettingsMonth.value, readValue);
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
    ? t('dash.lastSaveAt', { value: formatMoscowDateTime(data.last_saved_at) })
    : t('dash.lastSaveNone');

  if (updateSnapshot) {
    planSettingsSavedData = JSON.parse(JSON.stringify(data));
    planSettingsSavedSnapshot = JSON.stringify(collectPlanSettingsPayload());
    setPlanSettingsDirty(false);
  } else {
    setPlanSettingsDirty(dirty);
  }
}

function confirmDiscardServiceManagement() {
  return !serviceManagementDirty || window.confirm(t('dash.confirmDiscardServiceManagement'));
}

function setPlanSettingsLoading(isLoading) {
  els.planSettingsLoad.disabled = isLoading;
  els.planSettingsCopy.disabled = isLoading;
  els.planSettingsSave.disabled = isLoading || !planSettingsData;
  els.planSettingsReset.disabled = isLoading || !planSettingsDirty || !planSettingsSavedData;
  els.planSettingsLoad.textContent = isLoading ? t('common.loadingShort') : t('dash.load');
  els.planSettingsSave.textContent = isLoading ? t('common.saving') : t('dash.save');
}

async function loadPlanSettings({ month = els.planSettingsMonth.value, copyFrom = null, dirty = false } = {}) {
  const request = beginViewRequest('planSettings');
  clearError();
  setPlanSettingsLoading(true);
  const syncStatus = loadSyncStatus();
  try {
    const params = { month };
    if (copyFrom) params.copy_from = copyFrom;
    const payload = await fetchJson('/dashboard/plan/settings', params, {
      retry: () => loadPlanSettings({ month, copyFrom, dirty }),
      signal: request.signal,
    });
    if (!request.isCurrent()) return;
    renderPlanSettings(payload.data, { updateSnapshot: !copyFrom, dirty });
    viewsWithData.add('planSettings');
    clearError();
    setLoadedApiState(payload.data, {
      empty: !(payload.data?.branches?.length || payload.data?.staff?.length),
    });
    await syncStatus;
  } catch (error) {
    if (isSupersededRequest(error) || !request.isCurrent()) return;
    showError(error.message, { apiStatus: error.apiStatus, retry: () => loadPlanSettings({ month, copyFrom, dirty }) });
  } finally {
    if (request.isCurrent()) {
      setPlanSettingsLoading(false);
      request.finish();
    }
  }
}

async function savePlanSettings() {
  if (planSettingsSaving || !planSettingsData) return;
  planSettingsSaving = true;
  updateFloatingEditorSave();
  clearError();
  setPlanSettingsLoading(true);
  setApiState(t('dash.apiSaving'), 'warn');
  try {
    const requestPayload = collectPlanSettingsPayload();
    if (planSettingsData.copy_from) requestPayload.copy_from = planSettingsData.copy_from;
    const payload = await postJson('/dashboard/plan/settings', requestPayload);
    renderPlanSettings(payload.data);
    setApiState(t('dash.apiConnected'), 'ok');
  } catch (error) {
    showError(error.message, { apiStatus: error.apiStatus, retry: () => savePlanSettings() });
  } finally {
    planSettingsSaving = false;
    setPlanSettingsLoading(false);
    updateFloatingEditorSave();
  }
}

async function copyPreviousPlanSettings() {
  const month = els.planSettingsMonth.value || currentMonthValue();
  await protectedChangesGuard.run(() => loadPlanSettings({
    month,
    copyFrom: previousMonthValue(month),
    dirty: true,
  }));
}

async function reloadPlanSettingsMonth() {
  const month = els.planSettingsMonth.value || currentMonthValue();
  const loaded = await protectedChangesGuard.run(() => loadPlanSettings({ month }));
  if (!loaded) {
    els.planSettingsMonth.value = planSettingsLoadedMonth || currentMonthValue();
  }
}

function renderServiceBranchOptions() {
  const selected = els.serviceFilterBranch.value;
  els.serviceFilterBranch.innerHTML = `<option value="">${t('dash.allBranches')}</option>`;
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
  els.serviceFilterCategory.innerHTML = `<option value="">${t('dash.allCategories')}</option>`;
  (data.categories || []).forEach((category) => {
    const option = document.createElement('option');
    option.value = category;
    option.textContent = category;
    els.serviceFilterCategory.appendChild(option);
  });
  els.serviceFilterCategory.value = (data.categories || []).includes(selectedCategory) ? selectedCategory : '';

  const selectedGroup = els.serviceFilterGroup.value;
  els.serviceFilterGroup.innerHTML = `<option value="">${t('dash.allGroups')}</option>`;
  (data.groups || []).forEach((group) => {
    const option = document.createElement('option');
    option.value = group.id;
    option.textContent = group.is_active ? group.title : t('dash.archivedName', { name: group.title });
    els.serviceFilterGroup.appendChild(option);
  });
  els.serviceFilterGroup.value = (data.groups || []).some((group) => String(group.id) === selectedGroup) ? selectedGroup : '';
}

function serviceGroupOptionsHtml(selectedGroupId) {
  const groups = serviceManagementData.groups || [];
  const selected = selectedGroupId === null || selectedGroupId === undefined ? '' : String(selectedGroupId);
  const assignedInactive = groups.find((group) => String(group.id) === selected && !group.is_active);
  const activeGroups = groups.filter((group) => group.is_active);
  const options = [`<option value="">${t('dash.noGroup')}</option>`];
  activeGroups.forEach((group) => {
    options.push(`<option value="${escapeHtml(group.id)}" ${String(group.id) === selected ? 'selected' : ''}>${escapeHtml(group.title)}</option>`);
  });
  if (assignedInactive) {
    options.push(`<option value="${escapeHtml(assignedInactive.id)}" selected disabled>${escapeHtml(t('dash.archivedName', { name: assignedInactive.title }))}</option>`);
  }
  return options.join('');
}

function renderServiceCatalog(rows) {
  els.serviceCatalogMeta.textContent = t('dash.servicesCount', { count: rows.length });
  if (!rows.length) {
    els.serviceCatalogTable.innerHTML = `<div class="empty compact">${t('dash.noServicesForFilters')}</div>`;
    return;
  }
  els.serviceCatalogTable.innerHTML = `
    <div class="table-scroll service-catalog-scroll">
      <table class="service-table">
        <thead>
          <tr>
            <th>${t('dash.branch')}</th>
            <th>${t('dash.category')}</th>
            <th class="number">ID</th>
            <th>${t('dash.name')}</th>
            <th>${t('dash.extraService')}</th>
            <th>${t('dash.kpiGroup')}</th>
            <th>${t('dash.updated')}</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((row) => `
            <tr>
              <td>${escapeHtml(row.company_title || t('dash.branchFallbackWithId', { id: row.company_id }))}</td>
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
              <td>${escapeHtml(formatMoscowDateTime(latestServiceManagementTimestamp(
                row.updated_at,
                row.label_updated_at,
                row.kpi_assignment_updated_at,
                row.mutation_updated_at,
              )) || '')}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function renderServiceKpiGroups(groups) {
  els.serviceKpiGroupsMeta.textContent = t('dash.groupsCount', { count: groups.length });
  if (!groups.length) {
    els.serviceKpiGroupsTable.innerHTML = `<div class="empty compact">${t('dash.noKpiGroups')}</div>`;
    return;
  }
  els.serviceKpiGroupsTable.innerHTML = `
    <div class="table-scroll">
      <table class="service-group-table">
        <thead>
          <tr>
            <th>${t('dash.name')}</th>
            <th>${t('dash.code')}</th>
            <th>${t('dash.description')}</th>
            <th class="number">${t('dash.sortOrder')}</th>
            <th>${t('dash.active')}</th>
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
              <td class="number"><button type="button" class="secondary" data-service-group-archive>${t('dash.archive')}</button></td>
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
  updateServiceManagementControls();
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
  serviceManagementLoading = isLoading;
  updateServiceManagementControls();
  els.serviceFilterLoad.textContent = isLoading ? t('common.loadingShort') : t('dash.refresh');
  els.serviceManagementSave.textContent = isLoading ? t('common.saving') : t('dash.save');
}

function setServiceManagementMutationLoading(isLoading) {
  serviceManagementMutationPending = isLoading;
  setServiceManagementLoading(isLoading);
}

function updateServiceManagementControls() {
  const controls = serviceManagementControls({
    loading: serviceManagementLoading,
    hasData: Boolean(serviceManagementSavedData),
    hasSavedData: Boolean(serviceManagementSavedData),
    dirty: serviceManagementDirty,
  });
  els.serviceFilterLoad.disabled = controls.refreshDisabled;
  [
    els.serviceFilterBranch,
    els.serviceFilterCategory,
    els.serviceFilterGroup,
    els.serviceFilterExtra,
    els.serviceFilterQuery,
  ].forEach((filter) => {
    filter.disabled = controls.filtersDisabled;
  });
  [
    els.serviceGroupTitle,
    els.serviceGroupCode,
    els.serviceGroupDescription,
    ...els.serviceCatalogTable.querySelectorAll('input, select, button'),
    ...els.serviceKpiGroupsTable.querySelectorAll('input, select, button'),
  ].forEach((editor) => {
    editor.disabled = controls.editorDisabled;
  });
  els.serviceManagementSave.disabled = controls.saveDisabled;
  els.serviceManagementReset.disabled = controls.resetDisabled;
  els.serviceGroupAdd.disabled = controls.addGroupDisabled;
}

async function loadServiceManagement() {
  if (!serviceManagementLoadAllowed({
    loading: serviceManagementLoading,
    dirty: serviceManagementDirty,
  })) return;
  const request = beginViewRequest('serviceManagement');
  clearError();
  setServiceManagementLoading(true);
  try {
    const payload = await fetchJson('/dashboard/services', serviceManagementParams(), {
      retry: () => loadServiceManagement(),
      signal: request.signal,
    });
    if (!request.isCurrent()) return;
    renderServiceManagement(payload.data);
    viewsWithData.add('serviceManagement');
    clearError();
    setLoadedApiState(payload.data, {
      empty: !(payload.data?.rows?.length || payload.data?.groups?.length),
    });
    void loadSyncStatus();
  } catch (error) {
    if (isSupersededRequest(error) || !request.isCurrent()) return;
    showError(error.message, { apiStatus: error.apiStatus, retry: () => loadServiceManagement() });
  } finally {
    settleServiceManagementLoad(request, setServiceManagementLoading);
  }
}

async function saveServiceManagement() {
  if (serviceManagementLoading || !serviceManagementDirty) return;
  clearError();
  const current = collectServiceManagementPayload();
  const saved = serviceManagementSavedSnapshot ? JSON.parse(serviceManagementSavedSnapshot) : { rows: [], groups: [] };
  const changes = serviceManagementChanges(current, saved);
  if (!changes.row_changes.length && !changes.group_changes.length) {
    setServiceManagementDirty(false);
    return;
  }
  setApiState(t('dash.apiSaving'), 'warn');
  await runServiceManagementMutation(
    () => patchJson('/dashboard/services', changes),
    {
      setLoading: setServiceManagementMutationLoading,
      onSuccess: (payload) => {
        const currentData = mergeServiceManagementResult(serviceManagementData, current);
        const mergedData = mergeServiceManagementResult(currentData, payload.data);
        renderServiceManagement(filterServiceManagementResult(mergedData, serviceManagementParams()));
        setApiState(t('dash.apiConnected'), 'ok');
      },
      onError: (error) => {
        showError(error.message, { apiStatus: error.apiStatus, retry: () => saveServiceManagement() });
      },
    },
  );
}

async function addServiceKpiGroup() {
  if (serviceManagementLoading || serviceManagementDirty) return;
  clearError();
  const title = els.serviceGroupTitle.value.trim();
  if (!title) {
    showError(t('dash.kpiGroupTitleRequired'));
    return;
  }
  setApiState(t('dash.apiSaving'), 'warn');
  await runServiceManagementMutation(
    () => postJson('/dashboard/services/kpi_groups', {
      title,
      code: els.serviceGroupCode.value.trim() || null,
      description: els.serviceGroupDescription.value.trim() || null,
      is_active: true,
    }),
    {
      setLoading: setServiceManagementMutationLoading,
      onSuccess: (payload) => {
        renderServiceManagement(mergeServiceManagementResult(serviceManagementData, {
          rows: [],
          groups: [payload.data],
        }));
        els.serviceGroupTitle.value = '';
        els.serviceGroupCode.value = '';
        els.serviceGroupDescription.value = '';
        setApiState(t('dash.apiConnected'), 'ok');
      },
      onError: (error) => showError(error.message),
    },
  );
}

function renderReviewFactEditor(data) {
  reviewFactRows = data?.rows || [];
  const totalValue = data?.total_value || 0;
  els.reviewFactMeta.textContent = t('dash.reviewFactMeta', { admins: reviewFactRows.length, reviews: formatNumber(totalValue) });

  if (!reviewFactRows.length) {
    els.reviewFactEditor.innerHTML = `<div class="empty compact">${t('dash.noActiveAdministrators')}</div>`;
  } else {
    els.reviewFactEditor.innerHTML = `
      <div class="table-scroll manual-fact-scroll">
        <table class="manual-fact-table">
          <thead>
            <tr>
              <th>${t('dash.branch')}</th>
              <th>${t('dash.administrator')}</th>
              <th class="number">${t('dash.reviewsFact')}</th>
            </tr>
          </thead>
          <tbody>
            ${reviewFactRows
              .map((row) => {
                const inactiveMark = row.is_active === false
                  ? ` <span class="meta">· ${escapeHtml(t('dash.reviewFactInactive'))}</span>`
                  : '';
                return `
                  <tr>
                    <td>${escapeHtml(row.company_title || t('dash.branchFallbackWithId', { id: row.company_id }))}</td>
                    <td>${escapeHtml(row.staff_name)}${inactiveMark}</td>
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

  reviewFactSavedData = JSON.parse(JSON.stringify(data || { rows: [] }));
  reviewFactLoadedFilters = reviewFactFilters();
  reviewFactSavedSnapshot = reviewFactDraftSnapshot();
  setReviewFactDirty(false);
}

function reviewFactFilters() {
  const filter = filterEls.reviewFacts;
  return {
    month: filter.month.value,
    branch: filter.branch.value,
    staff: filter.staff.value,
  };
}

function restoreReviewFactFilters() {
  if (!reviewFactLoadedFilters) return;
  const filter = filterEls.reviewFacts;
  filter.month.value = reviewFactLoadedFilters.month;
  filter.branch.value = reviewFactLoadedFilters.branch;
  filter.staff.value = reviewFactLoadedFilters.staff;
  customFilterDropdowns[filter.branch.id]?.refresh();
  customFilterDropdowns[filter.staff.id]?.refresh();
}

function reviewFactDraftSnapshot() {
  return JSON.stringify([...els.reviewFactEditor.querySelectorAll('input[data-staff-id]')].map((input) => ({
    companyId: input.dataset.companyId,
    staffId: input.dataset.staffId,
    value: input.value.trim(),
  })));
}

function setReviewFactDirty(isDirty) {
  reviewFactDirty = isDirty;
  els.reviewFactSave.disabled = reviewFactSaving || !reviewFactRows.length;
  updateFloatingEditorSave();
}

function updateReviewFactDirtyFromForm() {
  setReviewFactDirty(reviewFactDraftSnapshot() !== reviewFactSavedSnapshot);
}

function reviewFactParams() {
  const filter = filterEls.reviewFacts;
  return {
    month: filter.month.value,
    company_id: filter.branch.value,
    staff_id: filter.staff.value,
  };
}

async function loadReviewFactEditor({ signal = null } = {}) {
  const payload = await fetchJson('/dashboard/plan/reviews_fact', reviewFactParams(), {
    retry: () => loadReviewFacts(),
    signal,
  });
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
      throw new Error(t('dash.reviewFactNonNegative'));
    }
    return {
      company_id: Number(input.dataset.companyId),
      staff_id: Number(input.dataset.staffId),
      value,
    };
  });

  return {
    month: filter.month.value,
    company_id: filter.branch.value ? Number(filter.branch.value) : null,
    staff_id: filter.staff.value ? Number(filter.staff.value) : null,
    items,
  };
}

async function saveReviewFactEditor() {
  if (reviewFactSaving || !reviewFactRows.length) return;
  reviewFactSaving = true;
  updateFloatingEditorSave();
  clearError();
  els.reviewFactSave.disabled = true;
  els.reviewFactSave.textContent = t('common.saving');
  setApiState(t('dash.apiSaving'), 'warn');

  try {
    const payload = await postJson('/dashboard/plan/reviews_fact', reviewFactPayload());
    renderReviewFactEditor(payload.data);
    setApiState(t('dash.apiConnected'), 'ok');
  } catch (error) {
    showError(error.message, { apiStatus: error.apiStatus, retry: () => saveReviewFactEditor() });
  } finally {
    reviewFactSaving = false;
    els.reviewFactSave.textContent = t('dash.saveFact');
    setReviewFactDirty(reviewFactDirty);
  }
}

function renderOpzFactEditor(data) {
  opzFactRows = data?.rows || [];
  els.opzFactMeta.textContent = t('dash.opzFactMeta', {
    admins: opzFactRows.length,
    current: formatNumber(data?.current_total || 0),
    additional: formatNumber(data?.manual_total || 0),
    total: formatNumber(data?.combined_total || 0),
  });

  if (!opzFactRows.length) {
    els.opzFactEditor.innerHTML = `<div class="empty compact">${t('dash.noActiveAdministrators')}</div>`;
  } else {
    els.opzFactEditor.innerHTML = `
      <div class="table-scroll manual-fact-scroll">
        <table class="manual-fact-table">
          <thead>
            <tr>
              <th>${t('dash.branch')}</th>
              <th>${t('dash.administrator')}</th>
              <th class="number">${t('dash.currentOpz')}</th>
              <th class="number">${t('dash.additionalOpz')}</th>
              <th class="number">${t('dash.opzFactTotal')}</th>
            </tr>
          </thead>
          <tbody>
            ${opzFactRows
              .map((row) => {
                const marks = [];
                if (row.is_active === false) marks.push(t('dash.opzFactInactive'));
                if (row.counted === false) marks.push(t('dash.opzFactNotCounted'));
                const inactiveMark = marks.length
                  ? ` <span class="meta">· ${escapeHtml(marks.join(' · '))}</span>`
                  : '';
                const current = Number(row.current_value || 0);
                return `
                  <tr data-current-value="${escapeHtml(current)}" data-counted="${row.counted === false ? 'false' : 'true'}">
                    <td>${escapeHtml(row.company_title || t('dash.branchFallbackWithId', { id: row.company_id }))}</td>
                    <td>${escapeHtml(row.staff_name)}${inactiveMark}</td>
                    <td class="number">${escapeHtml(formatNumber(current))}</td>
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
                    <td class="number" data-total-cell>${escapeHtml(formatNumber(row.total_value ?? current))}</td>
                  </tr>
                `;
              })
              .join('')}
          </tbody>
        </table>
      </div>
    `;
  }

  opzFactSavedData = JSON.parse(JSON.stringify(data || { rows: [] }));
  opzFactLoadedFilters = opzFactFilters();
  opzFactSavedSnapshot = opzFactDraftSnapshot();
  setOpzFactDirty(false);
}

// The fact is the sum, so the row shows it before saving — not only after the reload.
function refreshOpzFactTotals() {
  let currentTotal = 0;
  let additionalTotal = 0;
  els.opzFactEditor.querySelectorAll('input[data-staff-id]').forEach((input) => {
    const row = input.closest('tr');
    if (!row) return;
    const current = Number(row.dataset.currentValue || 0);
    const rawValue = input.value.trim().replace(',', '.');
    const parsed = rawValue === '' ? 0 : Number(rawValue);
    const valid = Number.isFinite(parsed) && parsed >= 0;
    // The server stores whole units, so the preview must not promise a fraction — and a
    // row the reports ignore must not add itself to the totals either.
    const counted = row.dataset.counted !== 'false';
    const additional = valid && counted ? Math.round(parsed) : 0;
    const totalCell = row.querySelector('[data-total-cell]');
    if (totalCell) totalCell.textContent = formatNumber(current + additional);
    currentTotal += current;
    additionalTotal += additional;
  });
  els.opzFactMeta.textContent = t('dash.opzFactMeta', {
    admins: opzFactRows.length,
    current: formatNumber(currentTotal),
    additional: formatNumber(additionalTotal),
    total: formatNumber(currentTotal + additionalTotal),
  });
}

function opzFactFilters() {
  const filter = filterEls.opzFacts;
  return {
    month: filter.month.value,
    branch: filter.branch.value,
    staff: filter.staff.value,
  };
}

function restoreOpzFactFilters() {
  if (!opzFactLoadedFilters) return;
  const filter = filterEls.opzFacts;
  filter.month.value = opzFactLoadedFilters.month;
  filter.branch.value = opzFactLoadedFilters.branch;
  filter.staff.value = opzFactLoadedFilters.staff;
  customFilterDropdowns[filter.branch.id]?.refresh();
  customFilterDropdowns[filter.staff.id]?.refresh();
}

function opzFactDraftSnapshot() {
  return JSON.stringify([...els.opzFactEditor.querySelectorAll('input[data-staff-id]')].map((input) => ({
    companyId: input.dataset.companyId,
    staffId: input.dataset.staffId,
    value: input.value.trim(),
  })));
}

function setOpzFactDirty(isDirty) {
  opzFactDirty = isDirty;
  els.opzFactSave.disabled = opzFactSaving || !opzFactRows.length;
  updateFloatingEditorSave();
}

function updateOpzFactDirtyFromForm() {
  setOpzFactDirty(opzFactDraftSnapshot() !== opzFactSavedSnapshot);
}

function opzFactParams() {
  const filter = filterEls.opzFacts;
  return {
    month: filter.month.value,
    company_id: filter.branch.value,
    staff_id: filter.staff.value,
  };
}

async function loadOpzFactEditor({ signal = null } = {}) {
  const payload = await fetchJson('/dashboard/plan/opz_fact', opzFactParams(), {
    retry: () => loadOpzFacts(),
    signal,
  });
  renderOpzFactEditor(payload.data);
}

async function loadOpzFacts() {
  const filter = filterEls.opzFacts;
  const request = beginViewRequest('opzFacts');
  clearError();
  setFilterLoading(filter, true);

  const syncStatus = loadSyncStatus();
  try {
    await loadOpzFactEditor({ signal: request.signal });
    if (!request.isCurrent()) return;
    viewsWithData.add('opzFacts');
    clearError();
    setLoadedApiState(null, { empty: opzFactRows.length === 0 });
    await syncStatus;
  } catch (error) {
    if (isSupersededRequest(error) || !request.isCurrent()) return;
    showError(error.message, { apiStatus: error.apiStatus, retry: () => loadOpzFacts() });
  } finally {
    if (request.isCurrent()) {
      setFilterLoading(filter, false);
      request.finish();
    }
  }
}

function opzFactPayload() {
  const filter = filterEls.opzFacts;
  const items = [...els.opzFactEditor.querySelectorAll('input[data-staff-id]')].map((input) => {
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
      throw new Error(t('dash.opzFactNonNegative'));
    }
    return {
      company_id: Number(input.dataset.companyId),
      staff_id: Number(input.dataset.staffId),
      value,
    };
  });

  return {
    month: filter.month.value,
    company_id: filter.branch.value ? Number(filter.branch.value) : null,
    staff_id: filter.staff.value ? Number(filter.staff.value) : null,
    items,
  };
}

async function saveOpzFactEditor() {
  if (opzFactSaving || !opzFactRows.length) return;
  opzFactSaving = true;
  updateFloatingEditorSave();
  clearError();
  els.opzFactSave.disabled = true;
  els.opzFactSave.textContent = t('common.saving');
  setApiState(t('dash.apiSaving'), 'warn');

  try {
    const payload = await postJson('/dashboard/plan/opz_fact', opzFactPayload());
    renderOpzFactEditor(payload.data);
    setApiState(t('dash.apiConnected'), 'ok');
  } catch (error) {
    showError(error.message, { apiStatus: error.apiStatus, retry: () => saveOpzFactEditor() });
  } finally {
    opzFactSaving = false;
    els.opzFactSave.textContent = t('dash.saveFact');
    setOpzFactDirty(opzFactDirty);
  }
}

function renderBundle(bundle) {
  const {
    summary,
    revenue_daily: daily = [],
    top_services: services = [],
    extra_services: extraServices = [],
  } = bundle;
  applyFinancialVisibility(summary);
  renderStaffPersonalScope();
  renderKpi(summary);
  renderStaffBranchMetrics(summary);
  renderVisitMetrics(summary);
  renderClientsMetrics(summary);
  if (summary.financials_hidden) {
    renderRevenueMetrics({});
    renderServicesMetrics({});
    destroyChart('revenue');
    destroyChart('services');
    renderServicesTable([]);
    renderExtraServicesTable([]);
  } else {
    renderRevenueMetrics(summary);
    renderServicesMetrics(summary);
    renderRevenueChart(daily);
    renderServicesChart(services.slice(0, 8));
    renderServicesTable(services);
    renderExtraServicesTable(extraServices);
  }
  renderAppointmentsMetrics(summary);
  renderAppointmentsChart(daily);
  renderOpzChart(daily);

  els.periodLabel.textContent = t('dash.subhead');
  els.revenueMeta.textContent = summary.financials_hidden ? '' : t('dash.daysCount', { count: daily.length });
  const appointmentsBreakdown = summary.appointments_breakdown || {};
  els.appointmentsMeta.textContent = appointmentsBreakdown.source_status === 'ready' || appointmentsBreakdown.source_status === 'local'
    ? t('dash.appointmentsCount', { count: formatNumber(appointmentsBreakdown.total) })
    : t('dash.noExactData');
  els.servicesMeta.textContent = t('dash.servicesCount', { count: services.length });
  const revenue = summary.revenue || {};
  els.extraServicesMeta.textContent = summary.financials_hidden
    ? ''
    : t('dash.doneMeta', { count: formatNumber(revenue.extra_service_count) });
  els.tableMeta.textContent = summary.financials_hidden
    ? ''
    : t('dash.totalMoneyMeta', { value: formatMoney(revenue.total) });
}

async function loadBranches() {
  const request = viewRequestScopes.branches.start();
  const branchInputs = [
    ...Object.values(filterEls).map((filter) => filter.branch),
    els.serviceFilterBranch,
  ].filter(Boolean);
  try {
    const payload = await fetchJson('/dashboard/branches', {}, { signal: request.signal, slowState: false });
    if (!request.isCurrent()) return;
    branchOptions = payload.data || [];
    Object.values(filterEls).forEach((filter) => renderBranchOptions(filter));
    renderServiceBranchOptions();
    branchInputs.forEach(clearFilterWarning);
  } catch (error) {
    if (isSupersededRequest(error) || !request.isCurrent()) return;
    branchInputs.forEach((input) => showFilterWarning(input, error.message, () => loadBranches()));
  } finally {
    if (request.isCurrent()) request.finish();
  }
}

function renderBranchOptions(filter) {
  const selected = filter.branch.value;
  filter.branch.innerHTML = `<option value="">${t('dash.allBranches')}</option>`;
  branchOptions.forEach((branch) => {
    const option = document.createElement('option');
    option.value = branch.id;
    option.textContent = branch.title;
    filter.branch.appendChild(option);
  });
  filter.branch.value = branchOptions.some((branch) => String(branch.id) === selected) ? selected : '';
  customFilterDropdowns[filter.branch.id]?.refresh();
}

async function loadStaff(filter, { force = false } = {}) {
  if (force) loadedStaffFilters.delete(filter);
  if (loadedStaffFilters.has(filter)) return 'ready';
  const request = requestScopeForStaff(filter).start();
  const selected = filter.staff.value;
  try {
    const payload = await fetchJson('/dashboard/staff', {
      company_id: filter.branch.value,
    }, {
      retry: () => loadStaff(filter, { force: true }),
      signal: request.signal,
      slowState: false,
    });
    if (!request.isCurrent()) return 'superseded';
    const staffOptions = payload.data || [];
    const defaultLabel = filter === filterEls.reviewFacts || filter === filterEls.opzFacts
      ? t('dash.allStaff')
      : t('dash.allWorkers');
    filter.staff.innerHTML = `<option value="">${defaultLabel}</option>`;
    staffOptions.forEach((staff) => {
      const option = document.createElement('option');
      option.value = staff.id;
      option.textContent = filter.branch.value
        ? staff.name
        : `${staff.name} · ${staff.company_title || t('dash.branchFallbackWithId', { id: staff.company_id })}`;
      filter.staff.appendChild(option);
    });
    filter.staff.value = staffOptions.some((staff) => String(staff.id) === selected) ? selected : '';
    customFilterDropdowns[filter.staff.id]?.refresh();
    loadedStaffFilters.add(filter);
    clearFilterWarning(filter.staff);
    return 'ready';
  } catch (error) {
    if (isSupersededRequest(error) || !request.isCurrent()) return 'superseded';
    showFilterWarning(filter.staff, error.message, () => loadStaff(filter, { force: true }));
    return 'failed';
  } finally {
    if (request.isCurrent()) request.finish();
  }
}

async function refreshStaffForBranch(filter, loadView) {
  const expectedBranch = filter.branch.value;
  filter.staff.value = '';
  customFilterDropdowns[filter.staff.id]?.refresh();
  const status = await loadStaff(filter, { force: true });
  if (!staffRefreshAllowsDataLoad(status, expectedBranch, filter.branch.value)) return;
  await loadView();
}

function filterForView(view) {
  if (view === 'plan') return filterEls.plan;
  if (view === 'reviewFacts') return filterEls.reviewFacts;
  if (view === 'opzFacts') return filterEls.opzFacts;
  return filterEls.overview;
}

async function ensureStaffForView(view) {
  if (view === 'planSettings' || view === 'serviceManagement' || view === 'reports') return;
  await loadStaff(filterForView(view));
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
  filter.load.textContent = isLoading ? t('common.loadingShort') : t('dash.refresh');
}

async function loadSyncStatus() {
  try {
    const payload = await fetchJson('/dashboard/widget/sync_status', undefined, { slowState: false });
    const sync = payload.data?.sync || {};
    const lastRun = sync.last_run;
    const lastSuccessfulAt = sync.last_successful_sync_at
      || (lastRun?.status === 'success' ? lastRun.finished_at : null);
    els.syncState.textContent = lastSuccessfulAt
      ? t('dash.syncUpdatedAt', { value: formatMoscowDateTime(lastSuccessfulAt) })
      : t('dash.syncNoSuccessfulUpdates');
  } catch {
    els.syncState.textContent = t('dash.syncStatusUnavailable');
  }
}

function viewFromLocation() {
  let view = 'overview';
  if (window.location.pathname.replace(/\/+$/, '') === '/reports' || window.location.pathname.startsWith('/reports/')) view = 'reports';
  else if (window.location.hash === '#plan-fact') view = 'plan';
  else if (window.location.hash === '#plan-settings') view = 'planSettings';
  else if (window.location.hash === '#services') view = 'serviceManagement';
  else if (window.location.hash === '#review-facts') view = 'reviewFacts';
  else if (window.location.hash === '#opz-facts') view = 'opzFacts';
  return accessibleView(view);
}

function setActiveView(view) {
  view = accessibleView(view);
  const previousView = activeView;
  Object.entries(viewRequestScopes).forEach(([requestView, scope]) => {
    if (requestView !== 'branches' && requestView !== view) scope.abort();
  });
  if (previousView === 'reports' && view !== 'reports') reportsController?.clear();
  activeView = view;
  els.overviewView.classList.toggle('active', view === 'overview');
  els.planView.classList.toggle('active', view === 'plan');
  els.planSettingsView.classList.toggle('active', view === 'planSettings');
  els.serviceManagementView.classList.toggle('active', view === 'serviceManagement');
  els.reviewFactsView.classList.toggle('active', view === 'reviewFacts');
  els.opzFactsView.classList.toggle('active', view === 'opzFacts');
  els.reportsView.classList.toggle('active', view === 'reports');
  els.viewLinks.forEach((link) => {
    link.classList.toggle('active', link.dataset.viewLink === view);
  });
  updateFloatingEditorSave();
  const labels = {
    overview: t('dash.subhead'),
    plan: t('dash.planSubhead'),
    planSettings: t('dash.planSettingsSubhead'),
    serviceManagement: t('dash.serviceManagementSubhead'),
    reviewFacts: t('dash.reviewFactsSubhead'),
    opzFacts: t('dash.opzFactsSubhead'),
    reports: t('dash.reportsSubhead'),
  };
  els.periodLabel.textContent = labels[view] || labels.overview;
}

async function loadPlanFact() {
  const filter = filterEls.plan;
  const request = beginViewRequest('plan');
  clearError();
  setFilterLoading(filter, true);

  // Sync status is independent of the view payload, so start it in parallel. It never rejects.
  const syncStatus = loadSyncStatus();
  try {
    const payload = await fetchJson('/dashboard/widget/plan_fact', filterParams(filter), {
      retry: () => loadPlanFact(),
      signal: request.signal,
    });
    if (!request.isCurrent()) return;
    currentPlanFactPayload = payload.data;
    renderPlanColumnPicker();
    renderCurrentPlanFact();
    viewsWithData.add('plan');
    clearError();
    setLoadedApiState(payload.data, { empty: !payload.data?.groups?.length });
    await syncStatus;
  } catch (error) {
    if (isSupersededRequest(error) || !request.isCurrent()) return;
    showError(error.message, { apiStatus: error.apiStatus, retry: () => loadPlanFact() });
  } finally {
    if (request.isCurrent()) {
      setFilterLoading(filter, false);
      request.finish();
    }
  }
}

async function loadReviewFacts() {
  const filter = filterEls.reviewFacts;
  const request = beginViewRequest('reviewFacts');
  clearError();
  setFilterLoading(filter, true);

  const syncStatus = loadSyncStatus();
  try {
    await loadReviewFactEditor({ signal: request.signal });
    if (!request.isCurrent()) return;
    viewsWithData.add('reviewFacts');
    clearError();
    setLoadedApiState(null, { empty: reviewFactRows.length === 0 });
    await syncStatus;
  } catch (error) {
    if (isSupersededRequest(error) || !request.isCurrent()) return;
    showError(error.message, { apiStatus: error.apiStatus, retry: () => loadReviewFacts() });
  } finally {
    if (request.isCurrent()) {
      setFilterLoading(filter, false);
      request.finish();
    }
  }
}

async function loadDashboard() {
  const filter = filterEls.overview;
  const request = beginViewRequest('overview');
  clearError();
  setFilterLoading(filter, true);

  const syncStatus = loadSyncStatus();
  try {
    const payload = await fetchJson('/dashboard/bundle', filterParams(filter), {
      retry: () => loadDashboard(),
      signal: request.signal,
    });
    if (!request.isCurrent()) return;
    renderBundle(payload.data);
    viewsWithData.add('overview');
    clearError();
    setLoadedApiState(payload.data, { empty: !payload.data?.summary });
    await syncStatus;
  } catch (error) {
    if (isSupersededRequest(error) || !request.isCurrent()) return;
    showError(error.message, { apiStatus: error.apiStatus, retry: () => loadDashboard() });
  } finally {
    if (request.isCurrent()) {
      setFilterLoading(filter, false);
      request.finish();
    }
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
  } else if (activeView === 'opzFacts') {
    await loadOpzFacts();
  } else if (activeView === 'reports') {
    await reportsController?.loadFromLocation();
  } else {
    await loadDashboard();
  }
}

const ROLE_LABELS = {
  platform_admin: t('dash.rolePlatformAdmin'),
  owner: t('dash.roleOwner'),
  branch_admin: t('dash.roleBranchAdmin'),
  manager: t('dash.roleManager'),
  viewer: t('dash.roleViewer'),
};

function accountDisplayName(user) {
  const fullName = user?.full_name?.trim();
  if (fullName) return fullName;
  return user?.email?.split('@')[0] || '';
}

function tenantOptionLabel(tenant) {
  const branchText = t('dash.branchesMeta', { count: tenant.branch_count || 0 });
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

  const payload = await authFetch('/auth/portal-accounts');
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
      els.tenantMeta.textContent = t('dash.noBusinessNetworks');
    }
    showError(t('dash.noPlatformTenants'));
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
      ? t('dash.selectedNetworkData')
      : t('dash.selectedNetworkNoBranches');
  }

  if (!selectedTenant.branch_count) {
    showError(t('dash.selectedNetworkNoConnectedBranches'));
    return false;
  }
  return true;
}

async function startDemoSession() {
  setSelectedPortalAccountId('');
  await authFetch('/auth/demo-login', { method: 'POST' });
  setSelectedPortalAccountId('');
}

async function loadStartupUser() {
  let me = null;
  try {
    me = await loadCurrentUserQuietly();
  } catch (error) {
    if (!DEMO_AUTOLOGIN) {
      throw error;
    }
    await startDemoSession();
    return loadCurrentUserQuietly();
  }

  if (DEMO_AUTOLOGIN && me?.data?.is_demo !== true) {
    await startDemoSession();
    return loadCurrentUserQuietly();
  }
  return me;
}

async function startSession() {
  clearError();
  if (!apiKey) {
    let me;
    try {
      me = await acquireStartupSession(loadStartupUser);
    } catch (error) {
      if (requiresLogin(error)) {
        redirectToLogin();
        return;
      }
      showError(t('authErrors.temporaryUnavailable'), { apiStatus: 'error', retry: () => startSession() });
      return;
    }

    currentUser = me.data;
    const onboardingOk = await ensureOnboardingComplete(currentUser);
    if (!onboardingOk) return;
    const isDemo = currentUser?.is_demo === true;
    document.body.classList.toggle('demo-mode', isDemo);
    updateFloatingEditorSave();
    const demoBanner = document.getElementById('demo-banner');
    if (demoBanner) {
      demoBanner.hidden = !isDemo;
    }
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
    applyDashboardPermissions();

    try {
      const canLoadTenantData = await setupPlatformTenantSelector();
      if (!canLoadTenantData) {
        setApiState(t('dash.apiNoTenant'), 'warn');
        return;
      }
    } catch (error) {
      showError(error.message);
      setApiState(t('dash.apiTenantError'), 'error');
      return;
    }
  } else {
    setSelectedPortalAccountId('');
    applyDashboardPermissions();
  }
  await loadBranches();
  setActiveView(viewFromLocation());
  const existingPosition = Number(history.state?.[HISTORY_POSITION_KEY]);
  historyPosition = Number.isFinite(existingPosition) ? existingPosition : 0;
  replaceDashboardHistory({ view: activeView });
  await ensureStaffForView(activeView);
  await loadCurrentView();
}

async function init() {
  reportsController = initReports({
    clearError,
    showError,
    setApiState,
    pushHistory: pushDashboardHistory,
  });
  mountLanguageSwitcher(document.getElementById('lang-switcher'), {
    beforeChange: () => runDashboardNavigation(async () => {}),
    afterChange: () => window.location.reload(),
  });
  [filterEls.overview, filterEls.plan].forEach((filter) => defaultDates(filter));
  setManualFactDefaultMonths();
  els.overviewPresetButtons.forEach((button) => {
    button.classList.toggle('active', button.dataset.overviewPreset === 'month');
  });
  els.planSettingsMonth.value = currentMonthValue();
  renderServicesTable([]);
  renderExtraServicesTable([]);
  renderServiceManagement({ rows: [], groups: [], categories: [] });
  await startSession();
}

filterEls.overview.load.addEventListener('click', () => loadDashboard());
els.tenantSelect?.addEventListener('change', async () => {
  const requestedTenantId = els.tenantSelect.value;
  const changed = await runDashboardNavigation(async () => {
    setSelectedPortalAccountId(requestedTenantId);
    window.location.reload();
  });
  if (!changed) els.tenantSelect.value = String(selectedTenant?.id || '');
});
filterEls.overview.start.addEventListener('change', () => {
  els.overviewPresetButtons.forEach((button) => button.classList.remove('active'));
});
filterEls.overview.end.addEventListener('change', () => {
  els.overviewPresetButtons.forEach((button) => button.classList.remove('active'));
});
filterEls.overview.branch.addEventListener('change', async () => {
  await refreshStaffForBranch(filterEls.overview, loadDashboard);
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
  await refreshStaffForBranch(filterEls.plan, loadPlanFact);
});
filterEls.plan.staff.addEventListener('change', () => loadPlanFact());
els.planColumnToggle.addEventListener('click', () => {
  setPlanColumnPickerOpen(els.planColumnMenu.hidden);
});
els.planColumnList.addEventListener('change', (event) => {
  const input = event.target.closest('[data-plan-metric-code]');
  if (!input) return;
  applyPlanMetricVisibility(
    setPlanMetricHidden(
      planMetricOptions(),
      hiddenPlanMetricCodes,
      input.dataset.planMetricCode,
      !input.checked,
    ),
  );
});
els.planColumnsHideMoney.addEventListener('click', () => {
  applyPlanMetricVisibility(hideMoneyPlanMetrics(planMetricOptions(), hiddenPlanMetricCodes));
});
els.planColumnsShowAll.addEventListener('click', () => {
  applyPlanMetricVisibility(new Set());
});
document.addEventListener('click', (event) => {
  if (!els.planColumnPicker.contains(event.target)) setPlanColumnPickerOpen(false);
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !els.planColumnMenu.hidden) {
    setPlanColumnPickerOpen(false, { restoreFocus: true });
  }
});
filterEls.reviewFacts.load.addEventListener('click', () => protectedChangesGuard.run(() => loadReviewFacts()));
filterEls.reviewFacts.month.addEventListener('change', async () => {
  const requestedMonth = filterEls.reviewFacts.month.value;
  const changed = await protectedChangesGuard.run(async () => {
    filterEls.reviewFacts.month.value = requestedMonth;
    await loadReviewFacts();
  });
  if (!changed) restoreReviewFactFilters();
});
filterEls.reviewFacts.branch.addEventListener('change', async () => {
  const requestedBranch = filterEls.reviewFacts.branch.value;
  const changed = await protectedChangesGuard.run(async () => {
    filterEls.reviewFacts.branch.value = requestedBranch;
    customFilterDropdowns[filterEls.reviewFacts.branch.id]?.refresh();
    await refreshStaffForBranch(filterEls.reviewFacts, loadReviewFacts);
  });
  if (!changed) restoreReviewFactFilters();
});
filterEls.reviewFacts.staff.addEventListener('change', async () => {
  const requestedStaff = filterEls.reviewFacts.staff.value;
  const changed = await protectedChangesGuard.run(async () => {
    filterEls.reviewFacts.staff.value = requestedStaff;
    customFilterDropdowns[filterEls.reviewFacts.staff.id]?.refresh();
    await loadReviewFacts();
  });
  if (!changed) restoreReviewFactFilters();
});
els.reviewFactSave.addEventListener('click', () => saveReviewFactEditor());
els.floatingEditorSaveButton.addEventListener('click', () => {
  if (activeView === 'planSettings') savePlanSettings();
  if (activeView === 'reviewFacts') saveReviewFactEditor();
  if (activeView === 'opzFacts') saveOpzFactEditor();
});
els.reviewFactEditor.addEventListener('input', () => updateReviewFactDirtyFromForm());
filterEls.opzFacts.load.addEventListener('click', () => protectedChangesGuard.run(() => loadOpzFacts()));
filterEls.opzFacts.month.addEventListener('change', async () => {
  const requestedMonth = filterEls.opzFacts.month.value;
  const changed = await protectedChangesGuard.run(async () => {
    filterEls.opzFacts.month.value = requestedMonth;
    await loadOpzFacts();
  });
  if (!changed) restoreOpzFactFilters();
});
filterEls.opzFacts.branch.addEventListener('change', async () => {
  const requestedBranch = filterEls.opzFacts.branch.value;
  const changed = await protectedChangesGuard.run(async () => {
    filterEls.opzFacts.branch.value = requestedBranch;
    customFilterDropdowns[filterEls.opzFacts.branch.id]?.refresh();
    await refreshStaffForBranch(filterEls.opzFacts, loadOpzFacts);
  });
  if (!changed) restoreOpzFactFilters();
});
filterEls.opzFacts.staff.addEventListener('change', async () => {
  const requestedStaff = filterEls.opzFacts.staff.value;
  const changed = await protectedChangesGuard.run(async () => {
    filterEls.opzFacts.staff.value = requestedStaff;
    customFilterDropdowns[filterEls.opzFacts.staff.id]?.refresh();
    await loadOpzFacts();
  });
  if (!changed) restoreOpzFactFilters();
});
els.opzFactSave.addEventListener('click', () => saveOpzFactEditor());
els.opzFactEditor.addEventListener('input', () => {
  refreshOpzFactTotals();
  updateOpzFactDirtyFromForm();
});
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

document.addEventListener('click', async (event) => {
  const link = event.target.closest('[data-report-link]');
  if (!link) return;
  const href = link.getAttribute('href');
  if (!href || !href.startsWith('/reports')) return;
  event.preventDefault();
  await runDashboardNavigation(async () => {
    pushDashboardHistory({ view: 'reports' }, href);
    setActiveView('reports');
    await ensureStaffForView('reports');
    await loadCurrentView();
  });
});

els.viewLinks.forEach((link) => {
  link.addEventListener('click', async (event) => {
    const view = link.dataset.viewLink;
    if (!view) return;
    event.preventDefault();
    if (!canAccessView(view)) {
      await runDashboardNavigation(async () => {
        replaceDashboardHistory({ view: 'overview' }, dashboardPath('overview'));
        setActiveView('overview');
        await ensureStaffForView('overview');
        await loadCurrentView();
      });
      return;
    }
    await runDashboardNavigation(async () => {
      pushDashboardHistory({ view }, dashboardPath(view));
      setActiveView(view);
      await ensureStaffForView(view);
      await loadCurrentView();
    });
  });
});

els.profileSettingsLink?.addEventListener('click', async (event) => {
  if (!shouldHandleSameTabNavigation(event)) return;
  const href = els.profileSettingsLink.getAttribute('href');
  if (!href) return;
  event.preventDefault();
  await runDashboardNavigation(async () => {
    window.location.assign(href);
  });
});

window.addEventListener('popstate', async (event) => {
  const targetPosition = Number(event.state?.[HISTORY_POSITION_KEY]);
  const needsGuard = protectedSavePending()
    || hasProtectedDirtyChanges()
    || (activeView === 'serviceManagement' && (serviceManagementDirty || serviceManagementMutationPending));
  const decision = historyNavigationDecision({
    targetPosition,
    currentPosition: historyPosition,
    restorationPosition: suppressedHistoryTraversal?.expectedPosition ?? null,
    handlingNavigation: handlingHistoryNavigation,
    needsGuard,
  });

  if (decision.type === 'completeRestoration') {
    historyPosition = decision.position;
    const { resolve } = suppressedHistoryTraversal;
    suppressedHistoryTraversal = null;
    resolve();
    return;
  }

  if (decision.type === 'restore') {
    if (!decision.delta) return;
    if (suppressedHistoryTraversal) {
      history.go(decision.delta);
      return;
    }
    const correction = restoreHistoryPosition(decision.delta);
    historyCorrectionPromise = correction;
    try {
      await correction;
    } finally {
      if (historyCorrectionPromise === correction) historyCorrectionPromise = null;
    }
    return;
  }

  if (decision.type === 'guard') {
    handlingHistoryNavigation = true;
    try {
      await restoreHistoryPosition(decision.delta);
      await runDashboardNavigation(async () => {
        if (historyCorrectionPromise) await historyCorrectionPromise;
        history.go(-decision.delta);
      });
    } finally {
      handlingHistoryNavigation = false;
    }
    return;
  }

  if (decision.type === 'guardUnknown') {
    const targetUrl = window.location.href;
    const nextView = viewFromLocation();
    replaceDashboardHistory({ view: activeView }, currentHistoryUrl);
    handlingHistoryNavigation = true;
    try {
      await runDashboardNavigation(async () => {
        pushDashboardHistory({ view: nextView }, targetUrl);
        setActiveView(nextView);
        await ensureStaffForView(nextView);
        await loadCurrentView();
      });
    } finally {
      handlingHistoryNavigation = false;
    }
    return;
  }

  if (decision.position !== null) historyPosition = decision.position;
  currentHistoryUrl = window.location.href;
  const nextView = viewFromLocation();
  if (nextView === activeView && nextView !== 'reports') return;
  setActiveView(nextView);
  await ensureStaffForView(nextView);
  await loadCurrentView();
});
window.addEventListener('hashchange', async (event) => {
  const taggedPosition = Number(history.state?.[HISTORY_POSITION_KEY]);
  if (Number.isFinite(taggedPosition)) return;

  const nextView = viewFromLocation();
  const targetPosition = historyPosition + 1;
  history.replaceState({
    ...(history.state || {}),
    view: nextView,
    [HISTORY_POSITION_KEY]: targetPosition,
  }, '', event.newURL);

  const needsGuard = protectedSavePending()
    || hasProtectedDirtyChanges()
    || (activeView === 'serviceManagement' && (serviceManagementDirty || serviceManagementMutationPending));
  if (!needsGuard) {
    historyPosition = targetPosition;
    currentHistoryUrl = event.newURL;
    setActiveView(nextView);
    await ensureStaffForView(nextView);
    await loadCurrentView();
    return;
  }

  handlingHistoryNavigation = true;
  try {
    await restoreHistoryPosition(-1);
    await runDashboardNavigation(async () => {
      history.go(1);
    });
  } finally {
    handlingHistoryNavigation = false;
  }
});
window.addEventListener('beforeunload', (event) => {
  if (
    !planSettingsDirty
    && !planSettingsSaving
    && !reviewFactDirty
    && !reviewFactSaving
    && !opzFactDirty
    && !opzFactSaving
    && !serviceManagementDirty
    && !serviceManagementMutationPending
  ) return;
  event.preventDefault();
  event.returnValue = '';
});
init();
