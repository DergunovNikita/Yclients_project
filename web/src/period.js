// Local-calendar date helpers, kept free of i18n imports so node --test can load them.
// Every value here stays on the calendar day the user sees: toISOString() would move
// the window a day back for any timezone ahead of UTC.

export function inputDateValue(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

export function monthValue(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`;
}

export function defaultReportDates() {
  const now = new Date();
  return {
    start: inputDateValue(new Date(now.getFullYear(), now.getMonth(), 1)),
    end: inputDateValue(now),
  };
}

/**
 * Window a picked month stands for: its 1st through its last day.
 *
 * A month still running ends at today instead: the days ahead hold no facts, so asking
 * for them would put a part-month of data against a whole month of plan, and every
 * average would be divided by days that never happened.
 */
export function monthRange(value, today = new Date()) {
  const match = /^(\d{4})-(\d{2})$/.exec(String(value ?? ''));
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  if (month < 1 || month > 12) return null;
  const running = today.getFullYear() === year && today.getMonth() === month - 1;
  return {
    start: inputDateValue(new Date(year, month - 1, 1)),
    end: inputDateValue(running ? today : new Date(year, month, 0)),
  };
}

/**
 * The month a window was picked as, or '' for any other window.
 *
 * Only ever used to show the picker back what it asked for. Which preset a window
 * carries is never guessed from its dates — see previous_period() on the backend and
 * the note on nextComparePeriod: a hand-typed window keeps the day-stepped baseline its
 * compare field shows, and inferring a month here would render one delta beside a
 * compare window describing another.
 */
export function monthOfRange(startValue, endValue, today = new Date()) {
  const match = /^(\d{4})-(\d{2})-01$/.exec(String(startValue ?? ''));
  if (!match) return '';
  const month = `${match[1]}-${match[2]}`;
  const range = monthRange(month, today);
  return range && range.end === endValue ? month : '';
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
