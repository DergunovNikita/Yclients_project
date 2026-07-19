export function rankingRowsForMetric(table, metric) {
  const ranking = table?.ranking;
  if (!ranking) return table?.rows || [];
  return ranking.rows_by_metric?.[metric] || [];
}

export function tableHasRows(table) {
  if ((table?.rows || []).length) return true;
  const byMetric = table?.ranking?.rows_by_metric || {};
  return Object.values(byMetric).some((rows) => (rows || []).length > 0);
}
