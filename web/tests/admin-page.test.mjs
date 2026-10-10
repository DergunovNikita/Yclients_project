import assert from 'node:assert/strict';
import test from 'node:test';

import { createModalManager, tenantRequestGuard } from '../src/adminPage.js';

function page(names) {
  const classes = new Set();
  const body = { classList: { add: (name) => classes.add(name), remove: (name) => classes.delete(name) } };
  const closed = [];
  const modals = Object.fromEntries(names.map((name) => [name, { name, hidden: true }]));
  let manager;
  const entries = names.map((name) => ({ modal: modals[name], close: () => { closed.push(name); manager.close(modals[name]); } }));
  manager = createModalManager(entries, body);
  return { manager, modals, classes, closed };
}

test('the body class stays while any modal is open', () => {
  const { manager, modals, classes } = page(['a', 'b']);
  manager.open(modals.a);
  manager.open(modals.b);
  assert.equal(classes.has('admin-modal-open'), true);
  manager.close(modals.a);
  assert.equal(classes.has('admin-modal-open'), true, 'b is still open');
  manager.close(modals.b);
  assert.equal(classes.has('admin-modal-open'), false);
  assert.equal(manager.anyOpen(), false);
});

test('Escape closes the top-most open modal, in the order the entries were given', () => {
  const { manager, modals, closed, classes } = page(['credentials', 'delete', 'create']);
  manager.open(modals.create);
  manager.open(modals.delete);
  assert.equal(manager.closeTopmost(), true);
  assert.deepEqual(closed, ['delete']);
  assert.equal(modals.create.hidden, false);
  assert.equal(manager.closeTopmost(), true);
  assert.deepEqual(closed, ['delete', 'create']);
  assert.equal(manager.closeTopmost(), false);
  assert.equal(classes.has('admin-modal-open'), false);
});

test('an answer for a tenant that is no longer selected is recognised as stale', () => {
  let selected = '1';
  const guard = tenantRequestGuard(() => selected);
  assert.equal(guard(), true);
  selected = '2';
  assert.equal(guard(), false);
  selected = '1';
  assert.equal(guard(), true, 'back on the same tenant, its own answer is still valid');
  assert.equal(tenantRequestGuard(() => selected)(), true);
});
