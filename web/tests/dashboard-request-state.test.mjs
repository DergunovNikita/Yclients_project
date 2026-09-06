import test from 'node:test';
import assert from 'node:assert/strict';

import {
  apiStatusForHttpStatus,
  createLatestRequestScope,
  createTimedAbortContext,
  filterServiceManagementResult,
  latestServiceManagementTimestamp,
  REPORT_FILTER_KEYS,
  reportCompareParams,
  reportDataCacheKey,
  reportDataState,
  reportFiltersFromParams,
  reportHistoryAction,
  reportLinkSearch,
  reportPeriodIsValid,
  reportRequestFilters,
  staffSelectionForOptions,
  reportSearchParams,
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

test('a report link carries the whole filter state, not just what it mentions', () => {
  const full = reportFiltersFromParams(new URLSearchParams(
    'start_date=2026-08-01&end_date=2026-08-31&company_id=7&staff_id=42'
    + '&granularity=month&compare_start_date=2025-08-01&compare_end_date=2025-08-31',
  ));
  assert.deepEqual(full, {
    start_date: '2026-08-01',
    end_date: '2026-08-31',
    company_id: '7',
    staff_id: '42',
    granularity: 'month',
    compare_start_date: '2025-08-01',
    compare_end_date: '2025-08-31',
    compare_enabled: true,
    period_preset: '',
  });

  // Going back to an entry that named no branch, staff or comparison must drop them
  // rather than inherit the filters of the report being left behind.
  const bare = reportFiltersFromParams(new URLSearchParams('start_date=2026-08-01'));
  assert.equal(bare.company_id, '');
  assert.equal(bare.staff_id, '');
  assert.equal(bare.compare_start_date, '');
  assert.equal(bare.compare_enabled, false);
  assert.equal(bare.granularity, 'day');
  // Half a window is not a window: the API rejects one bound without the other.
  const half = reportFiltersFromParams(new URLSearchParams('compare_end_date=2025-08-31'));
  assert.equal(half.compare_enabled, false);
  assert.equal(half.compare_end_date, '');
  // A link written without a preset leaves the report on its own baseline rule.
  assert.equal(bare.period_preset, '');
  assert.equal(
    reportFiltersFromParams(new URLSearchParams('period_preset=quarter')).period_preset,
    'quarter',
  );
});

test('a report link reads back exactly what it was written from', () => {
  const filters = {
    start_date: '2026-08-01',
    end_date: '2026-08-31',
    company_id: '7',
    staff_id: '42',
    granularity: 'month',
    compare_start_date: '2025-08-01',
    compare_end_date: '2025-08-31',
    compare_enabled: true,
    period_preset: '',
  };
  assert.deepEqual(reportFiltersFromParams(reportSearchParams(filters)), filters);

  // The Overview preset rides along so a reload keeps measuring the report against the
  // same baseline as the card that linked to it. It is not a filter and has no input,
  // so nothing else in the link machinery would carry it.
  const fromCard = { ...filters, period_preset: 'week' };
  const link = reportSearchParams(fromCard);
  assert.equal(link.get('period_preset'), 'week');
  assert.deepEqual(reportFiltersFromParams(link), fromCard);

  // An unticked comparison leaves no trace in the link.
  const offRoundTrip = reportFiltersFromParams(
    reportSearchParams({ ...filters, compare_enabled: false }),
  );
  assert.equal(offRoundTrip.compare_enabled, false);
  assert.equal(offRoundTrip.compare_start_date, '');

  // Neither does half a window, which the API would reject on the way back in.
  const halfRoundTrip = reportFiltersFromParams(
    reportSearchParams({ ...filters, compare_end_date: '' }),
  );
  assert.equal(halfRoundTrip.compare_enabled, false);
  assert.equal(halfRoundTrip.compare_start_date, '');

  // An empty form produces an empty link, and granularity comes back at its default.
  assert.equal(reportSearchParams({}).toString(), '');
  assert.equal(reportFiltersFromParams(reportSearchParams({})).granularity, 'day');
});

test('the request and the link ask for the same comparison', () => {
  const base = {
    start_date: '2026-08-01',
    end_date: '2026-08-31',
    granularity: 'month',
    compare_start_date: '2025-08-01',
    compare_end_date: '2025-08-31',
  };
  const cases = [
    { ...base, compare_enabled: true },
    { ...base, compare_enabled: false },
    { ...base, compare_enabled: true, compare_end_date: '' },
    { ...base, compare_enabled: true, compare_start_date: '', compare_end_date: '' },
    // Inverted like the main period, and just as much a window still being typed.
    { ...base, compare_enabled: true, compare_start_date: '2025-09-30', compare_end_date: '2025-08-31' },
  ];
  for (const filters of cases) {
    const request = reportCompareParams(filters);
    const link = reportFiltersFromParams(reportSearchParams(filters));
    assert.equal(link.compare_enabled, Boolean(request), `disagreed on ${JSON.stringify(filters)}`);
    if (!request) continue;
    assert.equal(link.compare_start_date, request.compare_start_date);
    assert.equal(link.compare_end_date, request.compare_end_date);
  }
});

test('only a filter change rewrites the history entry', () => {
  const currentUrl = '/reports/financial_overview?start_date=2026-08-01&granularity=day';
  // Opening a report from the catalog is a navigation.
  assert.equal(reportHistoryAction({
    push: true, historyUrl: currentUrl, currentUrl,
  }), 'push');
  // Changing a filter keeps one entry but makes its URL tell the truth.
  assert.equal(reportHistoryAction({
    push: false,
    historyUrl: '/reports/financial_overview?start_date=2026-08-01&granularity=month',
    currentUrl,
  }), 'replace');
  // Landing on an entry that already says this leaves history untouched.
  assert.equal(reportHistoryAction({
    push: false, historyUrl: currentUrl, currentUrl,
  }), 'none');
});

test('a period caught mid-edit never reaches the address bar', () => {
  const valid = {
    start_date: '2026-08-01',
    end_date: '2026-08-31',
    company_id: '7',
    granularity: 'month',
  };
  assert.equal(reportPeriodIsValid(valid), true);
  // Typing the new start before the new end inverts the range the API accepts.
  assert.equal(reportPeriodIsValid({ start_date: '2026-09-30', end_date: '2026-09-01' }), false);
  // A single day is a range. An empty bound is not this rule's business: the query
  // parameter is required, so the endpoint rejects it before the ordering matters.
  assert.equal(reportPeriodIsValid({ start_date: '2026-08-01', end_date: '2026-08-01' }), true);
  assert.equal(reportPeriodIsValid({ start_date: '2026-08-01', end_date: '' }), true);

  const currentSearch = 'start_date=2026-06-01&end_date=2026-06-30&granularity=day';
  // Every link — the report's and the catalog's alike — is written through this one rule.
  assert.equal(
    reportLinkSearch({ filters: valid, currentSearch }),
    'start_date=2026-08-01&end_date=2026-08-31&company_id=7&granularity=month',
  );
  assert.equal(
    reportLinkSearch({ filters: { ...valid, start_date: '2026-09-30', end_date: '2026-09-01' }, currentSearch }),
    currentSearch,
  );

  // The compare row is typed the same way and carries the same hazard: an inverted window
  // reaches neither the request nor the link, so no round trip is spent on a 400.
  const comparing = {
    ...valid,
    compare_enabled: true,
    compare_start_date: '2025-09-30',
    compare_end_date: '2025-08-31',
  };
  assert.equal(reportCompareParams(comparing), null);
  assert.equal(
    reportLinkSearch({ filters: comparing, currentSearch }).includes('compare_start_date'),
    false,
  );
});

test('a staff link keeps its employee only while that branch still has them', () => {
  // The select reports '' for an id whose option has not been rendered yet, so the
  // selection is decided against the ids that are about to be rendered instead.
  assert.equal(staffSelectionForOptions('42', [7, 42, 99]), '42');
  assert.equal(staffSelectionForOptions(42, ['42']), '42');
  // An employee of another branch falls back to the whole branch, deliberately.
  assert.equal(staffSelectionForOptions('42', [7, 99]), '');
  assert.equal(staffSelectionForOptions('42', []), '');
  assert.equal(staffSelectionForOptions('', [7, 42]), '');
});

test('every report filter reaches the link and comes back', () => {
  // The request, the link and the parser walk REPORT_FILTER_KEYS, so a filter added to it
  // cannot reach the request while quietly falling out of the link a user shares.
  const filters = Object.fromEntries(REPORT_FILTER_KEYS.map((key) => [key, `${key}-value`]));
  filters.granularity = 'month';
  const roundTripped = reportFiltersFromParams(reportSearchParams(filters));
  for (const key of REPORT_FILTER_KEYS) {
    assert.equal(roundTripped[key], filters[key], `${key} did not survive the link`);
  }
});

test('a report that hides the period is run and linked with a valid one', () => {
  // The date inputs are shared, so this report can inherit an inverted range it never shows.
  const inherited = {
    start_date: '2026-09-30',
    end_date: '2026-09-01',
    company_id: '7',
    granularity: 'day',
  };
  const fallbackPeriod = { start: '2026-08-01', end: '2026-08-31' };

  // A report that uses the period keeps exactly what the form holds.
  assert.deepEqual(
    reportRequestFilters({ filters: inherited, periodApplies: true, fallbackPeriod }),
    inherited,
  );

  const substituted = reportRequestFilters({
    filters: inherited,
    periodApplies: false,
    fallbackPeriod,
  });
  assert.equal(substituted.start_date, '2026-08-01');
  assert.equal(substituted.end_date, '2026-08-31');
  assert.equal(substituted.company_id, '7');
  // Valid, so the report is neither blocked nor left with a frozen link that would drop
  // the branch the user picked.
  assert.equal(reportPeriodIsValid(substituted), true);
});

test('a report that hides the period still publishes the filters the user did pick', () => {
  // Composed the way reportSearch() composes it: the link is built from the form's own
  // values, and only the freeze rule is told whether the period is in use.
  const inverted = {
    start_date: '2026-09-05',
    end_date: '2026-08-31',
    company_id: '7',
    granularity: 'day',
  };
  const currentSearch = 'start_date=2026-09-05&end_date=2026-08-31&company_id=5&granularity=day';

  // A report that shows the period keeps the last valid link while the range is inverted —
  // the user can see the dates and fix them.
  assert.equal(
    reportLinkSearch({ filters: inverted, currentSearch, periodApplies: true }),
    currentSearch,
  );

  // One that hides the period cannot be fixed by its user, so freezing its link would strand
  // the branch they switched to in the data while the URL kept advertising the old one.
  const published = reportLinkSearch({ filters: inverted, currentSearch, periodApplies: false });
  assert.equal(published.includes('company_id=7'), true);
  assert.equal(published.includes('start_date=2026-09-05'), true);
});
