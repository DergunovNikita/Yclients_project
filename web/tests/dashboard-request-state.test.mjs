import test from 'node:test';
import assert from 'node:assert/strict';

import {
  apiStatusForHttpStatus,
  createLatestRequestScope,
  createTimedAbortContext,
  filterServiceManagementResult,
  latestServiceManagementTimestamp,
  reportDataCacheKey,
  reportDataState,
  chartTooltipValue,
  reportFilterVisibility,
  reportRefreshPresentation,
  reportScopedFilterAllowsLoad,
  responseError,
  runServiceManagementMutation,
  serviceManagementChanges,
  serviceManagementControls,
  serviceManagementLoadAllowed,
  serviceManagementNavigationAllowed,
  settleServiceManagementLoad,
  shouldRenderReportDataLabel,
  mergeServiceManagementResult,
  staffRefreshAllowsDataLoad,
} from '../src/dashboardRequestState.js';
import { rankingRowsForMetric, tableHasRows } from '../src/reports/ranking.js';

test('HTTP errors retain distinct dashboard states and retry metadata', () => {
  assert.equal(apiStatusForHttpStatus(401), 'auth_required');
  assert.equal(apiStatusForHttpStatus(403), 'forbidden');
  assert.equal(apiStatusForHttpStatus(500), 'server_error');
  assert.equal(apiStatusForHttpStatus(503), 'server_error');
  assert.equal(apiStatusForHttpStatus(504), 'timeout');

  const error = responseError(
    { status: 503 },
    { detail: { code: 'report_calculation_failed', message: 'Повторите расчёт', retryable: true } },
    'fallback',
  );
  assert.equal(error.message, 'Повторите расчёт');
  assert.equal(error.code, 'report_calculation_failed');
  assert.equal(error.apiStatus, 'server_error');
  assert.equal(error.retryable, true);
});

test('report payload distinguishes partial, empty and ready states', () => {
  assert.equal(reportDataState({ source_status: 'partial', tables: [] }), 'partial');
  assert.equal(reportDataState({ source_status: 'ready', cards: [], charts: [], tables: [] }), 'empty');
  assert.equal(reportDataState({ source_status: 'ready', tables: [{ rows: [{ id: 1 }] }] }), 'ready');
  assert.equal(reportDataState({
    source_status: 'ready',
    tables: [{ rows: [], ranking: { rows_by_metric: { sum: [], qty: [{ id: 1 }] } } }],
  }), 'ready');
});

test('year-over-year filter metadata hides dates and null chart points stay unlabeled', () => {
  assert.deepEqual(reportFilterVisibility({
    date_range: false,
    granularity: false,
    compare: false,
  }), {
    dateRange: false,
    granularity: false,
    compare: false,
  });
  assert.equal(shouldRenderReportDataLabel(null), false);
  assert.equal(shouldRenderReportDataLabel(undefined), false);
  assert.equal(shouldRenderReportDataLabel(''), false);
  assert.equal(shouldRenderReportDataLabel(0), true);
  assert.equal(shouldRenderReportDataLabel(1250), true);
});

test('chart tooltip reads the measured axis, not the category index', () => {
  // Chart.js parses a vertical bar to {x: <label index>, y: <value>}. Reading x
  // reported the category position as the value: 2025 (the 9th bar) showed "8 ₽".
  assert.equal(chartTooltipValue({ x: 8, y: 93_700_000 }), 93_700_000);
  assert.equal(chartTooltipValue({ x: 8, y: 93_700_000 }, 'x'), 93_700_000);
  // Horizontal bars swap the roles.
  assert.equal(chartTooltipValue({ x: 93_700_000, y: 8 }, 'y'), 93_700_000);
  // Arc charts parse to a bare number.
  assert.equal(chartTooltipValue(4200), 4200);
  assert.equal(chartTooltipValue({ x: 3, y: 0 }), 0);
  assert.equal(chartTooltipValue(null), null);
});

