import assert from 'node:assert/strict';
import test from 'node:test';

import {
  BRANCH_PLAN_SETTING_FIELDS,
  STAFF_PLAN_SETTING_FIELDS,
  buildPlanSettingsPayload,
  isScheduleAttributionDiagnostic,
  planSettingsInputReader,
} from '../src/planSettings.js';

test('plan settings payload preserves the editable field contract', () => {
  const data = {
    month: '2026-08',
    branches: [{ company_id: '7' }],
    staff: [
      { company_id: '7', staff_id: '11', staff_category: 'barber' },
      { company_id: '7', staff_id: '12', staff_category: 'administrator' },
    ],
  };
  const values = new Map([
    ['branch:7:wax_pct', '12.5'],
    ['staff:11:clients', '40'],
    ['staff:11:avg_check_total', '3000'],
    ['staff:12:clients', '30'],
    ['staff:12:reviews_qty', '8'],
    ['staff:12:cosmo_qty', '4'],
    ['staff:12:extra_services_qty', '12'],
    ['staff:12:extra_services_pct', '40'],
  ]);
  const readValue = (scope, row, field) => (
    values.get(`${scope}:${scope === 'branch' ? row.company_id : row.staff_id}:${field}`) ?? null
  );

  const payload = buildPlanSettingsPayload(data, readValue);

  assert.deepEqual(Object.keys(payload.branches[0]), ['company_id', ...BRANCH_PLAN_SETTING_FIELDS]);
  assert.deepEqual(
    Object.keys(payload.staff[0]),
    ['company_id', 'staff_id', 'staff_category', ...STAFF_PLAN_SETTING_FIELDS],
  );
  assert.deepEqual(payload, {
    month: '2026-08',
    branches: [{
      company_id: 7,
      wax_pct: '12.5',
      head_care_pct: null,
      face_care_pct: null,
      camouflage_pct: null,
      cosmo_pct: null,
      opz_pct: null,
      cosmo_price: null,
    }],
    staff: [
      {
        company_id: 7,
        staff_id: 11,
        staff_category: 'barber',
        clients: '40',
        avg_check_total: '3000',
        reviews_qty: null,
        cosmo_qty: null,
        extra_services_qty: null,
        extra_services_pct: null,
      },
      {
        company_id: 7,
        staff_id: 12,
        staff_category: 'administrator',
        clients: '30',
        avg_check_total: null,
        reviews_qty: '8',
        cosmo_qty: '4',
        extra_services_qty: '12',
        extra_services_pct: '40',
      },
    ],
  });
});

test('the payload is addressed to the month the rows were loaded for', () => {
  // The picker already shows the next month while the table still holds the old one.
  const payload = buildPlanSettingsPayload({ month: '2026-08', branches: [], staff: [] }, () => null);
  assert.equal(payload.month, '2026-08');
});

test('schedule attribution diagnostics share the coverage renderer', () => {
  assert.equal(isScheduleAttributionDiagnostic('staff_schedule_coverage'), true);
  assert.equal(isScheduleAttributionDiagnostic('appointment_timestamp_coverage'), true);
  assert.equal(isScheduleAttributionDiagnostic('admin_barber_clients_mismatch'), false);
});

// The per-cell selector the payload used to run, modelled over a list of inputs in document order.
function legacyRead(inputs, scope, row, field) {
  const match = inputs.find(({ dataset }) => (
    (scope === 'branch' ? dataset.planBranch !== undefined : dataset.planStaff !== undefined)
    && (scope === 'branch'
      ? dataset.companyId === String(row.company_id)
      : dataset.staffId === String(row.staff_id))
    && dataset.field === field
  ));
  const value = match?.value.trim() || '';
  return value === '' ? null : value;
}

test('reading the inputs through one index gives what the per-cell selector gave', () => {
  const input = (scope, ids, field, value) => ({
    dataset: { [scope === 'branch' ? 'planBranch' : 'planStaff']: '', field, ...ids },
    value,
  });
  const inputs = [
    input('branch', { companyId: '7' }, 'wax_pct', ' 12,5 '),
    input('branch', { companyId: '7' }, 'opz_pct', ''),
    input('branch', { companyId: '8' }, 'wax_pct', '3'),
    input('staff', { companyId: '7', staffId: '11' }, 'clients', '40'),
    input('staff', { companyId: '7', staffId: '11' }, 'avg_check_total', '   '),
    input('staff', { companyId: '7', staffId: '12' }, 'reviews_qty', '8'),
    // The same staff id in a second branch row: the first one in the document keeps winning.
    input('staff', { companyId: '9', staffId: '12' }, 'reviews_qty', '99'),
    { dataset: { somethingElse: '' }, value: 'ignored' },
  ];
  const read = planSettingsInputReader(inputs);
  const rows = [
    ['branch', { company_id: 7 }], ['branch', { company_id: '8' }], ['branch', { company_id: 9 }],
    ['staff', { company_id: 7, staff_id: 11 }], ['staff', { staff_id: '12' }], ['staff', { staff_id: 13 }],
  ];
  const fields = [...BRANCH_PLAN_SETTING_FIELDS, ...STAFF_PLAN_SETTING_FIELDS];
  for (const [scope, row] of rows) {
    for (const field of fields) {
      assert.equal(read(scope, row, field), legacyRead(inputs, scope, row, field), `${scope} ${JSON.stringify(row)} ${field}`);
    }
  }
  assert.equal(read('branch', { company_id: 7 }, 'wax_pct'), '12,5');
  assert.equal(read('staff', { staff_id: 12 }, 'reviews_qty'), '8');
  assert.equal(read('staff', { staff_id: 11 }, 'avg_check_total'), null);
});
