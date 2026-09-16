// Backend timestamps arrive as naive UTC (`factual_now()` server-side). The marker has to be
// added before parsing: without it the browser reads the value as local time, so anything
// saved after midnight UTC shows up a day early for every viewer west of UTC.
// Every business timestamp in this product is read in branch time, not in the viewer's.
export const BRANCH_TIME_ZONE = 'Europe/Moscow';

export function parseServerInstant(value) {
  if (!value) return null;
  const text = String(value);
  const iso = /(?:Z|[+-]\d{2}:?\d{2})$/.test(text) ? text : `${text}Z`;
  const instant = new Date(iso);
  return Number.isNaN(instant.getTime()) ? null : instant;
}
