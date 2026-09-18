/**
 * Which branches a period may be filtered by.
 *
 * A branch belongs to its tenant only inside its reporting window, and every metric is cut
 * by `reporting_window_clause` on the server. Offering a branch whose window does not reach
 * the chosen period would therefore offer a selection that can only ever return zeroes —
 * and, for a branch that left the tenant, one it has no business showing at all.
 *
 * The dates arrive on the branch payload, so this is a filter over data the server already
 * sent, not a second implementation of the rule. Both bounds are inclusive ISO days, which
 * compare correctly as plain strings.
 */
export function branchInPeriod(branch, start, end) {
  if (!start || !end) return true;
  const opened = branch?.reporting_start_date;
  const left = branch?.reporting_end_date;
  if (opened && opened > end) return false;
  if (left && left < start) return false;
  return true;
}

export function branchesForPeriod(branches, start, end) {
  return (branches || []).filter((branch) => branchInPeriod(branch, start, end));
}
