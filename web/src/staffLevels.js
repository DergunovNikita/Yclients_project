// Barber levels («ТОП-барбер», «ПРЕМИУМ-барбер») are YClients positions. One title can sit under several position
// ids, so a level option stands for a SET of ids: its value is the sorted ids joined by commas, the very string
// `staff_position_ids` carries to the API. The id set differs per scope (the network, one branch), so what the user
// chose is remembered by the level `key`: the select carries it on each option as `data-key`.

const LEVEL_CATEGORIES = new Set(['barber', 'mixed']);
// Roles clamped to their own staff row: the server ignores a level for them (as it does a chosen colleague).
const PERSONAL_ROLES = new Set(['admin', 'barber']);

function compareText(left, right) {
  return left.localeCompare(right, 'ru');
}

// The most common title of the group; a tie goes to the alphabetically first one.
function dominantTitle(titles) {
  const counts = new Map();
  titles.forEach((title) => counts.set(title, (counts.get(title) || 0) + 1));
  return [...counts.entries()]
    .sort(([leftTitle, leftCount], [rightTitle, rightCount]) => rightCount - leftCount || compareText(leftTitle, rightTitle))[0][0];
}

/**
 * Level options of a staff list (`/dashboard/staff` items), grouped by the normalized level key.
 *
 * @returns {{key: string, value: string, label: string}[]} Sorted by label. Items without a level, a position id, or with an
 *   administrator category are left out: only barbers have levels.
 */
export function levelOptions(staffItems = []) {
  const groups = new Map();
  staffItems.forEach((item) => {
    if (!item?.level?.key || item.position_id === null || item.position_id === undefined) return;
    if (item.category && !LEVEL_CATEGORIES.has(item.category)) return;
    const group = groups.get(item.level.key) || { ids: new Set(), titles: [] };
    group.ids.add(Number(item.position_id));
    group.titles.push(item.level.title);
    groups.set(item.level.key, group);
  });
  return [...groups.entries()]
    .map(([key, group]) => ({
      key,
      value: [...group.ids].sort((a, b) => a - b).join(','),
      label: dominantTitle(group.titles),
    }))
    .sort((left, right) => compareText(left.label, right.label) || compareText(left.key, right.key));
}

/** A personal role (`admin`, `barber`) sees only its own row, so a level would pick nothing for it. */
export function canPickLevel(user) {
  return !PERSONAL_ROLES.has(user?.role);
}

/**
 * Options the level select offers: none when the list has a single person (nothing to choose between) or the
 * user is a personal role, which hides the select.
 */
export function selectableLevelOptions(staffItems = [], user = null) {
  return canPickLevel(user) && staffItems.length > 1 ? levelOptions(staffItems) : [];
}

/** What a level select currently stands for: its id set and the level key of the selected option. */
export function chosenLevel(select) {
  return { value: select?.value || '', key: select?.selectedOptions?.[0]?.dataset?.key || '' };
}

/**
 * The option value to select after the options were rebuilt, `''` («all levels») when the level is gone.
 *
 * With a `key` the level is looked up by it, whatever ids it has in the new scope. Without one (a link carries only
 * ids) the option with exactly those ids wins, otherwise the one sharing the most of them.
 *
 * @param {{key: string, value: string}[]} options
 * @param {{value?: string, key?: string}} previous
 */
export function levelSelection(options, { value = '', key = '' } = {}) {
  if (key) return options.find((option) => option.key === key)?.value ?? '';
  const ids = String(value ?? '').split(',').filter(Boolean);
  if (!ids.length) return '';
  const wanted = [...new Set(ids.map(Number))].sort((a, b) => a - b).join(',');
  const exact = options.find((option) => option.value === wanted);
  if (exact) return exact.value;
  let best = null;
  let bestShared = 0;
  options.forEach((option) => {
    const shared = option.value.split(',').filter((id) => ids.includes(id)).length;
    if (shared > bestShared) {
      best = option;
      bestShared = shared;
    }
  });
  return best ? best.value : '';
}

// Reports whose catalog entry offers the level filter (`filters.staff_level` on the server). The Overview has no
// catalog at hand, so a card link under a level filter asks this list instead.
const LEVEL_FILTER_REPORTS = new Set(['staff_leaderboard']);

/**
 * Extra query params of an Overview card's report link while the Overview is filtered by a level
 * (`summary.level_filter`), or null when the report cannot honour it: the link would open the whole branch under numbers
 * that differ from the card, so the card goes without one.
 */
export function levelReportParams(reportId, levelFilter) {
  if (!levelFilter) return {};
  const ids = (levelFilter.position_ids || []).join(',');
  return LEVEL_FILTER_REPORTS.has(reportId) && ids ? { staff_position_ids: ids } : null;
}

/** The `staff_position_ids` a request carries: a chosen person wins over a level, as on the server. */
export function levelRequestParam(filter) {
  return filter?.staff?.value ? '' : filter?.level?.value || '';
}
