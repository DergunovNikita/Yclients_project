const HOP_BY_HOP_HEADERS = new Set([
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
]);
const PROXY_TIMEOUT_MS = Number(process.env.PROXY_TIMEOUT_MS || 55000);
const REQUEST_HEADER_ALLOWLIST = new Set([
  'accept',
  'content-type',
  'cookie',
  'x-csrf-token',
  'x-portal-account-id',
]);

const AUTH_ROUTE_RULES = [
  { methods: ['POST'], pattern: /^(login|demo-login|register|refresh|logout|logout-all|logout-others|verify-email|forgot-password|resend-verification|reset-password|change-password)$/ },
  { methods: ['GET'], pattern: /^(me|sessions|portal-accounts|admin\/portal-accounts|admin\/meta|admin\/users|admin\/yclients-credentials|admin\/initial-passwords)$/ },
  { methods: ['DELETE'], pattern: /^sessions\/[^/]+$/ },
  { methods: ['POST'], pattern: /^(admin\/users|admin\/provision-accounts|admin\/distribute-credentials|admin\/yclients-credentials|admin\/yclients-credentials\/test)$/ },
  { methods: ['PATCH', 'DELETE'], pattern: /^admin\/users\/[^/]+$/ },
  { methods: ['PATCH', 'DELETE'], pattern: /^admin\/staff\/[^/]+$/ },
  { methods: ['POST'], pattern: /^admin\/staff\/[^/]+\/create-account$/ },
  { methods: ['PATCH', 'DELETE'], pattern: /^admin\/yclients-credentials\/[^/]+$/ },
  { methods: ['POST'], pattern: /^admin\/yclients-credentials\/[^/]+\/test$/ },
];

const DASHBOARD_ROUTE_RULES = [
  { methods: ['GET'], pattern: /^(branches|staff|staff_directory\.csv|services|services\/kpi_groups|reports|reports\/data|widget\/sync_status|widget\/summary|widget\/revenue_daily|widget\/top_services|widget\/extra_services|widget\/plan_fact|plan\/settings|plan\/reviews_fact|bundle)$/ },
  { methods: ['GET', 'PUT'], pattern: /^metric-visibility$/ },
  { methods: ['POST'], pattern: /^(services\/kpi_groups|plan\/settings|plan\/reviews_fact|plan\/sync)$/ },
  { methods: ['PATCH'], pattern: /^services\/[^/]+\/[^/]+\/(labels|kpi_group)$/ },
  { methods: ['PATCH', 'DELETE'], pattern: /^services\/kpi_groups\/[^/]+$/ },
];

const ONBOARDING_ROUTE_RULES = [
  { methods: ['GET'], pattern: /^state$/ },
  { methods: ['POST'], pattern: /^(credentials|branches)$/ },
];

export function env(name) {
  const value = process.env[name];
  return value && value.trim() ? value.trim() : '';
}

function trustedPeerIp(req) {
  const value = req.socket?.remoteAddress || req.connection?.remoteAddress || '';
  return typeof value === 'string' ? value.trim() : '';
}

export function forwardedHeaders(req) {
  const headers = {};
  for (const [name, value] of Object.entries(req.headers)) {
    const lowerName = name.toLowerCase();
    if (REQUEST_HEADER_ALLOWLIST.has(lowerName)) {
      headers[lowerName] = value;
    }
  }

  const peerIp = trustedPeerIp(req);
  if (peerIp) {
    // The VM trusts these headers for session metadata and rate limits. Browser-supplied
    // forwarding headers are intentionally ignored; only this proxy's socket peer is forwarded.
    headers['x-forwarded-for'] = peerIp;
    headers['x-real-ip'] = peerIp;
  }

  return headers;
}

export function normalizeProxyPath(rawPath) {
  const source = String(rawPath || '').replace(/^\/+/, '').replace(/\/+$/, '');
  if (!source) {
    return null;
  }

  const segments = source.split('/');
  const normalized = [];
  for (const segment of segments) {
    if (!segment || segment.includes('\\')) {
      return null;
    }

    let decoded;
    try {
      decoded = decodeURIComponent(segment);
    } catch {
      return null;
    }

    if (!decoded || decoded === '.' || decoded === '..' || decoded.includes('/') || decoded.includes('\\')) {
      return null;
    }
    normalized.push(encodeURIComponent(decoded));
  }

  return normalized.join('/');
}

export function rawRequestPath(req) {
  const rawUrl = String(req.url || '/');
  const queryStart = rawUrl.indexOf('?');
  return queryStart === -1 ? rawUrl : rawUrl.slice(0, queryStart);
}

function methodsForRules(path, rules) {
  const methods = new Set();
  for (const rule of rules) {
    if (rule.pattern.test(path)) {
      for (const method of rule.methods) {
        methods.add(method);
      }
    }
  }
  if (methods.has('GET')) {
    methods.add('HEAD');
  }
  return [...methods].sort();
}

