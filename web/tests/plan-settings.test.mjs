import assert from 'node:assert/strict';
import test from 'node:test';

import {
  BRANCH_PLAN_SETTING_FIELDS,
  STAFF_PLAN_SETTING_FIELDS,
  buildPlanSettingsPayload,
} from '../src/planSettings.js';

test('plan settings payload preserves the editable field contract', () => {
  const data = {
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
  ]);
  const readValue = (scope, row, field) => (
    values.get(`${scope}:${scope === 'branch' ? row.company_id : row.staff_id}:${field}`) ?? null
  );

  const payload = buildPlanSettingsPayload(data, '2026-08', readValue);

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
      },
      {
        company_id: 7,
        staff_id: 12,
        staff_category: 'administrator',
        clients: '30',
        avg_check_total: null,
        reviews_qty: '8',
        cosmo_qty: '4',
      },
    ],
  });
});
