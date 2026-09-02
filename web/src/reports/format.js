import { intlLocale } from '../i18n.js';

export { escapeHtml } from '../html.js';

export function formatMoney(value) {
  if (value === null || value === undefined || value === '') return '—';
  return `${Math.round(Number(value || 0)).toLocaleString(intlLocale())} ₽`;
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
