const TOKEN_KEY = 'portal_access_token';
const apiBase = import.meta.env.VITE_API_BASE || '';

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
  }
}

export function authHeaders(extra = {}) {
  const headers = { ...extra };
  const token = getToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  return headers;
}

export async function authFetch(path, options = {}) {
  const response = await fetch(resolveApiPath(path), {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...authHeaders(options.headers || {}),
    },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    let message = payload.detail || payload.message || `HTTP ${response.status}`;
    if (Array.isArray(message)) {
      message = message.map((item) => item.msg || JSON.stringify(item)).join('; ');
    } else if (message && typeof message === 'object') {
      message = JSON.stringify(message);
    }
    if (response.status === 404 && message === 'Not Found') {
      message = 'Сервис недоступен. Перезапустите API (uvicorn) и обновите страницу.';
    }
    throw new Error(String(message));
  }
  return payload;
}

export function requireAuthRedirect(loginPath = '/login.html') {
  if (!getToken()) {
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
