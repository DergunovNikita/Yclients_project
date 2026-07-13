import assert from 'node:assert/strict';
import test from 'node:test';
import { createServer } from 'vite';
import { forbiddenBrowserAuthTokens, scanBrowserAuthTokens } from './browser-auth-scan.mjs';

class MemoryStorage {
  constructor() {
    this.store = new Map();
  }

  getItem(key) {
    return this.store.has(key) ? this.store.get(key) : null;
  }

  setItem(key, value) {
    this.store.set(key, String(value));
  }

  removeItem(key) {
    this.store.delete(key);
  }

  key(index) {
    return [...this.store.keys()][index] ?? null;
  }

  get length() {
    return this.store.size;
  }
}

async function loadAuthModule() {
  globalThis.localStorage = new MemoryStorage();
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: { language: 'en-US', languages: ['en-US'] },
  });
  globalThis.window = { location: { href: '', pathname: '/' } };
  globalThis.document = {
    cookie: 'portal_csrf=csrf-runtime-token',
    documentElement: { lang: 'en' },
  };

  const server = await createServer({
    appType: 'custom',
    logLevel: 'silent',
    root: new URL('..', import.meta.url).pathname,
    server: { middlewareMode: true },
  });
  const auth = await server.ssrLoadModule('/src/auth.js');
  return { auth, server };
}

test('browser source has no bearer-token construction patterns', async () => {
  const webRoot = new URL('..', import.meta.url);
  assert.deepEqual(await scanBrowserAuthTokens(webRoot), []);
});

test('browser auth token detector catches split-string construction', () => {
  assert.deepEqual(
    forbiddenBrowserAuthTokens("headers[['Author', 'ization'].join('')] = ['Bear', 'er'].join('')"),
    ['Authorization', 'Bearer'],
  );
  assert.deepEqual(
    forbiddenBrowserAuthTokens("localStorage.setItem(['portal', '_access', '_token'].join(''), value)"),
    ['access_token', 'portal_access_token'],
  );
  assert.deepEqual(
    forbiddenBrowserAuthTokens("localStorage.setItem(['portal', 'access', 'token'].join('_'), value)"),
    ['access_token', 'portal_access_token'],
  );
});

test('authHeaders never returns browser bearer credentials', async (t) => {
  const { auth, server } = await loadAuthModule();
  t.after(() => server.close());

  localStorage.setItem('portal_account_id', '42');
  localStorage.setItem('portal_access_token', 'legacy-token');
  const headers = auth.authHeaders({ Accept: 'application/json' });

  assert.equal(headers.Accept, 'application/json');
  assert.equal(headers['X-Portal-Account-Id'], '42');
  assert.equal(headers.Authorization, undefined);
  assert.equal(localStorage.getItem('portal_access_token'), null);
});

test('authFetch uses cookie credentials and CSRF for mutating browser requests', async (t) => {
  const { auth, server } = await loadAuthModule();
  t.after(() => server.close());

  let captured;
  globalThis.fetch = async (url, options) => {
    captured = { url, options };
    return new Response(JSON.stringify({ success: true, data: { user: { email: 'owner@example.com' } } }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  await auth.authFetch('/auth/change-password', {
    method: 'POST',
    body: JSON.stringify({ current_password: 'old', new_password: 'new' }),
  });

  assert.match(captured.url, /\/auth\/change-password$/);
  assert.equal(captured.options.credentials, 'include');
  assert.equal(captured.options.headers.Authorization, undefined);
  assert.equal(captured.options.headers['X-CSRF-Token'], 'csrf-runtime-token');
  assert.equal(captured.options.headers['Content-Type'], 'application/json');
});

test('logout posts cookie logout, clears local auth state, and redirects after request failure', async (t) => {
  const { auth, server } = await loadAuthModule();
  t.after(() => server.close());

  localStorage.setItem('portal_account_id', '42');
  localStorage.setItem('portal_access_token', 'legacy-token');
  let captured;
  globalThis.fetch = async (url, options) => {
    captured = { url, options };
    throw new Error('network down');
  };

  await auth.logout('/login.html?logged_out=1');

  assert.match(captured.url, /\/auth\/logout$/);
  assert.equal(captured.options.method, 'POST');
  assert.equal(captured.options.credentials, 'include');
  assert.equal(captured.options.headers.Authorization, undefined);
  assert.equal(captured.options.headers['X-CSRF-Token'], 'csrf-runtime-token');
  assert.equal(localStorage.getItem('portal_account_id'), null);
  assert.equal(localStorage.getItem('portal_access_token'), null);
  assert.equal(window.location.href, '/login.html?logged_out=1');
});

test('requestWithReauth refreshes once and retries the original request', async (t) => {
  const { auth, server } = await loadAuthModule();
  t.after(() => server.close());

  const refreshCalls = [];
  const requestCalls = [];
  globalThis.fetch = async (url, options) => {
    refreshCalls.push({ url, options });
    return new Response(JSON.stringify({ success: true, data: { user: { email: 'owner@example.com' } } }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };
  const requestFn = async (path, options = {}) => {
    requestCalls.push({ path, options });
    return new Response('{}', { status: requestCalls.length === 1 ? 401 : 200 });
  };

  const response = await auth.requestWithReauth('/dashboard/bundle', { method: 'POST', body: 'payload' }, requestFn);

  assert.equal(response.status, 200);
  assert.equal(refreshCalls.length, 1);
  assert.match(refreshCalls[0].url, /\/auth\/refresh$/);
  assert.equal(refreshCalls[0].options.credentials, 'include');
  assert.equal(refreshCalls[0].options.headers['X-CSRF-Token'], 'csrf-runtime-token');
  assert.deepEqual(requestCalls, [
    { path: '/dashboard/bundle', options: { method: 'POST', body: 'payload' } },
    { path: '/dashboard/bundle', options: { method: 'POST', body: 'payload', __retried: true } },
  ]);
});
