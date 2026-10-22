// Card definitions of the Overview sections. Pure: a summary goes in, card objects (and their
// HTML) come out, so the same card shown in two sections cannot drift and node can test it.
import { escapeHtml } from './html.js';
import { intlLocale, t } from './i18n.js';
import { deltaClass, formatDecimal, formatMoney, formatNumber, formatPct } from './reports/format.js';

export function formatMetricValue(value, format) {
  if (value === null || value === undefined) return '—';
  if (format === 'money') return formatMoney(value);
  if (format === 'percent') return `${Number(value).toLocaleString(intlLocale(), { maximumFractionDigits: 2 })}%`;
  return formatNumber(value);
}

/**
 * The delta of a card whose change in percent is missing, which happens exactly when the previous value is 0
 * (or unknown). A current value of 0 has nothing to say about it; a non-zero one grew from nothing, which
 * "+N %" cannot express. `previous`, when the summary carries it, tells a zero from an unknown base.
 */
function deltaWithoutChange({ current, previous }) {
  if (current === null || current === undefined || !Number(current)) return null;
  // `undefined`: the summary does not carry the previous value, a missing percent then means a zero base.
  if (previous !== undefined && Number(previous) !== 0 || previous === null) return null;
  return t('dash.changeFromZero');
}

/**
 * One card. `name` picks the `dash.card<name>` label and the `dash.hint<name>` tooltip.
 *
 * The delta is either a change in percent (`change`, rendered against the previous period and
 * coloured by its sign), "was 0" when the percent is missing because the base was 0 (`current` is the raw
 * value; the card has no delta when it is 0 too), or a fixed `note` with no colour; a card with none shows no delta.
 * An optional `count` is the line between the value and the delta ("22 of 378 visits" under a percent).
 */
export function metricCard(name, options) {
  const { value, present, href, action, count } = options;
  const card = { label: t(`dash.card${name}`), hint: t(`dash.hint${name}`), value };
  if (count) card.count = count;
  // `in`, not `!== undefined`: a change the summary lacks is still a delta ("was 0"), not no delta.
  if ('note' in options) {
    card.delta = options.note;
    card.deltaValue = null;
  } else if ('change' in options) {
    const noChange = options.change === null || options.change === undefined;
    const delta = noChange ? deltaWithoutChange(options) : formatPct(options.change);
    if (delta) {
      card.delta = delta;
      card.deltaValue = noChange ? null : options.change;
    }
  }
  if ('present' in options) card.present = present;
  if (href) {
    card.href = href;
    card.action = action;
  }
  return card;
}

function presentCards(cards) {
  return cards.filter((card) => card.present !== false);
}

export function usesAdministratorSchedule(summary) {
  return summary?.service_attribution?.mode === 'administrator_schedule';
}

let cardHintSeq = 0;

// The hint button sits next to the card, not inside it: a <button> nested in the
// card's <a> would be invalid markup and the delegated [data-report-link] handler
// would open the report on every tap of the hint.
function cardHintHtml(hint, label) {
  if (!hint) return '';
  const id = `card-hint-${++cardHintSeq}`;
  return `
          <button
            type="button"
            class="card__hint"
            data-card-hint
            aria-describedby="${id}"
            aria-label="${escapeHtml(t('dash.hintAria', { label }))}"
          >i</button>
          <span class="card__hint-body" id="${id}" role="tooltip">${escapeHtml(hint)}</span>`;
}

