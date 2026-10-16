// Plan/fact staff view: barbers and administrators share one table, so a person reads the same
// columns everywhere. Columns are the union of what each category shows; a cell the row's category
// does not carry is left empty rather than coloured. Administrators keep the cells credited to them
// by records (visits, OPZ, extra services) — the block reports that so the page can star it.

const MERGED_CATEGORIES = ['barber', 'administrator', 'unknown'];
const RECORDS_SCOPE = 'administrator_records';

function categoryMetricCodes(category, metricSets, metrics) {
  return new Set((metricSets[category] || metrics).map((metric) => metric.code));
}

/**
 * Split staff rows into category blocks and pick the shared column set.
 *
 * @param {{groups: object[], metrics: object[], metricSets: object}} input — groups are the staff rows
 *   of the plan/fact payload, metrics its (already visibility-filtered) ordered metric list.
 * @returns {{columns: object[], blocks: {category: string, groups: object[], hasRecordsCells: boolean}[]}}
 *   Each block's rows carry `cells` — metric code to cell; a code a row lacks is an empty cell.
 *   `hasRecordsCells` — some row of the block shows a cell credited by records.
 */
export function mergedStaffTable({ groups, metrics, metricSets = {} }) {
  const blocks = [];
  const usedCodes = new Set();
  MERGED_CATEGORIES.forEach((category) => {
    const categoryGroups = groups.filter((group) => (group.category || 'unknown') === category);
    if (!categoryGroups.length) return;
    const allowed = categoryMetricCodes(category, metricSets, metrics);
    const rows = categoryGroups.map((group) => {
      const cells = new Map();
      (group.metrics || []).forEach((cell) => {
        if (allowed.has(cell.code)) cells.set(cell.code, cell);
      });
      return { group, cells };
    });
    if (!rows.some((row) => row.cells.size)) return;
    rows.forEach((row) => row.cells.forEach((_cell, code) => usedCodes.add(code)));
    const hasRecordsCells = rows.some((row) => [...row.cells.values()].some((cell) => cell.calculation_scope === RECORDS_SCOPE));
    blocks.push({ category, groups: rows, hasRecordsCells });
  });
  return { columns: metrics.filter((metric) => usedCodes.has(metric.code)), blocks };
}
