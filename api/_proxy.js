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

export function env(name) {
  const value = process.env[name];
  return value && value.trim() ? value.trim() : '';
}

export function forwardedHeaders(req) {
  const headers = {};
  for (const [name, value] of Object.entries(req.headers)) {
    const lowerName = name.toLowerCase();
    if (!HOP_BY_HOP_HEADERS.has(lowerName) && lowerName !== 'host') {
      headers[name] = value;
    }
  }

  return headers;
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
