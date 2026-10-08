// Request items for the reviews / additional-OPZ editors. Pure functions only — the page owns
// the DOM, this owns what a typed cell means and what it is compared against.

export function manualFactRowKey(companyId, staffId) {
  return `${companyId}:${staffId}`;
}

function storedValue(row) {
  return row.value === null || row.value === undefined ? null : Number(row.value);
}

/**
 * Items for every rendered row of the editor.
 *
 * `previous_value` is the value the last response carried, not what the cell held when it was
 * drawn: it is what the server compares against to notice that someone else saved in between.
 * An empty cell is `null` ("nothing entered"), never `0`. Returns `{ items }`, or `{ error, row }`
 * for the first cell that is not a non-negative number.
 */
export function buildManualFactItems(rows, rawByKey) {
  const items = [];
  for (const row of rows) {
    const raw = rawByKey.get(manualFactRowKey(row.company_id, row.staff_id));
    if (raw === undefined) continue;
    const text = String(raw).trim().replace(',', '.');
    let value = null;
    if (text !== '') {
      value = Number(text);
      if (!Number.isFinite(value) || value < 0) return { error: 'invalid', row };
    }
    items.push({
      company_id: Number(row.company_id),
      staff_id: Number(row.staff_id),
      value,
      previous_value: storedValue(row),
    });
  }
  return { items };
}

/**
 * The month / branch / staff a save is addressed to: the ones the rows were loaded for. The
 * pickers may already show another month (reload in flight or failed), and `previous_value`
 * only means anything against the month it came from.
 */
export function manualFactSaveScope(loadedFilters, liveFilters) {
  const filters = loadedFilters || liveFilters;
  return {
    month: filters.month,
    company_id: filters.branch ? Number(filters.branch) : null,
    staff_id: filters.staff ? Number(filters.staff) : null,
  };
}
