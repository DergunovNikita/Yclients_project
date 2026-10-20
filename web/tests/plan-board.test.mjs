import assert from 'node:assert/strict';
import test from 'node:test';

import { planBoard } from '../src/planBoard.js';

const ALL_CODES = [
  'revenue', 'clients', 'avg_check_total', 'wax_qty', 'head_care_qty', 'face_care_qty', 'camouflage_qty',
  'cosmo_qty', 'cosmo_sum', 'opz_pct', 'opz_qty', 'reviews_qty', 'extra_services_qty', 'extra_services_pct',
];
const metrics = ALL_CODES.map((code) => ({ code, label: code, format: 'number' }));
const cells = (plans, scope = 'personal') => Object.entries(plans).map(([code, plan]) => ({
  code, plan, fact: 1, calculation_scope: scope,
}));
const person = (staffId, category, plans, scope) => ({
  title: `P${staffId}`, category, staff_id: staffId, metrics: cells(plans, scope),
});
const codes = (section) => section.rows.map((row) => row.code);
const titles = (section) => section.columns.map((column) => column.title);

const branchPayload = (overrides = {}) => ({
  view_scope: 'staff',
  metrics,
  groups: [
    person(1, 'barber', { opz_qty: 3, revenue: 100, wax_qty: 5, clients: 40, extra_services_qty: 9, extra_services_pct: 20 }),
    person(2, 'barber', { revenue: 200, clients: 50 }),
    person(
      3,
      'administrator',
      { opz_pct: 20, opz_qty: 60, reviews_qty: 4, clients: 300, extra_services_qty: 7 },
      'administrator_records',
    ),
  ],
  parent_group: {
    title: 'Филиал',
    metrics: cells({ revenue: 300, clients: 90, wax_qty: 5, avg_check_total: 3333, extra_services_qty: 9 }),
  },
  plan_rules: { wax_qty: 30, opz_qty: 30 },
  ...overrides,
});

test('barbers and administrators split, rows in the spreadsheet order, total last', () => {
  const { sections } = planBoard(branchPayload());
  assert.deepEqual(sections.map((section) => section.key), ['barber', 'administrator']);
  const [barbers, admins] = sections;
  assert.deepEqual(titles(barbers), ['P1', 'P2', 'Филиал']);
  assert.deepEqual(barbers.columns.map((column) => column.isTotal), [false, false, true]);
  assert.deepEqual(
    codes(barbers),
    ['revenue', 'clients', 'avg_check_total', 'wax_qty', 'opz_qty', 'extra_services_qty', 'extra_services_pct'],
  );
  assert.deepEqual(barbers.rows[0].values, [100, 200, 300]);
  assert.deepEqual(barbers.rows.find((row) => row.code === 'wax_qty').values, [5, null, 5]);
  assert.deepEqual(titles(admins), ['P3']);
  assert.deepEqual(codes(admins), ['reviews_qty', 'opz_qty', 'opz_pct', 'clients', 'extra_services_qty']);
});

test('administrator_records plans still show', () => {
  const [, admins] = planBoard(branchPayload()).sections;
  assert.deepEqual(admins.rows.find((row) => row.code === 'clients').values, [300]);
});

test('a personal role has no total column', () => {
  const { sections } = planBoard(branchPayload({ parent_group: undefined }));
  assert.deepEqual(titles(sections[0]), ['P1', 'P2']);
  assert.ok(sections[0].columns.every((column) => !column.isTotal));
});

const ruleByCode = (section) => Object.fromEntries(section.rows.map((row) => [row.code, row.rule]));

test('barbers get every rule, administrators only the OPZ one', () => {
  const [barbers, admins] = planBoard(branchPayload()).sections;
  assert.deepEqual(
    ruleByCode(barbers),
    {
      revenue: null, clients: null, avg_check_total: null, wax_qty: 30, opz_qty: 30,
      extra_services_qty: null, extra_services_pct: null,
    },
  );
  assert.equal(barbers.showRule, true);
  assert.deepEqual(
    ruleByCode(admins),
    { reviews_qty: null, opz_qty: 30, opz_pct: null, clients: null, extra_services_qty: null },
  );
  assert.equal(admins.showRule, true);
});

test('an administrator section has no rule column without an OPZ rule, and other rules never leak into it', () => {
  const [, admins] = planBoard(branchPayload({ plan_rules: { wax_qty: 30, clients: 5 } })).sections;
  assert.equal(admins.showRule, false);
  assert.ok(admins.rows.every((row) => row.rule === null));
});

