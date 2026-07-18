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
  const hasRows = data?.tables?.some((table) => (
    table.rows?.length
    || Object.values(table.ranking?.rows_by_metric || {}).some((rows) => rows?.length)
  ));
  return data?.cards?.length || data?.charts?.length || hasRows ? 'ready' : 'empty';
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
