import assert from 'node:assert/strict';
import { readdir, readFile } from 'node:fs/promises';
import { Readable } from 'node:stream';
import test from 'node:test';

import rootProxyHandler, { buildTargetUrl as buildRootProxyTarget } from '../../api/[...path].js';
import * as rootProxy from '../../api/_proxy.js';
import credentialByIdHandler from '../api/auth/admin/yclients-credentials/[credential_id].js';
import { buildTargetUrl as buildAuthProxyTarget } from '../api/auth-proxy.js';
import webProxyHandler, { buildTargetUrl as buildWebProxyTarget } from '../api/[...path].js';
import * as webProxy from '../api/_proxy.js';

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

  assert.deepEqual(webProxy.responseSetCookies(headers), [
    'portal_access=a; Path=/; HttpOnly',
    'portal_refresh=b; Expires=Wed, 21 Oct 2030 07:28:00 GMT; Path=/auth; HttpOnly',
    'portal_csrf=c; Path=/',
  ]);
  assert.deepEqual(rootProxy.responseSetCookies(headers), webProxy.responseSetCookies(headers));
});

test('responseSetCookies splits Expires-final cookies before the next cookie', () => {
  const headers = {
    get(name) {
      if (String(name).toLowerCase() !== 'set-cookie') return null;
      return 'portal_refresh=b; Path=/auth; HttpOnly; Expires=Wed, 21 Oct 2030 07:28:00 GMT, portal_csrf=c; Path=/';
    },
  };

  assert.deepEqual(webProxy.responseSetCookies(headers), [
    'portal_refresh=b; Path=/auth; HttpOnly; Expires=Wed, 21 Oct 2030 07:28:00 GMT',
    'portal_csrf=c; Path=/',
  ]);
  assert.deepEqual(rootProxy.responseSetCookies(headers), webProxy.responseSetCookies(headers));
});

test('responseSetCookies preserves quoted commas inside cookie values', () => {
  const headers = {
    get(name) {
      if (String(name).toLowerCase() !== 'set-cookie') return null;
      return 'portal_access="a,b"; Path=/; HttpOnly, portal_csrf=c; Path=/';
    },
  };

  assert.deepEqual(webProxy.responseSetCookies(headers), [
    'portal_access="a,b"; Path=/; HttpOnly',
    'portal_csrf=c; Path=/',
  ]);
  assert.deepEqual(rootProxy.responseSetCookies(headers), webProxy.responseSetCookies(headers));
});

test('responseSetCookies preserves raw set-cookie arrays', () => {
  const cookies = [
    'portal_access=a; Path=/; HttpOnly',
    'portal_refresh=b; Path=/auth; HttpOnly',
    'portal_csrf=c; Path=/',
  ];

  assert.deepEqual(webProxy.responseSetCookies(rawHeaders(cookies)), cookies);
  assert.deepEqual(rootProxy.responseSetCookies(rawHeaders(cookies)), cookies);
});

test('proxyToVm forwards multiple Set-Cookie headers as an array in both proxy copies', async () => {
  await assertProxyForwardsSetCookies(webProxy.proxyToVm);
  await assertProxyForwardsSetCookies(rootProxy.proxyToVm);
});