export function cardsHtml(cards) {
  return cards
    .map(
      (card) => {
        const tag = card.href ? 'a' : 'article';
        const actionLabel = card.href ? t('dash.openReportWithLabel', { label: card.label }) : '';
        const attrs = card.href
          ? ` href="${escapeHtml(card.href)}" data-report-link class="card card--link" aria-label="${escapeHtml(actionLabel)}" title="${escapeHtml(actionLabel)}"`
          : ' class="card"';
        // A card that names its column skips auto-placement — the last row of a grid
        // keeps each card under its own column even where the rows above are full.
        const slotStyle = card.column ? ` style="--card-column: ${Number(card.column)}"` : '';
        return `
        <div class="card-slot"${slotStyle}>
          <${tag}${attrs}>
            <div class="label">${escapeHtml(card.label)}</div>
            <div class="value">${escapeHtml(card.value)}</div>${card.count ? `
            <div class="card__count">${escapeHtml(card.count)}</div>` : ''}
            ${card.delta ? `<div class="delta ${deltaClass(card.deltaValue)}">${escapeHtml(card.delta)}</div>` : ''}
            ${card.action ? `<div class="card__action">${escapeHtml(card.action)}</div>` : ''}
          </${tag}>${cardHintHtml(card.hint, card.label)}
        </div>
      `;
      },
    )
    .join('');
}

// "22 pcs of 378 visits" under a share: the numerator and the base the percent was made of.
function opzCountLine(qty, visits) {
  if (qty === null || qty === undefined || visits === null || visits === undefined) return '';
  const form = new Intl.PluralRules(intlLocale()).select(Number(visits)) === 'one' ? 'One' : 'Other';
  return t(`dash.opzOfVisits${form}`, { qty: formatNumber(qty), visits: formatNumber(visits) });
}

function goodsRevenueShare(revenue) {
  const total = Number(revenue.total || 0);
  if (!total) return null;
  return (Number(revenue.goods_revenue || 0) / total) * 100;
}

// Cards that more than one section shows, defined once.
function extraServicesPerAppointmentCard(visitMetrics) {
  return metricCard('ExtraServicesPerAppointment', {
    value: formatMetricValue(visitMetrics.extra_services_per_appointment_pct, 'percent'),
    change: visitMetrics.extra_services_per_appointment_pct_change_pct,
    current: visitMetrics.extra_services_per_appointment_pct,
  });
}

function extraServiceClientsCard(visitMetrics) {
  return metricCard('ExtraServiceClients', {
    value: formatMetricValue(visitMetrics.extra_service_clients_pct, 'percent'),
    note: t('dash.uniqueClientsCount', { count: formatNumber(visitMetrics.extra_service_clients) }),
  });
}

export function kpiCards(summary) {
  const revenueBlock = summary.revenue;
  const averageCheckBlock = summary.average_check;
  const revenue = revenueBlock || {};
  const averageCheck = averageCheckBlock || {};
  const partial = averageCheck.source_status === 'partial';
  return presentCards([
    metricCard('TotalRevenue', {
      value: formatMoney(revenue.total),
      change: revenue.change_pct,
      current: revenue.total,
      present: Boolean(revenueBlock),
    }),
    metricCard('VisitedAppointments', {
      value: formatNumber(revenue.appointments),
      change: revenue.appointments_change_pct,
      current: revenue.appointments,
      present: Boolean(revenueBlock),
    }),
    metricCard(partial ? 'AverageCheckTotalPartial' : 'AverageCheckTotal', {
      value: formatMoney(averageCheck.total),
      change: averageCheck.total_change_pct,
      current: averageCheck.total,
      present: Boolean(averageCheckBlock),
    }),
  ]);
}

export function staffBranchCards(summary) {
  const attribution = summary.service_attribution || {};
  // Extra services are not here: the administrator's own are the ones credited to him (his personal block), and the
  // branch's extras during his shifts would contradict them.
  return presentCards([
    metricCard('BranchShiftAppointments', { value: formatMetricValue(attribution.appointment_count, 'number') }),
    metricCard('BranchShiftClients', { value: formatMetricValue(attribution.unique_client_count, 'number') }),
  ]);
}

