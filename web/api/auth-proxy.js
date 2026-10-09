import { proxyToVm, rejectProxyRequest, validateProxyRequest, vmOrigin } from './_proxy.js';

// Vercel's Hobby plan caps a deployment at 12 functions, and every nested path prefix would
// otherwise need a function file of its own (a catch-all answers one segment only). vercel.json
// rewrites such paths here instead, with the scope and the path in the query string. Same
// VM targets as api/[...path].js.
const SCOPE_TARGET_PREFIX = {
  auth: 'dashboard/auth/',
  dashboard: 'dashboard/',
  onboarding: 'dashboard/onboarding/',
};

export function buildTargetUrl(req) {
  const incoming = new URL(req.url, `https://${req.headers.host}`);
  // Vercel merges the original query into a rewrite's destination, so a client can add its own
  // `scope`/`path` next to the rewrite's. Whichever copy wins, conflicting values are refused rather
  // than guessed at. Identical copies pass: if Vercel itself ever repeats a parameter it substituted,
  // refusing that would take every auth call down with it.
  const ambiguous = (name) => new Set(incoming.searchParams.getAll(name)).size > 1;
  if (ambiguous('scope') || ambiguous('path')) {
    return { ok: false, statusCode: 400, error: 'Bad request' };
  }
  const scope = incoming.searchParams.get('scope') || 'auth';
  if (!Object.hasOwn(SCOPE_TARGET_PREFIX, scope)) {
    return { ok: false, statusCode: 404, error: 'Not found' };
  }
  const validation = validateProxyRequest(req, scope, incoming.searchParams.get('path') || '');
  if (!validation.ok) {
    return validation;
  }
  incoming.searchParams.delete('path');
  incoming.searchParams.delete('scope');

  const target = new URL(`/${SCOPE_TARGET_PREFIX[scope]}${validation.path}`, vmOrigin());
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