test('absent or null plan_rules leave every rule empty', () => {
  [undefined, null, {}].forEach((rules) => {
    const { sections } = planBoard(branchPayload({ plan_rules: rules }));
    sections.forEach((section) => {
      assert.equal(section.showRule, false);
      assert.ok(section.rows.every((row) => row.rule === null));
    });
  });
});

test('empty columns, rows and sections are dropped', () => {
  const payload = branchPayload({
    groups: [
      person(1, 'barber', { revenue: 100 }),
      person(2, 'barber', { revenue: null, clients: null }),
      person(3, 'administrator', { opz_pct: null }),
    ],
    parent_group: { title: 'Филиал', metrics: cells({ revenue: 100 }) },
  });
  const { sections } = planBoard(payload);
  assert.deepEqual(sections.map((section) => section.key), ['barber']);
  assert.deepEqual(titles(sections[0]), ['P1', 'Филиал']);
  assert.deepEqual(codes(sections[0]), ['revenue']);
});

test('metrics missing from the payload list (hidden) or stripped from the cells vanish', () => {
  const hidden = branchPayload({ metrics: metrics.filter((metric) => metric.code !== 'revenue') });
  assert.ok(!codes(planBoard(hidden).sections[0]).includes('revenue'));
  const stripped = branchPayload({
    groups: [{ title: 'P1', category: 'barber', metrics: cells({ clients: 40 }) }],
    parent_group: undefined,
  });
  assert.deepEqual(codes(planBoard(stripped).sections[0]), ['clients']);
});

test('uncategorised staff get their own section with barber rows and no total', () => {
  const { sections } = planBoard(branchPayload({ groups: [{ title: 'X', metrics: cells({ clients: 5 }) }] }));
  const unknown = sections.find((section) => section.key === 'unknown');
  assert.deepEqual(titles(unknown), ['X']);
  assert.deepEqual(codes(unknown), ['clients']);
});

test('network: branches in payload order, the network row last as the total', () => {
  const payload = {
    metrics,
    groups: [
      { title: 'Сеть', scope: 'network', company_id: null, metrics: cells({ revenue: 300, reviews_qty: 6 }) },
      { title: 'A', scope: 'branch', company_id: 1, metrics: cells({ revenue: 100, reviews_qty: 2 }) },
      { title: 'B', scope: 'branch', company_id: 2, metrics: cells({ revenue: 200, reviews_qty: 4 }) },
    ],
  };
  const { sections } = planBoard(payload);
  assert.equal(sections.length, 1);
  assert.equal(sections[0].key, 'network');
  assert.deepEqual(titles(sections[0]), ['A', 'B', 'Сеть']);
  assert.deepEqual(sections[0].columns.map((column) => column.isTotal), [false, false, true]);
  assert.deepEqual(codes(sections[0]), ['revenue', 'reviews_qty']);
  assert.equal(sections[0].showRule, false);
});

test('extra services show for barbers and, in the barber position, for the network', () => {
  const [barbers] = planBoard(branchPayload()).sections;
  assert.deepEqual(barbers.rows.find((row) => row.code === 'extra_services_qty').values, [9, null, 9]);
  assert.deepEqual(barbers.rows.find((row) => row.code === 'extra_services_pct').values, [20, null, null]);
  const network = planBoard({
    metrics,
    groups: [
      { title: 'A', scope: 'branch', metrics: cells({ opz_qty: 1, extra_services_qty: 4, extra_services_pct: 10, reviews_qty: 2 }) },
      { title: 'Сеть', scope: 'network', metrics: cells({ opz_qty: 1, extra_services_qty: 4, extra_services_pct: 10, reviews_qty: 2 }) },
    ],
  }).sections[0];
  assert.deepEqual(codes(network), ['opz_qty', 'extra_services_qty', 'extra_services_pct', 'reviews_qty']);
});

const weightedCells = (section) => section.rows.flatMap((row) => row.weighted.map((flag, index) => (
  flag ? [row.code, section.columns[index].title] : null
))).filter(Boolean);

test('only the avg_check_total cell of a total column is flagged as weighted', () => {
  const [barbers, admins] = planBoard(branchPayload({
    groups: [
      person(1, 'barber', { revenue: 100, avg_check_total: 2500 }),
      person(3, 'administrator', { avg_check_total: 1, clients: 3 }, 'administrator_records'),
    ],
  })).sections;
  assert.deepEqual(weightedCells(barbers), [['avg_check_total', 'Филиал']]);
  assert.equal(barbers.hasWeighted, true);
  assert.equal(admins.hasWeighted, false);
  assert.ok(barbers.rows.every((row) => row.weighted.length === row.values.length));
});

