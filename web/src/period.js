// Local-calendar date helpers, kept free of i18n imports so node --test can load them.
// Every value here stays on the calendar day the user sees: toISOString() would move
// the window a day back for any timezone ahead of UTC.

export function inputDateValue(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

export function defaultReportDates() {
  const now = new Date();
  return {
    start: inputDateValue(new Date(now.getFullYear(), now.getMonth(), 1)),
    end: inputDateValue(now),
  };
}

/**
 * Whether the compare inputs may follow a new period.
 *
 * They follow while they still hold the window that was filled in for them. A window
 * the user typed, cleared, or brought along in a link is theirs: `ours: false` settles
 * it outright, and an unrecognised window settles it for the caller to remember.
 */
export function shouldAdoptComparePeriod({ compareStart, compareEnd, autoPeriod, ours = true }) {
  if (!ours) return false;
  if (!autoPeriod) return !compareStart && !compareEnd;
  return compareStart === autoPeriod.start && compareEnd === autoPeriod.end;
}

/**
 * Compare-window state a freshly loaded link starts from.
 *
 * A window equal to the default for that period is one an earlier filter change wrote
 * into the URL, so it keeps following the period. Anything else is the sender's own
 * choice — a year-over-year window typed by hand must survive reload and back/forward.
 */
export function comparePeriodOnLoad({ start, end, compareStart, compareEnd }) {
  if (!compareStart || !compareEnd) return { autoPeriod: null, ours: true };
  const fallback = defaultComparePeriod(start, end);
  const isDefault = Boolean(fallback)
    && fallback.start === compareStart
    && fallback.end === compareEnd;
  return isDefault
    ? { autoPeriod: { start: compareStart, end: compareEnd }, ours: true }
    : { autoPeriod: null, ours: false };
}

/**
 * Compare-window state after the period changed, and the window to show.
 *
 * A null window means leave the inputs as they are.
 */
export function nextComparePeriod(state, { start, end, compareStart, compareEnd }) {
  const candidate = defaultComparePeriod(start, end);
  if (!candidate) return { ...state, window: null };
  if (!shouldAdoptComparePeriod({
    compareStart,
    compareEnd,
    autoPeriod: state.autoPeriod,
    ours: state.ours,
  })) {
    return { autoPeriod: state.autoPeriod, ours: false, window: null };
  }
  return { autoPeriod: candidate, ours: true, window: candidate };
}

/** Window of the same length immediately before the given period. */
export function defaultComparePeriod(startValue, endValue) {
  if (!startValue || !endValue) return null;
  const start = new Date(`${startValue}T00:00:00`);
  const end = new Date(`${endValue}T00:00:00`);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime()) || end < start) return null;
  const span = Math.round((end - start) / 86400000);
  const compareEnd = new Date(start);
  compareEnd.setDate(compareEnd.getDate() - 1);
  const compareStart = new Date(compareEnd);
  compareStart.setDate(compareStart.getDate() - span);
  return { start: inputDateValue(compareStart), end: inputDateValue(compareEnd) };
}
