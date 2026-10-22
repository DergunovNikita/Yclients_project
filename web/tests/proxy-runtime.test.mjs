import assert from 'node:assert/strict';
import { readdir, readFile } from 'node:fs/promises';
import { Readable } from 'node:stream';
import test, { mock } from 'node:test';

// api/ and web/api/ are byte-identical (tests/test_proxy.py guards that), so one tree stands for both.
import handler, { buildTargetUrl } from '../api/auth-proxy.js';
import * as proxy from '../api/_proxy.js';

class ResponseRecorder {
  constructor() {
    this.headers = new Map();
    this.statusCode = 0;
    this.body = null;
  }

  setHeader(name, value) {
    this.headers.set(name.toLowerCase(), value);
  }

  end(body) {
    this.body = body;
  }
}

function requestStub(method = 'GET', headers = {}, url = '/') {
  const req = Readable.from([]);
  req.method = method;
  req.url = url;
  req.headers = { accept: 'application/json', host: 'example.test', ...headers };
  return req;
}

// Locally there is no Vercel layer, so the routing the deployment does is replayed here: the first
// vercel.json rewrite whose source matches wins, `:path*` is substituted into the destination, and
// Vercel merges the original query string into it.
const VERCEL_CONFIGS = ['../../vercel.json', '../vercel.json'];
const vercelConfigs = await Promise.all(
  VERCEL_CONFIGS.map(async (file) => JSON.parse(await readFile(new URL(file, import.meta.url), 'utf8'))),
);
const ROOT_REWRITES = vercelConfigs[0].rewrites;

function rewriteUrl(rewrites, url) {
  const queryStart = url.indexOf('?');
  const pathname = queryStart === -1 ? url : url.slice(0, queryStart);
  const query = queryStart === -1 ? '' : url.slice(queryStart + 1);
  for (const rule of rewrites) {
    let captured = '';
    if (rule.source.endsWith('/:path*')) {
      const base = rule.source.slice(0, -'/:path*'.length);
      if (pathname === base) {
        captured = '';
      } else if (pathname.startsWith(`${base}/`)) {
        captured = pathname.slice(base.length + 1);
      } else {
        continue;
      }
    } else if (pathname !== rule.source) {
      continue;
    }
    const destination = rule.destination.replace(':path*', captured);
    if (!query) return destination;
    return `${destination}${destination.includes('?') ? '&' : '?'}${query}`;
  }
  return null;
}

function viaVercel(method, url, rewrites = ROOT_REWRITES) {
  // A file under api/ wins over every rewrite; auth-proxy.js is the only one left.
  const routed = url.startsWith('/api/auth-proxy') ? url : rewriteUrl(rewrites, url);
  if (routed === null) return { ok: false, statusCode: 404, error: 'Not found' };
  return buildTargetUrl(requestStub(method, {}, routed));
}

function upstreamHeaders(cookies) {
  return {
    entries() {
      return [
        ['content-type', 'application/json'],
        ['set-cookie', 'this=must-not-collapse'],
      ][Symbol.iterator]();
    },
    get(name) {
      return String(name).toLowerCase() === 'set-cookie' ? cookies.join(', ') : null;
    },
    getSetCookie() {
      return cookies;
    },
  };
}

function rawHeaders(cookies) {
  return {
    get() {
      return null;
    },
    raw() {
      return { 'set-cookie': cookies };
    },
  };
}

async function assertProxyForwardsSetCookies(proxyToVm) {
  const cookies = [
    'portal_access=a; Path=/; HttpOnly',
    'portal_refresh=b; Path=/auth; HttpOnly',
    'portal_csrf=c; Path=/',
  ];
  const originalFetch = globalThis.fetch;

  globalThis.fetch = async () => ({
    status: 200,
    headers: upstreamHeaders(cookies),
    async arrayBuffer() {
      return new TextEncoder().encode('{"success":true}').buffer;
    },
  });

  try {
    const res = new ResponseRecorder();
    await proxyToVm(requestStub(), res, new URL('https://vm.example.test/dashboard/auth/login'));

    assert.equal(res.statusCode, 200);
    assert.equal(res.headers.get('content-type'), 'application/json');
    assert.deepEqual(res.headers.get('set-cookie'), cookies);
    assert.equal(Buffer.from(res.body).toString('utf8'), '{"success":true}');
  } finally {
    globalThis.fetch = originalFetch;
  }
}

