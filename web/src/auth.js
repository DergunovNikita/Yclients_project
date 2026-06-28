const TOKEN_KEY = 'portal_access_token';
const PORTAL_ACCOUNT_KEY = 'portal_account_id';
const CSRF_COOKIE_NAME = 'portal_csrf';
const CSRF_HEADER_NAME = 'X-CSRF-Token';
const apiBase = import.meta.env.VITE_API_BASE || '';
let refreshInFlight = null;

export function resolveApiPath(path) {
  if (!apiBase) {
    return path;
  }
  return `${apiBase.replace(/\/$/, '')}${path}`;
}

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || '';
}

export function setToken(token) {
  if (token) {
    localStorage.setItem(TOKEN_KEY, token);
  } else {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(PORTAL_ACCOUNT_KEY);
  }
}

export function getSelectedPortalAccountId() {
  return localStorage.getItem(PORTAL_ACCOUNT_KEY) || '';
}

export function setSelectedPortalAccountId(portalAccountId) {
  if (portalAccountId) {
    localStorage.setItem(PORTAL_ACCOUNT_KEY, String(portalAccountId));
  } else {
    localStorage.removeItem(PORTAL_ACCOUNT_KEY);
  }
}

export function authHeaders(extra = {}) {
  const headers = { ...extra };
  const token = getToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  const portalAccountId = getSelectedPortalAccountId();
  if (portalAccountId) {
    headers['X-Portal-Account-Id'] = portalAccountId;
  }
  return headers;
}

export function getCsrfToken() {
  if (typeof document === 'undefined') return '';
  const cookie = document.cookie
    .split(';')
    .map((item) => item.trim())
    .find((item) => item.startsWith(`${CSRF_COOKIE_NAME}=`));
  return cookie ? decodeURIComponent(cookie.slice(CSRF_COOKIE_NAME.length + 1)) : '';
}

export function hasSessionHint() {
  return Boolean(getToken() || getCsrfToken());
}

function isMutatingMethod(method) {
  return !['GET', 'HEAD', 'OPTIONS'].includes(String(method || 'GET').toUpperCase());
}

function isPublicAuthPath(path) {
  return ['/auth/login', '/auth/register', '/auth/forgot-password', '/auth/reset-password', '/auth/verify-email', '/auth/resend-verification']
    .some((prefix) => path.startsWith(prefix));
}

function normalizeHeaders(path, options = {}) {
  const headers = authHeaders(options.headers || {});
  if (!(options.body instanceof FormData) && !headers['Content-Type'] && !headers['content-type']) {
    headers['Content-Type'] = 'application/json';
  }
  if (isMutatingMethod(options.method)) {
    const csrf = getCsrfToken();
    if (csrf) headers[CSRF_HEADER_NAME] = csrf;
  }
  return headers;
}

async function parsePayload(response) {
  return response.json().catch(() => ({}));
}

function errorMessage(response, payload) {
  let message = payload.detail || payload.message || `HTTP ${response.status}`;
  if (Array.isArray(message)) {
    message = message.map((item) => item.msg || JSON.stringify(item)).join('; ');
  } else if (message && typeof message === 'object') {
    message = JSON.stringify(message);
  }
  if (response.status === 404 && message === 'Not Found') {
    message = 'Сервис недоступен. Перезапустите API (uvicorn) и обновите страницу.';
  }
  return String(message);
}

async function refreshSession() {
  if (!refreshInFlight) {
    refreshInFlight = fetch(resolveApiPath('/auth/refresh'), {
      method: 'POST',
      credentials: 'include',
      headers: normalizeHeaders('/auth/refresh', { method: 'POST' }),
    }).finally(() => {
      refreshInFlight = null;
    });
  }
  const response = await refreshInFlight;
  if (!response.ok) {
    setToken('');
    throw new Error('Authentication required');
  }
  const payload = await parsePayload(response);
  if (payload?.data?.access_token) {
    setToken(payload.data.access_token);
  }
  return payload;
}

async function doFetch(path, options = {}) {
  const { __retried, ...fetchOptions } = options;
  return fetch(resolveApiPath(path), {
    ...fetchOptions,
    credentials: 'include',
    headers: normalizeHeaders(path, options),
  });
}

export async function authFetch(path, options = {}) {
  let response = await doFetch(path, options);
  if (response.status === 401 && !options.__retried && !path.startsWith('/auth/refresh') && !isPublicAuthPath(path)) {
    await refreshSession();
    response = await doFetch(path, { ...options, __retried: true });
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(errorMessage(response, payload));
  }
  return payload;
}

export function requireAuthRedirect(loginPath = '/login.html') {
  if (!hasSessionHint()) {
    window.location.href = loginPath;
    return false;
  }
  return true;
}

export function logout(loginPath = '/login.html') {
  setToken('');
  window.location.href = loginPath;
}

export async function loadCurrentUser() {
  return authFetch('/auth/me');
}

export async function ensureOnboardingComplete(user, onboardingPath = '/onboarding.html') {
  if (!user || user.role !== 'owner') return true;
  try {
    const response = await authFetch('/onboarding/state');
    if (response?.data?.step && response.data.step !== 'done') {
      if (!window.location.pathname.endsWith('onboarding.html')) {
        window.location.href = onboardingPath;
        return false;
      }
    }
    return true;
  } catch (error) {
    console.warn('onboarding state check failed', error);
    return true;
  }
}
