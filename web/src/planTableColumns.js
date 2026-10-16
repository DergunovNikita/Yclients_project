// Plan/fact tables on one screen (branch + staff) share their columns, so one column key gets one width in all of
// them: the widest min-content of that key across the tables. The room left over in the box goes to the metric
// columns in proportion to that width; the two identity columns ("Разрез", "Показатель") keep theirs.

// All keys in table order: a key missing from the first table goes right after the key it follows elsewhere.
export function unionColumnKeys(keyLists) {
  const union = [];
  keyLists.forEach((keys) => {
    keys.forEach((key, index) => {
      if (union.includes(key)) return;
      const after = index ? union.indexOf(keys[index - 1]) : -1;
      union.splice(after + 1, 0, key);
    });
  });
  return union;
}

// Laid out over all keys, then a column a table does not have hands its width to the column before it: every column
// the table does have starts where it starts in the others (the staff table without "Отзывы" ends one column early,
// its last column covering the gap).
export function alignedColumnWidths(keys, allKeys, minWidths, inner, fixedCount = 2) {
  const base = allKeys.map((key) => minWidths.get(key) || 0);
  const total = base.reduce((sum, width) => sum + width, 0);
  const flexible = base.slice(fixedCount).reduce((sum, width) => sum + width, 0);
  const extra = Math.max(0, inner - total);
  const widths = base.map((width, index) => (
    index < fixedCount || !flexible ? width : width + (extra * width) / flexible
  ));
  const result = [];
  let carried = 0;
  allKeys.forEach((key, index) => {
    if (keys.includes(key)) {
      result.push(widths[index] + carried);
      carried = 0;
    } else if (result.length) {
      result[result.length - 1] += widths[index];
    } else {
      carried += widths[index];
    }
  });
  return result;
}
