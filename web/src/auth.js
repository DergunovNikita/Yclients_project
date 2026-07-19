import { t } from './i18n.js';

const PORTAL_ACCOUNT_KEY = 'portal_account_id';
const USER_EMAIL_KEY = 'portal_user_email';
const CSRF_COOKIE_NAME = 'portal_csrf';
const CSRF_HEADER_NAME = 'X-CSRF-Token';
const apiBase = import.meta.env.VITE_API_BASE || '';
let refreshInFlight = null;
let reauthInFlight = null;

function currentReturnTo() {
  if (typeof window === 'undefined') return '/';
  return `${window.location.pathname}${window.location.search}${window.location.hash}` || '/';
}

function safeReturnTo(value) {
  const raw = String(value || '').trim();
  if (!raw || raw.startsWith('//') || /^[a-z][a-z0-9+.-]*:/i.test(raw)) {
    return '/';
  }
  return raw.startsWith('/') ? raw : '/';
}

export function loginPathWithReturnTo(loginPath = '/login.html', returnTo = currentReturnTo()) {
  const url = new URL(loginPath, window.location.origin);
  url.searchParams.set('return_to', safeReturnTo(returnTo));
  return `${url.pathname}${url.search}${url.hash}`;
}

export function redirectToLogin(loginPath = '/login.html') {
  window.location.href = loginPathWithReturnTo(loginPath);
}

function clearLegacyAccessToken() {
  for (let index = localStorage.length - 1; index >= 0; index -= 1) {
    const key = localStorage.key(index);
    const parts = key ? key.split('_') : [];
    if (parts.length === 3 && parts[0] === 'portal' && parts[1] === 'access' && parts[2] === 'token') {
      localStorage.removeItem(key);
    }
  }
}

function clearLocalAuthState({ clearPortalAccount = true } = {}) {
  clearLegacyAccessToken();
  if (clearPortalAccount) {
    localStorage.removeItem(PORTAL_ACCOUNT_KEY);
  }
}

clearLocalAuthState({ clearPortalAccount: false });

export function resolveApiPath(path) {
  if (!apiBase) {
    return path;
  }
  return `${apiBase.replace(/\/$/, '')}${path}`;
}

export function setToken() {
  clearLocalAuthState();
}

function rememberUser(payload) {
  const user = payload?.data?.user || (payload?.data?.email ? payload.data : null);
  if (user?.email) {
    localStorage.setItem(USER_EMAIL_KEY, String(user.email));
  }
}

function cachedUserEmail() {
  return localStorage.getItem(USER_EMAIL_KEY) || '';
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
  clearLegacyAccessToken();
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
  return Boolean(getCsrfToken());
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
  if (response.status === 401 && String(message).toLowerCase().includes('invalid email')) {
    message = t('authErrors.invalidCredentials');
  } else if (response.status === 403 && String(message).toLowerCase().includes('account disabled')) {
    message = t('authErrors.accountDisabled');
  } else if ([502, 503, 504].includes(response.status)) {
    message = t('authErrors.temporaryUnavailable');
  } else if (response.status === 404 && message === 'Not Found') {
    message = t('authErrors.serviceUnavailable');
  }
  return String(message);
}

export function isTransientAuthError(error) {
  return [502, 503, 504].includes(Number(error?.status));
}

export function isAuthFailure(error) {
  return [401, 403].includes(Number(error?.status));
}

export function wait(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
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
    throw new Error(t('authErrors.authenticationRequired'));
  }
  const payload = await parsePayload(response);
  rememberUser(payload);
  return payload;
}

function ensureReauthModal() {
  let modal = document.getElementById('reauth-modal');
  if (modal) return modal;

  const style = document.createElement('style');
  style.textContent = `
    .reauth-modal[hidden]{display:none!important}
    .reauth-modal{position:fixed;inset:0;z-index:10000;display:grid;place-items:center;background:rgba(15,23,42,.42);padding:20px}
    .reauth-modal__dialog{width:min(420px,100%);background:#fff;border:1px solid #e2e8f0;border-radius:8px;box-shadow:0 24px 80px rgba(15,23,42,.24);padding:22px}
    .reauth-modal__dialog h2{margin:0 0 8px;font-size:20px;letter-spacing:0;color:#0f172a}
    .reauth-modal__dialog p{margin:0 0 16px;color:#64748b;font-size:14px;line-height:1.45}
    .reauth-modal__dialog label{display:grid;gap:6px;margin-bottom:12px;color:#334155;font-size:13px;font-weight:600}
    .reauth-modal__dialog input{min-height:38px;border:1px solid #cbd5e1;border-radius:6px;padding:8px 10px;font:inherit}
    .reauth-modal__password{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;border:1px solid #cbd5e1;border-radius:6px;background:#fff}
    .reauth-modal__password input{border:0;border-radius:6px;min-width:0}
    .reauth-modal__password button{height:38px;border:0;background:transparent;color:#0f766e;padding:0 10px;font:inherit;font-weight:700;cursor:pointer}
    .reauth-modal__error{margin:0 0 12px;color:#b91c1c;font-size:13px}
    .reauth-modal__actions{display:flex;justify-content:flex-end;gap:10px;margin-top:16px}
    .reauth-modal__actions button{min-width:92px;min-height:38px;border-radius:6px;border:1px solid #cbd5e1;background:#fff;color:#334155;padding:0 14px;font:inherit;font-weight:600;cursor:pointer}
    .reauth-modal__actions button[type=submit]{border-color:#0f766e;background:#0f766e;color:#fff}
  `;
  document.head.appendChild(style);

  modal = document.createElement('div');
  modal.id = 'reauth-modal';
  modal.className = 'reauth-modal';
  modal.hidden = true;
  modal.innerHTML = `
    <form class="reauth-modal__dialog" id="reauth-form">
      <h2>${t('reauth.title')}</h2>
      <p>${t('reauth.text')}</p>
      <div class="reauth-modal__error" id="reauth-error" hidden></div>
      <label>Email<input type="email" id="reauth-email" autocomplete="username" readonly /></label>
      <label>${t('common.password')}
        <span class="reauth-modal__password">
          <input type="password" id="reauth-password" autocomplete="current-password" required />
          <button type="button" id="reauth-toggle-password">${t('common.showPassword')}</button>
        </span>
      </label>
      <div class="reauth-modal__actions">
        <button type="button" id="reauth-cancel">${t('common.cancel')}</button>
        <button type="submit" id="reauth-submit">${t('login.submit')}</button>
      </div>
    </form>
  `;
  document.body.appendChild(modal);
  return modal;
}

