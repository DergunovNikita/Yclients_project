import assert from 'node:assert/strict';
import test from 'node:test';
import { createServer } from 'vite';
import { buildCsv, csvCell } from '../src/adminSecurity.js';
import { escapeHtml } from '../src/html.js';
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
  globalThis.window = {
    location: {
      href: '',
      origin: 'https://app.example',
      pathname: '/',
      search: '',
      hash: '',
    },
  };
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

async function loadCustomSelectModule() {
  const server = await createServer({
    appType: 'custom',
    logLevel: 'silent',
    root: new URL('..', import.meta.url).pathname,
    server: { middlewareMode: true },
  });
  const customSelect = await server.ssrLoadModule('/src/customSelect.js');
  return { customSelect, server };
}

class FakeElement {
  constructor(ownerDocument) {
    this.ownerDocument = ownerDocument;
    this.children = [];
    this.attributes = new Map();
    this.className = '';
    this.textContent = '';
    this.innerHTML = '';
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }
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

test('admin escaping neutralizes user and branch HTML payloads', () => {
  const payload = '<img src=x onerror=alert(1)>';
  assert.equal(escapeHtml(payload), '&lt;img src=x onerror=alert(1)&gt;');
  assert.equal(escapeHtml('"quoted" & tagged'), '&quot;quoted&quot; &amp; tagged');
});

test('custom select renders option labels as text, not HTML', async (t) => {
  const { customSelect, server } = await loadCustomSelectModule();
  t.after(() => server.close());

  const ownerDocument = {
    createElement: () => new FakeElement(ownerDocument),
  };
  const item = new FakeElement(ownerDocument);
  const payload = '<img src=x onerror=alert(1)>';

  customSelect.appendOptionContent(item, payload, false);

  assert.equal(item.innerHTML, '');
  assert.equal(item.children.length, 1);
  assert.equal(item.children[0].className, 'custom-select__label');
  assert.equal(item.children[0].textContent, payload);
});

test('admin CSV export quotes cells and prefixes spreadsheet formulas', () => {
  assert.equal(csvCell('=cmd|calc'), `"'=cmd|calc"`);
  assert.equal(csvCell('+SUM(A1:A2)'), `"'+SUM(A1:A2)"`);
  assert.equal(csvCell('plain "quoted"'), '"plain ""quoted"""');

  const csv = buildCsv(
    [{ email: '=evil@example.com', name: '<Admin>' }],
    [
      { key: 'email', label: 'Email' },
      { key: 'name', label: 'Name' },
    ],
  );
  assert.equal(csv, '"Email","Name"\r\n"\'=evil@example.com","<Admin>"');
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

test('login redirect preserves only same-origin return_to paths', async (t) => {
  const { auth, server } = await loadAuthModule();
  t.after(() => server.close());

  window.location.pathname = '/';
  window.location.search = '?company_id=7';
  window.location.hash = '#plan-fact';

  assert.equal(auth.loginPathWithReturnTo(), '/login.html?return_to=%2F%3Fcompany_id%3D7%23plan-fact');
  assert.equal(auth.loginPathWithReturnTo('/login.html', '/reports?id=1'), '/login.html?return_to=%2Freports%3Fid%3D1');
  assert.equal(auth.loginPathWithReturnTo('/login.html', 'https://evil.example'), '/login.html?return_to=%2F');
  assert.equal(auth.loginPathWithReturnTo('/login.html', '//evil.example'), '/login.html?return_to=%2F');
});

test('startup auth failures are distinguished from transient errors', async (t) => {
  const { auth, server } = await loadAuthModule();
  t.after(() => server.close());

  assert.equal(auth.isAuthFailure({ status: 401 }), true);
  assert.equal(auth.isAuthFailure({ status: 403 }), true);
  assert.equal(auth.isAuthFailure({ status: 500 }), false);
  assert.equal(auth.isAuthFailure({ status: 503 }), false);
  assert.equal(auth.isAuthFailure(new TypeError('Failed to fetch')), false);
  assert.equal(auth.isAuthFailure(undefined), false);

  assert.equal(auth.isTransientAuthError({ status: 503 }), true);
  assert.equal(auth.isTransientAuthError({ status: 401 }), false);

  // requiresLogin folds the auth-status check with the session hint (csrf cookie).
  document.cookie = 'portal_csrf=csrf-runtime-token';
  assert.equal(auth.requiresLogin({ status: 401 }), true);
  assert.equal(auth.requiresLogin({ status: 503 }), false);
  assert.equal(auth.requiresLogin(new TypeError('Failed to fetch')), false);
  document.cookie = '';
  assert.equal(auth.requiresLogin({ status: 503 }), true);
});

test('startup retry budget retries transient errors but never login-required ones', async (t) => {
  const { auth, server } = await loadAuthModule();
  t.after(() => server.close());

  document.cookie = 'portal_csrf=csrf-runtime-token';
  // Transient error with a live session: retry while budget remains.
  assert.equal(auth.shouldRetryStartup({ status: 503 }, 1, 3), true);
  assert.equal(auth.shouldRetryStartup({ status: 503 }, 2, 3), true);
  // Budget exhausted: stop (caller surfaces a retryable error, stays on page).
  assert.equal(auth.shouldRetryStartup({ status: 503 }, 3, 3), false);
  // Login-required (401): never retried, even on the first attempt.
  assert.equal(auth.shouldRetryStartup({ status: 401 }, 1, 3), false);
  // No session hint: login-required, so never retried.
  document.cookie = '';
  assert.equal(auth.shouldRetryStartup({ status: 503 }, 1, 3), false);
});

test('acquireStartupSession retries transient failures within budget but not login-required ones', async (t) => {
  const { auth, server } = await loadAuthModule();
  t.after(() => server.close());
  document.cookie = 'portal_csrf=csrf-runtime-token';

  const httpError = (status) => Object.assign(new Error(`http ${status}`), { status });

  // Succeeds after two transient failures; backoff grows with the attempt.
  let okCalls = 0;
  const okWaits = [];
  const recovered = await auth.acquireStartupSession(async () => {
    okCalls += 1;
    if (okCalls < 3) throw httpError(503);
    return { data: { ok: true } };
  }, { maxAttempts: 3, backoffMs: 800, waitFn: (ms) => { okWaits.push(ms); return Promise.resolve(); } });
  assert.deepEqual(recovered, { data: { ok: true } });
  assert.equal(okCalls, 3);
  assert.deepEqual(okWaits, [800, 1600]);

  // Persistent transient failure: capped at maxAttempts, then rethrows.
  let downCalls = 0;
  const downWaits = [];
  await assert.rejects(
    auth.acquireStartupSession(async () => { downCalls += 1; throw httpError(503); },
      { maxAttempts: 3, backoffMs: 800, waitFn: (ms) => { downWaits.push(ms); return Promise.resolve(); } }),
    /http 503/,
  );
  assert.equal(downCalls, 3);
  assert.deepEqual(downWaits, [800, 1600]);

  // Login-required error: thrown immediately, no retry, no wait.
  let authCalls = 0;
  let authWaited = false;
  await assert.rejects(
    auth.acquireStartupSession(async () => { authCalls += 1; throw httpError(401); },
      { maxAttempts: 3, backoffMs: 800, waitFn: () => { authWaited = true; return Promise.resolve(); } }),
    /http 401/,
  );
  assert.equal(authCalls, 1);
  assert.equal(authWaited, false);
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

test('requestWithReauth can skip password prompt for quiet session checks', async (t) => {
  const { auth, server } = await loadAuthModule();
  t.after(() => server.close());

  let refreshCalls = 0;
  let requestCalls = 0;
  globalThis.fetch = async () => {
    refreshCalls += 1;
    return new Response(JSON.stringify({ detail: 'expired' }), {
      status: 401,
      headers: { 'Content-Type': 'application/json' },
    });
  };
  const requestFn = async () => {
    requestCalls += 1;
    return new Response('{}', { status: 401 });
  };

  const response = await auth.requestWithReauth('/auth/me', { __skipReauth: true }, requestFn);

  assert.equal(response.status, 401);
  assert.equal(refreshCalls, 1);
  assert.equal(requestCalls, 1);
});
