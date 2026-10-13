import {
  BarController,
  BarElement,
  CategoryScale,
  Chart,
  Filler,
  Legend,
  LinearScale,
  LineController,
  LineElement,
  PointElement,
  Tooltip,
} from 'chart.js';
// Side-effect only: registers the global responsive tick/legend plugin shared with the
// report viewer's charts.js — see chartResponsive.js for how/why it applies before layout.
import './chartResponsive.js';
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
  fetchJson as sharedFetchJson,
  isSupersededRequest,
  patchJson as sharedPatchJson,
  postJson as sharedPostJson,
  apiKey,
} from './dashboardApi.js';
import {
  createLatestRequestScope,
  filterServiceManagementResult,
  latestServiceManagementTimestamp,
  mergeServiceManagementResult,
  reportSearchParams,
  serviceManagementChanges,
  serviceManagementControls,
  serviceManagementLoadAllowed,
  serviceManagementNavigationAllowed,
  runServiceManagementMutation,
  settleServiceManagementLoad,
  staffRefreshAllowsDataLoad,
} from './dashboardRequestState.js';

import { escapeHtml } from './html.js';
import {
  appointmentsCards,
  cardsHtml,
  clientsCards,
  formatMetricValue,
  kpiCards,
  revenueCards,
  servicesCards,
  staffBranchCards,
  usesAdministratorSchedule,
  visitCards,
} from './overviewCards.js';
import {
  BRANCH_PLAN_SETTING_FIELDS,
  STAFF_PLAN_SETTING_FIELDS_BY_CATEGORY,
  buildPlanSettingsPayload,
  isScheduleAttributionDiagnostic,
  planSettingsInputReader,
} from './planSettings.js';
import {
  filterPlanFactForDisplay,
  hideMoneyPlanMetrics,
  normalizeHiddenPlanMetricCodes,
  setPlanMetricHidden,
} from './planMetricVisibility.js';
import { branchToday, inputDateValue, monthOfRange, monthRange, monthValue } from './period.js';
import { branchesForPeriod } from './reportingWindow.js';
// Shared with Reports: a financials_hidden field (e.g. revenue.service_revenue for the manager
// default, see AGENTS.md) renders as "—" instead of a misleading "0 ₽", and formatDate pins a
// date-only string to local midnight so it does not render a day early west of UTC.
import {
  formatDate,
  formatMoney,
  formatNumber,
} from './reports/format.js';
import { canEnterManualFacts, entersManualFactsForSelf } from './manualFactAccess.js';
import { canViewBranchPayments } from './branchPaymentsAccess.js';
import { formatInputNumber } from './manualFactInput.js';
import { createManualEditors } from './manualEditors.js';
import { formatBranchDateTime } from './timestamps.js';
import { initReports } from './reports/index.js';
import { applyTranslations, getLocale, mountLanguageSwitcher, roleLabel, t } from './i18n.js';
import {
  createUnsavedChangesGuard,
  editEventChecksDraft,
  editorSaveDockState,
  historyNavigationDecision,
  shouldHandleSameTabNavigation,
} from './unsavedChanges.js';

// Only bar and line charts (line/bar controllers, mixed in the OPZ chart) are used here —
// see reports/charts.js for the doughnut-capable registration the report viewer needs.
// Filler backs the revenue chart's `fill: true`; Legend/Tooltip are relied on implicitly
// (only renderServicesChart turns the legend off) by every chart built in this file.
Chart.register(
  BarController,
  LineController,
  BarElement,
  LineElement,
  PointElement,
  CategoryScale,
  LinearScale,
  Legend,
  Tooltip,
  Filler,
);

document.documentElement.lang = getLocale();
applyTranslations();

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
  reviewFactSelfHint: document.getElementById('review-fact-self-hint'),
  reviewFactSave: document.getElementById('review-fact-save'),
  opzFactEditor: document.getElementById('opz-fact-editor'),
  opzFactMeta: document.getElementById('opz-fact-meta'),
  opzFactSelfHint: document.getElementById('opz-fact-self-hint'),
  opzFactSave: document.getElementById('opz-fact-save'),
  yandexPayEditor: document.getElementById('yandex-pay-editor'),
  yandexPayMeta: document.getElementById('yandex-pay-meta'),
  yandexPayFutureHint: document.getElementById('yandex-pay-future-hint'),
  yandexPaySave: document.getElementById('yandex-pay-save'),
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
  yandexPayView: document.getElementById('yandex-pay-view'),
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
  serviceFilterQuery: document.getElementById('service-filter-query'),
  serviceFilterExtra: document.getElementById('service-filter-extra'),
  serviceFilterLoad: document.getElementById('service-filter-load'),
  serviceCatalogMeta: document.getElementById('service-catalog-meta'),
  serviceCatalogTable: document.getElementById('service-catalog-table'),
  serviceManagementDirty: document.getElementById('service-management-dirty'),
  serviceManagementReset: document.getElementById('service-management-reset'),
  serviceManagementSave: document.getElementById('service-management-save'),
  tenantSwitcher: document.getElementById('tenant-switcher'),
  tenantSelect: document.getElementById('tenant-select'),
  tenantMeta: document.getElementById('tenant-meta'),
  overviewPresetButtons: [...document.querySelectorAll('[data-overview-preset]')],
  compareNote: document.getElementById('overview-compare-note'),
  overviewJumpButtons: [...document.querySelectorAll('[data-overview-jump]')],
  profileSettingsLink: document.querySelector('#user-profile-link a[href]'),
  unsavedChangesDialog: document.getElementById('unsaved-changes-dialog'),
  unsavedChangesStay: document.getElementById('unsaved-changes-stay'),
};

