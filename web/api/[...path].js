import {
  SCOPE_TARGET_PREFIX,
  createProxyHandler,
  rawRequestPath,
  validateProxyRequest,
  vmOrigin,
} from './_proxy.js';

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

  if (!Object.hasOwn(SCOPE_TARGET_PREFIX, firstSegment)) {
    return { ok: false, statusCode: 404, error: 'Not found' };
  }

  const scope = firstSegment;
  const scopedPath = path.split('/').slice(1).join('/');
  const validation = validateProxyRequest(req, scope, scopedPath);
  if (!validation.ok) {
    return validation;
  }

  const target = new URL(`/${SCOPE_TARGET_PREFIX[scope]}${validation.path}`, vmOrigin());
  target.search = incoming.search;
  return { ok: true, target };
}

export default createProxyHandler(buildTargetUrl);
