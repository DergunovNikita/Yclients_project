import { reportFilterVisibility } from '../dashboardRequestState.js';
import { intlLocale, t } from '../i18n.js';

export { escapeHtml } from '../html.js';

export function formatMoney(value) {
  if (value === null || value === undefined || value === '') return '—';
  return `${Math.round(Number(value || 0)).toLocaleString(intlLocale())} ₽`;
}

// Kopecks matter where a person types the amount in; formatMoney's whole roubles suit totals.
export function formatMoneyExact(value) {
  if (value === null || value === undefined || value === '') return '—';
  return `${Number(value || 0).toLocaleString(intlLocale(), { minimumFractionDigits: 0, maximumFractionDigits: 2 })} ₽`;
}

export function formatNumber(value) {
  if (value === null || value === undefined || value === '') return '—';
  return Number(value || 0).toLocaleString(intlLocale());
}

export function formatDecimal(value) {
  if (value === null || value === undefined || value === '') return '—';
  return Number(value || 0).toLocaleString(intlLocale(), { maximumFractionDigits: 2 });
}

export function formatPercent(value) {
  if (value === null || value === undefined || value === '') return '—';
  return `${Number(value || 0).toLocaleString(intlLocale(), { maximumFractionDigits: 1 })}%`;
}

export function formatDate(value) {
  if (!value) return '—';
  const text = String(value);
  const match = text.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!match) return text;
  return new Intl.DateTimeFormat(intlLocale()).format(new Date(`${match[1]}-${match[2]}-${match[3]}T00:00:00`));
}

export function formatValue(value, format = 'text') {
  if (format === 'money') return formatMoney(value);
  if (format === 'number') return formatNumber(value);
  if (format === 'decimal') return formatDecimal(value);
  if (format === 'percent') return formatPercent(value);
  if (format === 'date') return formatDate(value);
  return value === null || value === undefined || value === '' ? '—' : String(value);
}

// Digits each metric format shows for a change. A change that rounds to zero must read as plain "0",
// neither "-0" (negative zero survives toLocaleString) nor "+0", and must carry no up/down colour.
const DELTA_DIGITS = { money: 0, number: 3, decimal: 2, percent: 1 };

function roundedTo(value, digits) {
  return Number(Number(value).toFixed(digits)) + 0;
}

export function deltaDigits(format) {
  return DELTA_DIGITS[format] ?? DELTA_DIGITS.number;
}

/** CSS tone of a change; with `digits` the change is judged as displayed, so a rounded-away change has none. */
export function deltaClass(value, digits = null) {
  if (value === null || value === undefined) return '';
  const shown = digits === null ? value : roundedTo(value, digits);
  if (shown === 0) return '';
  return shown > 0 ? 'up' : 'down';
}

function signed(value, digits) {
  return roundedTo(value, digits) > 0 ? '+' : '';
}

/** Percentage change as a sentence for an Overview card ("+5% к прошлому периоду"). */
export function formatPct(value) {
  if (value === null || value === undefined) return t('dash.noBase');
  const shown = roundedTo(value, 3);
  return t('dash.changeVsPrevious', { value: `${signed(shown, 3)}${shown.toLocaleString(intlLocale())}%` });
}

/** Percentage change on its own, for table cells; a missing base is a dash, not a made-up number. */
export function formatDeltaPct(value) {
  if (value === null || value === undefined) return '—';
  const shown = roundedTo(value, 1);
  return `${signed(shown, 1)}${shown.toLocaleString(intlLocale(), { maximumFractionDigits: 1 })}%`;
}

/** Signed difference in the metric's own unit; a difference of two shares is in percentage points. */
export function formatSignedValue(value, format = 'number') {
  if (value === null || value === undefined || value === '') return '—';
  const digits = deltaDigits(format);
  const shown = roundedTo(value, digits);
  const unit = format === 'percent'
    ? `${shown.toLocaleString(intlLocale(), { maximumFractionDigits: 1 })} ${t('reports.percentagePoints')}`
    : formatValue(shown, format);
  return `${signed(shown, digits)}${unit}`;
}

const GRANULARITY_KEYS = new Set(['day', 'week', 'month']);

/** Subtitle of an open report: its period, plus the bucket size when the report offers that choice. */
export function periodSubtitle(data, meta) {
  const period = data?.period;
  if (!period) return '';
  const range = `${formatDate(period.start)} .. ${formatDate(period.end)}`;
  return reportFilterVisibility(meta?.filters).granularity && GRANULARITY_KEYS.has(period.granularity)
    ? `${range} · ${t(`dash.${period.granularity}`)}`
    : range;
}
