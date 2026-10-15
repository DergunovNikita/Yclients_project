// Plan/fact staff view: barbers and administrators' personal KPIs share one table.
// Columns are the union of what each category shows; a cell the row's category does not
// carry (or whose calculation scope is not personal) is left empty rather than coloured.

const MERGED_CATEGORIES = ['barber', 'administrator', 'unknown'];

function isPersonal(cell) {
  return (cell?.calculation_scope || 'personal') === 'personal';
}

function categoryMetricCodes(category, metricSets, metrics) {
  return new Set((metricSets[category] || metrics).map((metric) => metric.code));
}

/**
 * Split staff rows into category blocks and pick the shared column set.
 *
 * @param {{groups: object[], metrics: object[], metricSets: object}} input — groups are the staff rows
 *   of the plan/fact payload, metrics its (already visibility-filtered) ordered metric list.
 * @returns {{columns: object[], blocks: {category: string, groups: object[]}[]}}
 *   Each block's rows carry `cells` — metric code to cell (or null for an empty cell).
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
        if (allowed.has(cell.code) && isPersonal(cell)) cells.set(cell.code, cell);
      });
      return { group, cells };
    });
    // An administrator whose KPIs are all credited by records has nothing to show here.
    if (!rows.some((row) => row.cells.size)) return;
    rows.forEach((row) => row.cells.forEach((_cell, code) => usedCodes.add(code)));
    blocks.push({ category, groups: rows });
  });
  return { columns: metrics.filter((metric) => usedCodes.has(metric.code)), blocks };
}
