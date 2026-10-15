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
  administrator: [
    'clients',
    'reviews_qty',
    'cosmo_qty',
    'extra_services_qty',
    'extra_services_pct',
  ],
};

export const STAFF_PLAN_SETTING_FIELDS = [
  ...new Set(Object.values(STAFF_PLAN_SETTING_FIELDS_BY_CATEGORY).flat()),
];

export function isScheduleAttributionDiagnostic(code) {
  return code === 'staff_schedule_coverage' || code === 'appointment_timestamp_coverage';
}

/**
 * `readValue` for `buildPlanSettingsPayload`, over the plan inputs currently on the page.
 *
 * The inputs are indexed once: the payload asks for every field of every row on each keystroke, and a
 * selector scan of the whole document per field made that rows x fields scans. Where an id repeats the
 * first input in document order wins, as it did with `querySelector`. An empty cell is `null`.
 */
export function planSettingsInputReader(inputs) {
  const values = new Map();
  for (const input of inputs) {
    const { planBranch, planStaff, companyId, staffId, field } = input.dataset;
    const scope = planBranch !== undefined ? 'branch' : planStaff !== undefined ? 'staff' : null;
    if (!scope) continue;
    const key = `${scope}:${scope === 'branch' ? companyId : staffId}:${field}`;
    if (values.has(key)) continue;
    const value = (input.value || '').trim();
    values.set(key, value === '' ? null : value);
  }
  return (scope, row, field) => (
    values.get(`${scope}:${scope === 'branch' ? row.company_id : row.staff_id}:${field}`) ?? null
  );
}

function valuesFor(fields, scope, row, readValue) {
  return Object.fromEntries(fields.map((field) => [field, readValue(scope, row, field)]));
}

// The month is the one the rows were loaded for, never the picker: it moves before the rows do.
export function buildPlanSettingsPayload(data, readValue) {
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
  return { month: data?.month, branches, staff };
}

function planInputKey({ planBranch, companyId, staffId, field }) {
  return planBranch !== undefined ? `branch:${companyId}:${field}` : `staff:${companyId}:${staffId}:${field}`;
}

const isPlanInput = (input) => input.dataset.planBranch !== undefined || input.dataset.planStaff !== undefined;

/**
 * What the person typed since the tables were drawn: `{ cellKey: value }` for every plan input whose text differs
 * from the one the server rendered (`defaultValue`, which typing does not touch). Empty when nothing changed.
 */
export function planSettingsTypedCells(inputs) {
  const cells = {};
  for (const input of inputs) {
    if (!isPlanInput(input)) continue;
    const value = (input.value || '').trim();
    if (value !== (input.defaultValue || '').trim()) cells[planInputKey(input.dataset)] = value;
  }
  return cells;
}

/** Puts drafted cells back into the inputs that are on the page; a cell with no input (row gone) is skipped. */
export function applyPlanSettingsCells(inputs, cells) {
  let applied = 0;
  for (const input of inputs) {
    if (!isPlanInput(input)) continue;
    const key = planInputKey(input.dataset);
    if (!Object.hasOwn(cells, key)) continue;
    input.value = cells[key];
    applied += 1;
  }
  return applied;
}