export function revenueCards(summary) {
  if (!summary.revenue) return [];
  const revenue = summary.revenue;
  const administratorSchedule = usesAdministratorSchedule(summary);
  return [
    metricCard('ServiceRevenue', {
      value: formatMoney(revenue.service_revenue),
      change: revenue.service_revenue_change_pct,
      current: revenue.service_revenue,
    }),
    metricCard('GoodsRevenue', {
      value: formatMoney(revenue.goods_revenue),
      change: revenue.goods_revenue_change_pct,
      current: revenue.goods_revenue,
    }),
    ...(administratorSchedule ? [] : [metricCard('ExtraServiceRevenue', {
      value: formatMoney(revenue.extra_service_revenue),
      change: revenue.extra_service_revenue_change_pct,
      current: revenue.extra_service_revenue,
    })]),
  ];
}

/** Services section cards and the number of grid columns they need. */
export function servicesCards(summary) {
  const revenueBlock = summary.revenue;
  const averageCheckBlock = summary.average_check;
  const revenue = revenueBlock || {};
  const averageCheck = averageCheckBlock || {};
  const visitMetrics = summary.visit_metrics || {};
  const revenuePresent = Boolean(revenueBlock);
  const averageCheckPresent = Boolean(averageCheckBlock);
  // One column per kind of sale: counts fill the first row, each average check sits
  // right under its own count, so the grid needs as many columns as visible kinds.
  const columns = [
    {
      count: metricCard('ServiceCount', {
        value: formatNumber(revenue.service_count),
        change: revenue.service_count_change_pct,
        current: revenue.service_count,
        present: revenuePresent,
      }),
      average: metricCard('ServiceAverageCheck', {
        value: formatMoney(averageCheck.services),
        change: averageCheck.services_change_pct,
        current: averageCheck.services,
        present: averageCheckPresent,
      }),
    },
    {
      count: metricCard('GoodsCount', {
        value: formatNumber(revenue.goods_count),
        change: revenue.goods_count_change_pct,
        current: revenue.goods_count,
        present: revenuePresent,
      }),
      average: metricCard('GoodsAverageCheck', {
        value: formatMoney(averageCheck.goods),
        change: averageCheck.goods_change_pct,
        current: averageCheck.goods,
        present: averageCheckPresent,
      }),
      share: metricCard('GoodsRevenueShare', {
        value: formatMetricValue(goodsRevenueShare(revenue), 'percent'),
        note: t('dash.ofTotalRevenue'),
        present: revenuePresent,
      }),
    },
    {
      count: metricCard('ExtraServiceCount', {
        value: formatNumber(revenue.extra_service_count),
        change: revenue.extra_service_count_change_pct,
        current: revenue.extra_service_count,
        present: revenuePresent,
      }),
      average: metricCard('ExtraServiceAverageCheck', {
        value: formatMoney(averageCheck.extra_services),
        change: averageCheck.extra_services_change_pct,
        current: averageCheck.extra_services,
        present: averageCheckPresent,
      }),
      // It closes the extra-services column the way the goods share closes the goods one.
      // No `present`: it comes from the visit metrics, not from the revenue block.
      share: extraServicesPerAppointmentCard(visitMetrics),
    },
  ];

  const visibleColumns = usesAdministratorSchedule(summary) ? columns.slice(0, 2) : columns;
  return {
    columnCount: visibleColumns.length,
    cards: [
      ...presentCards(visibleColumns.map((column) => column.count)),
      ...presentCards(visibleColumns.map((column) => column.average)),
      // The share row is shorter than the ones above it, so every card names its column.
      ...presentCards(visibleColumns.flatMap((column, index) => (
        column.share ? [{ ...column.share, column: index + 1 }] : []
      ))),
    ],
  };
}

