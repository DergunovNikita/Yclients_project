import { tableHasRows } from './reports/ranking.js';

export function apiStatusForHttpStatus(status) {
  if (status === 401) return 'auth_required';
  if (status === 403) return 'forbidden';
  if (status === 408 || status === 504) return 'timeout';
  if (status >= 500) return 'server_error';
  return 'error';
}

export function payloadErrorMessage(payload, fallback) {
  const detail = payload?.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object' && typeof detail.message === 'string') return detail.message;
  if (typeof payload?.message === 'string') return payload.message;
  return fallback;
}

export function responseError(response, payload, fallback) {
  const error = new Error(payloadErrorMessage(payload, fallback));
  error.status = response.status;
  error.apiStatus = apiStatusForHttpStatus(response.status);
  error.code = payload?.detail?.code || payload?.code || null;
  error.retryable = Boolean(payload?.detail?.retryable ?? payload?.retryable ?? response.status >= 500);
  return error;
}

export function reportDataState(data) {
  if (data?.source_status === 'partial') return 'partial';
  const hasRows = data?.tables?.some(tableHasRows);
  return data?.cards?.length || data?.charts?.length || hasRows ? 'ready' : 'empty';
}

// The filters a report is scoped by. The form, the request, the link and the parser all
// walk this list, so a filter added here cannot reach one of them and quietly miss the
// others — the form side ties each key to its input in one map (FILTER_INPUTS).
export const DEFAULT_GRANULARITY = 'day';

export const REPORT_FILTER_KEYS = [
  'start_date',
  'end_date',
  'company_id',
  'staff_id',
  'granularity',
];

/**
 * Filter values a report URL asks for.
 *
 * Absent means empty, never "keep what the form happens to hold": going back to an
 * entry that carried no staff_id must drop the staff filter of the report left behind,
 * not inherit it and then write it into that entry.
 */
export function reportFiltersFromParams(params) {
  const value = (key) => params.get(key) || '';
  const filters = {};
  REPORT_FILTER_KEYS.forEach((key) => { filters[key] = value(key); });
  filters.granularity = filters.granularity || DEFAULT_GRANULARITY;
  return {
    ...filters,
    ...comparePeriod(value('compare_start_date'), value('compare_end_date')),
    period_preset: value('period_preset'),
  };
}

/**
 * Comparison a set of filters actually asks for, or null.
 *
 * Half a window is not a window, and an inverted one is a range the API rejects — both
 * are windows still being typed. The request and the link both take the answer from
 * here, so neither spends a round trip on a period mid-edit.
 */
export function reportCompareParams(filters = {}) {
  const start = filters.compare_start_date;
  const end = filters.compare_end_date;
  if (!filters.compare_enabled || !start || !end || start > end) return null;
  return { compare_start_date: start, compare_end_date: end };
}

function comparePeriod(start, end) {
  const window = reportCompareParams({
    compare_start_date: start,
    compare_end_date: end,
    compare_enabled: true,
  });
  return {
    compare_start_date: window ? window.compare_start_date : '',
    compare_end_date: window ? window.compare_end_date : '',
    compare_enabled: Boolean(window),
  };
}

/** Query string a report link carries; the inverse of reportFiltersFromParams. */
export function reportSearchParams(filters = {}) {
  const params = new URLSearchParams();
  REPORT_FILTER_KEYS.forEach((key) => {
    if (filters[key]) params.set(key, String(filters[key]));
  });
  const compare = reportCompareParams(filters);
  if (compare) {
    params.set('compare_start_date', compare.compare_start_date);
    params.set('compare_end_date', compare.compare_end_date);
  }
  // Not a filter — it names the Overview preset the period came from, and so the
  // baseline the deltas are measured against. Dropping it here would let a reload
  // silently re-measure the report against a different window than the card that
  // linked to it.
  if (filters.period_preset) params.set('period_preset', String(filters.period_preset));
  return params;
}

/**
 * What a report load should do to the history entry.
 *
 * A load that merely reproduces the current URL — opening a report from a popped
 * entry — leaves history and its position bookkeeping alone.
 */
export function reportHistoryAction({ push, historyUrl, currentUrl }) {
  if (push) return 'push';
  return historyUrl === currentUrl ? 'none' : 'replace';
}

/**
 * Filters a report is actually run and linked with.
 *
 * A report that hides the period ignores it, but the endpoint still wants a valid range.
 * Substituting one keeps a period left inverted on another report from blocking it — and
 * from freezing its link, which would drop the branch the user picked.
 */
