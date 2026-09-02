import { authHeaders, getCsrfToken, requestWithReauth } from './auth.js';
import { t, userDataLoadErrorMessage } from './i18n.js';
import {
  createTimedAbortContext,
  responseError,
} from './dashboardRequestState.js';

export { createLatestRequestScope } from './dashboardRequestState.js';
export { reportDataState } from './dashboardRequestState.js';
export { reportDataCacheKey } from './dashboardRequestState.js';
export { reportRefreshPresentation } from './dashboardRequestState.js';
export { reportScopedFilterAllowsLoad } from './dashboardRequestState.js';
export { reportSearchParams } from './dashboardRequestState.js';
export { staffRefreshAllowsDataLoad } from './dashboardRequestState.js';
export { filterServiceManagementResult } from './dashboardRequestState.js';
export { latestServiceManagementTimestamp } from './dashboardRequestState.js';
export { mergeServiceManagementResult } from './dashboardRequestState.js';
export { serviceManagementChanges } from './dashboardRequestState.js';
export { serviceManagementControls } from './dashboardRequestState.js';
export { serviceManagementLoadAllowed } from './dashboardRequestState.js';
export { serviceManagementNavigationAllowed } from './dashboardRequestState.js';
export { runServiceManagementMutation } from './dashboardRequestState.js';
export { settleServiceManagementLoad } from './dashboardRequestState.js';

export const REQUEST_TIMEOUT_MS = 60000;
export const SLOW_REQUEST_MS = 12000;

const apiBase = import.meta.env?.VITE_API_BASE || '';
const apiKey = import.meta.env?.VITE_API_KEY || '';

function apiUrl(path, params = {}) {
  const query = new URLSearchParams();
  Object.entries(params || {}).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') query.set(key, value);
  });
  const normalizedPath = query.size ? `${path}?${query}` : path;
  return apiBase ? `${apiBase.replace(/\/$/, '')}${normalizedPath}` : normalizedPath;
}

function apiUrlCandidates(path, params = {}) {
  const primary = apiUrl(path, params);
  const candidates = [primary];
  if (apiBase.includes('127.0.0.1')) candidates.push(primary.replace('127.0.0.1', 'localhost'));
  if (apiBase.includes('localhost')) candidates.push(primary.replace('localhost', '127.0.0.1'));
  return [...new Set(candidates)];
}

function requestHeaders(method, extra = {}) {
  const headers = { ...extra };
  if (apiKey) headers['X-API-Key'] = apiKey;
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    headers['Content-Type'] = 'application/json';
    const csrf = getCsrfToken();
    if (csrf) headers['X-CSRF-Token'] = csrf;
  }
  return authHeaders(headers);
}

export function apiErrorForResponse(response, payload = {}) {
  return responseError(response, payload, userDataLoadErrorMessage());
}

function timeoutError() {
  const error = new Error(t('dash.apiTimeoutMessage'));
  error.name = 'TimeoutError';
  error.apiStatus = 'timeout';
  error.retryable = true;
  return error;
}

function supersededError() {
  const error = new Error('Request superseded');
  error.name = 'AbortError';
  error.apiStatus = 'superseded';
  return error;
}

function invalidJsonResponseError(response) {
  const error = new Error(userDataLoadErrorMessage());
  error.name = 'ResponseContractError';
  error.status = response.status;
  error.apiStatus = 'server_error';
  error.code = 'invalid_json_response';
  error.retryable = true;
  return error;
}

export function isSupersededRequest(error) {
  return error?.apiStatus === 'superseded';
}

function timedSignal(externalSignal, { slowState, onSlow, retry, timeoutMs, slowMs }) {
  return createTimedAbortContext(externalSignal, {
    slowState,
    onSlow,
    retry,
    timeoutMs,
    slowMs,
    timeoutError,
    supersededError,
  });
}

export async function requestJson(path, {
  method = 'GET',
  body = null,
  params = {},
  signal = null,
  slowState = true,
  onSlow = null,
  retry = null,
  timeoutMs = REQUEST_TIMEOUT_MS,
  slowMs = SLOW_REQUEST_MS,
  fetchImpl = globalThis.fetch,
  requestWithReauthImpl = requestWithReauth,
} = {}) {
  const normalizedMethod = String(method || 'GET').toUpperCase();
  const timing = timedSignal(signal, { slowState, onSlow, retry, timeoutMs, slowMs });
  const connectionErrors = [];
  try {
    for (const url of apiUrlCandidates(path, params)) {
      let response;
      try {
        response = await requestWithReauthImpl(
          url,
          { method: normalizedMethod, body, signal: timing.signal },
          (_path, options = {}) => {
            const { __retried, body: requestBody = null, ...fetchOptions } = options;
            return fetchImpl(url, {
              ...fetchOptions,
              credentials: 'include',
              headers: requestHeaders(normalizedMethod, fetchOptions.headers || {}),
              body: requestBody === null ? undefined : JSON.stringify(requestBody),
            });
          },
        );
      } catch (error) {
        const abortError = error?.name === 'AbortError' ? timing.abortError() : null;
        if (abortError) throw abortError;
        connectionErrors.push(`${url}\n${error?.message || error}`);
        continue;
      }

      let payload = {};
      let payloadParseFailed = false;
      try {
        payload = await response.json();
      } catch (error) {
        if (timing.signal.aborted) {
          const abortError = timing.abortError();
          if (abortError) throw abortError;
        }
        payloadParseFailed = true;
        payload = {};
      }
      if (!response.ok) {
        console.error('Dashboard API request failed', { status: response.status, url });
        throw apiErrorForResponse(response, payload);
      }
      if (
        payloadParseFailed
        || payload === null
        || typeof payload !== 'object'
        || Array.isArray(payload)
      ) {
        throw invalidJsonResponseError(response);
      }
      if (payload.success === false) {
        const error = new Error(userDataLoadErrorMessage());
        error.apiStatus = 'error';
        throw error;
      }
      return payload;
    }
  } finally {
    timing.cleanup();
  }

  console.error('Dashboard API connection failed', connectionErrors);
  const error = new Error(userDataLoadErrorMessage());
  error.apiStatus = 'error';
  error.retryable = true;
  throw error;
}

export function fetchJson(path, params = {}, options = {}) {
  return requestJson(path, { ...options, method: 'GET', params });
}

export function postJson(path, body, options = {}) {
  return requestJson(path, { ...options, method: 'POST', body });
}

export function patchJson(path, body, options = {}) {
  return requestJson(path, { ...options, method: 'PATCH', body });
}
