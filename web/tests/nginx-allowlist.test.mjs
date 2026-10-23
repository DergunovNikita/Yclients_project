import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import { ROUTE_RULES, SCOPE_TARGET_PREFIX, validateProxyRequest } from '../api/_proxy.js';
import { renderAllowlist } from '../../scripts/nginx-allowlist.mjs';

const CHECKED_IN = new URL('../../deploy/nginx/api-allowlist.conf', import.meta.url);

test('the checked-in nginx allowlist is what scripts/nginx-allowlist.mjs renders from the proxy rules', async () => {
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

test('nginx maps agree with validateProxyRequest on every route, method and near-miss path', () => {
  for (const [scope, path] of samplePaths()) {
    for (const method of ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS']) {
      const expected = validateProxyRequest({ method }, scope, path);
      for (const prefix of ['/api', '']) {
        const uri = `${prefix}/${scope}/${path}`;
        const allow = lookup(allowMap, uri);
        const target = lookup(targetMap, `${method} ${uri}`);
        const label = `${method} ${uri}`;
        if (expected.ok) {
          assert.equal(target, `/${SCOPE_TARGET_PREFIX[scope]}${expected.path}`, label);
          assert.ok(allow.split(', ').includes(method), label);
        } else if (expected.statusCode === 405) {
          assert.equal(target, '', label);
          assert.equal(allow, expected.allowedMethods.join(', '), label);
        } else {
          assert.equal(target, '', label);
          assert.equal(allow, '', label);
        }
      }
    }
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
