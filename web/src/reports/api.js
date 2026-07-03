import { authHeaders, requestWithReauth } from '../auth.js';
import { userDataLoadErrorMessage } from '../i18n.js';

const apiBase = import.meta.env.VITE_API_BASE || '';

function apiUrl(path, params = {}) {
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') {
      qs.set(key, value);
    }
  });
  const suffix = qs.toString() ? `?${qs}` : '';
  const normalizedPath = `${path}${suffix}`;
  if (!apiBase) return normalizedPath;
  return `${apiBase.replace(/\/$/, '')}${normalizedPath}`;
}

function apiUrlCandidates(path, params = {}) {
  const primary = apiUrl(path, params);
  const candidates = [primary];
  if (apiBase.includes('127.0.0.1')) {
    candidates.push(primary.replace('127.0.0.1', 'localhost'));
  } else if (apiBase.includes('localhost')) {
    candidates.push(primary.replace('localhost', '127.0.0.1'));
  }
  return [...new Set(candidates)];
}

export async function fetchJson(path, params = {}) {
  const errors = [];
  for (const url of apiUrlCandidates(path, params)) {
    let response;
    try {
      response = await requestWithReauth(url, {}, (_path, options = {}) => {
        const { __retried, ...fetchOptions } = options;
        return fetch(url, {
          ...fetchOptions,
          credentials: 'include',
          headers: authHeaders(fetchOptions.headers || {}),
        });
      });
    } catch (error) {
      errors.push(`${url}\n${error.message}`);
      continue;
    }

    if (!response.ok) {
      const body = await response.text();
      console.error('Reports API request failed', { status: response.status, url, body: body.slice(0, 1000) });
      throw new Error(userDataLoadErrorMessage());
    }

    const payload = await response.json();
    if (payload.success === false) {
      console.error('Reports API returned success=false', { url, payload });
      throw new Error(userDataLoadErrorMessage());
    }
    return payload;
  }

  console.error('Reports API connection failed', errors);
  throw new Error(userDataLoadErrorMessage());
}
