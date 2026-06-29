import { proxyToVm, vmOrigin } from '../../../../_proxy.js';

export default async function handler(req, res) {
  const incoming = new URL(req.url, `https://${req.headers.host}`);
  const parts = incoming.pathname.split('/').filter(Boolean);
  const credentialId = parts[parts.length - 2];
  const target = new URL(`/auth/admin/yclients-credentials/${encodeURIComponent(credentialId)}/test`, vmOrigin());
  target.search = incoming.search;
  await proxyToVm(req, res, target);
}