// The OPZ cards of the Overview: the total, its share, and the share after a first and after a repeat visit.
// `qty` and `pct` are `{ value, change, previous? }` pairs so an administrator's cards can take them from his Plan/fact row
// while everyone else's come from the visit metrics; `qtyCount` is the line under the total.
function opzCards(visitMetrics, { qty, pct, qtyCount }) {
  return [
    metricCard('OpzQty', {
      value: formatNumber(qty.value),
      count: qtyCount,
      change: qty.change,
      current: qty.value,
      previous: qty.previous,
    }),
    metricCard('OpzPct', {
      value: formatMetricValue(pct.value, 'percent'),
      change: pct.change,
      current: pct.value,
      previous: pct.previous,
    }),
    metricCard('OpzFirstVisit', {
      value: formatMetricValue(visitMetrics.opz_first_visit_pct, 'percent'),
      count: opzCountLine(visitMetrics.opz_first_visit_qty, visitMetrics.first_visits),
      change: visitMetrics.opz_first_visit_pct_change_pct,
      current: visitMetrics.opz_first_visit_pct,
      previous: visitMetrics.opz_first_visit_pct_previous,
    }),
    metricCard('OpzRepeatVisit', {
      value: formatMetricValue(visitMetrics.opz_repeat_visit_pct, 'percent'),
      count: opzCountLine(visitMetrics.opz_repeat_visit_qty, visitMetrics.repeat_visits),
      change: visitMetrics.opz_repeat_visit_pct_change_pct,
      current: visitMetrics.opz_repeat_visit_pct,
      previous: visitMetrics.opz_repeat_visit_pct_previous,
    }),
  ];
}

export function visitCards(summary) {
  const visitMetrics = summary.visit_metrics || {};
  return opzCards(visitMetrics, {
    qty: { value: visitMetrics.opz_qty, change: visitMetrics.opz_qty_change_pct },
    pct: { value: visitMetrics.opz_pct, change: visitMetrics.opz_pct_change_pct },
  });
}

/**
 * The selected administrator's own cards, in the order of his Plan/fact row, from `administrator_personal`
 * (his Plan/fact facts for the window) and the OPZ split of the visit metrics. A money metric the role may not
 * see is absent from the block and so from the cards.
 */
export function administratorCards(summary) {
  const personal = summary.administrator_personal;
  if (!personal) return { cards: [], columnCount: 4 };
  const visitMetrics = summary.visit_metrics || {};
  const metrics = personal.metrics || {};
  const pair = (code) => ({
    value: metrics[code]?.value,
    change: metrics[code]?.change_pct,
    previous: metrics[code]?.previous_value,
  });
  const metricCardFor = (name, code, format) => metricCard(name, {
    value: formatMetricValue(metrics[code]?.value, format),
    change: metrics[code]?.change_pct,
    current: metrics[code]?.value,
    previous: metrics[code]?.previous_value,
    present: code in metrics,
  });
  const manual = Number(personal.opz_manual_qty || 0);
  const [opzQty, opzPct, opzFirst, opzRepeat] = opzCards(visitMetrics, {
    qty: pair('opz_qty'),
    pct: pair('opz_pct'),
    qtyCount: manual > 0 ? t('dash.opzManualNote', { count: formatNumber(manual) }) : '',
  });
  const cards = presentCards([
    metricCardFor('AdminClients', 'clients', 'number'),
    metricCardFor('AdminCosmoQty', 'cosmo_qty', 'number'),
    metricCardFor('AdminCosmoSum', 'cosmo_sum', 'money'),
    opzQty,
    opzPct,
    opzFirst,
    opzRepeat,
    metricCardFor('AdminExtraServices', 'extra_services_qty', 'number'),
    metricCardFor('AdminExtraServicesPct', 'extra_services_pct', 'percent'),
    metricCardFor('AdminReviews', 'reviews_qty', 'number'),
  ]);
  // Two even rows when there are many cards (ten make two of five); the usual four fit the shorter ones.
  return { cards, columnCount: cards.length >= 9 ? Math.ceil(cards.length / 2) : 4 };
}