test('refreshing a cached report retains that report while the request is pending', () => {
  const cached = { report_id: 'staff_leaderboard', tables: [{ rows: [{ staff: 'Master' }] }] };
  assert.deepEqual(reportRefreshPresentation(cached), {
    state: 'refreshing',
    retainedData: cached,
  });
  assert.deepEqual(reportRefreshPresentation(null), {
    state: 'loading',
    retainedData: null,
  });
});

test('report cache keys include every effective filter and ignore object key order', () => {
  const base = reportDataCacheKey({
    report_id: 'staff_leaderboard',
    start_date: '2026-07-01',
    end_date: '2026-07-18',
    company_id: 1,
    staff_id: 10,
    granularity: 'day',
  });
  assert.equal(base, reportDataCacheKey({
    granularity: 'day',
    staff_id: 10,
    company_id: 1,
    end_date: '2026-07-18',
    start_date: '2026-07-01',
    report_id: 'staff_leaderboard',
  }));
  assert.notEqual(base, reportDataCacheKey({
    report_id: 'staff_leaderboard',
    start_date: '2026-07-01',
    end_date: '2026-07-18',
    company_id: 2,
    staff_id: 10,
    granularity: 'day',
  }));
  assert.notEqual(base, reportDataCacheKey({
    report_id: 'staff_leaderboard',
    start_date: '2026-07-01',
    end_date: '2026-07-18',
    company_id: 1,
    staff_id: 11,
    granularity: 'day',
  }));
});

test('a newer filter request aborts and invalidates the stale request', () => {
  const scope = createLatestRequestScope();
  const first = scope.start();
  const second = scope.start();

  assert.equal(first.signal.aborted, true);
  assert.equal(first.isCurrent(), false);
  assert.equal(second.signal.aborted, false);
  assert.equal(second.isCurrent(), true);

  scope.abort();
  assert.equal(second.signal.aborted, true);
  assert.equal(second.isCurrent(), false);
});

test('branch data loads after staff success or failure, but not after stale refreshes', () => {
  assert.equal(staffRefreshAllowsDataLoad('ready', '1', '1'), true);
  assert.equal(staffRefreshAllowsDataLoad('failed', '1', 1), true);
  assert.equal(staffRefreshAllowsDataLoad('superseded', '1', '1'), false);
  assert.equal(staffRefreshAllowsDataLoad('ready', '1', '2'), false);
});

test('service management sends only changed fields in one batch', () => {
  const saved = {
    rows: [
      { company_id: 1, service_id: 10, is_extra: false, kpi_group_id: null },
      { company_id: 1, service_id: 20, is_extra: false, kpi_group_id: 2 },
    ],
    groups: [
      { id: 2, title: 'Care', code: 'care', description: '', sort_order: 0, is_active: true },
    ],
  };
  const current = structuredClone(saved);
  current.rows[0].is_extra = true;
  current.rows[1].kpi_group_id = null;
  current.groups[0].title = 'Face Care';

  assert.deepEqual(serviceManagementChanges(current, saved), {
    row_changes: [
      { company_id: 1, service_id: 10, is_extra: true },
      { company_id: 1, service_id: 20, kpi_group_id: null },
    ],
    group_changes: [{ id: 2, title: 'Face Care' }],
  });
  assert.deepEqual(serviceManagementChanges(saved, saved), { row_changes: [], group_changes: [] });
});

test('service management controls reflect dirty and loading state', () => {
  assert.deepEqual(serviceManagementControls({ loading: false, hasData: true, hasSavedData: true, dirty: false }), {
    filtersDisabled: false,
    editorDisabled: false,
    refreshDisabled: false,
    saveDisabled: true,
    resetDisabled: true,
    addGroupDisabled: false,
  });
  assert.deepEqual(serviceManagementControls({ loading: false, hasData: true, hasSavedData: true, dirty: true }), {
    filtersDisabled: true,
    editorDisabled: false,
    refreshDisabled: true,
    saveDisabled: false,
    resetDisabled: false,
    addGroupDisabled: true,
  });
  assert.deepEqual(serviceManagementControls({ loading: true, hasData: true, hasSavedData: true, dirty: true }), {
    filtersDisabled: true,
    editorDisabled: true,
    refreshDisabled: true,
    saveDisabled: true,
    resetDisabled: true,
    addGroupDisabled: true,
  });
  assert.equal(serviceManagementLoadAllowed({ loading: false, dirty: false }), true);
  assert.equal(serviceManagementLoadAllowed({ loading: false, dirty: true }), false);
  assert.equal(serviceManagementLoadAllowed({ loading: true, dirty: false }), false);
});

