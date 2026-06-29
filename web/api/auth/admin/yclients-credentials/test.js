import { proxyToVm, vmOrigin } from '../../../_proxy.js';

export default async function handler(req, res) {
  const incoming = new URL(req.url, `https://${req.headers.host}`);
  const target = new URL('/dashboard/auth/admin/yclients-credentials/test', vmOrigin());
  target.search = incoming.search;
  await proxyToVm(req, res, target);
}