export function reportRequestFilters({ filters, periodApplies = true, fallbackPeriod }) {
  if (periodApplies || !fallbackPeriod) return filters;
  return { ...filters, start_date: fallbackPeriod.start, end_date: fallbackPeriod.end };
}

/** A period caught mid-edit — the new start typed before the new end — the API rejects. */
export function reportPeriodIsValid(filters = {}) {
  const start = filters.start_date;
  const end = filters.end_date;
  return !start || !end || start <= end;
}

/**
 * Query string every report link is written from.
 *
 * An inverted period never reaches the address bar — from the report, from the catalog,
 * or from anywhere else — so the last valid link keeps standing while the user types.
 * A report that hides the period is the exception: its user cannot fix what they cannot
 * see, so its link keeps moving and carries the branch and staff they did pick.
 */
export function reportLinkSearch({ filters, currentSearch = '', periodApplies = true }) {
  return !periodApplies || reportPeriodIsValid(filters)
    ? reportSearchParams(filters).toString()
    : currentSearch;
}

export function reportFilterVisibility(filters = {}) {
  return {
    dateRange: filters.date_range !== false,
    granularity: filters.granularity !== false,
    compare: filters.compare !== false,
  };
}

export function shouldRenderReportDataLabel(raw) {
  return raw !== null && raw !== undefined && raw !== '' && Number.isFinite(Number(raw));
}

export function chartTooltipValue(parsed, indexAxis = 'x') {
  // Arc charts parse to a bare number.
  if (typeof parsed === 'number') return parsed;
  if (!parsed || typeof parsed !== 'object') return null;
  // On a category chart one axis carries the label index rather than the
  // measurement: vertical charts measure on y, horizontal bars on x. Reading the
  // wrong one reports the category position (2025 -> 8) as the value.
  if (indexAxis === 'y') return parsed.x;
  return parsed.y !== undefined ? parsed.y : parsed.x;
}

export function reportRefreshPresentation(previousData) {
  return previousData
    ? { state: 'refreshing', retainedData: previousData }
    : { state: 'loading', retainedData: null };
}

export function staffRefreshAllowsDataLoad(status, expectedBranch, currentBranch) {
  return status !== 'superseded' && String(expectedBranch) === String(currentBranch);
}

/**
 * Staff option to select once a branch's list has been rendered.
 *
 * Taken from the id the URL asked for rather than from the select, which reports '' for
 * an id whose option does not exist yet and would widen a per-employee link to the
 * whole branch.
 */
export function staffSelectionForOptions(requestedStaffId, optionIds) {
  if (!requestedStaffId) return '';
  return (optionIds || []).some((id) => String(id) === String(requestedStaffId))
    ? String(requestedStaffId)
    : '';
}

export function reportScopedFilterAllowsLoad(requestedId, optionsLoaded, optionIds) {
  if (!requestedId) return true;
  return Boolean(optionsLoaded)
    && (optionIds || []).some((optionId) => String(optionId) === String(requestedId));
}

export function reportDataCacheKey(params = {}) {
  return JSON.stringify(
    Object.entries(params)
      .filter(([, value]) => value !== undefined && value !== null && value !== '')
      .map(([key, value]) => [key, String(value)])
      .sort(([left], [right]) => left.localeCompare(right)),
  );
}

export function serviceManagementChanges(current = {}, saved = {}) {
  const savedRows = new Map(
    (saved.rows || []).map((row) => [`${row.company_id}:${row.service_id}`, row]),
  );
  const rowChanges = [];
  for (const row of current.rows || []) {
    const previous = savedRows.get(`${row.company_id}:${row.service_id}`);
    const change = { company_id: row.company_id, service_id: row.service_id };
    if (!previous || previous.is_extra !== row.is_extra) change.is_extra = row.is_extra;
    if (!previous || previous.kpi_group_id !== row.kpi_group_id) change.kpi_group_id = row.kpi_group_id;
    if (Object.keys(change).length > 2) rowChanges.push(change);
  }

  const savedGroups = new Map((saved.groups || []).map((group) => [group.id, group]));
  const groupChanges = [];
  const mutableGroupFields = ['title', 'code', 'description', 'sort_order', 'is_active'];
  for (const group of current.groups || []) {
    const previous = savedGroups.get(group.id);
    const change = { id: group.id };
    mutableGroupFields.forEach((field) => {
      if (!previous || previous[field] !== group[field]) change[field] = group[field];
    });
    if (Object.keys(change).length > 1) groupChanges.push(change);
  }
  return { row_changes: rowChanges, group_changes: groupChanges };
}

