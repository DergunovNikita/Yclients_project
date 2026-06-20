import { proxyToVm, vmOrigin } from '../_proxy.js';

function buildTargetUrl(req) {
  const incoming = new URL(req.url, `https://${req.headers.host}`);
  const path = incoming.pathname.replace(/^\/api\/?/, '');
  const target = new URL(`/${path}`, vmOrigin());
  target.search = incoming.search;
  return target;
}

export default async function handler(req, res) {
  let target;
  try {
    target = buildTargetUrl(req);
  } catch (error) {
    res.statusCode = 500;
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ error: error.message }));
    return;
  }

  await proxyToVm(req, res, target);
}
