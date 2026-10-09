/** Alias → canonical id, built from the catalog's `aliases` (the backend stays the one source of truth). */
export function buildAliasMap(reports = []) {
  const aliases = new Map();
  reports.forEach((report) => {
    (report.aliases || []).forEach((alias) => aliases.set(alias, report.id));
  });
  return aliases;
}

/**
 * What an id from the URL means for this catalog.
 *
 * `renamed` is set when it was a retired alias, so the page can rewrite the address and say why.
 * An unknown id is `{ id: '', unknown: true }`; no id at all is the catalog itself.
 */
export function resolveReportRoute(rawId, reports = []) {
  if (!rawId) return { id: '', renamed: false, unknown: false };
  if (reports.some((report) => report.id === rawId)) return { id: rawId, renamed: false, unknown: false };
  const canonical = buildAliasMap(reports).get(rawId);
  if (canonical) return { id: canonical, renamed: true, unknown: false };
  return { id: '', renamed: false, unknown: true };
}

/** Path segment of a report URL, or '' when it is not decodable (a stray `%` must not crash the page). */
export function decodePathSegment(segment) {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

/**
 * Favourites rewritten for the current catalog: retired ids follow their alias, duplicates collapse.
 *
 * Ids this catalog does not list stay as they are: the catalog is filtered by role, so a report
 * hidden from this login may still be a favourite of another login in the same browser.
 * Returns the list and whether it differs from the stored one, so storage is rewritten only when needed.
 */
export function migrateFavorites(stored, reports = []) {
  const aliases = buildAliasMap(reports);
  const list = Array.isArray(stored) ? stored.filter((id) => typeof id === 'string') : [];
  const migrated = [...new Set(list.map((id) => aliases.get(id) || id))];
  const changed = migrated.length !== (Array.isArray(stored) ? stored.length : 0)
    || migrated.some((id, index) => id !== stored[index]);
  return { favorites: migrated, changed };
}