export function serviceManagementControls({ loading, hasData, hasSavedData, dirty }) {
  return {
    filtersDisabled: Boolean(loading || dirty),
    editorDisabled: Boolean(loading),
    refreshDisabled: Boolean(loading || dirty),
    saveDisabled: Boolean(loading || !hasData || !dirty),
    resetDisabled: Boolean(loading || !hasSavedData || !dirty),
    addGroupDisabled: Boolean(loading || !hasData || dirty),
  };
}

export function serviceManagementLoadAllowed({ loading, dirty }) {
  return !loading && !dirty;
}

export function serviceManagementNavigationAllowed({ mutationPending, activeView }) {
  return !(mutationPending && activeView === 'serviceManagement');
}

export function latestServiceManagementTimestamp(...values) {
  return values.reduce((latest, value) => {
    if (!value) return latest;
    if (!latest) return value;
    const latestTime = Date.parse(latest);
    const valueTime = Date.parse(value);
    if (Number.isNaN(valueTime)) return latest;
    return Number.isNaN(latestTime) || valueTime > latestTime ? value : latest;
  }, null);
}

export function filterServiceManagementResult(data = {}, {
  is_extra: isExtra,
  kpi_group_id: kpiGroupId,
} = {}) {
  const hasGroupFilter = kpiGroupId !== undefined && kpiGroupId !== null && kpiGroupId !== '';
  const rows = (data.rows || []).filter((row) => {
    if (typeof isExtra === 'boolean' && Boolean(row.is_extra) !== isExtra) return false;
    if (hasGroupFilter && String(row.kpi_group_id ?? '') !== String(kpiGroupId)) return false;
    return true;
  });
  return { ...data, rows, total: rows.length };
}

export function settleServiceManagementLoad(request, setLoading) {
  setLoading(false);
  if (request.isCurrent()) request.finish();
}

export async function runServiceManagementMutation(mutate, {
  setLoading,
  onSuccess,
  onError,
}) {
  setLoading(true);
  try {
    const result = await mutate();
    onSuccess(result);
    return { ok: true, result };
  } catch (error) {
    onError(error);
    return { ok: false, error };
  } finally {
    setLoading(false);
  }
}

export function mergeServiceManagementResult(data = {}, result = {}) {
  const rows = [...(data.rows || [])];
  const rowIndexes = new Map(rows.map((row, index) => [`${row.company_id}:${row.service_id}`, index]));
  for (const row of result.rows || []) {
    const key = `${row.company_id}:${row.service_id}`;
    const index = rowIndexes.get(key);
    if (index === undefined) {
      rowIndexes.set(key, rows.length);
      rows.push({ ...row });
    } else {
      rows[index] = { ...rows[index], ...row };
    }
  }

  const groups = [...(data.groups || [])];
  const groupIndexes = new Map(groups.map((group, index) => [group.id, index]));
  for (const group of result.groups || []) {
    const index = groupIndexes.get(group.id);
    if (index === undefined) {
      groupIndexes.set(group.id, groups.length);
      groups.push({ ...group });
    } else {
      groups[index] = { ...groups[index], ...group };
    }
  }
  return { ...data, rows, groups };
}

export function createLatestRequestScope() {
  let current = null;
  let sequence = 0;
  return {
    start() {
      current?.controller.abort();
      const request = { id: ++sequence, controller: new AbortController() };
      current = request;
      return {
        signal: request.controller.signal,
        isCurrent: () => current?.id === request.id,
        finish: () => {
          if (current?.id === request.id) current = null;
        },
      };
    },
    abort() {
      current?.controller.abort();
      current = null;
    },
  };
}

export function createTimedAbortContext(externalSignal, {
  slowState,
  onSlow,
  retry,
  timeoutMs,
  slowMs,
  timeoutError,
  supersededError,
  setTimer = globalThis.setTimeout,
  clearTimer = globalThis.clearTimeout,
}) {
  const controller = new AbortController();
  let timedOut = false;
  let superseded = Boolean(externalSignal?.aborted);
  const abortFromExternal = () => {
    superseded = true;
    controller.abort();
  };
  externalSignal?.addEventListener('abort', abortFromExternal, { once: true });
  if (superseded) controller.abort();
  const slowTimer = slowState ? setTimer(() => onSlow?.({ state: 'slow', retry }), slowMs) : null;
  const timeoutTimer = setTimer(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  return {
    signal: controller.signal,
    abortError() {
      return timedOut ? timeoutError() : (superseded ? supersededError() : null);
    },
    cleanup() {
      if (slowTimer !== null) clearTimer(slowTimer);
      clearTimer(timeoutTimer);
      externalSignal?.removeEventListener('abort', abortFromExternal);
    },
  };
}
