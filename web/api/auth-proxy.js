import { proxyToVm, rejectProxyRequest, validateProxyRequest, vmOrigin } from './_proxy.js';

export function buildTargetUrl(req) {
  const incoming = new URL(req.url, `https://${req.headers.host}`);
  const validation = validateProxyRequest(req, 'auth', incoming.searchParams.get('path') || '');
  if (!validation.ok) {
    return validation;
  }
  incoming.searchParams.delete('path');

  const target = new URL(`/dashboard/auth/${validation.path}`, vmOrigin());
  target.search = incoming.searchParams.toString();
  return { ok: true, target };
}

export default async function handler(req, res) {
  let result;
  try {
    result = buildTargetUrl(req);
  } catch (error) {
    res.statusCode = 500;
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ error: error.message }));
    return;
  }
  if (!result.ok) {
    rejectProxyRequest(res, result);
    return;
  }

  await proxyToVm(req, res, result.target);
}