test('responseSetCookies preserves combined cookie headers with Expires commas', () => {
  const headers = {
    get(name) {
      if (String(name).toLowerCase() !== 'set-cookie') return null;
      return 'portal_access=a; Path=/; HttpOnly, portal_refresh=b; Expires=Wed, 21 Oct 2030 07:28:00 GMT; Path=/auth; HttpOnly, portal_csrf=c; Path=/';
    },
  };

  assert.deepEqual(proxy.responseSetCookies(headers), [
    'portal_access=a; Path=/; HttpOnly',
    'portal_refresh=b; Expires=Wed, 21 Oct 2030 07:28:00 GMT; Path=/auth; HttpOnly',
    'portal_csrf=c; Path=/',
  ]);
});

test('responseSetCookies splits Expires-final cookies before the next cookie', () => {
  const headers = {
    get(name) {
      if (String(name).toLowerCase() !== 'set-cookie') return null;
      return 'portal_refresh=b; Path=/auth; HttpOnly; Expires=Wed, 21 Oct 2030 07:28:00 GMT, portal_csrf=c; Path=/';
    },
  };

  assert.deepEqual(proxy.responseSetCookies(headers), [
    'portal_refresh=b; Path=/auth; HttpOnly; Expires=Wed, 21 Oct 2030 07:28:00 GMT',
    'portal_csrf=c; Path=/',
  ]);
});

test('responseSetCookies preserves quoted commas inside cookie values', () => {
  const headers = {
    get(name) {
      if (String(name).toLowerCase() !== 'set-cookie') return null;
      return 'portal_access="a,b"; Path=/; HttpOnly, portal_csrf=c; Path=/';
    },
  };

  assert.deepEqual(proxy.responseSetCookies(headers), [
    'portal_access="a,b"; Path=/; HttpOnly',
    'portal_csrf=c; Path=/',
  ]);
});

test('responseSetCookies preserves raw set-cookie arrays', () => {
  const cookies = [
    'portal_access=a; Path=/; HttpOnly',
    'portal_refresh=b; Path=/auth; HttpOnly',
    'portal_csrf=c; Path=/',
  ];

  assert.deepEqual(proxy.responseSetCookies(rawHeaders(cookies)), cookies);
});

test('proxyToVm forwards multiple Set-Cookie headers as an array', async () => {
  await assertProxyForwardsSetCookies(proxy.proxyToVm);
});

test('forwardedHeaders strips sensitive and hop-by-hop browser headers', () => {
  const headers = proxy.forwardedHeaders({
    headers: {
      Accept: 'application/json',
      Authorization: 'Bearer attacker',
      Connection: 'keep-alive',
      Cookie: 'portal_access=a',
      'Content-Type': 'application/json',
      Host: 'frontend.example.test',
      'X-Api-Key': 'api-key',
      'X-Csrf-Token': 'csrf-token',
      'X-Forwarded-For': '203.0.113.10',
      'X-Portal-Account-Id': 'portal-1',
      'X-Sync-Token': 'sync-token',
    },
  });

  assert.deepEqual(headers, {
    accept: 'application/json',
    cookie: 'portal_access=a',
    'content-type': 'application/json',
    'x-csrf-token': 'csrf-token',
    'x-portal-account-id': 'portal-1',
  });
});

test('forwardedHeaders rewrites trusted client IP headers from the proxy socket peer', () => {
  const req = {
    headers: {
      accept: 'application/json',
      'x-forwarded-for': '203.0.113.10',
      'x-real-ip': '203.0.113.11',
    },
    socket: { remoteAddress: '198.51.100.42' },
  };

  const headers = proxy.forwardedHeaders(req);
  assert.equal(headers.accept, 'application/json');
  assert.equal(headers['x-forwarded-for'], '198.51.100.42');
  assert.equal(headers['x-real-ip'], '198.51.100.42');
  assert.notEqual(headers['x-forwarded-for'], '203.0.113.10');
  assert.notEqual(headers['x-real-ip'], '203.0.113.11');
});

function withProxySecret(value, run) {
  const original = process.env.VM_PROXY_SECRET;
  if (value === undefined) delete process.env.VM_PROXY_SECRET;
  else process.env.VM_PROXY_SECRET = value;
  try {
    run();
  } finally {
    if (original === undefined) delete process.env.VM_PROXY_SECRET;
    else process.env.VM_PROXY_SECRET = original;
  }
}

test('forwardedHeaders without VM_PROXY_SECRET never sends a proxy secret, even a browser-supplied one', () => {
  withProxySecret(undefined, () => {
    const headers = proxy.forwardedHeaders({
      headers: {
        'x-proxy-secret': 'attacker-guess',
        'x-vercel-forwarded-for': '203.0.113.7',
        'x-forwarded-for': '203.0.113.10',
      },
      socket: { remoteAddress: '198.51.100.42' },
    });
    assert.equal(headers['x-proxy-secret'], undefined);
    assert.equal(headers['x-forwarded-for'], '198.51.100.42');
    assert.equal(headers['x-real-ip'], '198.51.100.42');
  });
});

