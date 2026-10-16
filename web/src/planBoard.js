// Plan/fact "Планы" mode: the same payload turned the way the owner keeps plans in a spreadsheet —
// one column per person (plus a total), one row per metric, plan values only.

const BARBER_ROWS = [
  'revenue', 'clients', 'avg_check_total', 'wax_qty', 'head_care_qty', 'face_care_qty',
  'camouflage_qty', 'cosmo_qty', 'cosmo_sum', 'opz_pct', 'opz_qty', 'extra_services_qty', 'extra_services_pct',
];
const ADMIN_ROWS = [
  'reviews_qty', 'opz_qty', 'opz_pct', 'cosmo_sum', 'cosmo_qty', 'clients', 'extra_services_qty', 'extra_services_pct',
];
const NETWORK_ROWS = [...BARBER_ROWS, 'reviews_qty'];
// Administrators enter their plans directly; only OPZ is derived (clients x branch OPZ %), so only it has a rule.
const ADMIN_RULE_CODES = ['opz_qty'];
// An aggregate column's average check (a branch or network total; every network column is one) is planned revenue /
// planned visits, not an average of the people's checks.
const WEIGHTED_TOTAL_CODE = 'avg_check_total';

function planOf(group, code) {
  // The calculation scope is ignored on purpose: an administrator's records-scope plan is still a plan.
  const plan = (group?.metrics || []).find((cell) => cell.code === code)?.plan;
  return plan === undefined ? null : plan;
}

function section(key, groups, codes, metricsByCode, { rules = null, ruleCodes = null, allAggregate = false } = {}) {
  const rows = codes
    .filter((code) => metricsByCode.has(code))
    .map((code) => ({
      code,
      metric: metricsByCode.get(code),
      rule: !ruleCodes || ruleCodes.includes(code) ? rules?.[code] ?? null : null,
      values: groups.map(({ group }) => planOf(group, code)),
    }))
    .filter((row) => row.values.some((value) => value !== null));
  const keep = groups.map((_column, index) => index).filter((index) => rows.some((row) => row.values[index] !== null));
  if (!rows.length || !keep.length) return null;
  const columns = keep.map((index) => ({ title: groups[index].title, isTotal: groups[index].isTotal }));
  const shownRows = rows.map((row) => {
    const values = keep.map((index) => row.values[index]);
    return {
      ...row,
      values,
      weighted: values.map(
        (value, index) => value !== null && (allAggregate || columns[index].isTotal) && row.code === WEIGHTED_TOTAL_CODE,
      ),
    };
  });
  return {
    key,
    columns,
    rows: shownRows,
    showRule: shownRows.some((row) => row.rule !== null),
    hasWeighted: shownRows.some((row) => row.weighted.some(Boolean)),
  };
}

function column(group, isTotal = false) {
  return { group, title: group.title, isTotal };
}

/**
 * Build the sections of the plan board.
 *
 * @param {object} payload — a (visibility-filtered) plan/fact payload.
 * @returns {{sections: object[]}} Sections with `columns`, `rows` (values per column, null = no plan; `weighted`
 *   marks the weighted average-check cells), `showRule` and `hasWeighted`.
 */
export function planBoard(payload) {
  const metricsByCode = new Map((payload?.metrics || []).map((metric) => [metric.code, metric]));
  const groups = payload?.groups || [];
  const sections = [];
  if (payload?.view_scope === 'staff') {
    const byCategory = (category) => groups
      .filter((group) => (group.category || 'unknown') === category)
      .map((group) => column(group));
    const barbers = byCategory('barber');
    // One chosen person is shown alone, as in the table mode: the branch total would be a second column.
    if (payload.parent_group && !payload.selected_staff_plan) {
      barbers.push(column(payload.parent_group, true));
    }
    sections.push(
      section('barber', barbers, BARBER_ROWS, metricsByCode, { rules: payload.plan_rules }),
      section('administrator', byCategory('administrator'), ADMIN_ROWS, metricsByCode, {
        rules: payload.plan_rules,
        ruleCodes: ADMIN_RULE_CODES,
      }),
      section('unknown', byCategory('unknown'), BARBER_ROWS, metricsByCode),
    );
  } else {
    const isNetwork = (group) => group.scope === 'network';
    const ordered = [...groups.filter((group) => !isNetwork(group)), ...groups.filter(isNetwork)];
    sections.push(section(
      'network',
      ordered.map((group) => column(group, isNetwork(group))),
      NETWORK_ROWS,
      metricsByCode,
      { allAggregate: true },
    ));
  }
  return { sections: sections.filter(Boolean) };
}