test('failed service mutation keeps edits retryable and blocks overlapping reloads', async () => {
  let dirty = true;
  let loading = false;
  let rejectMutation;
  let observedError = null;
  let successCalled = false;
  const pendingMutation = new Promise((resolve, reject) => {
    rejectMutation = reject;
  });

  const operation = runServiceManagementMutation(
    () => pendingMutation,
    {
      setLoading: (value) => { loading = value; },
      onSuccess: () => { successCalled = true; },
      onError: (error) => { observedError = error; },
    },
  );

  assert.equal(loading, true);
  assert.equal(serviceManagementLoadAllowed({ loading, dirty }), false);
  const timeout = Object.assign(new Error('timeout'), { apiStatus: 'timeout' });
  rejectMutation(timeout);
  const result = await operation;

  assert.equal(result.ok, false);
  assert.equal(result.error, timeout);
  assert.equal(observedError, timeout);
  assert.equal(successCalled, false);
  assert.equal(dirty, true);
  assert.equal(loading, false);
  assert.equal(serviceManagementLoadAllowed({ loading, dirty }), false);
});

test('an aborted service load releases its loading state', () => {
  const scope = createLatestRequestScope();
  const request = scope.start();
  let loading = true;

  scope.abort();
  settleServiceManagementLoad(request, (value) => { loading = value; });

  assert.equal(request.signal.aborted, true);
  assert.equal(request.isCurrent(), false);
  assert.equal(loading, false);
});

test('pending service mutations block every SPA navigation path', () => {
  assert.equal(serviceManagementNavigationAllowed({
    mutationPending: true,
    activeView: 'serviceManagement',
  }), false);
  assert.equal(serviceManagementNavigationAllowed({
    mutationPending: false,
    activeView: 'serviceManagement',
  }), true);
  assert.equal(serviceManagementNavigationAllowed({
    mutationPending: true,
    activeView: 'overview',
  }), true);
});

test('service management displays the latest catalog mutation timestamp', () => {
  assert.equal(latestServiceManagementTimestamp(
    '2026-07-01T10:00:00',
    '2026-07-10T10:00:00',
    null,
    '2026-07-18T10:00:00',
  ), '2026-07-18T10:00:00');
  assert.equal(latestServiceManagementTimestamp(null, '2026-07-10T10:00:00', null), '2026-07-10T10:00:00');
  assert.equal(latestServiceManagementTimestamp(null, null), null);
});

test('service management removes saved rows that no longer match active filters', () => {
  const data = {
    rows: [
      { company_id: 1, service_id: 10, is_extra: false, kpi_group_id: 2 },
      { company_id: 1, service_id: 20, is_extra: true, kpi_group_id: 3 },
      { company_id: 1, service_id: 30, is_extra: true, kpi_group_id: 2 },
    ],
    groups: [{ id: 2 }, { id: 3 }],
    total: 3,
  };

  const extraOnly = filterServiceManagementResult(data, { is_extra: true });
  assert.deepEqual(extraOnly.rows.map((row) => row.service_id), [20, 30]);
  assert.equal(extraOnly.total, 2);

  const selectedGroup = filterServiceManagementResult(data, { kpi_group_id: '2' });
  assert.deepEqual(selectedGroup.rows.map((row) => row.service_id), [10, 30]);
  assert.equal(selectedGroup.total, 2);
  assert.equal(selectedGroup.groups, data.groups);
});