test('forwardedHeaders strips sensitive and hop-by-hop browser headers', () => {
  const headers = webProxy.forwardedHeaders({
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
  assert.deepEqual(rootProxy.forwardedHeaders({
    headers: {
      Accept: 'application/json',
      Authorization: 'Bearer attacker',
      Cookie: 'portal_access=a',
      'X-Csrf-Token': 'csrf-token',
    },
  }), {
    accept: 'application/json',
    cookie: 'portal_access=a',
    'x-csrf-token': 'csrf-token',
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

  for (const proxy of [webProxy, rootProxy]) {
    const headers = proxy.forwardedHeaders(req);
    assert.equal(headers.accept, 'application/json');
    assert.equal(headers['x-forwarded-for'], '198.51.100.42');
    assert.equal(headers['x-real-ip'], '198.51.100.42');
    assert.notEqual(headers['x-forwarded-for'], '203.0.113.10');
    assert.notEqual(headers['x-real-ip'], '203.0.113.11');
  }
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
    await webProxy.proxyToVm(requestStub('POST', {
      authorization: 'Bearer attacker',
      cookie: 'portal_access=a',
      'content-type': 'application/json',
      'x-api-key': 'api-key',
      'x-csrf-token': 'csrf-token',
      'x-forwarded-for': '203.0.113.10',
      'x-portal-account-id': 'portal-1',
      'x-sync-token': 'sync-token',
    }), res, new URL('https://vm.example.test/dashboard/auth/login'));

    assert.deepEqual(forwarded, {
      accept: 'application/json',
      cookie: 'portal_access=a',
      'content-type': 'application/json',
      'x-csrf-token': 'csrf-token',
      'x-portal-account-id': 'portal-1',
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('auth proxy allows only expected paths and methods', () => {
  const originalOrigin = process.env.VM_API_ORIGIN;
  process.env.VM_API_ORIGIN = 'https://vm.example.test';

  try {
    const login = buildAuthProxyTarget(requestStub('POST', {}, '/api/auth-proxy?path=login'));
    assert.equal(login.ok, true);
    assert.equal(login.target.href, 'https://vm.example.test/dashboard/auth/login');

    const methodBlocked = buildAuthProxyTarget(requestStub('GET', {}, '/api/auth-proxy?path=login'));
    assert.equal(methodBlocked.ok, false);
    assert.equal(methodBlocked.statusCode, 405);
    assert.deepEqual(methodBlocked.allowedMethods, ['POST']);

    const traversalBlocked = buildAuthProxyTarget(requestStub('POST', {}, '/api/auth-proxy?path=admin/%2e%2e/users'));
    assert.equal(traversalBlocked.ok, false);
    assert.equal(traversalBlocked.statusCode, 404);

    const unknownBlocked = buildAuthProxyTarget(requestStub('POST', {}, '/api/auth-proxy?path=admin/shell'));
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

test('catch-all proxy enforces dashboard and auth route allowlists', () => {
  const originalOrigin = process.env.VM_API_ORIGIN;
  process.env.VM_API_ORIGIN = 'https://vm.example.test';

  try {
    const dashboard = buildWebProxyTarget(requestStub('GET', {}, '/api/dashboard/branches?company_id=1'));
    assert.equal(dashboard.ok, true);
    assert.equal(dashboard.target.href, 'https://vm.example.test/dashboard/branches?company_id=1');

    const metricVisibility = buildWebProxyTarget(requestStub('PUT', {}, '/api/dashboard/metric-visibility'));
    assert.equal(metricVisibility.ok, true);
    assert.equal(metricVisibility.target.href, 'https://vm.example.test/dashboard/metric-visibility');

    // Every dashboard route the SPA calls has to be listed here, in both copies of the
    // proxy — an endpoint missing from the allowlist answers 404 only in the deployed app.
    for (const buildTarget of [buildWebProxyTarget, buildRootProxyTarget]) {
      for (const [method, path] of [
        ['GET', '/api/dashboard/plan/reviews_fact?month=2025-01'],
        ['POST', '/api/dashboard/plan/reviews_fact'],
        ['GET', '/api/dashboard/plan/opz_fact?month=2025-01'],
        ['POST', '/api/dashboard/plan/opz_fact'],
        ['GET', '/api/dashboard/payments/yandex_pay?month=2025-01'],
        ['POST', '/api/dashboard/payments/yandex_pay'],
      ]) {
        const result = buildTarget(requestStub(method, {}, path));
        assert.equal(result.ok, true, `${method} ${path}`);
      }
    }

    const serviceBatch = buildWebProxyTarget(requestStub('PATCH', {}, '/api/dashboard/services'));
    assert.equal(serviceBatch.ok, true);
    assert.equal(serviceBatch.target.href, 'https://vm.example.test/dashboard/services');

    const serviceLabel = buildRootProxyTarget(requestStub('PATCH', {}, '/api/dashboard/services/1/10/labels'));
    assert.equal(serviceLabel.ok, true);
    assert.equal(serviceLabel.target.href, 'https://vm.example.test/dashboard/services/1/10/labels');

    const serviceGroup = buildWebProxyTarget(requestStub('DELETE', {}, '/api/dashboard/services/kpi_groups/3'));
    assert.equal(serviceGroup.ok, true);
    assert.equal(serviceGroup.target.href, 'https://vm.example.test/dashboard/services/kpi_groups/3');

    const auth = buildRootProxyTarget(requestStub('POST', {}, '/api/auth/admin/users'));
    assert.equal(auth.ok, true);
    assert.equal(auth.target.href, 'https://vm.example.test/dashboard/auth/admin/users');

    const health = buildWebProxyTarget(requestStub('HEAD', {}, '/api/health'));
    assert.equal(health.ok, true);
    assert.equal(health.target.href, 'https://vm.example.test/health');

    const onboarding = buildWebProxyTarget(requestStub('POST', {}, '/api/onboarding/credentials'));
    assert.equal(onboarding.ok, true);
    assert.equal(onboarding.target.href, 'https://vm.example.test/dashboard/onboarding/credentials');

    const methodBlocked = buildWebProxyTarget(requestStub('DELETE', {}, '/api/dashboard/branches'));
    assert.equal(methodBlocked.ok, false);
    assert.equal(methodBlocked.statusCode, 405);
    assert.deepEqual(methodBlocked.allowedMethods, ['GET', 'HEAD']);

    const unknownBlocked = buildWebProxyTarget(requestStub('GET', {}, '/api/sync/jobs'));
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

test('catch-all proxy rejects encoded traversal and encoded separators', () => {
  const originalOrigin = process.env.VM_API_ORIGIN;
  process.env.VM_API_ORIGIN = 'https://vm.example.test';

  try {
    const blockedUrls = [
      '/api/dashboard/%2e%2e/auth/login',
      '/api/auth/admin/%2e%2e/users',
      '/api/dashboard/services/company%2fservice/labels',
      '/api/dashboard/services/company%5cservice/labels',
      '/api/dashboard/services//labels',
    ];

    for (const url of blockedUrls) {
      const result = buildWebProxyTarget(requestStub('GET', {}, url));
      assert.equal(result.ok, false, `${url} should be blocked`);
      assert.equal(result.statusCode, 404, `${url} should return 404`);
    }
  } finally {
    if (originalOrigin === undefined) {
      delete process.env.VM_API_ORIGIN;
    } else {
      process.env.VM_API_ORIGIN = originalOrigin;
    }
  }
});

test('catch-all proxy handler delegates service mutations to the shared target', async () => {
  const originalOrigin = process.env.VM_API_ORIGIN;
  const originalFetch = globalThis.fetch;
  process.env.VM_API_ORIGIN = 'https://vm.example.test';
  const targets = [];

  globalThis.fetch = async (target) => {
    targets.push(String(target));
    return {
      status: 200,
      headers: upstreamHeaders([]),
      async arrayBuffer() {
        return new TextEncoder().encode('{}').buffer;
      },
    };
  };

  try {
    const cases = [
      [rootProxyHandler, 'PATCH', '/api/dashboard/services/1/10/labels'],
      [rootProxyHandler, 'PATCH', '/api/dashboard/services/1/10/kpi_group'],
      [rootProxyHandler, 'PATCH', '/api/dashboard/services/kpi_groups/3'],
      [rootProxyHandler, 'DELETE', '/api/dashboard/services/kpi_groups/3'],
      [webProxyHandler, 'PATCH', '/api/dashboard/services/1/10/labels'],
      [webProxyHandler, 'PATCH', '/api/dashboard/services/1/10/kpi_group'],
      [webProxyHandler, 'PATCH', '/api/dashboard/services/kpi_groups/3'],
      [webProxyHandler, 'DELETE', '/api/dashboard/services/kpi_groups/3'],
    ];

    for (const [handler, method, url] of cases) {
      const res = new ResponseRecorder();
      await handler(requestStub(method, {}, url), res);
      assert.equal(res.statusCode, 200);
    }

    assert.deepEqual(targets, cases.map(([, , url]) => `https://vm.example.test${url.replace('/api', '')}`));
  } finally {
    globalThis.fetch = originalFetch;
    if (originalOrigin === undefined) {
      delete process.env.VM_API_ORIGIN;
    } else {
      process.env.VM_API_ORIGIN = originalOrigin;
    }
  }
});

test('direct admin credential handlers delegate through the proxy allowlist', async () => {
  const originalOrigin = process.env.VM_API_ORIGIN;
  const originalFetch = globalThis.fetch;
  process.env.VM_API_ORIGIN = 'https://vm.example.test';
  let fetchCalled = false;

  globalThis.fetch = async () => {
    fetchCalled = true;
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
    await credentialByIdHandler(requestStub('POST', {}, '/auth/admin/yclients-credentials/123'), res);

    assert.equal(fetchCalled, false);
    assert.equal(res.statusCode, 405);
    assert.equal(res.headers.get('allow'), 'DELETE, PATCH');
    assert.equal(Buffer.from(res.body).toString('utf8'), '{"error":"Method not allowed"}');
  } finally {
    globalThis.fetch = originalFetch;
    if (originalOrigin === undefined) {
      delete process.env.VM_API_ORIGIN;
    } else {
      process.env.VM_API_ORIGIN = originalOrigin;
    }
  }
});

test('root and web proxy files stay synchronized', async () => {
  const syncedFiles = [
    '_proxy.js',
    'auth-proxy.js',
    '[...path].js',
    'auth/[...path].js',
    'dashboard/[...path].js',
    'auth/admin/yclients-credentials.js',
    'auth/admin/yclients-credentials/test.js',
    'auth/admin/yclients-credentials/[credential_id].js',
    'auth/admin/yclients-credentials/[credential_id]/test.js',
  ];

  for (const file of syncedFiles) {
    const [rootSource, webSource] = await Promise.all([
      readFile(new URL(`../../api/${file}`, import.meta.url), 'utf8'),
      readFile(new URL(`../api/${file}`, import.meta.url), 'utf8'),
    ]);

    assert.equal(rootSource, webSource, `${file} differs between api and web/api`);
  }
});

// Vercel's catch-all `[...path].js` only answers one segment below its own directory, so every
// scope and every nested prefix the allowlist opens (`plan/…`, `payments/…`) must be reachable by
// either a function under `api/<dir>/` or a vercel.json rewrite to api/auth-proxy.js, which takes
// the scope and the path as query parameters. New prefixes go the rewrite way: the Hobby plan caps a
// deployment at 12 functions, and the 13th failed the whole Vercel build on 2026-10-09. Locally
// there is no such layer, so only this test notices either gap.
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

test('every allowlisted route reaches a Vercel function in both trees', async () => {
  const source = await readFile(new URL('../../api/_proxy.js', import.meta.url), 'utf8');
  const ruleBlock = (scope, nextMarker) => source.slice(
    source.indexOf(`const ${scope.toUpperCase()}_ROUTE_RULES`),
    source.indexOf(nextMarker),
  );
  const scopes = {
    dashboard: ruleBlock('dashboard', 'const ONBOARDING_ROUTE_RULES'),
    onboarding: ruleBlock('onboarding', 'export function env'),
  };
  const dirs = [];
  for (const [scope, rules] of Object.entries(scopes)) {
    assert.ok(rules.includes('pattern:'), `no route rules found for ${scope}`);
    dirs.push({ scope, dir: scope, prefix: '' });
    for (const match of new Set([...rules.matchAll(/(?:\^|\(|\|)([a-z_-]+)\\\//g)].map((m) => m[1]))) {
      dirs.push({ scope, dir: `${scope}/${match}`, prefix: `${match}/` });
    }
  }
  assert.ok(dirs.some(({ dir }) => dir === 'dashboard/payments'), 'payments prefix not found in the allowlist');

  for (const vercelConfig of ['../../vercel.json', '../vercel.json']) {
    const { rewrites } = JSON.parse(await readFile(new URL(vercelConfig, import.meta.url), 'utf8'));
    const ruleIndex = (src, destination) => rewrites.findIndex(
      (rule) => rule.source === src && (destination === undefined || rule.destination === destination),
    );
    for (const { scope, dir, prefix } of dirs) {
      const viaProxy = `/api/auth-proxy?scope=${scope}&path=${prefix}:path*`;
      if (ruleIndex(`/api/${dir}/:path*`, viaProxy) !== -1) {
        // The bare path (`/dashboard/payments/…`) must hit the same rule before the scope-wide one.
        const bare = ruleIndex(`/${dir}/:path*`, viaProxy);
        assert.ok(bare !== -1, `${vercelConfig}: /${dir}/ is not rewritten like /api/${dir}/`);
        const scopeWide = ruleIndex(`/${scope}/:path*`);
        assert.ok(dir === scope || bare < scopeWide, `${vercelConfig}: /${dir}/ rule comes after /${scope}/:path*`);
        continue;
      }
      for (const tree of ['../../api', '../api']) {
        const files = await readdir(new URL(`${tree}/${dir}/`, import.meta.url)).catch(() => []);
        assert.ok(
          files.some((file) => file.endsWith('.js')),
          `${tree}/${dir}/ has no function file and ${vercelConfig} does not rewrite /api/${dir}/ to auth-proxy`,
        );
      }
    }
    // The scope-wide dashboard rule is what sends every one-segment path to its catch-all function.
    assert.ok(
      ruleIndex('/dashboard/:path*', '/api/dashboard/:path*') !== -1,
      `${vercelConfig} no longer sends /dashboard/ to api/dashboard/`,
    );
    // Auth paths ride in a query parameter to a single function, so their depth never matters.
    assert.ok(
      ruleIndex('/auth/:path*', '/api/auth-proxy?path=:path*') !== -1,
      `${vercelConfig} no longer sends /auth/ through api/auth-proxy.js`,
    );
  }
});

test('each proxy tree stays within the Vercel function limit', async () => {
  for (const tree of ['../../api', '../api']) {
    const files = await functionFiles(tree);
    assert.ok(
      files.length <= VERCEL_FUNCTION_LIMIT,
      `${tree} has ${files.length} functions (limit ${VERCEL_FUNCTION_LIMIT}): ${files.join(', ')}`,
    );
  }
});

test('auth proxy forwards rewritten dashboard and onboarding scopes', () => {
  const originalOrigin = process.env.VM_API_ORIGIN;
  process.env.VM_API_ORIGIN = 'https://vm.example.test';

  try {
    const editor = buildAuthProxyTarget(
      requestStub('GET', {}, '/api/auth-proxy?scope=dashboard&path=payments/yandex_pay&month=2026-10'),
    );
    assert.equal(editor.ok, true);
    assert.equal(editor.target.href, 'https://vm.example.test/dashboard/payments/yandex_pay?month=2026-10');

    const save = buildAuthProxyTarget(requestStub('POST', {}, '/api/auth-proxy?scope=dashboard&path=payments/yandex_pay'));
    assert.equal(save.ok, true);

    const onboarding = buildAuthProxyTarget(requestStub('GET', {}, '/api/auth-proxy?scope=onboarding&path=state'));
    assert.equal(onboarding.ok, true);
    assert.equal(onboarding.target.href, 'https://vm.example.test/dashboard/onboarding/state');

    const login = buildAuthProxyTarget(requestStub('POST', {}, '/api/auth-proxy?path=login'));
    assert.equal(login.target.href, 'https://vm.example.test/dashboard/auth/login', 'auth stays the default scope');

    const keepsClientQuery = buildAuthProxyTarget(
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
      const refused = buildAuthProxyTarget(requestStub('GET', {}, url));
      assert.equal(refused.ok, false, url);
      assert.equal(refused.statusCode, 400, url);
    }

    // A repeated identical copy is not ambiguous: refusing it would break every auth call if Vercel
    // ever echoed the parameter it substituted.
    const echoedAuth = buildAuthProxyTarget(requestStub('GET', {}, '/api/auth-proxy?path=me&path=me'));
    assert.equal(echoedAuth.ok, true);
    assert.equal(echoedAuth.target.href, 'https://vm.example.test/dashboard/auth/me');
    const echoedScope = buildAuthProxyTarget(
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
      const blocked = buildAuthProxyTarget(requestStub('GET', {}, url));
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

test('both vercel.json files agree, and the auth proxy outlasts a YClients credential check', async () => {
  const [root, web] = await Promise.all(
    ['../../vercel.json', '../vercel.json'].map(async (file) => JSON.parse(await readFile(new URL(file, import.meta.url), 'utf8'))),
  );
  assert.deepEqual(root.rewrites, web.rewrites, 'rewrites differ between vercel.json and web/vercel.json');
  assert.deepEqual(root.functions, web.functions, 'functions differ between vercel.json and web/vercel.json');
  // Onboarding verifies credentials against YClients (30s timeout, retried) inside this function.
  assert.ok(root.functions['api/auth-proxy.js'].maxDuration >= 60);
  // The proxy aborts its own upstream call at PROXY_TIMEOUT_MS (160 s) and must still have time to
  // answer; a shorter function limit turns every slow call into a bare Vercel timeout.
  for (const fn of ['api/auth-proxy.js', 'api/dashboard/**/*.js']) {
    assert.ok(root.functions[fn].maxDuration > 160, `${fn} maxDuration must exceed the 160 s proxy timeout`);
  }
});
