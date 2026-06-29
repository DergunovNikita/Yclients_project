import { proxyToVm, vmOrigin } from '../../_proxy.js';

export default async function handler(req, res) {
  const incoming = new URL(req.url, `https://${req.headers.host}`);
  const target = new URL('/auth/admin/yclients-credentials', vmOrigin());
  target.search = incoming.search;
  await proxyToVm(req, res, target);
}
