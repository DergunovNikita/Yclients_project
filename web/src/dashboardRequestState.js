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

export function reportRefreshPresentation(previousData) {
  return previousData
    ? { state: 'refreshing', retainedData: previousData }
    : { state: 'loading', retainedData: null };
}

export function staffRefreshAllowsDataLoad(status, expectedBranch, currentBranch) {
  return status !== 'superseded' && String(expectedBranch) === String(currentBranch);
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
