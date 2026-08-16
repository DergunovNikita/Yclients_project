function metricCodeSet(metrics = []) {
  return new Set(metrics.map((metric) => metric?.code).filter(Boolean));
}

export function normalizeHiddenPlanMetricCodes(metrics = [], hiddenCodes = []) {
  const availableCodes = metricCodeSet(metrics);
  const normalized = new Set([...hiddenCodes].filter((code) => availableCodes.has(code)));
  if (availableCodes.size && normalized.size === availableCodes.size) {
    normalized.delete(metrics.find((metric) => availableCodes.has(metric?.code))?.code);
  }
  return normalized;
}

export function visiblePlanMetrics(metrics = [], hiddenCodes = []) {
  const hidden = normalizeHiddenPlanMetricCodes(metrics, hiddenCodes);
  return metrics.filter((metric) => !hidden.has(metric?.code));
}

export function moneyPlanMetricCodes(metrics = []) {
  return new Set(
    metrics
      .filter((metric) => metric?.code && metric.format === 'money')
      .map((metric) => metric.code),
  );
}

export function setPlanMetricHidden(metrics = [], hiddenCodes = [], code, hidden) {
  const availableCodes = metricCodeSet(metrics);
  const next = normalizeHiddenPlanMetricCodes(metrics, hiddenCodes);
  if (!availableCodes.has(code)) return next;

  if (!hidden) {
    next.delete(code);
    return next;
  }
  if (!next.has(code) && availableCodes.size - next.size <= 1) return next;
  next.add(code);
  return next;
}

export function hideMoneyPlanMetrics(metrics = [], hiddenCodes = []) {
  const availableCodes = metricCodeSet(metrics);
  const moneyCodes = moneyPlanMetricCodes(metrics);
  const next = normalizeHiddenPlanMetricCodes(metrics, hiddenCodes);
  const previouslyVisibleCodes = new Set([...availableCodes].filter((code) => !next.has(code)));
  moneyCodes.forEach((code) => next.add(code));

  if (availableCodes.size && next.size === availableCodes.size) {
    const fallbackMetric = metrics.find(
      (metric) => metric?.code && metric.format !== 'money',
    ) || metrics.find((metric) => previouslyVisibleCodes.has(metric?.code));
    next.delete(fallbackMetric?.code);
  }
  return next;
}

function filterMetricItems(items, hiddenCodes) {
  if (!Array.isArray(items)) return items;
  return items.filter((item) => !hiddenCodes.has(item?.code));
}

function filterMetricGroup(group, hiddenCodes) {
  if (!group) return group;
  return {
    ...group,
    metrics: filterMetricItems(group.metrics, hiddenCodes),
  };
}

export function filterPlanFactForDisplay(planFact, hiddenCodes = []) {
  if (!planFact) return planFact;
  const hidden = normalizeHiddenPlanMetricCodes(planFact.metrics, hiddenCodes);
  const metricSets = Object.fromEntries(
    Object.entries(planFact.metric_sets || {}).map(([key, metrics]) => [
      key,
      filterMetricItems(metrics, hidden),
    ]),
  );

  return {
    ...planFact,
    metrics: filterMetricItems(planFact.metrics, hidden),
    metric_sets: metricSets,
    parent_group: filterMetricGroup(planFact.parent_group, hidden),
    groups: (planFact.groups || []).map((group) => filterMetricGroup(group, hidden)),
    selected_staff_plan: planFact.selected_staff_plan
      ? filterMetricGroup(planFact.selected_staff_plan, hidden)
      : planFact.selected_staff_plan,
    goods_kpi_execution: filterMetricItems(planFact.goods_kpi_execution, hidden),
  };
}