async function promptReauth() {
  if (typeof document === 'undefined') {
    throw new Error(t('authErrors.authenticationRequired'));
  }
  if (reauthInFlight) return reauthInFlight;

  reauthInFlight = new Promise((resolve, reject) => {
    const modal = ensureReauthModal();
    const form = modal.querySelector('#reauth-form');
    const emailInput = modal.querySelector('#reauth-email');
    const passwordInput = modal.querySelector('#reauth-password');
    const errorEl = modal.querySelector('#reauth-error');
    const submitBtn = modal.querySelector('#reauth-submit');
    const cancelBtn = modal.querySelector('#reauth-cancel');
    const togglePasswordBtn = modal.querySelector('#reauth-toggle-password');
    const email = cachedUserEmail();

    emailInput.value = email;
    emailInput.readOnly = Boolean(email);
    passwordInput.value = '';
    passwordInput.type = 'password';
    togglePasswordBtn.textContent = t('common.showPassword');
    cancelBtn.textContent = t('common.cancel');
    submitBtn.textContent = t('login.submit');
    errorEl.hidden = true;
    modal.hidden = false;
    passwordInput.focus();

    const cleanup = () => {
      form.removeEventListener('submit', onSubmit);
      cancelBtn.removeEventListener('click', onCancel);
      togglePasswordBtn.removeEventListener('click', onTogglePassword);
      modal.hidden = true;
      submitBtn.disabled = false;
      reauthInFlight = null;
    };

    const onCancel = () => {
      cleanup();
      reject(new Error(t('authErrors.authenticationRequired')));
    };

    const onTogglePassword = () => {
      const visible = passwordInput.type === 'text';
      passwordInput.type = visible ? 'password' : 'text';
      togglePasswordBtn.textContent = visible ? t('common.showPassword') : t('common.hidePassword');
    };

    const onSubmit = async (event) => {
      event.preventDefault();
      errorEl.hidden = true;
      submitBtn.disabled = true;
      try {
        const response = await fetch(resolveApiPath('/auth/login'), {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            email: emailInput.value.trim(),
            password: passwordInput.value,
          }),
        });
        const payload = await parsePayload(response);
        if (!response.ok) {
          const nextError = new Error(errorMessage(response, payload));
          nextError.status = response.status;
          throw nextError;
        }
        rememberUser(payload);
        cleanup();
        resolve(payload);
      } catch (error) {
        errorEl.textContent = error.message;
        errorEl.hidden = false;
        submitBtn.disabled = false;
        passwordInput.focus();
      }
    };

    form.addEventListener('submit', onSubmit);
    cancelBtn.addEventListener('click', onCancel);
    togglePasswordBtn.addEventListener('click', onTogglePassword);
  });

  return reauthInFlight;
}

async function doFetch(path, options = {}) {
  const { __retried, __skipReauth, ...fetchOptions } = options;
  return fetch(resolveApiPath(path), {
    ...fetchOptions,
    credentials: 'include',
    headers: normalizeHeaders(path, options),
  });
}

export async function requestWithReauth(path, options = {}, requestFn = doFetch) {
  let response = await requestFn(path, options);
  if (response.status === 401 && !options.__retried && !path.startsWith('/auth/refresh') && !isPublicAuthPath(path)) {
    try {
      await refreshSession();
    } catch {
      if (options.__skipReauth) {
        return response;
      }
      await promptReauth();
    }
    response = await requestFn(path, { ...options, __retried: true });
  }
  return response;
}

export async function authFetch(path, options = {}) {
  const response = await requestWithReauth(path, options, doFetch);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(errorMessage(response, payload));
    error.status = response.status;
    throw error;
  }
  rememberUser(payload);
  return payload;
}

export function requireAuthRedirect(loginPath = '/login.html') {
  if (!hasSessionHint()) {
    redirectToLogin(loginPath);
    return false;
  }
  return true;
}

export async function logout(loginPath = '/login.html') {
  try {
    await fetch(resolveApiPath('/auth/logout'), {
      method: 'POST',
      credentials: 'include',
      headers: normalizeHeaders('/auth/logout', { method: 'POST' }),
    });
  } catch {
    /* local cleanup and redirect still happen */
  }
  clearLocalAuthState();
  window.location.href = loginPath;
}

export async function loadCurrentUser() {
  return authFetch('/auth/me');
}

export async function loadCurrentUserQuietly() {
  return authFetch('/auth/me', { __skipReauth: true });
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