test('forwardedHeaders with VM_PROXY_SECRET sends the secret and the visitor address', () => {
  withProxySecret('s'.repeat(40), () => {
    const socket = { remoteAddress: '198.51.100.42' };
    // Vercel's own header wins over everything the visitor could have written.
    let headers = proxy.forwardedHeaders({
      headers: {
        'x-proxy-secret': 'attacker-guess',
        'x-vercel-forwarded-for': '203.0.113.7',
        'x-real-ip': '203.0.113.8',
        'x-forwarded-for': '203.0.113.9, 10.0.0.1',
      },
      socket,
    });
    assert.equal(headers['x-proxy-secret'], 's'.repeat(40));
    assert.equal(headers['x-forwarded-for'], '203.0.113.7');
    assert.equal(headers['x-real-ip'], '203.0.113.7');

    headers = proxy.forwardedHeaders({ headers: { 'x-real-ip': '203.0.113.8', 'x-forwarded-for': '203.0.113.9' }, socket });
    assert.equal(headers['x-forwarded-for'], '203.0.113.8');

    headers = proxy.forwardedHeaders({ headers: { 'x-forwarded-for': '203.0.113.9, 10.0.0.1' }, socket });
    assert.equal(headers['x-forwarded-for'], '203.0.113.9');

    headers = proxy.forwardedHeaders({ headers: {}, socket });
    assert.equal(headers['x-forwarded-for'], '198.51.100.42');
    assert.equal(headers['x-proxy-secret'], 's'.repeat(40));
  });
});

