// Pure helpers of the «Порядок филиалов» tab of the settings page.

/** The list with the item at `index` moved by `delta` places; out-of-range moves leave it as it was. */
export function moveBranch(list, index, delta) {
  const target = index + delta;
  if (index < 0 || index >= list.length || target < 0 || target >= list.length) return list;
  const next = [...list];
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

/** Whether the working order differs from the one the server returned. */
export function branchOrderChanged(saved, working) {
  return saved.length !== working.length || saved.some((branch, index) => branch.id !== working[index].id);
}
