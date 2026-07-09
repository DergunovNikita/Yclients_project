import { proxyToVm, rawRequestPath, rejectProxyRequest, validateProxyRequest, vmOrigin } from './_proxy.js';

export function buildTargetUrl(req) {
  const incoming = new URL(req.url, `https://${req.headers.host}`);
  const path = rawRequestPath(req).replace(/^\/api\/?/, '').replace(/^\/+/, '').replace(/\/+$/, '');
  const firstSegment = path.split('/')[0];

  if (firstSegment === 'health') {
    const validation = validateProxyRequest(req, 'health', path);
    if (!validation.ok) {
      return validation;
    }
    const target = new URL('/health', vmOrigin());
    target.search = incoming.search;
    return { ok: true, target };
  }

  if (firstSegment !== 'auth' && firstSegment !== 'dashboard' && firstSegment !== 'onboarding') {
    return { ok: false, statusCode: 404, error: 'Not found' };
  }

  const scope = firstSegment;
  const scopedPath = path.split('/').slice(1).join('/');
  const validation = validateProxyRequest(req, scope, scopedPath);
  if (!validation.ok) {
    return validation;
  }

  const targetPath = scope === 'auth'
    ? `dashboard/auth/${validation.path}`
    : scope === 'onboarding'
      ? `dashboard/onboarding/${validation.path}`
      : `dashboard/${validation.path}`;
  const target = new URL(`/${targetPath}`, vmOrigin());
  target.search = incoming.search;
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
