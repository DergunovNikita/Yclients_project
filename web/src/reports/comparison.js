const DELTA_FORMATS = new Set(['money', 'number', 'decimal', 'percent']);
function isNumber(value) {
  return value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
}

/**
 * Pairs every row of `table` with its row in the comparison window.
 *
 * Returns null when the table cannot be compared: no `row_key`, or no comparison table. An
 * unmatched row maps to undefined, so the cell shows a dash instead of a delta against nothing.
 */
export function matchComparisonRows(table, comparisonTable) {
  const rowKey = table?.row_key;
  if (!rowKey || !comparisonTable) return null;
  const previousRows = comparisonTable.rows || [];
  // Tables of consecutive periods (`x_kind: 'time'`) have different period labels in every window,
  // so their rows are paired by position, the way time charts are.
  if (table.x_kind === 'time') {
    return (rows) => rows.map((_, index) => previousRows[index]);
  }
  const byKey = new Map(previousRows.map((row) => [String(row[rowKey]), row]));
  return (rows) => rows.map((row) => byKey.get(String(row[rowKey])));
}

/**
 * Delta of one cell against the same cell of the previous row, or null when the cell has no value.
 *
 * Money, counts and decimals change in percent of the base; a share changes in percentage points,
 * since a percent of a percent reads as a second, different metric. A missing or zero base has no
 * percent change (`text` is a dash); a zero share is a real base for points.
 */
export function cellDelta(column, row, previousRow) {
  if (!DELTA_FORMATS.has(column.format) || !isNumber(row?.[column.key])) return null;
  const current = Number(row[column.key]);
  const previous = previousRow?.[column.key];
  if (!isNumber(previous)) return { value: null, format: column.format };
  const base = Number(previous);
  if (column.format === 'percent') return { value: current - base, format: 'percent' };
  if (base === 0) return { value: null, format: column.format };
  return { value: (100 * (current - base)) / Math.abs(base), format: column.format };
}

/** Rows of a table paired with their previous-window rows, ready for the renderer. */
export function pairedRows(table, rows, comparisonTable) {
  const match = matchComparisonRows(table, comparisonTable);
  const previous = match ? match(rows) : null;
  return rows.map((row, index) => ({ row, previous: previous ? previous[index] : undefined, compared: Boolean(previous) }));
}