test('service management merges normalized batch and created group results', () => {
  const data = {
    rows: [{
      company_id: 1,
      service_id: 10,
      title: 'Mask',
      is_extra: false,
      label_updated_at: '2026-07-01T10:00:00',
      kpi_group_id: null,
      kpi_assignment_updated_at: null,
    }],
    groups: [{ id: 1, title: 'Old', code: 'old', is_active: true }],
    categories: ['Care'],
  };
  const merged = mergeServiceManagementResult(data, {
    rows: [{
      company_id: 1,
      service_id: 10,
      is_extra: true,
      label_updated_at: '2026-07-18T10:00:00',
      kpi_group_id: 2,
      kpi_assignment_updated_at: '2026-07-18T10:00:00',
      mutation_updated_at: '2026-07-18T10:00:00',
    }],
    groups: [
      { id: 1, title: 'Renamed', code: 'old', is_active: true },
      { id: 2, title: 'New', code: 'new', is_active: true },
    ],
  });

  assert.deepEqual(merged.rows, [
    {
      company_id: 1,
      service_id: 10,
      title: 'Mask',
      is_extra: true,
      label_updated_at: '2026-07-18T10:00:00',
      kpi_group_id: 2,
      kpi_assignment_updated_at: '2026-07-18T10:00:00',
      mutation_updated_at: '2026-07-18T10:00:00',
    },
  ]);
  assert.deepEqual(merged.groups, [
    { id: 1, title: 'Renamed', code: 'old', is_active: true },
    { id: 2, title: 'New', code: 'new', is_active: true },
  ]);
  assert.deepEqual(merged.categories, ['Care']);
});

test('a deep-linked branch or staff never degrades to a broader scope', () => {
  assert.equal(reportScopedFilterAllowsLoad('', false, []), true);
  assert.equal(reportScopedFilterAllowsLoad('17', false, []), false);
  assert.equal(reportScopedFilterAllowsLoad('17', true, [17, 18]), true);
  assert.equal(reportScopedFilterAllowsLoad('17', true, [18]), false);
});

test('slow warning keeps retry action and timeout aborts the request', () => {
  const timers = new Map();
  let nextTimer = 0;
  const retry = () => 'retry';
  let slowEvent = null;
  const context = createTimedAbortContext(null, {
    slowState: true,
    onSlow: (event) => { slowEvent = event; },
    retry,
    slowMs: 12000,
    timeoutMs: 60000,
    timeoutError: () => Object.assign(new Error('timeout'), { apiStatus: 'timeout' }),
    supersededError: () => Object.assign(new Error('superseded'), { apiStatus: 'superseded' }),
    setTimer: (callback, delay) => {
      const id = ++nextTimer;
      timers.set(id, { callback, delay });
      return id;
    },
    clearTimer: (id) => timers.delete(id),
  });

  [...timers.values()].find((timer) => timer.delay === 12000).callback();
  assert.equal(slowEvent.state, 'slow');
  assert.equal(slowEvent.retry, retry);
  [...timers.values()].find((timer) => timer.delay === 60000).callback();
  assert.equal(context.signal.aborted, true);
  assert.equal(context.abortError().apiStatus, 'timeout');
  context.cleanup();
  assert.equal(timers.size, 0);
});

test('ranking variants switch locally through rows_by_metric', () => {
  const table = {
    rows: [{ staff: 'Default' }],
    ranking: {
      default_metric: 'pct',
      rows_by_metric: {
        qty: [{ staff: 'Quantity' }],
        pct: [{ staff: 'Percent' }],
      },
    },
  };

  assert.deepEqual(rankingRowsForMetric(table, 'qty'), [{ staff: 'Quantity' }]);
  assert.deepEqual(rankingRowsForMetric(table, 'pct'), [{ staff: 'Percent' }]);
  assert.deepEqual(rankingRowsForMetric(table, 'missing'), []);
});

test('tableHasRows detects data across plain rows and ranking metrics', () => {
  assert.equal(tableHasRows({ rows: [{ staff: 'A' }] }), true);
  assert.equal(tableHasRows({ rows: [] }), false);
  assert.equal(tableHasRows({ rows: [], ranking: { rows_by_metric: { qty: [], pct: [] } } }), false);
  assert.equal(tableHasRows({ rows: [], ranking: { rows_by_metric: { qty: [], pct: [{ staff: 'A' }] } } }), true);
  assert.equal(tableHasRows({}), false);
});
