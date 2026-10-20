// The report filter row sticks to the top of the page while a long report scrolls (see `.report-filter-row` in
// index.html). The jump nav scrolls to blocks of the report, and they must stop below the row, so its real height
// is published as a CSS variable the targets read for their `scroll-margin-top`. A ResizeObserver, not a one-off
// measure: the row changes height with the window width, with the compare fields and while the viewer is hidden.
export const FILTER_HEIGHT_VARIABLE = '--report-filter-height';

/** Keeps `--report-filter-height` on `host` equal to the height of `row`; returns the observer (null without support). */
export function publishFilterHeight(host, row, ObserverClass = globalThis.ResizeObserver) {
  if (!host || !row || !ObserverClass) return null;
  const publish = () => host.style.setProperty(FILTER_HEIGHT_VARIABLE, `${row.offsetHeight}px`);
  const observer = new ObserverClass(publish);
  observer.observe(row);
  publish();
  return observer;
}
