import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import { ROUTE_RULES, SCOPE_TARGET_PREFIX, allowedMethods } from '../../deploy/nginx/routes.mjs';
import { renderAllowlist } from '../../scripts/nginx-allowlist.mjs';

const CHECKED_IN = new URL('../../deploy/nginx/api-allowlist.conf', import.meta.url);

test('the checked-in nginx allowlist is what scripts/nginx-allowlist.mjs renders from deploy/nginx/routes.mjs', async () => {
  assert.equal(
    await readFile(CHECKED_IN, 'utf8'),
    renderAllowlist(),
    'run: node scripts/nginx-allowlist.mjs > deploy/nginx/api-allowlist.conf',
  );
});

// nginx maps take the first line whose regex matches the key; PCRE and JS agree on the plain syntax used here.
function loadMap(source, variable) {
  const block = source.split(`$${variable} {`)[1].split('\n}')[0];
  return [...block.matchAll(/^\s+"~([^"]+)" "([^"]*)";$/gm)].map(([, regex, value]) => [new RegExp(regex), value]);
}

function lookup(map, key) {
  for (const [regex, value] of map) {
    const match = regex.exec(key);
    if (match) {
      return value.replace(/\$(\d)/g, (_, n) => match[Number(n)]);
    }
  }
  return '';
}

const rendered = renderAllowlist();
const allowMap = loadMap(rendered, 'api_allow');
const targetMap = loadMap(rendered, 'api_target');

function samplePaths() {
  const paths = [];
  for (const [scope, rules] of Object.entries(ROUTE_RULES)) {
    for (const rule of rules) {
      const body = rule.pattern.source.replaceAll('\\/', '/').replace(/^\^/, '').replace(/\$$/, '');
      const items = body.startsWith('(') ? body.slice(1, -1).split('|') : [body];
      for (const item of items) {
        const path = item.replaceAll('[^/]+', '42');
        paths.push([scope, path], [scope, `${path}/`], [scope, `${path}/extra`], [scope, `${path}x`]);
      }
    }
  }
  paths.push(['auth', ''], ['auth', 'sessions/'], ['dashboard', 'bundle//'], ['onboarding', 'nope'], ['dashboard', 'reports/data/x']);
  return paths;
}

// What the maps must answer for a URI below a scope: trailing slashes are ignored, an empty segment is not a route.
function expectedAnswer(scope, path, method) {
  const trimmed = path.replace(/\/+$/, '');
  const methods = !trimmed || trimmed.includes('//') ? [] : allowedMethods(scope, trimmed);
  if (!methods.length) return { allow: '', target: '' };
  const allow = methods.join(', ');
  return methods.includes(method)
    ? { allow, target: `/${SCOPE_TARGET_PREFIX[scope]}${trimmed}` }
    : { allow, target: '' };
}

test('nginx maps agree with the route table on every route, method and near-miss path', () => {
  for (const [scope, path] of samplePaths()) {
    for (const method of ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS']) {
      const expected = expectedAnswer(scope, path, method);
      for (const prefix of ['/api', '']) {
        const uri = `${prefix}/${scope}/${path}`;
        const label = `${method} ${uri}`;
        assert.equal(lookup(allowMap, uri), expected.allow, label);
        assert.equal(lookup(targetMap, `${method} ${uri}`), expected.target, label);
      }
    }
  }
});

function answer(method, uri) {
  return { allow: lookup(allowMap, uri), target: lookup(targetMap, `${method} ${uri}`) };
}

test('every route the SPA calls is served with exactly the methods it uses', () => {
  for (const [method, uri, target] of [
    ['GET', '/api/dashboard/branches', '/dashboard/branches'],
    ['PUT', '/api/dashboard/metric-visibility', '/dashboard/metric-visibility'],
    ['GET', '/api/dashboard/plan/reviews_fact', '/dashboard/plan/reviews_fact'],
    ['POST', '/api/dashboard/plan/reviews_fact', '/dashboard/plan/reviews_fact'],
    ['GET', '/api/dashboard/plan/opz_fact', '/dashboard/plan/opz_fact'],
    ['POST', '/api/dashboard/plan/opz_fact', '/dashboard/plan/opz_fact'],
    ['GET', '/api/dashboard/payments/yandex_pay', '/dashboard/payments/yandex_pay'],
    ['POST', '/api/dashboard/payments/yandex_pay', '/dashboard/payments/yandex_pay'],
    ['GET', '/api/dashboard/branch-order', '/dashboard/branch-order'],
    ['PUT', '/api/dashboard/branch-order', '/dashboard/branch-order'],
    ['PATCH', '/api/dashboard/services', '/dashboard/services'],
    ['GET', '/api/dashboard/services', '/dashboard/services'],
    ['GET', '/api/dashboard/plan/settings', '/dashboard/plan/settings'],
    ['POST', '/api/dashboard/plan/settings', '/dashboard/plan/settings'],
    ['DELETE', '/api/auth/sessions/abc-1', '/dashboard/auth/sessions/abc-1'],
    ['PATCH', '/api/auth/admin/users/12', '/dashboard/auth/admin/users/12'],
    ['DELETE', '/api/auth/admin/users/12', '/dashboard/auth/admin/users/12'],
    ['POST', '/api/auth/admin/users', '/dashboard/auth/admin/users'],
    ['GET', '/api/auth/portal-accounts', '/dashboard/auth/portal-accounts'],
    ['POST', '/api/auth/admin/staff/7/create-account', '/dashboard/auth/admin/staff/7/create-account'],
    ['POST', '/api/onboarding/credentials', '/dashboard/onboarding/credentials'],
    ['GET', '/onboarding/state', '/dashboard/onboarding/state'],
    // The credential admin: collection, test-by-body, and per-id update, delete and test.
    ['GET', '/api/auth/admin/yclients-credentials', '/dashboard/auth/admin/yclients-credentials'],
    ['POST', '/api/auth/admin/yclients-credentials', '/dashboard/auth/admin/yclients-credentials'],
    ['POST', '/api/auth/admin/yclients-credentials/test', '/dashboard/auth/admin/yclients-credentials/test'],
    ['PATCH', '/api/auth/admin/yclients-credentials/12', '/dashboard/auth/admin/yclients-credentials/12'],
    ['DELETE', '/api/auth/admin/yclients-credentials/12', '/dashboard/auth/admin/yclients-credentials/12'],
    ['POST', '/api/auth/admin/yclients-credentials/12/test', '/dashboard/auth/admin/yclients-credentials/12/test'],
  ]) {
    assert.equal(answer(method, uri).target, target, `${method} ${uri}`);
  }
  // The same credential id with a wrong method is a 405, not a pass-through.
  assert.equal(answer('POST', '/api/auth/admin/yclients-credentials/12').target, '');
  assert.notEqual(answer('POST', '/api/auth/admin/yclients-credentials/12').allow, '');
});

