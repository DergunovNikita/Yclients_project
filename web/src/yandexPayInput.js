// Parsing and bookkeeping for the Yandex Pay editor: one rouble amount per branch per month,
// plus the day the amount is "data through" (the last day of the month it covers).
// Pure functions only — the page owns the DOM, this owns what a typed string means.

export const YANDEX_PAY_MAX = 1e9;
const MAX_DECIMALS = 2;

/**
 * What a typed amount means.
 *
 * Returns `{ value }` (null for an empty cell: "nothing entered" is not "entered zero") or
 * `{ error }` with one of `format`, `range`, `precision`. A comma is accepted as the decimal
 * separator, and spaces (including non-breaking ones) as digit grouping.
 */
export function parseYandexPayInput(raw) {
  const text = String(raw ?? '').replace(/[\s ]/g, '').replace(',', '.');
  if (text === '') return { value: null };
  if (/^-/.test(text)) return { error: 'range' };
  if (!/^\d+(\.\d+)?$/.test(text)) return { error: 'format' };
  const decimals = (text.split('.')[1] || '').replace(/0+$/, '');
  if (decimals.length > MAX_DECIMALS) return { error: 'precision' };
  const value = Number(text);
  if (!Number.isFinite(value) || value > YANDEX_PAY_MAX) return { error: 'range' };
  return { value };
}

/**
 * Comparable form of a cell, so "100" and "100,00" are the same draft but a half-typed
 * "12x" still counts as a change that needs saving or discarding.
 */
export function yandexPayDraftKey(raw) {
  const parsed = parseYandexPayInput(raw);
  if (parsed.error) return `!${String(raw ?? '').trim()}`;
  return parsed.value === null ? '' : String(parsed.value);
}

function storedValue(row) {
  return row.value === null || row.value === undefined ? null : Number(row.value);
}

function storedDataThrough(row) {
  return row.data_through || null;
}

const ISO_DAY = /^(\d{4})-(\d{2})-(\d{2})$/;
// A date input that holds half-typed text reports an empty value; the page marks such a cell
// with this token so it is rejected instead of being read as "no date".
export const UNREADABLE_DATE = '?';

function isRealDay(text) {
  const match = ISO_DAY.exec(text);
  if (!match) return false;
  const [year, month, day] = match.slice(1).map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
}

/** First day of a `YYYY-MM` month as an ISO date, or '' when the month is not readable. */
export function yandexPayMonthStart(month) {
  return /^\d{4}-(0[1-9]|1[0-2])$/.test(String(month ?? '')) ? `${month}-01` : '';
}

/**
 * What a "data through" cell means for a row of the given month.
 *
 * Returns `{ value }` (null for an empty cell) or `{ error }` with `format` (not an ISO day)
 * or `range` (before the 1st of the month, or after the row's `max_data_through`).
 */
export function parseYandexPayDataThrough(raw, row, month) {
  const text = String(raw ?? '').trim();
  if (text === '') return { value: null };
  if (!isRealDay(text)) return { error: 'format' };
  const min = yandexPayMonthStart(month);
  if (min && text < min) return { error: 'range' };
  if (row?.max_data_through && text > row.max_data_through) return { error: 'range' };
  return { value: text };
}

/**
 * The date cell after the amount cell changed: an amount typed into an empty date picks up
 * the row's default, an emptied amount takes its date with it, and a half-typed amount leaves
 * the date alone. A date the user already chose is never overwritten.
 */
export function syncYandexPayDate(amountRaw, dateRaw, defaultDate) {
  const parsed = parseYandexPayInput(amountRaw);
  if (parsed.error) return dateRaw;
  if (parsed.value === null) return '';
  return dateRaw ? dateRaw : defaultDate || '';
}

/** Comparable form of a date cell for dirty tracking. */
export function yandexPayDateKey(raw) {
  return String(raw ?? '').trim();
}

/**
 * Request items for every row of the editor.
 *
 * `previous_value` and `previous_data_through` are what the last response carried, not what the
 * cells held when they were drawn: the server compares the pair to notice that someone else
 * saved in between. A cleared amount sends no date; an amount without a date is refused. Returns
 * `{ items }`, or `{ error, row }` where `error` is `format`, `range`, `precision` (amount) or
 * `dateRequired`, `dateFormat`, `dateRange` (date).
 */
export function buildYandexPayItems(rows, rawByCompany, rawDateByCompany = new Map(), month = '') {
  const items = [];
  for (const row of rows) {
    const key = String(row.company_id);
    const parsed = parseYandexPayInput(rawByCompany.get(key));
    if (parsed.error) return { error: parsed.error, row };
    let dataThrough = null;
    if (parsed.value !== null) {
      const date = parseYandexPayDataThrough(rawDateByCompany.get(key), row, month);
      // The server would fill the month's default here — for a row with a stored date, a quiet edit of it.
      if (date.value === null) return { error: 'dateRequired', row };
      if (date.error) return { error: date.error === 'format' ? 'dateFormat' : 'dateRange', row };
      dataThrough = date.value;
    }
    items.push({
      company_id: Number(row.company_id),
      value: parsed.value,
      data_through: dataThrough,
      previous_value: storedValue(row),
      previous_data_through: storedDataThrough(row),
    });
  }
  return { items };
}

/**
 * Month total as it would stand after saving: only rows the reports count, and only cells
 * that read as an amount (a half-typed one adds nothing instead of poisoning the sum).
 */
export function yandexPayTotal(rows, rawByCompany) {
  const kopecks = rows.reduce((sum, row) => {
    if (row.counted === false) return sum;
    const parsed = parseYandexPayInput(rawByCompany.get(String(row.company_id)));
    return parsed.error || parsed.value === null ? sum : sum + Math.round(parsed.value * 100);
  }, 0);
  return kopecks / 100;
}
