import test from 'node:test';
import assert from 'node:assert/strict';

import {
  apiStatusForHttpStatus,
  createLatestRequestScope,
  createTimedAbortContext,
  reportDataCacheKey,
  reportDataState,
  reportRefreshPresentation,
  reportScopedFilterAllowsLoad,
  responseError,
  staffRefreshAllowsDataLoad,
} from '../src/dashboardRequestState.js';
import { rankingRowsForMetric } from '../src/reports/ranking.js';

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
