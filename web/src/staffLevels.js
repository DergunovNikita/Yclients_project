// Barber levels («ТОП-барбер», «ПРЕМИУМ-барбер») are YClients positions. One title can sit under several position
// ids, so a level option stands for a SET of ids: its value is the sorted ids joined by commas, the very string
// `staff_position_ids` carries to the API. The id set differs per scope (the network, one branch), so what the user
// chose is remembered by the level `key`: the select carries it on each option as `data-key`. The select is multiple:
// several chosen levels ask for the union of their id sets.

import { selectedValues } from './multiSelect.js';

const LEVEL_CATEGORIES = new Set(['barber', 'mixed']);
// Roles clamped to their own staff row: the server ignores a level for them (as it does a chosen colleague). The
// administrator is not one of them: like a manager they pick any branch, person and level inside their own branches.
const PERSONAL_ROLES = new Set(['barber']);

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

/** The role clamped to its own staff rows by the server (`barber`). */
export function isPersonalRole(user) {
  return PERSONAL_ROLES.has(user?.role);
}

/** A personal role sees only its own row, so a level would pick nothing for it. */
export function canPickLevel(user) {
  return !isPersonalRole(user);
}

/**
 * Options the level select offers: none when the list has a single person (nothing to choose between) or the
 * user is a personal role, which hides the select.
 */
export function selectableLevelOptions(staffItems = [], user = null) {
  return canPickLevel(user) && staffItems.length > 1 ? levelOptions(staffItems) : [];
}

/** What a level select currently stands for: the id set and the level key of every selected option. */
export function chosenLevels(select) {
  return Array.from(select?.options || [])
    .filter((option) => option.selected && option.value !== '')
    .map((option) => ({ value: option.value, key: option.dataset?.key || '' }));
}

function idsOf(value) {
  return String(value ?? '').split(',').filter(Boolean);
}

// Level options a link's id set stands for: those entirely inside it (a union of levels), otherwise the one sharing
// the most ids (a link written for another scope's id set of the same level).
function optionValuesForIds(options, value) {
  const wanted = new Set(idsOf(value).map(Number).filter(Number.isFinite));
  if (!wanted.size) return [];
  const inside = options.filter((option) => idsOf(option.value).every((id) => wanted.has(Number(id))));
  if (inside.length) return inside.map((option) => option.value);
  let best = null;
  let bestShared = 0;
  options.forEach((option) => {
    const shared = idsOf(option.value).filter((id) => wanted.has(Number(id))).length;
    if (shared > bestShared) {
      best = option;
      bestShared = shared;
    }
  });
  return best ? [best.value] : [];
}

/**
 * The option values to select after the options were rebuilt; a level that is gone drops out of the selection.
 *
 * An entry with a `key` is looked up by it, whatever ids the level has in the new scope. One without (a link carries
 * only ids) maps to the options its ids cover.
 *
 * @param {{key: string, value: string}[]} options
 * @param {{value?: string, key?: string}[]} previous
 * @returns {string[]} In option order.
 */
export function levelSelection(options, previous = []) {
  const chosen = new Set();
  (previous || []).forEach(({ value = '', key = '' } = {}) => {
    if (key) {
      const option = options.find((candidate) => candidate.key === key);
      if (option) chosen.add(option.value);
      return;
    }
    optionValuesForIds(options, value).forEach((optionValue) => chosen.add(optionValue));
  });
  return options.map((option) => option.value).filter((value) => chosen.has(value));
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

/** The ids of several level options as one `staff_position_ids` value: their union, sorted numerically. */
export function levelIdsParam(optionValues) {
  const ids = new Set();
  (optionValues || []).forEach((value) => idsOf(value).forEach((id) => ids.add(Number(id))));
  return [...ids].sort((a, b) => a - b).join(',');
}

/** The `staff_position_ids` a request carries: chosen people win over levels, as on the server. */
export function levelRequestParam(filter) {
  return selectedValues(filter?.staff).length ? '' : levelIdsParam(selectedValues(filter?.level));
}
