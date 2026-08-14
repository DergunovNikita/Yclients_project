export const BRANCH_PLAN_SETTING_FIELDS = [
  'wax_pct',
  'head_care_pct',
  'face_care_pct',
  'camouflage_pct',
  'cosmo_pct',
  'opz_pct',
  'cosmo_price',
];

export const STAFF_PLAN_SETTING_FIELDS_BY_CATEGORY = {
  barber: ['clients', 'avg_check_total'],
  administrator: ['clients', 'reviews_qty', 'cosmo_qty'],
};

export const STAFF_PLAN_SETTING_FIELDS = [
  ...new Set(Object.values(STAFF_PLAN_SETTING_FIELDS_BY_CATEGORY).flat()),
];

function valuesFor(fields, scope, row, readValue) {
  return Object.fromEntries(fields.map((field) => [field, readValue(scope, row, field)]));
}

export function buildPlanSettingsPayload(data, month, readValue) {
  const branches = (data?.branches || []).map((row) => ({
    company_id: Number(row.company_id),
    ...valuesFor(BRANCH_PLAN_SETTING_FIELDS, 'branch', row, readValue),
  }));
  const staff = (data?.staff || []).map((row) => ({
    company_id: Number(row.company_id),
    staff_id: Number(row.staff_id),
    staff_category: row.staff_category,
    ...valuesFor(STAFF_PLAN_SETTING_FIELDS, 'staff', row, readValue),
  }));
  return { month, branches, staff };
}