test('every network column is an aggregate, so each avg_check_total there is weighted', () => {
  const group = (title, scope) => ({ title, scope, metrics: cells({ revenue: 5, avg_check_total: 2000 }) });
  const [section] = planBoard({ metrics, groups: [group('Сеть', 'network'), group('A', 'branch')] }).sections;
  assert.equal(section.hasWeighted, true);
  assert.deepEqual(weightedCells(section), [['avg_check_total', 'A'], ['avg_check_total', 'Сеть']]);
});

test('no weighted marker without a total column or without a value in it', () => {
  const personal = planBoard(branchPayload({
    groups: [person(1, 'barber', { revenue: 100, avg_check_total: 2500 })],
    parent_group: undefined,
    selected_staff_plan: { title: 'P1', metrics: [] },
  })).sections[0];
  assert.equal(personal.hasWeighted, false);
  const emptyTotal = planBoard(branchPayload({
    groups: [person(1, 'barber', { revenue: 100, avg_check_total: 2500 })],
    parent_group: { title: 'Филиал', metrics: cells({ revenue: 100, avg_check_total: null }) },
  })).sections[0];
  assert.equal(emptyTotal.hasWeighted, false);
});

test('an empty payload gives no sections', () => {
  assert.deepEqual(planBoard(null), { sections: [] });
  assert.deepEqual(planBoard({ metrics, groups: [] }), { sections: [] });
});

test('a personal role (one clamped person, selected_staff_plan present) still gets a board with rules', () => {
  const payload = branchPayload({
    groups: [person(1, 'barber', { revenue: 100, wax_qty: 5 })],
    parent_group: undefined,
    selected_staff_plan: { title: 'P1', metrics: [] },
  });
  const { sections } = planBoard(payload);
  assert.deepEqual(sections.map((section) => section.key), ['barber']);
  assert.deepEqual(titles(sections[0]), ['P1']);
  assert.equal(sections[0].showRule, true);
  assert.equal(sections[0].rows.find((row) => row.code === 'wax_qty').rule, 30);
});

test('a chosen person is shown alone, without the branch total', () => {
  const payload = branchPayload({
    groups: [person(1, 'barber', { revenue: 100, wax_qty: 5 })],
    selected_staff_plan: { title: 'P1', metrics: [] },
  });
  const { sections } = planBoard(payload);
  assert.deepEqual(titles(sections[0]), ['P1']);
  assert.ok(sections[0].columns.every((column) => !column.isTotal));
});

test('a plan of 0 is a value, not an empty cell', () => {
  const { sections } = planBoard(branchPayload({
    groups: [person(1, 'barber', { revenue: 0, clients: null })],
    parent_group: undefined,
  }));
  assert.deepEqual(codes(sections[0]), ['revenue']);
  assert.deepEqual(sections[0].rows[0].values, [0]);
});

test('a level payload of one branch has people columns and no total', () => {
  const { sections } = planBoard(branchPayload({ parent_group: undefined, level_filter: { position_ids: [3] } }));
  const [barbers] = sections;
  assert.deepEqual(titles(barbers), ['P1', 'P2']);
  assert.deepEqual(barbers.columns.map((column) => column.isTotal), [false, false]);
  assert.deepEqual(barbers.columns.map((column) => column.branch), [null, null]);
  assert.equal(barbers.hasWeighted, false);
});

test('a network level payload lists people of every branch and names each one\'s branch', () => {
  const at = (staffId, companyId, title, plans) => ({
    ...person(staffId, 'barber', plans),
    company_id: companyId,
    company_title: title,
  });
  const payload = {
    view_scope: 'staff',
    metrics,
    groups: [
      at(1, 10, 'Арбат', { revenue: 100, clients: 40 }),
      at(2, 20, 'Тверская', { revenue: 200, clients: 50 }),
    ],
    plan_rules: null,
    level_filter: { position_ids: [3, 4] },
  };
  const { sections } = planBoard(payload);
  assert.deepEqual(sections.map((section) => section.key), ['barber']);
  const [barbers] = sections;
  assert.deepEqual(titles(barbers), ['P1', 'P2']);
  assert.deepEqual(barbers.columns.map((column) => column.branch), ['Арбат', 'Тверская']);
  assert.equal(barbers.showRule, false);
  assert.deepEqual(barbers.rows.find((row) => row.code === 'revenue').values, [100, 200]);
});