export function allowedMethodsForProxyPath(scope, path) {
  if (scope === 'auth') {
    return methodsForRules(path, AUTH_ROUTE_RULES);
  }
  if (scope === 'dashboard') {
    return methodsForRules(path, DASHBOARD_ROUTE_RULES);
  }
  if (scope === 'onboarding') {
    return methodsForRules(path, ONBOARDING_ROUTE_RULES);
  }
  if (scope === 'health' && path === 'health') {
    return ['GET', 'HEAD'];
  }
  return [];
}

export function validateProxyRequest(req, scope, rawPath) {
  const path = scope === 'health' ? String(rawPath || '').replace(/^\/+/, '').replace(/\/+$/, '') : normalizeProxyPath(rawPath);
  if (!path) {
    return { ok: false, statusCode: 404, error: 'Not found' };
  }

  const allowedMethods = allowedMethodsForProxyPath(scope, path);
  if (!allowedMethods.length) {
    return { ok: false, statusCode: 404, error: 'Not found' };
  }

  const method = String(req.method || 'GET').toUpperCase();
  if (!allowedMethods.includes(method)) {
    return {
      ok: false,
      statusCode: 405,
      error: 'Method not allowed',
      allowedMethods,
    };
  }

  return { ok: true, path, allowedMethods };
}

export function rejectProxyRequest(res, validation) {
  res.statusCode = validation.statusCode || 404;
  if (validation.allowedMethods?.length) {
    res.setHeader('Allow', validation.allowedMethods.join(', '));
  }
  res.setHeader('Content-Type', 'application/json');
  res.end(JSON.stringify({ error: validation.error || 'Not found' }));
}

export function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on('data', (chunk) => chunks.push(chunk));
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

function splitCombinedSetCookie(value) {
  if (!value) return [];
  const cookies = [];
  let start = 0;
  let inExpires = false;
  let expiresSawGmt = false;
  let inQuotes = false;
  const source = String(value);
  for (let index = 0; index < source.length; index += 1) {
    const char = source[index];
    if (char === '"') {
      inQuotes = !inQuotes;
      continue;
    }
    if (inQuotes) {
      continue;
    }
    if (source.slice(index, index + 8).toLowerCase() === 'expires=') {
      inExpires = true;
      expiresSawGmt = false;
      index += 7;
      continue;
    }
    if (inExpires && source.slice(index, index + 3).toLowerCase() === 'gmt') {
      expiresSawGmt = true;
      index += 2;
      continue;
    }
    if (inExpires && char === ';') {
      inExpires = false;
      expiresSawGmt = false;
      continue;
    }
    if (char === ',' && (!inExpires || expiresSawGmt)) {
      const item = source.slice(start, index).trim();
      if (item) cookies.push(item);
      start = index + 1;
      inExpires = false;
      expiresSawGmt = false;
    }
  }
  const finalItem = source.slice(start).trim();
  if (finalItem) cookies.push(finalItem);
  return cookies;
}

export function responseSetCookies(headers) {
  if (typeof headers.getSetCookie === 'function') {
    return headers.getSetCookie();
  }
  if (typeof headers.raw === 'function') {
    const rawSetCookie = headers.raw()['set-cookie'];
    if (Array.isArray(rawSetCookie)) return rawSetCookie;
  }
  return splitCombinedSetCookie(headers.get?.('set-cookie'));
}

export async function proxyToVm(req, res, target) {
  if (!target) {
    res.statusCode = 404;
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ error: 'Not found' }));
    return;
  }

  const method = req.method.toUpperCase();
  const hasBody = method !== 'GET' && method !== 'HEAD';
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), PROXY_TIMEOUT_MS);
  let upstream;
  try {
    upstream = await fetch(target, {
      method,
      headers: forwardedHeaders(req),
      body: hasBody ? await readBody(req) : undefined,
      redirect: 'manual',
      signal: controller.signal,
    });
  } catch (error) {
    res.statusCode = 503;
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({
      success: false,
      detail: 'Upstream API is temporarily unavailable',
      error: error.name === 'AbortError' ? 'upstream_timeout' : 'upstream_unavailable',
    }));
    return;
  } finally {
    clearTimeout(timeout);
  }

  res.statusCode = upstream.status;
  const setCookies = responseSetCookies(upstream.headers);
  for (const [name, value] of upstream.headers.entries()) {
    const lowerName = name.toLowerCase();
    if (!HOP_BY_HOP_HEADERS.has(lowerName) && lowerName !== 'set-cookie') {
      res.setHeader(name, value);
    }
  }
  if (setCookies.length) {
    res.setHeader('set-cookie', setCookies);
  }

  const body = Buffer.from(await upstream.arrayBuffer());
  res.end(body);
}

export function vmOrigin() {
  const origin = env('VM_API_ORIGIN');
  if (!origin) {
    throw new Error('VM_API_ORIGIN is not configured');
  }
  return origin.replace(/\/$/, '');
}
