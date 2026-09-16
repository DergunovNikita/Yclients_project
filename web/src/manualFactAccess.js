// Who may open the reviews / additional-OPZ tabs, and whether they report only for themselves.
// The server decides (`manual_fact_scope` on /auth/me); this module only reads the answer.

export function manualFactScope(user, { hasApiKey = false } = {}) {
  if (!user) return hasApiKey ? 'branch' : 'none';
  const scope = user.manual_fact_scope;
  return scope === 'branch' || scope === 'self' ? scope : 'none';
}

export function canEnterManualFacts(user, options) {
  return manualFactScope(user, options) !== 'none';
}

export function entersManualFactsForSelf(user, options) {
  return manualFactScope(user, options) === 'self';
}
