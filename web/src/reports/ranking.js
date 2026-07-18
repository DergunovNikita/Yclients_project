export function rankingRowsForMetric(table, metric) {
  const ranking = table?.ranking;
  if (!ranking) return table?.rows || [];
  return ranking.rows_by_metric?.[metric] || [];
}
