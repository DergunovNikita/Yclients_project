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
 * One card. `name` picks the `dash.card<name>` label and the `dash.hint<name>` tooltip.
 *
 * The delta is either a change in percent (`change`, rendered against the previous period and
 * coloured by its sign) or a fixed `note` with no colour; a card with neither shows no delta.
 */
export function metricCard(name, options) {
  const { value, present, href, action } = options;
  const card = { label: t(`dash.card${name}`), hint: t(`dash.hint${name}`), value };
  // `in`, not `!== undefined`: a change the summary lacks is still a delta ("no base"), not no delta.
  if ('note' in options) {
    card.delta = options.note;
    card.deltaValue = null;
  } else if ('change' in options) {
    card.delta = formatPct(options.change);
    card.deltaValue = options.change;
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
            <div class="value">${escapeHtml(card.value)}</div>
            ${card.delta ? `<div class="delta ${deltaClass(card.deltaValue)}">${escapeHtml(card.delta)}</div>` : ''}
            ${card.action ? `<div class="card__action">${escapeHtml(card.action)}</div>` : ''}
          </${tag}>${cardHintHtml(card.hint, card.label)}
        </div>
      `;
      },
    )
    .join('');
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
      present: Boolean(revenueBlock),
    }),
    metricCard('VisitedAppointments', {
      value: formatNumber(revenue.appointments),
      change: revenue.appointments_change_pct,
      present: Boolean(revenueBlock),
    }),
    metricCard(partial ? 'AverageCheckTotalPartial' : 'AverageCheckTotal', {
      value: formatMoney(averageCheck.total),
      change: averageCheck.total_change_pct,
      present: Boolean(averageCheckBlock),
    }),
  ]);
}

export function staffBranchCards(summary) {
  const attribution = summary.service_attribution || {};
  const revenue = summary.revenue || {};
  const averageCheck = summary.average_check || {};
  const visitMetrics = summary.visit_metrics || {};
  const moneyShown = !summary.financials_hidden;
  return presentCards([
    metricCard('BranchShiftAppointments', { value: formatMetricValue(attribution.appointment_count, 'number') }),
    metricCard('BranchShiftClients', { value: formatMetricValue(attribution.unique_client_count, 'number') }),
    metricCard('ExtraServiceCount', {
      value: formatMetricValue(revenue.extra_service_count, 'number'),
      change: revenue.extra_service_count_change_pct,
    }),
    extraServicesPerAppointmentCard(visitMetrics),
    extraServiceClientsCard(visitMetrics),
    metricCard('ExtraServiceRevenue', {
      value: formatMetricValue(revenue.extra_service_revenue, 'money'),
      change: revenue.extra_service_revenue_change_pct,
      present: moneyShown,
    }),
    metricCard('ExtraServiceAverageCheck', {
      value: formatMetricValue(averageCheck.extra_services, 'money'),
      change: averageCheck.extra_services_change_pct,
      present: moneyShown,
    }),
  ]);
}

export function revenueCards(summary) {
  if (!summary.revenue) return [];
  const revenue = summary.revenue;
  const cards = [
    metricCard('ServiceRevenue', {
      value: formatMoney(revenue.service_revenue),
      change: revenue.service_revenue_change_pct,
    }),
    metricCard('GoodsRevenue', {
      value: formatMoney(revenue.goods_revenue),
      change: revenue.goods_revenue_change_pct,
    }),
    metricCard('ExtraServiceRevenue', {
      value: formatMoney(revenue.extra_service_revenue),
      change: revenue.extra_service_revenue_change_pct,
    }),
  ];
  return usesAdministratorSchedule(summary) ? cards.filter((_, index) => index !== 2) : cards;
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
        present: revenuePresent,
      }),
      average: metricCard('ServiceAverageCheck', {
        value: formatMoney(averageCheck.services),
        change: averageCheck.services_change_pct,
        present: averageCheckPresent,
      }),
    },
    {
      count: metricCard('GoodsCount', {
        value: formatNumber(revenue.goods_count),
        change: revenue.goods_count_change_pct,
        present: revenuePresent,
      }),
      average: metricCard('GoodsAverageCheck', {
        value: formatMoney(averageCheck.goods),
        change: averageCheck.goods_change_pct,
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
        present: revenuePresent,
      }),
      average: metricCard('ExtraServiceAverageCheck', {
        value: formatMoney(averageCheck.extra_services),
        change: averageCheck.extra_services_change_pct,
        present: averageCheckPresent,
      }),
      // Also shown under Appointments: there it measures how often extras are sold,
      // here it closes the extra-services column the same way the goods share does.
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

export function visitCards(summary) {
  const visitMetrics = summary.visit_metrics || {};
  const cards = [
    metricCard('OpzQty', {
      value: formatNumber(visitMetrics.opz_qty),
      change: visitMetrics.opz_qty_change_pct,
    }),
    metricCard('OpzPct', {
      value: formatMetricValue(visitMetrics.opz_pct, 'percent'),
      change: visitMetrics.opz_pct_change_pct,
    }),
    extraServicesPerAppointmentCard(visitMetrics),
  ];
  return usesAdministratorSchedule(summary) ? cards.slice(0, 2) : cards;
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
  const cards = [
    metricCard('UniqueClients', {
      value: formatNumber(visitMetrics.unique_clients),
      change: visitMetrics.unique_clients_change_pct,
    }),
    metricCard('VisitsPerClient', {
      value: formatDecimal(visitMetrics.visits_per_client),
      change: visitMetrics.visits_per_client_change_pct,
    }),
    extraServiceClientsCard(visitMetrics),
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
      ...reportLink,
    }),
    metricCard('NewClientsPct', {
      value: formatMetricValue(visitMetrics.new_clients_pct, 'percent'),
      change: visitMetrics.new_clients_pct_change_pct,
      ...reportLink,
    }),
    metricCard('RepeatClients', {
      value: formatNumber(visitMetrics.repeat_clients),
      change: visitMetrics.repeat_clients_change_pct,
      ...reportLink,
    }),
    metricCard('RepeatClientsPct', {
      value: formatMetricValue(visitMetrics.repeat_clients_pct, 'percent'),
      change: visitMetrics.repeat_clients_pct_change_pct,
      ...reportLink,
    }),
  ];
  return usesAdministratorSchedule(summary) ? cards.filter((_, index) => index !== 2) : cards;
}

/** Appointment cards plus the notice under them; `exact` is false when the numbers are an estimate or missing. */
export function appointmentsCards(summary) {
  const breakdown = summary.appointments_breakdown || {};
  const exact = breakdown.source_status === 'ready';
  const ready = exact || breakdown.source_status === 'local';
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
    exact,
    notice: ready
      ? exact ? '' : t('dash.appointmentsLocalEstimate')
      : t('dash.appointmentsUnavailable'),
  };
}
