// content-encoding and content-length describe the upstream's wire bytes, but fetch() hands us the
// decoded body: forwarding them would label plain bytes as compressed (Node sets the length itself).
const HOP_BY_HOP_HEADERS = new Set([
  'connection',
  'content-encoding',
  'content-length',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
]);
// Stays above nginx proxy_read_timeout (150s) so a slow report comes back as the
// upstream's own 504 rather than this generic 503, and below maxDuration so the
// function still has time to write the response.
const PROXY_TIMEOUT_MS = Number(process.env.PROXY_TIMEOUT_MS || 160000);
const REQUEST_HEADER_ALLOWLIST = new Set([
  'accept',
  'content-type',
  'cookie',
  'user-agent',
  'x-csrf-token',
  'x-portal-account-id',
]);

const AUTH_ROUTE_RULES = [
  { methods: ['POST'], pattern: /^(login|demo-login|register|refresh|logout|logout-all|logout-others|verify-email|forgot-password|resend-verification|reset-password|change-password)$/ },
  { methods: ['GET'], pattern: /^(me|sessions|portal-accounts|admin\/meta|admin\/users|admin\/yclients-credentials|admin\/initial-passwords)$/ },
  { methods: ['DELETE'], pattern: /^sessions\/[^/]+$/ },
  { methods: ['POST'], pattern: /^(admin\/users|admin\/provision-accounts|admin\/distribute-credentials|admin\/yclients-credentials|admin\/yclients-credentials\/test)$/ },
  { methods: ['PATCH', 'DELETE'], pattern: /^admin\/users\/[^/]+$/ },
  { methods: ['POST'], pattern: /^admin\/staff\/[^/]+\/create-account$/ },
  { methods: ['PATCH', 'DELETE'], pattern: /^admin\/yclients-credentials\/[^/]+$/ },
  { methods: ['POST'], pattern: /^admin\/yclients-credentials\/[^/]+\/test$/ },
];

const DASHBOARD_ROUTE_RULES = [
  { methods: ['GET'], pattern: /^(branches|staff|services|reports|reports\/data|widget\/sync_status|widget\/plan_fact|plan\/settings|plan\/reviews_fact|plan\/opz_fact|payments\/yandex_pay|bundle)$/ },
  { methods: ['GET', 'PUT'], pattern: /^(metric-visibility|branch-order)$/ },
  { methods: ['POST'], pattern: /^(plan\/settings|plan\/reviews_fact|plan\/opz_fact|payments\/yandex_pay)$/ },
  { methods: ['PATCH'], pattern: /^services$/ },
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

function firstHop(value) {
  const text = Array.isArray(value) ? value[0] : value;
  return typeof text === 'string' ? text.split(',')[0].trim() : '';
}

// On Vercel the platform overwrites these headers with the visitor's address; the socket peer
// is Vercel's own infrastructure, so it is only the last resort.
function visitorIp(req) {
  return (
    firstHop(req.headers['x-vercel-forwarded-for'])
    || firstHop(req.headers['x-real-ip'])
    || firstHop(req.headers['x-forwarded-for'])
    || trustedPeerIp(req)
  );
}

export function forwardedHeaders(req) {
  const headers = {};
  for (const [name, value] of Object.entries(req.headers)) {
    const lowerName = name.toLowerCase();
    if (REQUEST_HEADER_ALLOWLIST.has(lowerName)) {
      headers[lowerName] = value;
    }
  }

  // x-proxy-secret is not in the allowlist, so a browser-supplied one never gets here; it is
  // only ever set from the environment below.
  const proxySecret = env('VM_PROXY_SECRET');
  const clientIp = proxySecret ? visitorIp(req) : trustedPeerIp(req);
  if (clientIp) {
    // Without the shared secret the VM ignores these and sees this proxy's address. With it,
    // the VM believes x-forwarded-for as the visitor, so only the real visitor address is sent.
    headers['x-forwarded-for'] = clientIp;
    headers['x-real-ip'] = clientIp;
  }
  if (proxySecret) {
    headers['x-proxy-secret'] = proxySecret;
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

// `detail` is what the SPA reads out of an error body (FastAPI's key); `error` stays for older readers.
function sendJsonError(res, statusCode, message) {
  res.statusCode = statusCode;
  res.setHeader('Content-Type', 'application/json');
  res.end(JSON.stringify({ error: message, detail: message }));
}

export function rejectProxyRequest(res, validation) {
  if (validation.allowedMethods?.length) {
    res.setHeader('Allow', validation.allowedMethods.join(', '));
  }
  sendJsonError(res, validation.statusCode || 404, validation.error || 'Not found');
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
    sendJsonError(res, 404, 'Not found');
    return;
  }

  const method = req.method.toUpperCase();
  const hasBody = method !== 'GET' && method !== 'HEAD';
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), PROXY_TIMEOUT_MS);
  let upstream;
  let body;
  try {
    upstream = await fetch(target, {
      method,
      headers: forwardedHeaders(req),
      body: hasBody ? await readBody(req) : undefined,
      redirect: 'manual',
      signal: controller.signal,
    });
    // The abort timer must keep running until the body is in: a stalled upstream body would
    // otherwise hold the function until maxDuration, and a reset mid-body would escape as an
    // unhandled rejection instead of the structured 503 the frontend knows how to show.
    body = Buffer.from(await upstream.arrayBuffer());
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

  res.end(body);
}

export function vmOrigin() {
  const origin = env('VM_API_ORIGIN');
  if (!origin) {
    throw new Error('VM_API_ORIGIN is not configured');
  }
  return origin.replace(/\/$/, '');
}

// Where each proxy scope lives on the VM. Both entry points (api/[...path].js and api/auth-proxy.js) read it.
export const SCOPE_TARGET_PREFIX = {
  auth: 'dashboard/auth/',
  dashboard: 'dashboard/',
  onboarding: 'dashboard/onboarding/',
};

// `buildTargetUrl(req)` returns `{ ok: true, target }` or a rejection from validateProxyRequest.
export function createProxyHandler(buildTargetUrl) {
  return async function handler(req, res) {
    let result;
    try {
      result = buildTargetUrl(req);
    } catch (error) {
      // The message can name a missing env var; it goes to the function log, not to the caller.
      console.error('proxy target build failed:', error);
      sendJsonError(res, 500, 'Internal error');
      return;
    }
    if (!result.ok) {
      rejectProxyRequest(res, result);
      return;
    }

    await proxyToVm(req, res, result.target);
  };
}
