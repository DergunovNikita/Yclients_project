import assert from 'node:assert/strict';
import test from 'node:test';

import { FILTER_HEIGHT_VARIABLE, publishFilterHeight } from '../src/reports/stickyFilters.js';

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
