// The API routes the same-origin nginx lets through, and the one place to add a new one.
//
// scripts/nginx-allowlist.mjs renders this table as deploy/nginx/api-allowlist.conf. A route missing here answers
// 404 on the deployed site, whatever the API itself serves. After editing:
//
//   node scripts/nginx-allowlist.mjs > deploy/nginx/api-allowlist.conf
//
// and install the file on the VM (private runbook). web/tests/nginx-allowlist.test.mjs fails on a stale file.

const AUTH_ROUTE_RULES = [
  { methods: ['POST'], pattern: /^(login|demo-login|register|refresh|logout|logout-all|verify-email|forgot-password|reset-password|change-password)$/ },
  { methods: ['GET'], pattern: /^(me|sessions|portal-accounts|admin\/meta|admin\/users|admin\/yclients-credentials|admin\/initial-passwords)$/ },
  { methods: ['DELETE'], pattern: /^sessions\/[^/]+$/ },
  { methods: ['POST'], pattern: /^(admin\/users|admin\/provision-accounts|admin\/distribute-credentials|admin\/yclients-credentials|admin\/yclients-credentials\/test)$/ },
  { methods: ['PATCH', 'DELETE'], pattern: /^admin\/users\/[^/]+$/ },
  { methods: ['POST'], pattern: /^admin\/staff\/[^/]+\/create-account$/ },
  { methods: ['PATCH', 'DELETE'], pattern: /^admin\/yclients-credentials\/[^/]+$/ },
  { methods: ['POST'], pattern: /^admin\/yclients-credentials\/[^/]+\/test$/ },
];

const DASHBOARD_ROUTE_RULES = [
  { methods: ['GET'], pattern: /^(branches|staff|services|reports|reports\/data|widget\/sync_status|widget\/plan_fact|plan\/settings|plan\/reviews_fact|plan\/opz_fact|payments\/yandex_pay|bundle)$/ },
  { methods: ['GET', 'PUT'], pattern: /^(metric-visibility|branch-order)$/ },
  { methods: ['POST'], pattern: /^(plan\/settings|plan\/reviews_fact|plan\/opz_fact|payments\/yandex_pay)$/ },
  { methods: ['PATCH'], pattern: /^services$/ },
];

const ONBOARDING_ROUTE_RULES = [
  { methods: ['GET'], pattern: /^state$/ },
  { methods: ['POST'], pattern: /^(credentials|branches)$/ },
];

// Path below the scope prefix (`/api/<scope>/<path>`, or without `/api`) -> the rules for it.
export const ROUTE_RULES = {
  auth: AUTH_ROUTE_RULES,
  dashboard: DASHBOARD_ROUTE_RULES,
  onboarding: ONBOARDING_ROUTE_RULES,
};

// Where each scope lives on the API (the router prefixes in dashboard_routes.py).
export const SCOPE_TARGET_PREFIX = {
  auth: 'dashboard/auth/',
  dashboard: 'dashboard/',
  onboarding: 'dashboard/onboarding/',
};

// Methods a rule table allows for a path (HEAD rides along with GET). Used by the generator to fold routes into
// the maps and by the test as the oracle the rendered maps are checked against.
export function allowedMethods(scope, path) {
  const methods = new Set();
  for (const rule of ROUTE_RULES[scope] ?? []) {
    if (rule.pattern.test(path)) {
      for (const method of rule.methods) methods.add(method);
    }
  }
  if (methods.has('GET')) methods.add('HEAD');
  return [...methods].sort();
}
