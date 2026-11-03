// The branch, staff and level filters are `<select multiple>`: a selection is an array of option values, and an empty
// one means «all». Every read and write of these selects goes through here — a bare `.value` would return only the first
// chosen option. The helpers also accept a single select (the manual-fact editors keep theirs), whose `''` option is the
// «all» placeholder and never a choice.

/** Values of the chosen options, in option order; the `''` placeholder is not a choice. */
export function selectedValues(select) {
  return Array.from(select?.options || [])
    .filter((option) => option.selected && option.value !== '')
    .map((option) => option.value);
}

/** Choose exactly the options holding `values`; a single select takes the first of them, none selects its placeholder. */
export function setSelectedValues(select, values = []) {
  const wanted = new Set((select.multiple ? values : values.slice(0, 1)).map(String));
  Array.from(select.options).forEach((option) => {
    option.selected = wanted.has(option.value);
  });
}

/** The part of a selection that the list still offers: a stale choice must not filter by what is not shown. */
export function keepListed(values, listedValues) {
  const listed = new Set((listedValues || []).map(String));
  return (values || []).map(String).filter((value) => listed.has(value));
}

/** `1,2,3` as a request or link carries it; empty when nothing is chosen. */
export function idListParam(values) {
  return [...new Set((values || []).map(String))].join(',');
}

/** The ids of a comma-separated param, trimmed and unique; empty entries are dropped. */
export function parseIdList(text) {
  return [...new Set(String(text ?? '').split(',').map((part) => part.trim()).filter(Boolean))];
}