test('proxyToVm forwards only allowlisted request headers', async () => {
  const originalFetch = globalThis.fetch;
  let forwarded;

  globalThis.fetch = async (_target, options) => {
    forwarded = options.headers;
    return {
      status: 200,
      headers: upstreamHeaders([]),
      async arrayBuffer() {
        return new TextEncoder().encode('{}').buffer;
      },
    };
  };

  try {
    const res = new ResponseRecorder();
    await proxy.proxyToVm(requestStub('POST', {
      authorization: 'Bearer attacker',
      cookie: 'portal_access=a',
      'content-type': 'application/json',
      'x-api-key': 'api-key',
      'user-agent': 'Mozilla/5.0 (Macintosh) Safari/605.1.15',
      'x-csrf-token': 'csrf-token',
      'x-forwarded-for': '203.0.113.10',
      'x-portal-account-id': 'portal-1',
      'x-sync-token': 'sync-token',
    }), res, new URL('https://vm.example.test/dashboard/auth/login'));

    assert.deepEqual(forwarded, {
      accept: 'application/json',
      cookie: 'portal_access=a',
      'content-type': 'application/json',
      'user-agent': 'Mozilla/5.0 (Macintosh) Safari/605.1.15',
      'x-csrf-token': 'csrf-token',
      'x-portal-account-id': 'portal-1',
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('proxyToVm answers a failing upstream body with the structured 503', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => ({
    status: 200,
    headers: upstreamHeaders([]),
    async arrayBuffer() {
      throw new TypeError('terminated');
    },
  });

  try {
    const res = new ResponseRecorder();
    await proxy.proxyToVm(requestStub(), res, new URL('https://vm.example.test/dashboard/auth/me'));

    assert.equal(res.statusCode, 503);
    assert.equal(JSON.parse(res.body).error, 'upstream_unavailable');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

// Without the abort timer the pending body never settles, so a regression would hang: fail fast instead.
test('proxyToVm gives up on an upstream body that never arrives, with the structured 503', { timeout: 5000 }, async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (_target, options) => ({
    status: 200,
    headers: upstreamHeaders([]),
    arrayBuffer: () => new Promise((_resolve, reject) => {
      options.signal.addEventListener('abort', () => {
        reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
      }, { once: true });
    }),
  });
  mock.timers.enable({ apis: ['setTimeout'] });

  try {
    const res = new ResponseRecorder();
    const pending = proxy.proxyToVm(requestStub(), res, new URL('https://vm.example.test/dashboard/auth/me'));
    await new Promise((resolve) => { setImmediate(resolve); });
    mock.timers.tick(proxy.PROXY_TIMEOUT_MS);
    await pending;

    assert.equal(res.statusCode, 503);
    assert.equal(JSON.parse(res.body).error, 'upstream_timeout');
  } finally {
    mock.timers.reset();
    globalThis.fetch = originalFetch;
  }
});

test('proxy rejections carry `detail`, the key the SPA reads', async () => {
  const res = new ResponseRecorder();
  proxy.rejectProxyRequest(res, { statusCode: 404, error: 'Not found' });

  assert.equal(res.statusCode, 404);
  assert.deepEqual(JSON.parse(res.body), { error: 'Not found', detail: 'Not found' });

  const blocked = new ResponseRecorder();
  proxy.rejectProxyRequest(blocked, { statusCode: 405, error: 'Method not allowed', allowedMethods: ['GET'] });
  assert.equal(blocked.headers.get('allow'), 'GET');
  assert.equal(JSON.parse(blocked.body).detail, 'Method not allowed');
});

test('proxyToVm does not forward the upstream content-encoding or content-length', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => ({
    status: 200,
    headers: new Headers({
      'content-type': 'application/json',
      'content-encoding': 'gzip',
      'content-length': '999',
    }),
    async arrayBuffer() {
      return new TextEncoder().encode('{"success":true}').buffer;
    },
  });

  try {
    const res = new ResponseRecorder();
    await proxy.proxyToVm(requestStub(), res, new URL('https://vm.example.test/dashboard/auth/me'));

    assert.equal(res.headers.get('content-type'), 'application/json');
    assert.equal(res.headers.has('content-encoding'), false);
    assert.equal(res.headers.has('content-length'), false);
    assert.equal(Buffer.from(res.body).toString('utf8'), '{"success":true}');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('handlers answer a build failure with a fixed 500 that does not echo the error', async () => {
  const originalOrigin = process.env.VM_API_ORIGIN;
  const originalConsoleError = console.error;
  delete process.env.VM_API_ORIGIN;
  console.error = () => {};

  try {
    const res = new ResponseRecorder();
    await handler(requestStub('GET', {}, '/api/auth-proxy?scope=health&path=health'), res);

    assert.equal(res.statusCode, 500);
    assert.equal(res.body, '{"error":"Internal error","detail":"Internal error"}');
  } finally {
    console.error = originalConsoleError;
    if (originalOrigin !== undefined) {
      process.env.VM_API_ORIGIN = originalOrigin;
    }
  }
});

test('auth proxy allows only expected paths and methods', () => {
  const originalOrigin = process.env.VM_API_ORIGIN;
  process.env.VM_API_ORIGIN = 'https://vm.example.test';

  try {
    const login = buildTargetUrl(requestStub('POST', {}, '/api/auth-proxy?path=login'));
    assert.equal(login.ok, true);
    assert.equal(login.target.href, 'https://vm.example.test/dashboard/auth/login');

    const methodBlocked = buildTargetUrl(requestStub('GET', {}, '/api/auth-proxy?path=login'));
    assert.equal(methodBlocked.ok, false);
    assert.equal(methodBlocked.statusCode, 405);
    assert.deepEqual(methodBlocked.allowedMethods, ['POST']);

    const traversalBlocked = buildTargetUrl(requestStub('POST', {}, '/api/auth-proxy?path=admin/%2e%2e/users'));
    assert.equal(traversalBlocked.ok, false);
    assert.equal(traversalBlocked.statusCode, 404);

    const unknownBlocked = buildTargetUrl(requestStub('POST', {}, '/api/auth-proxy?path=admin/shell'));
    assert.equal(unknownBlocked.ok, false);
    assert.equal(unknownBlocked.statusCode, 404);

    // The SPA calls /auth/portal-accounts; the admin/ alias is not proxied.
    assert.equal(buildTargetUrl(requestStub('GET', {}, '/api/auth-proxy?path=portal-accounts')).ok, true);
    const aliasBlocked = buildTargetUrl(requestStub('GET', {}, '/api/auth-proxy?path=admin/portal-accounts'));
    assert.equal(aliasBlocked.ok, false);
    assert.equal(aliasBlocked.statusCode, 404);

    // Synced staff rows are read-only in the portal: only account creation is proxied.
    assert.equal(buildTargetUrl(requestStub('POST', {}, '/api/auth-proxy?path=admin/staff/7/create-account')).ok, true);
    for (const method of ['PATCH', 'DELETE']) {
      const staffBlocked = buildTargetUrl(requestStub(method, {}, '/api/auth-proxy?path=admin/staff/7'));
      assert.equal(staffBlocked.ok, false, method);
      assert.equal(staffBlocked.statusCode, 404, method);
    }
  } finally {
    if (originalOrigin === undefined) {
      delete process.env.VM_API_ORIGIN;
    } else {
      process.env.VM_API_ORIGIN = originalOrigin;
    }
  }
});

test('the proxy enforces dashboard and auth route allowlists behind the rewrites', () => {
  const originalOrigin = process.env.VM_API_ORIGIN;
  process.env.VM_API_ORIGIN = 'https://vm.example.test';

  try {
    const dashboard = viaVercel('GET', '/api/dashboard/branches?company_id=1');
    assert.equal(dashboard.ok, true);
    assert.equal(dashboard.target.href, 'https://vm.example.test/dashboard/branches?company_id=1');

    const bare = viaVercel('GET', '/dashboard/branches?company_id=1');
    assert.equal(bare.target.href, dashboard.target.href, 'the bare /dashboard path is routed like /api/dashboard');

    const metricVisibility = viaVercel('PUT', '/api/dashboard/metric-visibility');
    assert.equal(metricVisibility.ok, true);
    assert.equal(metricVisibility.target.href, 'https://vm.example.test/dashboard/metric-visibility');

    // Every dashboard route the SPA calls has to be listed here, in both copies of the
    // proxy — an endpoint missing from the allowlist answers 404 only in the deployed app.
    for (const [method, path] of [
      ['GET', '/api/dashboard/plan/reviews_fact?month=2025-01'],
      ['POST', '/api/dashboard/plan/reviews_fact'],
      ['GET', '/api/dashboard/plan/opz_fact?month=2025-01'],
      ['POST', '/api/dashboard/plan/opz_fact'],
      ['GET', '/api/dashboard/payments/yandex_pay?month=2025-01'],
      ['POST', '/api/dashboard/payments/yandex_pay'],
      ['GET', '/api/dashboard/branch-order'],
      ['PUT', '/api/dashboard/branch-order'],
    ]) {
      assert.equal(viaVercel(method, path).ok, true, `${method} ${path}`);
    }

    const serviceBatch = viaVercel('PATCH', '/api/dashboard/services');
    assert.equal(serviceBatch.ok, true);
    assert.equal(serviceBatch.target.href, 'https://vm.example.test/dashboard/services');

    const auth = viaVercel('POST', '/api/auth/admin/users');
    assert.equal(auth.ok, true);
    assert.equal(auth.target.href, 'https://vm.example.test/dashboard/auth/admin/users');

    const health = viaVercel('HEAD', '/api/health');
    assert.equal(health.ok, true);
    assert.equal(health.target.href, 'https://vm.example.test/health');
    assert.equal(viaVercel('POST', '/api/health').statusCode, 405);
    // Health is answered only for its own path: it is not a scope that reaches a VM prefix.
    assert.equal(viaVercel('GET', '/api/auth-proxy?scope=health&path=health/x').statusCode, 404);

    const onboarding = viaVercel('POST', '/api/onboarding/credentials');
    assert.equal(onboarding.ok, true);
    assert.equal(onboarding.target.href, 'https://vm.example.test/dashboard/onboarding/credentials');

    const methodBlocked = viaVercel('DELETE', '/api/dashboard/branches');
    assert.equal(methodBlocked.ok, false);
    assert.equal(methodBlocked.statusCode, 405);
    assert.deepEqual(methodBlocked.allowedMethods, ['GET', 'HEAD']);

    // The mutating routes accept exactly the methods the SPA uses; nothing destructive rides along.
    for (const path of ['plan/settings', 'plan/reviews_fact', 'plan/opz_fact', 'payments/yandex_pay', 'metric-visibility', 'branch-order']) {
      for (const method of ['DELETE', 'PATCH']) {
        assert.equal(viaVercel(method, `/api/dashboard/${path}`).ok, false, `${method} ${path}`);
      }
    }

    const unknownBlocked = viaVercel('GET', '/api/sync/jobs');
    assert.equal(unknownBlocked.ok, false);
    assert.equal(unknownBlocked.statusCode, 404);
  } finally {
    if (originalOrigin === undefined) {
      delete process.env.VM_API_ORIGIN;
    } else {
      process.env.VM_API_ORIGIN = originalOrigin;
    }
  }
});

test('encoded traversal and separators never reach anything but an allowlisted route', () => {
  const originalOrigin = process.env.VM_API_ORIGIN;
  process.env.VM_API_ORIGIN = 'https://vm.example.test';

  try {
    const blockedUrls = [
      '/api/dashboard/%2e%2e/auth/login',
      '/api/dashboard/%252e%252e/auth/login',
      '/api/auth/admin/%2e%2e/users',
      '/api/dashboard/payments%5cyandex_pay',
      '/api/dashboard/payments//yandex_pay',
      '/api/dashboard/plan/../auth/login',
      '/dashboard/%2e%2e/%2e%2e/health',
    ];

    for (const url of blockedUrls) {
      const result = viaVercel('GET', url);
      assert.equal(result.ok, false, `${url} should be blocked`);
      assert.equal(result.statusCode, 404, `${url} should return 404`);
    }

    // The path travels in a query value, so an encoded separator decodes there into the plain path:
    // that is the allowlisted route itself, never another one.
    const encodedSlash = viaVercel('GET', '/api/dashboard/payments%2fyandex_pay');
    if (encodedSlash.ok) {
      assert.equal(encodedSlash.target.href, 'https://vm.example.test/dashboard/payments/yandex_pay');
    }
  } finally {
    if (originalOrigin === undefined) {
      delete process.env.VM_API_ORIGIN;
    } else {
      process.env.VM_API_ORIGIN = originalOrigin;
    }
  }
});

test('every admin credential path goes through the generic auth rewrite and the allowlist', () => {
  const originalOrigin = process.env.VM_API_ORIGIN;
  process.env.VM_API_ORIGIN = 'https://vm.example.test';
  try {
    const cases = [
      ['GET', 'admin/yclients-credentials'],
      ['POST', 'admin/yclients-credentials'],
      ['POST', 'admin/yclients-credentials/test'],
      ['PATCH', 'admin/yclients-credentials/12'],
      ['DELETE', 'admin/yclients-credentials/12'],
      ['POST', 'admin/yclients-credentials/12/test'],
    ];
    for (const [method, path] of cases) {
      const target = buildTargetUrl(requestStub(method, {}, `/api/auth-proxy?path=${path}`));
      assert.equal(target.ok, true, `${method} ${path}`);
      assert.equal(target.target.href, `https://vm.example.test/dashboard/auth/${path}`);
    }
    const wrongMethod = buildTargetUrl(requestStub('POST', {}, '/api/auth-proxy?path=admin/yclients-credentials/12'));
    assert.equal(wrongMethod.statusCode, 405);
  } finally {
    if (originalOrigin === undefined) {
      delete process.env.VM_API_ORIGIN;
    } else {
      process.env.VM_API_ORIGIN = originalOrigin;
    }
  }
});

// The deployment has exactly one function, api/auth-proxy.js, and every proxied path reaches it
// through a vercel.json rewrite that carries the scope and the path in the query string. A catch-all
// file would answer one path segment only, so a nested route (`plan/…`, `payments/…`) would need a
// function of its own — and the Hobby plan caps a deployment at 12 functions (the 13th failed the
// whole Vercel build on 2026-10-09). Locally there is no such layer, so only this test notices a gap.
const VERCEL_FUNCTION_LIMIT = 12;

async function functionFiles(tree, dir = '') {
  const entries = await readdir(new URL(`${tree}/${dir}`, import.meta.url), { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const relative = `${dir}${entry.name}`;
    if (entry.isDirectory()) {
      files.push(...await functionFiles(tree, `${relative}/`));
    } else if (entry.name.endsWith('.js') && !entry.name.startsWith('_')) {
      files.push(relative);
    }
  }
  return files;
}

test('nested dashboard, onboarding and auth paths are routed to auth-proxy with the right scope and path', () => {
  const originalOrigin = process.env.VM_API_ORIGIN;
  process.env.VM_API_ORIGIN = 'https://vm.example.test';
  try {
    const query = 'company_id=7&start_date=2026-01-01';
    for (const config of vercelConfigs) {
      for (const [scope, method, path] of [
        ['dashboard', 'POST', 'plan/settings'],
        ['dashboard', 'GET', 'widget/plan_fact'],
        ['dashboard', 'GET', 'reports/data'],
        ['dashboard', 'POST', 'payments/yandex_pay'],
        ['dashboard', 'GET', 'bundle'],
        ['onboarding', 'GET', 'state'],
        ['onboarding', 'POST', 'credentials'],
      ]) {
        for (const prefix of [`/${scope}/`, `/api/${scope}/`]) {
          const routed = rewriteUrl(config.rewrites, `${prefix}${path}?${query}`);
          assert.equal(routed, `/api/auth-proxy?scope=${scope}&path=${path}&${query}`, `${prefix}${path}`);
          const result = viaVercel(method, `${prefix}${path}?${query}`, config.rewrites);
          assert.equal(result.ok, true, `${method} ${prefix}${path}`);
          assert.equal(
            result.target.href,
            `https://vm.example.test/dashboard/${scope === 'onboarding' ? 'onboarding/' : ''}${path}?${query}`,
            `${method} ${prefix}${path}: the client query must reach the VM unchanged`,
          );
        }
      }
      // Auth paths ride in a query parameter, so their depth never matters.
      for (const [method, path] of [
        ['POST', 'login'],
        ['GET', 'me'],
        ['DELETE', 'sessions/abc'],
        ['PATCH', 'admin/users/7'],
        ['POST', 'admin/staff/7/create-account'],
        ['POST', 'admin/yclients-credentials/12/test'],
      ]) {
        for (const prefix of ['/auth/', '/api/auth/']) {
          const result = viaVercel(method, `${prefix}${path}`, config.rewrites);
          assert.equal(result.ok, true, `${method} ${prefix}${path}`);
          assert.equal(result.target.href, `https://vm.example.test/dashboard/auth/${path}`);
        }
      }
      assert.equal(viaVercel('GET', '/api/health', config.rewrites).target.href, 'https://vm.example.test/health');
      // Bare scope roots name no route.
      for (const url of ['/dashboard', '/api/dashboard', '/api/dashboard/', '/onboarding', '/auth', '/api/auth']) {
        assert.equal(viaVercel('GET', url, config.rewrites).ok, false, url);
      }
    }
  } finally {
    if (originalOrigin === undefined) {
      delete process.env.VM_API_ORIGIN;
    } else {
      process.env.VM_API_ORIGIN = originalOrigin;
    }
  }
});

test('a client-supplied query passes through a dashboard rewrite untouched', () => {
  const originalOrigin = process.env.VM_API_ORIGIN;
  process.env.VM_API_ORIGIN = 'https://vm.example.test';
  try {
    const roundTrip = (url) => new URL(viaVercel('GET', url).target.href).searchParams;
    const params = roundTrip(
      '/api/dashboard/widget/plan_fact?company_id=1&company_id=2&staff_position_ids=1,2,3&q=%D0%91+%20a&flag=&x=a%26b',
    );
    assert.deepEqual([...params], [
      ['company_id', '1'],
      ['company_id', '2'],
      ['staff_position_ids', '1,2,3'],
      ['q', 'Б  a'],
      ['flag', ''],
      ['x', 'a&b'],
    ]);
    // Conflicting copies of a routing parameter are refused instead of guessed at; no dashboard route
    // takes a query parameter of either name (see dashboard_routes.py).
    for (const url of ['/api/dashboard/bundle?path=auth/login', '/api/dashboard/bundle?scope=onboarding']) {
      const refused = viaVercel('GET', url);
      assert.equal(refused.ok, false, url);
      assert.equal(refused.statusCode, 400, url);
    }
  } finally {
    if (originalOrigin === undefined) {
      delete process.env.VM_API_ORIGIN;
    } else {
      process.env.VM_API_ORIGIN = originalOrigin;
    }
  }
});

test('the deployment stays within the Vercel function limit', async () => {
  for (const tree of ['../../api', '../api']) {
    const files = await functionFiles(tree);
    assert.ok(
      files.length <= VERCEL_FUNCTION_LIMIT,
      `${tree} has ${files.length} functions (limit ${VERCEL_FUNCTION_LIMIT}): ${files.join(', ')}`,
    );
    // A function file beats every rewrite, so a second one would answer a path the allowlist never sees.
    assert.deepEqual(files, ['auth-proxy.js'], `${tree} must contain no function besides auth-proxy.js`);
  }
});

test('auth proxy forwards rewritten dashboard and onboarding scopes', () => {
  const originalOrigin = process.env.VM_API_ORIGIN;
  process.env.VM_API_ORIGIN = 'https://vm.example.test';

  try {
    const editor = buildTargetUrl(
      requestStub('GET', {}, '/api/auth-proxy?scope=dashboard&path=payments/yandex_pay&month=2026-10'),
    );
    assert.equal(editor.ok, true);
    assert.equal(editor.target.href, 'https://vm.example.test/dashboard/payments/yandex_pay?month=2026-10');

    const save = buildTargetUrl(requestStub('POST', {}, '/api/auth-proxy?scope=dashboard&path=payments/yandex_pay'));
    assert.equal(save.ok, true);

    const onboarding = buildTargetUrl(requestStub('GET', {}, '/api/auth-proxy?scope=onboarding&path=state'));
    assert.equal(onboarding.ok, true);
    assert.equal(onboarding.target.href, 'https://vm.example.test/dashboard/onboarding/state');

    const login = buildTargetUrl(requestStub('POST', {}, '/api/auth-proxy?path=login'));
    assert.equal(login.target.href, 'https://vm.example.test/dashboard/auth/login', 'auth stays the default scope');

    const keepsClientQuery = buildTargetUrl(
      requestStub('GET', {}, '/api/auth-proxy?scope=dashboard&company_id=7&path=payments/yandex_pay&month=2026-10'),
    );
    assert.equal(
      keepsClientQuery.target.href,
      'https://vm.example.test/dashboard/payments/yandex_pay?company_id=7&month=2026-10',
      'only the routing parameters are dropped from the forwarded query',
    );

    // Vercel merges the original query into a rewrite's destination: a client-supplied copy of a
    // routing parameter shows up next to the rewrite's own, and such a request is refused outright.
    const ambiguous = [
      '/api/auth-proxy?scope=dashboard&path=payments/yandex_pay&scope=onboarding',
      '/api/auth-proxy?scope=dashboard&path=payments/yandex_pay&path=plan/settings',
      '/api/auth-proxy?path=me&path=sessions',
    ];
    for (const url of ambiguous) {
      const refused = buildTargetUrl(requestStub('GET', {}, url));
      assert.equal(refused.ok, false, url);
      assert.equal(refused.statusCode, 400, url);
    }

    // A repeated identical copy is not ambiguous: refusing it would break every auth call if Vercel
    // ever echoed the parameter it substituted.
    const echoedAuth = buildTargetUrl(requestStub('GET', {}, '/api/auth-proxy?path=me&path=me'));
    assert.equal(echoedAuth.ok, true);
    assert.equal(echoedAuth.target.href, 'https://vm.example.test/dashboard/auth/me');
    const echoedScope = buildTargetUrl(
      requestStub('GET', {}, '/api/auth-proxy?scope=onboarding&path=state&scope=onboarding&path=state'),
    );
    assert.equal(echoedScope.ok, true);
    assert.equal(echoedScope.target.href, 'https://vm.example.test/dashboard/onboarding/state');

    for (const url of [
      // A rewritten /api/auth/login with a client-added scope never reaches another scope's routes.
      '/api/auth-proxy?path=login&scope=dashboard',
      '/api/auth-proxy?scope=constructor&path=login',
      '/api/auth-proxy?scope=toString&path=login',
      '/api/auth-proxy?scope=dashboard&path=payments//yandex_pay',
      '/api/auth-proxy?scope=dashboard&path=payments/yandex_pay%3Fx=1',
      '/api/auth-proxy?scope=dashboard&path=payments%2F..%2Fplan/settings',
      '/api/auth-proxy?scope=dashboard&path=payments/%252e%252e/plan/settings',
      '/api/auth-proxy?scope=onboarding&path=payments/yandex_pay',
      '/api/auth-proxy?scope=dashboard&path=',
      '/api/auth-proxy?scope=health&path=x',
      '/api/auth-proxy?scope=__proto__&path=login',
      '/api/auth-proxy?scope=dashboard&path=admin/users',
      '/api/auth-proxy?scope=dashboard&path=payments/%2e%2e/plan/settings',
    ]) {
      const blocked = buildTargetUrl(requestStub('GET', {}, url));
      assert.equal(blocked.ok, false, url);
      assert.equal(blocked.statusCode, 404, url);
    }
  } finally {
    if (originalOrigin === undefined) {
      delete process.env.VM_API_ORIGIN;
    } else {
      process.env.VM_API_ORIGIN = originalOrigin;
    }
  }
});

test('both vercel.json files agree, and the auth proxy outlasts its own upstream timeout', () => {
  const [root, web] = vercelConfigs;
  assert.deepEqual(root.rewrites, web.rewrites, 'rewrites differ between vercel.json and web/vercel.json');
  // A rewrite into /api/ may only name the one function that exists: anything else is a 404 from Vercel.
  for (const { source, destination } of root.rewrites.filter((rule) => rule.destination.startsWith('/api/'))) {
    assert.ok(destination.startsWith('/api/auth-proxy?'), `${source} rewrites to ${destination}, which is no function`);
  }
  assert.deepEqual(root.functions, web.functions, 'functions differ between vercel.json and web/vercel.json');
  // The proxy aborts its own upstream call at PROXY_TIMEOUT_MS and must still have time to answer;
  // a shorter function limit turns every slow call into a bare Vercel timeout.
  for (const [fn, { maxDuration }] of Object.entries(root.functions)) {
    assert.ok(maxDuration * 1000 > proxy.PROXY_TIMEOUT_MS, `${fn} maxDuration must exceed the proxy timeout`);
  }
});
