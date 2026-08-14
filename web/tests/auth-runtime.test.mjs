import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
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

class MemoryLockManager {
  constructor() {
    this.busy = false;
    this.queue = [];
  }

  request(_name, options, callback) {
    const requestOptions = typeof options === 'function' ? {} : options;
    const requestCallback = typeof options === 'function' ? options : callback;
    if (requestOptions.ifAvailable && this.busy) {
      return Promise.resolve(requestCallback(null));
    }
    return new Promise((resolve, reject) => {
      const run = async () => {
        this.busy = true;
        try {
          resolve(await requestCallback({ name: 'portal-auth-refresh' }));
        } catch (error) {
          reject(error);
        } finally {
          this.busy = false;
          this.queue.shift()?.();
        }
      };
      if (this.busy) this.queue.push(run);
      else run();
    });
  }
}

function fakeEventTarget(properties = {}) {
  const listeners = new Map();
  return {
    ...properties,
    addEventListener(type, listener) {
      const items = listeners.get(type) || [];
      items.push(listener);
      listeners.set(type, items);
    },
    removeEventListener(type, listener) {
      listeners.set(type, (listeners.get(type) || []).filter((item) => item !== listener));
    },
    dispatchEvent(event) {
      (listeners.get(event.type) || []).forEach((listener) => listener(event));
      return true;
    },
  };
}

