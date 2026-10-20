import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { FILTER_HEIGHT_VARIABLE, publishFilterHeight } from '../src/reports/stickyFilters.js';

const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
const jumpNavCss = readFileSync(new URL('../src/jumpNav.css', import.meta.url), 'utf8');

// The sticky rules live in a desktop-and-tablet query; `index.html` has one such block that names the report row.
const stickyBlock = (() => {
  const start = html.indexOf('.reports-viewer .report-filter-row {');
  assert.notEqual(start, -1);
  const media = html.lastIndexOf('@media', start);
  return { media: html.slice(media, start), rules: html.slice(start, html.indexOf('\n    }\n\n    .report-compare-row', start)) };
})();

test('the report filter row is sticky only where it is one or two lines (1101px up)', () => {
  assert.match(stickyBlock.media, /^@media \(min-width: 1101px\) \{\s*$/);
  assert.match(stickyBlock.rules, /position: sticky;\s*top: 0;/);
  assert.match(stickyBlock.rules, /box-shadow:/);
});

test('its z-index is over report content and under the jump nav, the save dock and the select menus', () => {
  const [, zIndex] = stickyBlock.rules.match(/z-index: (\d+);/);
  const jumpNavZ = Number(jumpNavCss.match(/\.jump-nav \{[^}]*z-index: (\d+)/)[1]);
  const dockZ = Number(html.match(/\.floating-editor-save \{[^}]*z-index: (\d+)/)[1]);
  const menuZ = Number(readFileSync(new URL('../src/custom-select.css', import.meta.url), 'utf8')
    .match(/\.custom-select__menu \{[^}]*z-index: (\d+)/)[1]);
  assert.ok(Number(zIndex) > 3, 'over the sticky table headers and chart boxes');
  assert.ok(Number(zIndex) < jumpNavZ && jumpNavZ < dockZ, `${zIndex} < ${jumpNavZ} < ${dockZ}`);
  assert.ok(menuZ > 0, 'menus open inside the row, so they stay above its own content');
});

test('no ancestor of the report viewer scrolls or clips, which would stop the row from sticking', () => {
  ['.shell', '.view', '.reports-viewer', 'body'].forEach((selector) => {
    const start = html.indexOf(`\n    ${selector} {`);
    assert.notEqual(start, -1, selector);
    assert.doesNotMatch(html.slice(start, html.indexOf('}', start)), /overflow/, selector);
  });
});

test('report jump targets stop below the sticky row', () => {
  assert.match(html, /\.reports-viewer \[data-jump-target\] \{\s*scroll-margin-top: calc\(var\(--report-filter-height, 0px\) \+ \d+px\);/);
});

test('publishFilterHeight keeps the variable equal to the row height as it resizes', () => {
  const props = new Map();
  const host = { style: { setProperty: (name, value) => props.set(name, value) } };
  const row = { offsetHeight: 168 };
  let notify;
  class FakeObserver {
    constructor(callback) { notify = callback; }
    observe(target) { this.target = target; }
  }
  const observer = publishFilterHeight(host, row, FakeObserver);
  assert.equal(observer.target, row);
  assert.equal(props.get(FILTER_HEIGHT_VARIABLE), '168px');
  row.offsetHeight = 96;
  notify();
  assert.equal(props.get(FILTER_HEIGHT_VARIABLE), '96px');
});

test('publishFilterHeight is a no-op without the elements or ResizeObserver', () => {
  assert.equal(publishFilterHeight(null, {}, class {}), null);
  assert.equal(publishFilterHeight({ style: {} }, null, class {}), null);
  assert.equal(publishFilterHeight({ style: {} }, {}, undefined), null);
});
