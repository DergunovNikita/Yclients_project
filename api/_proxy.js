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

export async function proxyToVm(req, res, target) {
  if (!target) {
    res.statusCode = 404;
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ error: 'Not found' }));
    return;
  }

  const method = req.method.toUpperCase();
  const hasBody = method !== 'GET' && method !== 'HEAD';
  const upstream = await fetch(target, {
    method,
    headers: forwardedHeaders(req),
    body: hasBody ? await readBody(req) : undefined,
    redirect: 'manual',
  });

  res.statusCode = upstream.status;
  for (const [name, value] of upstream.headers.entries()) {
    if (!HOP_BY_HOP_HEADERS.has(name.toLowerCase())) {
      res.setHeader(name, value);
    }
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