async function loadAuthModule({
  localValues = {},
  pathname = '/',
  reuseGlobals = false,
  locks = undefined,
} = {}) {
  if (!reuseGlobals) {
    globalThis.localStorage = new MemoryStorage();
    globalThis.sessionStorage = new MemoryStorage();
    Object.entries(localValues).forEach(([key, value]) => localStorage.setItem(key, value));
    Object.defineProperty(globalThis, 'navigator', {
      configurable: true,
      value: { language: 'en-US', languages: ['en-US'], locks },
    });
    globalThis.window = fakeEventTarget({
      location: {
        href: '',
        origin: 'https://app.example',
        pathname,
        search: '',
        hash: '',
        reloadCalls: 0,
        reload() {
          this.reloadCalls += 1;
        },
      },
    });
    globalThis.document = fakeEventTarget({
      cookie: 'portal_csrf=csrf-runtime-token',
      documentElement: { lang: 'en' },
      visibilityState: 'visible',
      body: {
        cleared: false,
        replaceChildren() {
          this.cleared = true;
        },
        setAttribute() {},
      },
    });
  }

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

test('every protected page and report flow uses the shared session coordinator', async () => {
  const webRoot = new URL('..', import.meta.url);
  const protectedEntries = new Map([
    ['src/main.js', 'loadCurrentUserQuietly'],
    ['src/admin.js', "authFetch('/auth/me')"],
    ['src/profile.js', "authFetch('/auth/me')"],
    ['src/settings.js', 'loadCurrentUser'],
    ['src/onboarding.js', 'loadCurrentUser'],
  ]);
  for (const [path, sessionLoader] of protectedEntries) {
    const source = await readFile(new URL(path, webRoot), 'utf8');
    assert.match(source, /from ['"]\.\/auth\.js['"]/);
    assert.ok(source.includes(sessionLoader), `${path} must initialize the current session identity`);
  }

  const reportsSource = await readFile(new URL('src/reports/index.js', webRoot), 'utf8');
  const dashboardApiSource = await readFile(new URL('src/dashboardApi.js', webRoot), 'utf8');
  assert.match(reportsSource, /from ['"]\.\.\/dashboardApi\.js['"]/);
  assert.match(dashboardApiSource, /requestWithReauth/);
  assert.match(dashboardApiSource, /from ['"]\.\/auth\.js['"]/);
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

test('portal tenant selection is tab-scoped and only platform admins send it', async (t) => {
  const { auth, server } = await loadAuthModule({ localValues: { portal_account_id: 'legacy-tenant' } });
  t.after(() => server.close());

  globalThis.fetch = async () => new Response(JSON.stringify({
    success: true,
    data: { id: 7, email: 'platform@example.com', role: 'platform_admin', company_ids: [] },
  }), { status: 200, headers: { 'Content-Type': 'application/json' } });
  await auth.loadCurrentUser();
  auth.setSelectedPortalAccountId('42');
  localStorage.setItem('portal_access_token', 'legacy-token');
  const headers = auth.authHeaders({ Accept: 'application/json' });

  assert.equal(headers.Accept, 'application/json');
  assert.equal(headers['X-Portal-Account-Id'], '42');
  assert.equal(headers.Authorization, undefined);
  assert.equal(sessionStorage.getItem('portal_account_id'), '42');
  assert.equal(localStorage.getItem('portal_account_id'), null);
  assert.equal(localStorage.getItem('portal_access_token'), null);
});

test('non-platform sessions never send a stale platform tenant header', async (t) => {
  const { auth, server } = await loadAuthModule();
  t.after(() => server.close());

  sessionStorage.setItem('portal_account_id', '42');
  globalThis.fetch = async () => new Response(JSON.stringify({
    success: true,
    data: { id: 8, email: 'manager@example.com', role: 'manager', company_ids: [1] },
  }), { status: 200, headers: { 'Content-Type': 'application/json' } });
  await auth.loadCurrentUser();

  const headers = auth.authHeaders();

  assert.equal(headers['X-Portal-Account-Id'], undefined);
  assert.equal(sessionStorage.getItem('portal_account_id'), null);
});

test('only session endpoints can replace the cached reauthentication email', async (t) => {
  const { auth, server } = await loadAuthModule({ pathname: '/login.html' });
  t.after(() => server.close());

  globalThis.fetch = async (url) => {
    if (String(url).endsWith('/auth/login')) {
      return new Response(JSON.stringify({
        success: true,
        data: { user: { id: 1, email: 'owner@example.com', role: 'owner', company_ids: [1] } },
      }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }
    return new Response(JSON.stringify({
      success: true,
      data: { id: 55, email: 'created.viewer@example.com', role: 'viewer' },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } });
  };

  await auth.authFetch('/auth/login', { method: 'POST', body: '{}' });
  await auth.authFetch('/auth/admin/users', { method: 'POST', body: '{}' });

  assert.equal(localStorage.getItem('portal_user_email'), 'owner@example.com');
});

test('session identity changes clear sensitive UI before reloading the page', async (t) => {
  const { auth, server } = await loadAuthModule();
  t.after(() => server.close());

  let role = 'owner';
  globalThis.fetch = async () => new Response(JSON.stringify({
    success: true,
    data: { id: role === 'owner' ? 1 : 2, email: `${role}@example.com`, role, company_ids: [1] },
  }), { status: 200, headers: { 'Content-Type': 'application/json' } });
  await auth.loadCurrentUser();
  role = 'manager';

  await auth.revalidateSessionIdentity({ force: true });

  assert.equal(document.body.cleared, true);
  assert.equal(window.location.reloadCalls, 1);
});

test('a login event from another page clears this page before synchronizing cookies', async (t) => {
  const { auth, server } = await loadAuthModule();
  t.after(() => server.close());

  globalThis.fetch = async () => new Response(JSON.stringify({
    success: true,
    data: { id: 1, email: 'owner@example.com', role: 'owner', company_ids: [1] },
  }), { status: 200, headers: { 'Content-Type': 'application/json' } });
  await auth.loadCurrentUser();

  window.dispatchEvent({
    type: 'storage',
    key: 'portal_auth_event',
    newValue: JSON.stringify({ id: 'other-page', type: 'session-changed' }),
  });

  assert.equal(document.body.cleared, true);
  assert.equal(window.location.reloadCalls, 1);
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
});

test('return_to normalizer accepts only root-relative paths', async (t) => {
  const { auth, server } = await loadAuthModule();
  t.after(() => server.close());

  const cases = [
    ['', '/'],
    ['   ', '/'],
    ['reports?id=1', '/'],
    ['/reports?id=1#revenue', '/reports?id=1#revenue'],
    ['HtTpS://evil.example', '/'],
    ['javascript:alert(1)', '/'],
    ['//evil.example', '/'],
  ];
  for (const [returnTo, expected] of cases) {
    assert.equal(auth.safeReturnTo(returnTo), expected);
  }
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

test('two page runtimes coordinate refresh and rotate cookies only once', async (t) => {
  const locks = new MemoryLockManager();
  const firstModule = await loadAuthModule({ locks });
  const secondModule = await loadAuthModule({ reuseGlobals: true });
  t.after(() => Promise.all([firstModule.server.close(), secondModule.server.close()]));

  let refreshed = false;
  let refreshCalls = 0;
  globalThis.fetch = async (url) => {
    assert.match(String(url), /\/auth\/refresh$/);
    refreshCalls += 1;
    await new Promise((resolve) => setTimeout(resolve, 10));
    refreshed = true;
    return new Response(JSON.stringify({
      success: true,
      data: { user: { id: 1, email: 'owner@example.com', role: 'owner', company_ids: [1] } },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } });
  };

  const requestCalls = [0, 0];
  const requestFn = (index) => async () => {
    requestCalls[index] += 1;
    return new Response('{}', { status: refreshed ? 200 : 401 });
  };

  const [firstResponse, secondResponse] = await Promise.all([
    firstModule.auth.requestWithReauth('/dashboard/bundle', {}, requestFn(0)),
    secondModule.auth.requestWithReauth('/dashboard/bundle', {}, requestFn(1)),
  ]);

  assert.equal(firstResponse.status, 200);
  assert.equal(secondResponse.status, 200);
  assert.equal(refreshCalls, 1);
  assert.deepEqual(requestCalls, [2, 2]);
});

test('password reauthentication never holds the cross-page refresh lock', async (t) => {
  const locks = new MemoryLockManager();
  const firstModule = await loadAuthModule({ locks });
  const secondModule = await loadAuthModule({ reuseGlobals: true });
  t.after(() => Promise.all([firstModule.server.close(), secondModule.server.close()]));

  globalThis.fetch = async () => new Response(JSON.stringify({ detail: 'expired' }), {
    status: 401,
    headers: { 'Content-Type': 'application/json' },
  });

  const promptStarted = [];
  const promptResolvers = [];
  const reauthFn = (index) => () => new Promise((resolve) => {
    promptResolvers[index] = resolve;
    promptStarted[index]();
  });
  const firstPromptStarted = new Promise((resolve) => { promptStarted[0] = resolve; });
  const secondPromptStarted = new Promise((resolve) => { promptStarted[1] = resolve; });
  const authenticated = [false, false];
  const requestFn = (index) => async () => new Response('{}', { status: authenticated[index] ? 200 : 401 });

  const firstRequest = firstModule.auth.requestWithReauth(
    '/dashboard/bundle',
    {},
    requestFn(0),
    async () => {
      await reauthFn(0)();
      authenticated[0] = true;
    },
  );
  await firstPromptStarted;
  const secondRequest = secondModule.auth.requestWithReauth(
    '/dashboard/bundle',
    {},
    requestFn(1),
    async () => {
      await reauthFn(1)();
      authenticated[1] = true;
    },
  );

  await secondPromptStarted;
  promptResolvers[0]();
  promptResolvers[1]();
  const [firstResponse, secondResponse] = await Promise.all([firstRequest, secondRequest]);

  assert.equal(firstResponse.status, 200);
  assert.equal(secondResponse.status, 200);
});

test('parallel pages wait for a winning refresh response when Web Locks are unavailable', async (t) => {
  const firstModule = await loadAuthModule();
  const secondModule = await loadAuthModule({ reuseGlobals: true });
  t.after(() => Promise.all([firstModule.server.close(), secondModule.server.close()]));

  const setLocalItem = localStorage.setItem.bind(localStorage);
  localStorage.setItem = (key, value) => {
    setLocalItem(key, value);
    if (key === 'portal_auth_event') {
      queueMicrotask(() => window.dispatchEvent({ type: 'storage', key, newValue: value }));
    }
  };
  let refreshCalls = 0;
  let refreshed = false;
  globalThis.fetch = async () => {
    refreshCalls += 1;
    if (refreshCalls === 1) {
      await new Promise((resolve) => setTimeout(resolve, 30));
      refreshed = true;
      return new Response(JSON.stringify({
        success: true,
        data: { user: { id: 1, email: 'owner@example.com', role: 'owner', company_ids: [1] } },
      }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }
    await new Promise((resolve) => setTimeout(resolve, 10));
    return new Response(JSON.stringify({ detail: 'already rotated' }), {
      status: 401,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  let reauthCalls = 0;
  const requestCalls = [0, 0];
  const requestFn = (index) => async () => {
    requestCalls[index] += 1;
    return new Response('{}', { status: refreshed ? 200 : 401 });
  };
  const reauthFn = async () => { reauthCalls += 1; };

  const [firstResponse, secondResponse] = await Promise.all([
    firstModule.auth.requestWithReauth('/dashboard/bundle', {}, requestFn(0), reauthFn),
    secondModule.auth.requestWithReauth('/dashboard/bundle', {}, requestFn(1), reauthFn),
  ]);

  assert.equal(firstResponse.status, 200);
  assert.equal(secondResponse.status, 200);
  assert.equal(refreshCalls, 2);
  assert.equal(reauthCalls, 0);
  assert.deepEqual(requestCalls.toSorted(), [2, 3]);
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
  assert.equal(requestCalls, 3);
});
