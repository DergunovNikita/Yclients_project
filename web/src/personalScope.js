// A personal account (the server clamps it to its own rows) whose rows are administrator rows in several branches would
// open on the GROUP of those rows (the server clamps «all branches» to every own row), and the Overview reads a group as
// master attribution: administrator mode needs one row, and an administrator's own visits as a master are almost none.
// So the first view is one branch of theirs. That is the `barber` role labelled onto an administrator's rows; the
// `admin` role is not personal and picks any branch itself. Barbers with barber rows keep «all branches»: the group of
// their own rows IS their numbers, summed over the branches.

import { isPersonalRole } from './staffLevels.js';

const ADMINISTRATIVE_CATEGORIES = new Set(['administrator', 'mixed']);

// The own, living rows of a personal role.
function livingRows(ownStaff) {
  return (ownStaff || []).filter((row) => row.is_active !== false && row.company_id !== null && row.company_id !== undefined);
}

/**
 * True for a personal account whose own rows are administrator rows in two or more branches: the one user for whom a group
 * of branches or of own rows is misleading. The administrator category comes from the rows themselves.
 *
 * @param {{role?: string}} user
 * @param {{company_id: number|string, category?: string|null, is_active?: boolean}[]} ownStaff
 *   `/dashboard/staff` of a personal role: its own rows.
 */
export function isMultiBranchAdministrator(user, ownStaff = []) {
  if (!isPersonalRole(user)) return false;
  const rows = livingRows(ownStaff);
  if (new Set(rows.map((row) => String(row.company_id))).size < 2) return false;
  return rows.some((row) => ADMINISTRATIVE_CATEGORIES.has(row.category));
}

/**
 * The branch a personal administrator's views open on, as an option value; '' when the default stays «all branches».
 * It is the branch of the own row with the latest shift on or before the branch today (`last_shift_date`: where the
 * person works now, not where they once did); ties and no shifts fall back to the first branch in branch order.
 *
 * @param {{role?: string}} user
 * @param {{company_id: number|string, category?: string|null, is_active?: boolean, last_shift_date?: string|null}[]} ownStaff
 * @param {{id: number|string}[]} branchOptions The branches the view can choose from, in branch order.
 */
export function defaultPersonalBranch(user, ownStaff = [], branchOptions = []) {
  if (!isMultiBranchAdministrator(user, ownStaff)) return '';
  const rows = livingRows(ownStaff);
  const ownBranches = new Set(rows.map((row) => String(row.company_id)));
  const lastShift = (branch) => rows
    .filter((row) => String(row.company_id) === String(branch.id) && row.last_shift_date)
    .reduce((latest, row) => (row.last_shift_date > latest ? row.last_shift_date : latest), '');
  let chosen = null;
  let chosenShift = '';
  (branchOptions || []).filter((branch) => ownBranches.has(String(branch.id))).forEach((branch) => {
    const shift = lastShift(branch);
    if (chosen === null || shift > chosenShift) {
      chosen = branch;
      chosenShift = shift;
    }
  });
  return chosen ? String(chosen.id) : '';
}

/**
 * The branch selection a view may hold. A multi-branch administrator is on exactly one branch: two would clamp the
 * request to a group of own rows (master attribution) and an empty selection would do the same, so the default branch
 * takes the place of «none» and the first of several wins. Everyone else keeps what they chose.
 *
 * @param {string[]} selected Chosen branch option values.
 * @returns {string[]}
 */
export function personalBranchSelection(user, ownStaff, branchOptions, selected = []) {
  if (!isMultiBranchAdministrator(user, ownStaff) || selected.length === 1) return selected;
  if (selected.length > 1) return [selected[0]];
  const branchId = defaultPersonalBranch(user, ownStaff, branchOptions);
  return branchId ? [branchId] : selected;
}
