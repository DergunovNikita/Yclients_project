import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

// The numbers behind these rules were measured live (min-content width of the 14-column tables against the work
// area at 1280-1920px); this only guards the rules from being edited back by accident.
const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
const rule = (selector) => {
  const start = html.indexOf(`${selector} {`);
  assert.notEqual(start, -1, selector);
  return html.slice(start, html.indexOf('}', start));
};

test('only the row-label cell is left to size itself, so the 14 columns fit at 1280px', () => {
  assert.doesNotMatch(rule('.row-label'), /\bwidth:/);
  assert.match(rule('.row-label'), /white-space: normal/);
  assert.match(rule('.plan-table th.number'), /text-transform: none/);
});

// The first column ("Разрез") is one fixed width in every plan table. The text box (width - 8px padding) is wider than the
// uppercase "OLDBOY МОСКВА" / "КОВАЛЕВСКОЙ, 6" (~114px) and narrower than "OLDBOY ЯХРОМСКАЯ" (~137px), which is what
// gives the breaks the owner asked for; re-measure before changing the number.
test('the first plan-table column has one fixed narrow width on desktop and leaves phones alone', () => {
  const start = html.indexOf('.branch-cell,\n      .plan-table thead th:first-child {\n        width:');
  assert.notEqual(start, -1);
  const block = html.slice(start, html.indexOf('}', start));
  const [, width] = block.match(/\bwidth: (\d+)px/);
  assert.equal(block.match(new RegExp(`min-width: ${width}px`))?.length, 1);
  assert.equal(block.match(new RegExp(`max-width: ${width}px`))?.length, 1);
  assert.ok(Number(width) - 8 > 114 && Number(width) - 8 < 137, width);
  // Inside the desktop-only query: the 560px rules pin their own 64px columns for the sticky pair.
  const media = html.lastIndexOf('@media', start);
  assert.match(html.slice(media, start), /^@media \(min-width: 561px\) \{\s*$/);
  assert.doesNotMatch(rule('.branch-cell'), /\bwidth:/);
});

test('plan table header is sticky; a table that fits is not a scroll container', () => {
  assert.match(rule('.plan-table thead th'), /position: sticky/);
  assert.match(rule('.table-scroll--sticky-head.table-scroll--fits'), /overflow: clip/);
  assert.match(rule('.table-scroll--sticky-head'), /max-height: min\(75dvh, 720px\)/);
});

test('the page width does not depend on the jump nav', () => {
  // The nav lives in a permanent gutter (38px + 6px offset fits into 32px + the panel padding); a width that shrank
  // while the nav was shown made the network view wider than a branch view.
  assert.doesNotMatch(html, /body\.jump-nav-visible \.shell/);
  assert.match(rule('.shell'), /width: min\(1180px, calc\(100% - 64px\)\)/);
  assert.match(rule('.shell--wide'), /width: min\(1680px, calc\(100% - 64px\)\)/);
  const css = readFileSync(new URL('../src/jumpNav.css', import.meta.url), 'utf8');
  assert.doesNotMatch(css, /body\.jump-nav-visible \.shell \{\s*width/);
  const nav = css.match(/\.jump-nav \{[^}]*\}/)[0];
  const offset = Number(nav.match(/right: (\d+)px/)[1]);
  const gutter = 32;
  const panelInset = 16 + 1;
  assert.ok(offset + 38 <= gutter + panelInset, 'the nav must stay inside the gutter plus the panel padding');
});

test('the fit decision does not depend on the box being a scroll container right now', () => {
  const main = readFileSync(new URL('../src/main.js', import.meta.url), 'utf8');
  const start = main.indexOf('function syncPlanTableFit');
  assert.notEqual(start, -1);
  const body = main.slice(start, main.indexOf('\n}\n', start));
  // A vertical scrollbar shrinks clientWidth, so scrollWidth <= clientWidth would keep a table that sits between the
  // two widths in whatever state it was in; the table's own width against the box's full inner width does not.
  assert.doesNotMatch(body, /clientWidth/);
  assert.doesNotMatch(body, /scrollWidth/);
  assert.match(body, /table\.offsetWidth/);
});

test('a jump to a sub-header row leaves room for the sticky table header above it', () => {
  // scrollIntoView puts the row at `scroll-margin-top`; the default 16px would slide it under the sticky <thead>.
  // The offset is the measured header height (published by syncPlanTableFit), not a guess that a locale can outgrow.
  assert.match(rule('.plan-table .plan-subheader th[data-jump-target]'), /scroll-margin-top: calc\(var\(--plan-head-height, \d+px\) \+ \d+px\)/);
  const main = readFileSync(new URL('../src/main.js', import.meta.url), 'utf8');
  assert.match(main, /setProperty\('--plan-head-height'/);
});
