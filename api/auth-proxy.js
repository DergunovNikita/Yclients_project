import { SCOPE_TARGET_PREFIX, createProxyHandler, validateProxyRequest, vmOrigin } from './_proxy.js';

// The only function of the deployment. Vercel's Hobby plan caps a deployment at 12 functions, and a
// catch-all file answers one path segment only, so every nested prefix would otherwise need a function
// file of its own. vercel.json rewrites every proxied path here instead (/auth, /dashboard, /onboarding
// and /api/health, with or without the /api prefix), carrying the scope and the path in the query string.

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
  const isHealth = scope === 'health';
  if (!isHealth && !Object.hasOwn(SCOPE_TARGET_PREFIX, scope)) {
    return { ok: false, statusCode: 404, error: 'Not found' };
  }
  const validation = validateProxyRequest(req, scope, incoming.searchParams.get('path') || '');
  if (!validation.ok) {
    return validation;
  }
  incoming.searchParams.delete('path');
  incoming.searchParams.delete('scope');

  const targetPath = isHealth ? '/health' : `/${SCOPE_TARGET_PREFIX[scope]}${validation.path}`;
  const target = new URL(targetPath, vmOrigin());
  target.search = incoming.searchParams.toString();
  return { ok: true, target };
}

export default createProxyHandler(buildTargetUrl);