/** `reportUrl(reportId)` builds the link of the cards that open a report. */
export function clientsCards(summary, reportUrl) {
  const visitMetrics = summary.visit_metrics || {};
  const clientFrequency = visitMetrics.client_visit_frequency || {};
  const oneVisit = clientFrequency.one_visit || {};
  const twoToThreeVisits = clientFrequency.two_to_three_visits || {};
  const fourPlusVisits = clientFrequency.four_plus_visits || {};
  const reportLink = { href: reportUrl('new_vs_returning_cross'), action: t('dash.openReport') };
  const frequencyNote = (bucket) => t('dash.ofClients', { value: formatMetricValue(bucket.pct, 'percent') });
  const administratorSchedule = usesAdministratorSchedule(summary);
  return [
    metricCard('UniqueClients', {
      value: formatNumber(visitMetrics.unique_clients),
      change: visitMetrics.unique_clients_change_pct,
      current: visitMetrics.unique_clients,
    }),
    metricCard('VisitsPerClient', {
      value: formatDecimal(visitMetrics.visits_per_client),
      change: visitMetrics.visits_per_client_change_pct,
      current: visitMetrics.visits_per_client,
    }),
    ...(administratorSchedule ? [] : [extraServiceClientsCard(visitMetrics)]),
    metricCard('OneVisitClients', { value: formatNumber(oneVisit.count), note: frequencyNote(oneVisit) }),
    metricCard('TwoThreeVisitClients', {
      value: formatNumber(twoToThreeVisits.count),
      note: frequencyNote(twoToThreeVisits),
    }),
    metricCard('FourPlusVisitClients', {
      value: formatNumber(fourPlusVisits.count),
      note: frequencyNote(fourPlusVisits),
    }),
    metricCard('NewClients', {
      value: formatNumber(visitMetrics.new_clients),
      change: visitMetrics.new_clients_change_pct,
      current: visitMetrics.new_clients,
      ...reportLink,
    }),
    metricCard('NewClientsPct', {
      value: formatMetricValue(visitMetrics.new_clients_pct, 'percent'),
      change: visitMetrics.new_clients_pct_change_pct,
      current: visitMetrics.new_clients_pct,
      ...reportLink,
    }),
    metricCard('RepeatClients', {
      value: formatNumber(visitMetrics.repeat_clients),
      change: visitMetrics.repeat_clients_change_pct,
      current: visitMetrics.repeat_clients,
      ...reportLink,
    }),
    metricCard('RepeatClientsPct', {
      value: formatMetricValue(visitMetrics.repeat_clients_pct, 'percent'),
      change: visitMetrics.repeat_clients_pct_change_pct,
      current: visitMetrics.repeat_clients_pct,
      ...reportLink,
    }),
  ];
}

/** Appointment cards plus the notice under them; a missing or estimated count is flagged in the notice. */
export function appointmentsCards(summary) {
  const breakdown = summary.appointments_breakdown || {};
  const ready = breakdown.source_status === 'ready' || breakdown.source_status === 'local';
  const metricValue = (value) => (ready ? formatNumber(value) : t('dash.noData'));
  const metricShare = (value) => (ready ? t('dash.ofTotal', { value: `${formatNumber(value)}%` }) : '');
  const share = (name, count, sharePct) => metricCard(name, { value: metricValue(count), note: metricShare(sharePct) });
  return {
    cards: [
      share('TotalAppointments', breakdown.total, breakdown.total_share_pct),
      share('CancelledAppointments', breakdown.cancelled, breakdown.cancelled_share_pct),
      share('CompletedAppointments', breakdown.completed, breakdown.completed_share_pct),
      share('IncompleteAppointments', breakdown.incomplete, breakdown.incomplete_share_pct),
    ],
    // A local count that is only the usual one (the period reaches today, a branch has a reporting window) needs
    // no apology; the warning is for YClients not answering.
    notice: ready
      ? breakdown.local_reason === 'upstream_error' ? t('dash.appointmentsLocalEstimate') : ''
      : t('dash.appointmentsUnavailable'),
  };
}
