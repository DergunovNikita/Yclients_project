// Who may open the Yandex Pay tab (and the payment-forms report). The server decides —
// `branch_payments_access` on /auth/me is the one predicate behind the catalog, the report
// data and the editor — so the browser only reads the answer and never derives it from a role.

export function canViewBranchPayments(user, { hasApiKey = false } = {}) {
  if (!user) return hasApiKey;
  return user.branch_payments_access === true;
}