// The paths the SPA hard-codes in web/src: a call to a route the table lacks is a 404 on the deployed site only.
function spaApiPaths() {
  const root = new URL('../src/', import.meta.url);
  const paths = new Map();
  for (const name of readdirSync(root, { recursive: true }).filter((file) => file.endsWith('.js'))) {
    const source = readFileSync(new URL(name, root), 'utf8');
    for (const [, literal] of source.matchAll(/['"`](\/(?:auth|dashboard|onboarding)\/[^'"`]*)['"`]/g)) {
      // Prefixes and comment globs (`/auth/admin/`, `/auth/admin/*`) are not routes.
      if (/[\s*]|\/$/.test(literal)) continue;
      paths.set(literal.split('?')[0].replace(/\$\{[^}]*\}/g, '42'), `src/${name}`);
    }
  }
  return paths;
}

test('every API path hard-coded in web/src is served by the allowlist', () => {
  const paths = spaApiPaths();
  assert.ok(paths.size > 30, `expected the scan to find the SPA's API paths, got ${paths.size}`);
  for (const [path, file] of paths) {
    assert.notEqual(answer('GET', `/api${path}`).allow, '', `${path} (${file}) is not in deploy/nginx/routes.mjs`);
  }
});

test('nothing destructive rides along with the mutating dashboard routes', () => {
  for (const path of ['plan/settings', 'plan/reviews_fact', 'plan/opz_fact', 'payments/yandex_pay', 'metric-visibility', 'branch-order']) {
    for (const method of ['DELETE', 'PATCH']) {
      assert.equal(answer(method, `/api/dashboard/${path}`).target, '', `${method} ${path}`);
    }
  }
  assert.deepEqual(answer('DELETE', '/api/dashboard/branches'), { allow: 'GET, HEAD', target: '' });
});

test('read-only and unlisted auth routes stay closed', () => {
  // The SPA calls /auth/portal-accounts; the admin/ alias is not served.
  assert.equal(answer('GET', '/api/auth/admin/portal-accounts').allow, '');
  assert.equal(answer('POST', '/api/auth/admin/shell').allow, '');
  // Synced staff rows are read-only in the portal: only account creation is served.
  for (const method of ['PATCH', 'DELETE', 'GET']) {
    assert.equal(answer(method, '/api/auth/admin/staff/7').allow, '', method);
  }
});

test('dot segments and empty segments never reach a route', () => {
  // nginx decodes and normalises `..` before the maps see $uri; a literal dot segment that survives must not match.
  for (const uri of [
    '/api/dashboard/plan/../auth/login',
    '/api/dashboard/payments//yandex_pay',
    '/api/dashboard//bundle',
    '/api/auth/admin/../users',
  ]) {
    assert.equal(answer('GET', uri).target, '', uri);
  }
});

test('nginx health route answers GET and HEAD under /api only', () => {
  assert.equal(lookup(targetMap, 'GET /api/health'), '/health');
  assert.equal(lookup(targetMap, 'HEAD /api/health/'), '/health');
  assert.equal(lookup(targetMap, 'POST /api/health'), '');
  assert.equal(lookup(allowMap, '/api/health'), 'GET, HEAD');
  assert.equal(lookup(allowMap, '/health'), '');
});

test('nothing outside the auth, dashboard and onboarding scopes is allowlisted', () => {
  for (const uri of ['/api/sync/trigger', '/sync/status', '/docs', '/openapi.json', '/api/docs', '/api/auth-proxy', '/api/dashboard', '/dashboard']) {
    assert.equal(lookup(allowMap, uri), '', uri);
  }
});