const filterEls = {
  overview: {
    month: document.getElementById('overview-month'),
    start: document.getElementById('overview-start'),
    end: document.getElementById('overview-end'),
    branch: document.getElementById('overview-branch'),
    staff: document.getElementById('overview-staff'),
    load: document.getElementById('overview-load'),
  },
  plan: {
    month: document.getElementById('plan-month'),
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
  // No worker filter: the amount belongs to a branch and a month, not to a person.
  yandexPay: {
    month: document.getElementById('yandex-pay-month'),
    branch: document.getElementById('yandex-pay-branch'),
    load: document.getElementById('yandex-pay-load'),
  },
};

const customFilterDropdowns = {};
Object.values(filterEls).forEach((filter) => {
  customFilterDropdowns[filter.branch.id] = enhanceSelect(filter.branch, { placeholder: t('dash.allBranches') });
  if (filter.staff) {
    customFilterDropdowns[filter.staff.id] = enhanceSelect(filter.staff, { placeholder: t('dash.allWorkers') });
  }
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

let activeView = 'overview';
let branchOptions = [];
let planSettingsData = null;
let planSettingsSavedData = null;
let planSettingsSavedSnapshot = '';
let planSettingsDirty = false;
let planSettingsLoadedMonth = '';
let planSettingsSaving = false;
let planSettingsLoading = false;
let serviceManagementData = { rows: [], categories: [] };
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
  yandexPayFacts: createLatestRequestScope(),
  branches: createLatestRequestScope(),
  syncStatus: createLatestRequestScope(),
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
    || Boolean(manualEditors.byView[activeView]?.dirty)
  );
}

function protectedSavePending() {
  return (
    (activeView === 'planSettings' && planSettingsSaving)
    || Boolean(manualEditors.byView[activeView]?.saving)
  );
}

function discardProtectedChanges() {
  if (activeView === 'planSettings') {
    if (planSettingsSavedData) renderPlanSettings(JSON.parse(JSON.stringify(planSettingsSavedData)));
    else setPlanSettingsDirty(false);
  }
  manualEditors.byView[activeView]?.discard();
}

const protectedChangesGuard = createUnsavedChangesGuard({
  isDirty: hasProtectedDirtyChanges,
  isBlocked: protectedSavePending,
  confirmLeave: showUnsavedChangesDialog,
  onDiscard: discardProtectedChanges,
});

const manualEditors = createManualEditors({
  els,
  filterEls,
  host: {
    fetchJson,
    postJson,
    beginViewRequest,
    clearError,
    showError,
    setApiState,
    setFilterLoading,
    setLoadedApiState,
    loadSyncStatus,
    viewsWithData,
    isSupersededRequest,
    updateFloatingEditorSave,
    refreshStaffForBranch,
    refilterForPeriod,
    guard: protectedChangesGuard,
    filterDropdowns: customFilterDropdowns,
  },
});

function updateFloatingEditorSave() {
  const state = editorSaveDockState({
    activeView,
    planSettingsDirty,
    planSettingsSaving,
    reviewFactDirty: manualEditors.review.dirty,
    reviewFactSaving: manualEditors.review.saving,
    opzFactDirty: manualEditors.opz.dirty,
    opzFactSaving: manualEditors.opz.saving,
    yandexPayDirty: manualEditors.yandex.dirty,
    yandexPaySaving: manualEditors.yandex.saving,
    isDemo: document.body.classList.contains('demo-mode'),
  });
  els.floatingEditorSave.hidden = !state.visible;
  document.body.classList.toggle('floating-editor-save-visible', state.visible);
  if (!state.visible) return;
  els.floatingEditorSaveStatus.textContent = t('dash.unsavedChanges');
  // Rows of one month with the picker already on another: saving then would write the wrong month.
  els.floatingEditorSaveButton.disabled = state.saving || (activeView === 'planSettings' && planSettingsLoading);
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
    yandexPayFacts: '/#yandex-pay',
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
const SETTINGS_VIEWS = new Set(['planSettings', 'serviceManagement']);
// Manual facts are not settings: a staff member enters their own value and the branch
// manager corrects it, so these two tabs follow the server-side scope instead of the role.
const MANUAL_FACT_VIEWS = new Set(['reviewFacts', 'opzFacts']);
// Branch-wide money by payment form: not a settings tab and not a per-person fact, so it
// has its own server-side flag (`branch_payments_access`) and not even every manual-fact
// role has it.
const BRANCH_PAYMENT_VIEWS = new Set(['yandexPayFacts']);

function hasSettingsAdminAccess() {
  if (apiKey && !currentUser) return true;
  return SETTINGS_ADMIN_ROLES.has(currentUser?.role);
}

function manualFactAccessOptions() {
  return { hasApiKey: Boolean(apiKey) };
}

function hasManualFactAccess() {
  return canEnterManualFacts(currentUser, manualFactAccessOptions());
}

function hasBranchPaymentsAccess() {
  return canViewBranchPayments(currentUser, manualFactAccessOptions());
}

function canAccessView(view) {
  if (MANUAL_FACT_VIEWS.has(view)) return hasManualFactAccess();
  if (BRANCH_PAYMENT_VIEWS.has(view)) return hasBranchPaymentsAccess();
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
  const hideManualFacts = !hasManualFactAccess();
  MANUAL_FACT_VIEWS.forEach((view) => setViewLinksHidden(view, hideManualFacts));
  els.reviewFactsView.hidden = hideManualFacts;
  els.opzFactsView.hidden = hideManualFacts;
  const hideBranchPayments = !hasBranchPaymentsAccess();
  BRANCH_PAYMENT_VIEWS.forEach((view) => setViewLinksHidden(view, hideBranchPayments));
  els.yandexPayView.hidden = hideBranchPayments;
  applyManualFactSelfScope();
}

// Reporting for yourself leaves nobody to pick from, so the worker filter goes away.
function applyManualFactSelfScope() {
  const selfOnly = entersManualFactsForSelf(currentUser, manualFactAccessOptions());
  [filterEls.reviewFacts, filterEls.opzFacts].forEach((filter) => {
    const label = filter.staff.closest('label');
    if (label) label.hidden = selfOnly;
  });
  els.reviewFactSelfHint.hidden = !selfOnly;
  els.opzFactSelfHint.hidden = !selfOnly;
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
  const now = branchToday();
  const start = new Date(now.getFullYear(), now.getMonth(), 1);
  filter.end.value = inputDateValue(now);
  filter.start.value = inputDateValue(start);
  filter.month.value = monthOfRange(filter.start.value, filter.end.value, now);
}

/**
 * Move a period onto the month its picker holds.
 *
 * Returns false for a half-typed month, which leaves the period where it was rather
 * than reloading the view against a window nobody asked for.
 */
function applyFilterMonth(filter) {
  const range = monthRange(filter.month.value);
  if (!range) return false;
  filter.start.value = range.start;
  filter.end.value = range.end;
  return true;
}

function setManualFactDefaultMonths() {
  const month = monthValue(branchToday());
  filterEls.reviewFacts.month.value = month;
  filterEls.opzFacts.month.value = month;
  filterEls.yandexPay.month.value = month;
  limitYandexPayMonth();
}

// A month that has not started has nothing to enter; the server refuses it too. Read per
// activation: a tab left open across the month boundary would otherwise keep the old limit.
function limitYandexPayMonth() {
  filterEls.yandexPay.month.max = monthValue(branchToday());
}

function overviewPresetRange(preset) {
  const end = branchToday();
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

// Which preset the Overview window came from; the backend picks its baseline from it.
// Kept here rather than read back off the buttons, because a month chosen in the picker
// keeps the month-over-month baseline while no button is lit: September measured against
// the 30 days before it would start on 2 August and drop that month's first day.
let overviewPreset = 'month';

function highlightOverviewPreset(preset) {
  els.overviewPresetButtons.forEach((button) => {
    button.classList.toggle('active', button.dataset.overviewPreset === preset);
  });
}

/**
 * Rebuild the branch and employee lists for the period a filter now holds.
 *
 * The employee list is scoped by the same window, so it is stale the moment the period moves: a branch
 * that left takes its people out of the filter with it, and the selected employee may go too. A view
 * that loads for the new period therefore waits for the list — a request sent first would carry the old
 * employee while the selector already reads "all". Resolves false when a newer rebuild took over.
 */
async function refilterForPeriod(filter) {
  renderBranchOptions(filter);
  if (!filter.staff) return true;
  return (await loadStaff(filter, { force: true })) !== 'superseded';
}

function setOverviewPreset(preset) {
  const filter = filterEls.overview;
  const range = overviewPresetRange(preset);
  filter.start.value = inputDateValue(range.start);
  filter.end.value = inputDateValue(range.end);
  overviewPreset = preset;
  highlightOverviewPreset(preset);
  // Only the month button picks a month; the shorter presets can land on the same window
  // by accident (a quarter opening today, in its first month) without meaning one.
  filter.month.value = preset === 'month'
    ? monthOfRange(filter.start.value, filter.end.value)
    : '';
}

/** The period is no longer the one a preset or the month picker chose. */
function clearOverviewPeriodChoice() {
  overviewPreset = '';
  highlightOverviewPreset('');
  filterEls.overview.month.value = '';
}

function currentMonthValue() {
  return monthValue(branchToday());
}

function previousMonthValue(month) {
  const [year, monthNumber] = String(month || currentMonthValue()).split('-').map(Number);
  return monthValue(new Date(year, monthNumber - 2, 1));
}

function overviewReportUrl(reportId) {
  const filter = filterEls.overview;
  const params = reportSearchParams({
    start_date: filter.start.value,
    end_date: filter.end.value,
    company_id: filter.branch.value,
    staff_id: filter.staff.value,
    granularity: 'month',
    // The report recomputes the same new/repeat deltas, so it has to measure against the
    // same baseline as the card that links to it.
    period_preset: activeOverviewPreset(),
  });
  return `/reports/${encodeURIComponent(reportId)}?${params.toString()}`;
}

function renderCards(target, cards) {
  target.innerHTML = cardsHtml(cards);
}

function renderKpi(summary) {
  renderCards(els.kpi, kpiCards(summary));
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
  renderCards(els.staffBranchMetrics, staffBranchCards(summary));
}

function renderRevenueMetrics(summary) {
  renderCards(els.revenueMetrics, revenueCards(summary));
}

function renderServicesMetrics(summary) {
  const { cards, columnCount } = servicesCards(summary);
  els.servicesMetrics.style.setProperty('--metric-columns', String(columnCount));
  renderCards(els.servicesMetrics, cards);
}

function renderVisitMetrics(summary) {
  const cards = visitCards(summary);
  els.visitMetrics.style.setProperty('--metric-columns', String(Math.max(4, cards.length)));
  renderCards(els.visitMetrics, cards);
}

function renderClientsMetrics(summary) {
  renderCards(els.clientsMetrics, clientsCards(summary, overviewReportUrl));
}

function renderAppointmentsMetrics(summary) {
  const { cards, exact, notice } = appointmentsCards(summary);
  renderCards(els.appointmentsMetrics, cards);
  els.appointmentsWarning.textContent = notice;
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
                        const statusClass = field === 'completion_pct' ? ` metric-status ${escapeHtml(cell.status || 'no-plan')}` : '';
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
    const title = t('dash.planVsFactTitle', { name: selectedStaffPlan.title || t('dash.staffFallbackLower') });
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
        const heading = escapeHtml(`${title} · ${planScopeLabel(scope)} · ${t(scale.labelKey)}`);
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

// The 1st sticky column's width varies by locale/content (e.g. Italian "Dimensione" won't
// break), so the 2nd column's `left` reads a CSS variable that this keeps in sync with it
// instead of a hardcoded offset. Both the observer and the initial measurement below share
// stickyColumnWidth() — getBoundingClientRect(), the border-box size `left` has to match
// under this page's `box-sizing: border-box` — never a ResizeObserver entry's own
// contentRect, which excludes padding and undershoots by exactly that much.
//
// A ResizeObserver, not a plain window resize listener: renderPlanFact can run while the
// Plan/actual view is still `display: none` (branch cell measures 0 then), and only the
// observer fires again once the view is shown and the cell gets its real layout box.
function stickyColumnWidth(cell) {
  return cell.getBoundingClientRect().width;
}

const planTableStickyObserver = new ResizeObserver((entries) => {
  entries.forEach((entry) => {
    const width = stickyColumnWidth(entry.target);
    if (!width) return;
    entry.target.closest('.plan-table')?.style.setProperty('--plan-col1-width', `${width}px`);
  });
});

function syncPlanTableStickyOffsets() {
  // Disconnect first: each render replaces the table markup wholesale, so the previous
  // .branch-cell elements are already detached and would otherwise pile up as dead
  // observations for as long as the page stays open.
  planTableStickyObserver.disconnect();
  document.querySelectorAll('.plan-table').forEach((table) => {
    const branchCell = table.querySelector('.branch-cell');
    if (!branchCell) return;
    // box: 'border-box' — the default (content-box) would miss a future CSS edit that
    // changes only padding at the 560px breakpoint without also changing `width`; today's
    // rule changes both, but observing the box we actually measure above is what makes
    // that true by construction instead of by coincidence.
    planTableStickyObserver.observe(branchCell, { box: 'border-box' });
    const width = stickyColumnWidth(branchCell);
    if (width) table.style.setProperty('--plan-col1-width', `${width}px`);
  });
}

function renderPlanFact(planFact) {
  const groups = planFact?.groups || [];
  const metrics = planFact?.metrics || [];
  const diagnosticsHtml = renderPlanDiagnostics(planFact?.diagnostics || []);
  if (!groups.length && !planFact?.parent_group) {
    renderPlanInsights(null);
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
  syncPlanTableStickyOffsets();
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
  els.planSettingsSave.disabled = planSettingsSaving || planSettingsLoading || !planSettingsData;
  els.planSettingsReset.disabled = planSettingsSaving || !isDirty || !planSettingsSavedData;
  updateFloatingEditorSave();
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
  const readValue = planSettingsInputReader(
    document.querySelectorAll('input[data-plan-branch], input[data-plan-staff]'),
  );
  return buildPlanSettingsPayload(planSettingsData, readValue);
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
    ? t('dash.lastSaveAt', { value: formatBranchDateTime(data.last_saved_at) ?? data.last_saved_at })
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
  planSettingsLoading = isLoading;
  els.planSettingsLoad.disabled = isLoading;
  els.planSettingsCopy.disabled = isLoading;
  els.planSettingsSave.disabled = isLoading || !planSettingsData;
  els.planSettingsReset.disabled = isLoading || !planSettingsDirty || !planSettingsSavedData;
  els.planSettingsLoad.textContent = isLoading ? t('common.loadingShort') : t('dash.load');
  els.planSettingsSave.textContent = isLoading ? t('common.saving') : t('dash.save');
  updateFloatingEditorSave();
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
    if (!request.isCurrent()) return false;
    renderPlanSettings(payload.data, { updateSnapshot: !copyFrom, dirty });
    viewsWithData.add('planSettings');
    clearError();
    setLoadedApiState(payload.data, {
      empty: !(payload.data?.branches?.length || payload.data?.staff?.length),
    });
    await syncStatus;
    return true;
  } catch (error) {
    if (isSupersededRequest(error) || !request.isCurrent()) return false;
    showError(error.message, { apiStatus: error.apiStatus, retry: () => loadPlanSettings({ month, copyFrom, dirty }) });
    return false;
  } finally {
    if (request.isCurrent()) {
      setPlanSettingsLoading(false);
      request.finish();
    }
  }
}

function setPlanSettingsInputsLocked(locked) {
  els.planSettingsBranches.inert = locked;
  els.planSettingsStaff.inert = locked;
}

async function savePlanSettings() {
  if (planSettingsSaving || planSettingsLoading || !planSettingsData) return;
  planSettingsSaving = true;
  try {
    // A key typed while the request is out would be lost: the response re-renders the tables and takes a new baseline.
    setPlanSettingsInputsLocked(true);
    updateFloatingEditorSave();
    clearError();
    setPlanSettingsLoading(true);
    setApiState(t('dash.apiSaving'), 'warn');
    const requestPayload = collectPlanSettingsPayload();
    if (planSettingsData.copy_from) requestPayload.copy_from = planSettingsData.copy_from;
    const payload = await postJson('/dashboard/plan/settings', requestPayload);
    renderPlanSettings(payload.data);
    setApiState(t('dash.apiConnected'), 'ok');
  } catch (error) {
    if (error.status === 409) {
      // Someone saved the same month first. Their plan wins; what we typed is not merged in,
      // because the sum of two people's edits is nobody's intent. The server's detail is English.
      const reloaded = await loadPlanSettings({ month: planSettingsData?.month || els.planSettingsMonth.value });
      if (reloaded) showError(t('dash.planSettingsConflict'), { apiStatus: 'ready' });
    } else {
      showError(error.message, { apiStatus: error.apiStatus, retry: () => savePlanSettings() });
    }
  } finally {
    planSettingsSaving = false;
    setPlanSettingsInputsLocked(false);
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
  // The catalogue is a setting, not a report, so there is no period to cut against and the
  // question is simply whether the branch is ours *now*: one that has left keeps its history
  // in the reports, but its services are no longer the tenant's to edit.
  const today = inputDateValue(branchToday());
  const options = branchesForPeriod(branchOptions, today, today);
  fillBranchSelect(els.serviceFilterBranch, options);
  els.serviceFilterBranch.value = options.some((branch) => String(branch.id) === selected) ? selected : '';
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
              <td>${escapeHtml(formatBranchDateTime(latestServiceManagementTimestamp(
                row.updated_at,
                row.label_updated_at,
                row.mutation_updated_at,
              )) || '')}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function collectServiceManagementPayload() {
  const rows = [...els.serviceCatalogTable.querySelectorAll('.service-extra-input')].map((input) => ({
    company_id: Number(input.dataset.companyId),
    service_id: Number(input.dataset.serviceId),
    is_extra: input.checked,
  }));
  return { rows };
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
  serviceManagementData = data || { rows: [], categories: [] };
  renderServiceFilterOptions(serviceManagementData);
  renderServiceCatalog(serviceManagementData.rows || []);
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
    dirty: serviceManagementDirty,
  });
  els.serviceFilterLoad.disabled = controls.refreshDisabled;
  [
    els.serviceFilterBranch,
    els.serviceFilterCategory,
    els.serviceFilterExtra,
    els.serviceFilterQuery,
  ].forEach((filter) => {
    filter.disabled = controls.filtersDisabled;
  });
  els.serviceCatalogTable.querySelectorAll('input, select, button').forEach((editor) => {
    editor.disabled = controls.editorDisabled;
  });
  els.serviceManagementSave.disabled = controls.saveDisabled;
  els.serviceManagementReset.disabled = controls.resetDisabled;
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
      empty: !payload.data?.rows?.length,
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
  const saved = serviceManagementSavedSnapshot ? JSON.parse(serviceManagementSavedSnapshot) : { rows: [] };
  const changes = serviceManagementChanges(current, saved);
  if (!changes.row_changes.length) {
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

// Which window the deltas are measured against depends on the preset, and the same dates
// picked two ways can land on two different baselines — so show it rather than let the
// reader guess.
function renderCompareNote(summary) {
  const note = els.compareNote;
  if (!note) return;
  const previous = summary?.previous_period;
  note.hidden = !previous;
  note.textContent = previous
    ? t('dash.overviewCompareNote', {
      start: formatDate(previous.start),
      end: formatDate(previous.end),
    })
    : '';
}

function renderBundle(bundle) {
  const {
    summary,
    revenue_daily: daily = [],
    top_services: services = [],
    extra_services: extraServices = [],
  } = bundle;
  applyFinancialVisibility(summary);
  renderCompareNote(summary);
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

function filterPeriod(filter) {
  if (filter.start && filter.end) return { start: filter.start.value, end: filter.end.value };
  const range = monthRange(filter.month?.value);
  return range || { start: '', end: '' };
}

function fillBranchSelect(select, branches) {
  select.innerHTML = `<option value="">${t('dash.allBranches')}</option>`;
  branches.forEach((branch) => {
    const option = document.createElement('option');
    option.value = branch.id;
    option.textContent = branch.title;
    select.appendChild(option);
  });
}

function renderBranchOptions(filter) {
  const selected = filter.branch.value;
  const { start, end } = filterPeriod(filter);
  const options = branchesForPeriod(branchOptions, start, end);
  fillBranchSelect(filter.branch, options);
  // A branch the new period cannot reach falls back to the whole scope, the same way an
  // unknown id always has: keeping it selected would filter by a branch that is not listed.
  filter.branch.value = options.some((branch) => String(branch.id) === selected) ? selected : '';
  customFilterDropdowns[filter.branch.id]?.refresh();
}

async function loadStaff(filter, { force = false } = {}) {
  if (force) loadedStaffFilters.delete(filter);
  if (loadedStaffFilters.has(filter)) return 'ready';
  const request = requestScopeForStaff(filter).start();
  try {
    const { start, end } = filterPeriod(filter);
    const payload = await fetchJson('/dashboard/staff', {
      company_id: filter.branch.value,
      start_date: start,
      end_date: end,
    }, {
      retry: () => loadStaff(filter, { force: true }),
      signal: request.signal,
      slowState: false,
    });
    if (!request.isCurrent()) return 'superseded';
    // Read after the response: a choice made while the list was loading has already reloaded the data.
    const selected = filter.staff.value;
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
  if (view === 'yandexPayFacts') return filterEls.yandexPay;
  return filterEls.overview;
}

async function ensureStaffForView(view) {
  if (view === 'planSettings' || view === 'serviceManagement' || view === 'reports' || view === 'yandexPayFacts') return;
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

/**
 * The preset the Overview window came from, for the backend to pick a baseline.
 *
 * Empty once the dates are edited by hand, and an empty param is dropped from the query.
 */
function activeOverviewPreset() {
  return overviewPreset;
}

function setFilterLoading(filter, isLoading) {
  filter.load.disabled = isLoading;
  filter.load.textContent = isLoading ? t('common.loadingShort') : t('dash.refresh');
}

async function loadSyncStatus() {
  const request = viewRequestScopes.syncStatus.start();
  try {
    const payload = await fetchJson('/dashboard/widget/sync_status', undefined, {
      signal: request.signal,
      slowState: false,
    });
    if (!request.isCurrent()) return;
    const sync = payload.data?.sync || {};
    const lastRun = sync.last_run;
    const lastSuccessfulAt = sync.last_successful_sync_at
      || (lastRun?.status === 'success' ? lastRun.finished_at : null);
    const updatedAt = lastSuccessfulAt ? formatBranchDateTime(lastSuccessfulAt) ?? lastSuccessfulAt : null;
    const failed = Boolean(sync.tenant_sync_failed);
    els.syncState.classList.toggle('sync-state--warning', failed);
    if (failed) {
      els.syncState.textContent = updatedAt
        ? t('dash.syncTenantFailed', { value: updatedAt })
        : t('dash.syncTenantFailedNoUpdates');
    } else {
      els.syncState.textContent = updatedAt
        ? t('dash.syncUpdatedAt', { value: updatedAt })
        : t('dash.syncNoSuccessfulUpdates');
    }
  } catch (error) {
    if (isSupersededRequest(error) || !request.isCurrent()) return;
    els.syncState.textContent = t('dash.syncStatusUnavailable');
  } finally {
    if (request.isCurrent()) request.finish();
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
  else if (window.location.hash === '#yandex-pay') view = 'yandexPayFacts';
  return accessibleView(view);
}

// .tabs scrolls horizontally on narrow screens; scrollIntoView would also move the
// page vertically, so nudge scrollLeft directly instead.
function scrollActiveTabIntoView(link) {
  const nav = link.closest('.tabs');
  if (!nav) return;
  const navRect = nav.getBoundingClientRect();
  const linkRect = link.getBoundingClientRect();
  if (linkRect.left < navRect.left) {
    nav.scrollLeft -= navRect.left - linkRect.left;
  } else if (linkRect.right > navRect.right) {
    nav.scrollLeft += linkRect.right - navRect.right;
  }
}

function setActiveView(view) {
  view = accessibleView(view);
  const previousView = activeView;
  Object.entries(viewRequestScopes).forEach(([requestView, scope]) => {
    if (requestView !== 'branches' && requestView !== 'syncStatus' && requestView !== view) scope.abort();
  });
  // An aborted load never reaches its own finally, which would leave Save disabled for good.
  if (view !== 'planSettings' && planSettingsLoading && !planSettingsSaving) setPlanSettingsLoading(false);
  if (previousView === 'reports' && view !== 'reports') reportsController?.clear({ keepCatalog: true });
  activeView = view;
  els.overviewView.classList.toggle('active', view === 'overview');
  els.planView.classList.toggle('active', view === 'plan');
  els.planSettingsView.classList.toggle('active', view === 'planSettings');
  els.serviceManagementView.classList.toggle('active', view === 'serviceManagement');
  els.reviewFactsView.classList.toggle('active', view === 'reviewFacts');
  els.opzFactsView.classList.toggle('active', view === 'opzFacts');
  els.yandexPayView.classList.toggle('active', view === 'yandexPayFacts');
  if (view === 'yandexPayFacts') limitYandexPayMonth();
  els.reportsView.classList.toggle('active', view === 'reports');
  els.viewLinks.forEach((link) => {
    const isActive = link.dataset.viewLink === view;
    link.classList.toggle('active', isActive);
    if (isActive) scrollActiveTabIntoView(link);
  });
  updateFloatingEditorSave();
  const labels = {
    overview: t('dash.subhead'),
    plan: t('dash.planSubhead'),
    planSettings: t('dash.planSettingsSubhead'),
    serviceManagement: t('dash.serviceManagementSubhead'),
    reviewFacts: t('dash.reviewFactsSubhead'),
    opzFacts: t('dash.opzFactsSubhead'),
    yandexPayFacts: t('dash.yandexPaySubhead'),
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

async function loadDashboard() {
  const filter = filterEls.overview;
  const request = beginViewRequest('overview');
  clearError();
  setFilterLoading(filter, true);

  const syncStatus = loadSyncStatus();
  try {
    const payload = await fetchJson('/dashboard/bundle', {
      ...filterParams(filter),
      period_preset: activeOverviewPreset(),
    }, {
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
  } else if (manualEditors.byView[activeView]) {
    await manualEditors.byView[activeView].load();
  } else if (activeView === 'reports') {
    await reportsController?.loadFromLocation();
  } else {
    await loadDashboard();
  }
}

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
      profileRole.textContent = roleLabel(me.data.role);
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
  setActiveView(viewFromLocation());
  const existingPosition = Number(history.state?.[HISTORY_POSITION_KEY]);
  historyPosition = Number.isFinite(existingPosition) ? existingPosition : 0;
  replaceDashboardHistory({ view: activeView });
  // Safe to run concurrently because loadStaff() reads filter.branch.value SYNCHRONOUSLY,
  // before its first await, so the value it sends is deterministically '' regardless of
  // which request resolves first. That synchronous read is the load-bearing part, not the
  // emptiness of the select: the later read (after the /dashboard/staff await, where the
  // label is built) IS order-dependent, and is benign today only because renderBranchOptions
  // also resolves to '' on a cold start. Give loadBranches() a preselected branch — a
  // restored selection, a single-branch tenant — and that later read becomes a
  // network-timing race. Re-check it before adding any such preselection.
  await Promise.all([loadBranches(), ensureStaffForView(activeView)]);
  await loadCurrentView();
}

async function init() {
  reportsController = initReports({
    clearError,
    showError,
    setApiState,
    pushHistory: pushDashboardHistory,
    replaceHistory: replaceDashboardHistory,
  });
  mountLanguageSwitcher(document.getElementById('lang-switcher'), {
    beforeChange: () => runDashboardNavigation(async () => {}),
    afterChange: () => window.location.reload(),
  });
  [filterEls.overview, filterEls.plan].forEach((filter) => defaultDates(filter));
  setManualFactDefaultMonths();
  highlightOverviewPreset(overviewPreset);
  els.planSettingsMonth.value = currentMonthValue();
  renderServicesTable([]);
  renderExtraServicesTable([]);
  renderServiceManagement({ rows: [], categories: [] });
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
[filterEls.overview.start, filterEls.overview.end].forEach((input) => {
  input.addEventListener('change', () => {
    clearOverviewPeriodChoice();
    refilterForPeriod(filterEls.overview);
  });
});
filterEls.overview.month.addEventListener('change', async () => {
  if (!applyFilterMonth(filterEls.overview)) return;
  overviewPreset = 'month';
  // The quick button says "this month", so it only lights up for the month it means —
  // the baseline stays month-over-month for every other one.
  highlightOverviewPreset(
    filterEls.overview.month.value === monthValue(branchToday()) ? 'month' : '',
  );
  if (await refilterForPeriod(filterEls.overview)) loadDashboard();
});
filterEls.overview.branch.addEventListener('change', async () => {
  await refreshStaffForBranch(filterEls.overview, loadDashboard);
});
filterEls.overview.staff.addEventListener('change', () => loadDashboard());
els.overviewPresetButtons.forEach((button) => {
  button.addEventListener('click', async () => {
    setOverviewPreset(button.dataset.overviewPreset || 'month');
    // Assigning the dates fires no change event, so the selectors would keep the old window's lists.
    if (await refilterForPeriod(filterEls.overview)) loadDashboard();
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
[filterEls.plan.start, filterEls.plan.end].forEach((input) => {
  input.addEventListener('change', () => {
    filterEls.plan.month.value = '';
    refilterForPeriod(filterEls.plan);
  });
});
filterEls.plan.month.addEventListener('change', async () => {
  if (applyFilterMonth(filterEls.plan) && await refilterForPeriod(filterEls.plan)) loadPlanFact();
});
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
els.floatingEditorSaveButton.addEventListener('click', () => {
  if (activeView === 'planSettings') savePlanSettings();
  else manualEditors.byView[activeView]?.save();
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
els.serviceFilterExtra.addEventListener('change', () => loadServiceManagement());
els.serviceFilterQuery.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') loadServiceManagement();
});
['input', 'change'].forEach((type) => els.serviceCatalogTable.addEventListener(type, (event) => {
  if (editEventChecksDraft(event)) updateServiceManagementDirtyFromForm();
}));
els.serviceManagementSave.addEventListener('click', () => saveServiceManagement());
els.serviceManagementReset.addEventListener('click', () => {
  if (serviceManagementSavedData) {
    renderServiceManagement(JSON.parse(JSON.stringify(serviceManagementSavedData)));
  }
});

function closeCardHints(except = null) {
  document.querySelectorAll('[data-card-hint].is-open').forEach((button) => {
    if (button === except) return;
    button.classList.remove('is-open');
  });
}

// Hover and focus are handled in CSS; this only covers touch, where neither fires.
document.addEventListener('click', (event) => {
  const hint = event.target.closest('[data-card-hint]');
  closeCardHints(hint);
  if (!hint) return;
  hint.classList.toggle('is-open');
});

// Escape must dismiss a tooltip opened by hover too, and CSS :hover cannot be cleared
// from script — so suppress the whole layer until the user moves on. Any pointer move,
// keystroke or focus change lifts it: leaving it to the pointer alone would lock a
// keyboard-only user out of every hint for the rest of the session.
function releaseCardHints() {
  document.body.classList.remove('hints-dismissed');
}

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') {
    releaseCardHints();
    return;
  }
  closeCardHints();
  if (document.body.classList.contains('hints-dismissed')) return;
  document.body.classList.add('hints-dismissed');
  document.addEventListener('pointermove', releaseCardHints, { once: true });
  document.addEventListener('focusin', releaseCardHints, { once: true });
});

document.addEventListener('click', async (event) => {
  const link = event.target.closest('[data-report-link]');
  if (!link || !shouldHandleSameTabNavigation(event)) return;
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
    if (!view || !shouldHandleSameTabNavigation(event)) return;
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

function navigationNeedsGuard() {
  return protectedSavePending()
    || hasProtectedDirtyChanges()
    || (activeView === 'serviceManagement' && (serviceManagementDirty || serviceManagementMutationPending));
}

window.addEventListener('popstate', async (event) => {
  const targetPosition = Number(event.state?.[HISTORY_POSITION_KEY]);
  const needsGuard = navigationNeedsGuard();
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

  const needsGuard = navigationNeedsGuard();
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
    && !Object.values(manualEditors.byView).some((editor) => editor.dirty || editor.saving)
    && !serviceManagementDirty
    && !serviceManagementMutationPending
  ) return;
  event.preventDefault();
  event.returnValue = '';
});
init();
