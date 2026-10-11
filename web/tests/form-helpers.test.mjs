import assert from 'node:assert/strict';
import test from 'node:test';

globalThis.localStorage = { getItem: () => 'en', setItem() {} };

const { clearAlert, showError, togglePasswordVisibility, withBusyButton } = await import('../src/formHelpers.js');

const fakeButton = () => {
  const classes = new Set();
  return { disabled: false, textContent: '', classList: { add: (name) => classes.add(name), remove: (name) => classes.delete(name), has: (name) => classes.has(name) } };
};

test('withBusyButton disables the button for the action and restores it after success and failure', async () => {
  const button = fakeButton();
  let seen;
  const result = await withBusyButton(button, async () => {
    seen = { disabled: button.disabled, loading: button.classList.has('is-loading') };
    return 7;
  });
  assert.equal(result, 7);
  assert.deepEqual(seen, { disabled: true, loading: true });
  assert.equal(button.disabled, false);
  assert.equal(button.classList.has('is-loading'), false);

  await assert.rejects(withBusyButton(button, async () => { throw new Error('nope'); }), /nope/);
  assert.equal(button.disabled, false);
  assert.equal(button.classList.has('is-loading'), false);
});

test('showError and clearAlert write and empty an alert, and tolerate a missing element', () => {
  const el = { textContent: '', hidden: true };
  showError(el, 'bad');
  assert.deepEqual(el, { textContent: 'bad', hidden: false });
  clearAlert(el);
  assert.deepEqual(el, { textContent: '', hidden: true });
  assert.doesNotThrow(() => { showError(null, 'x'); clearAlert(null); });
});

test('togglePasswordVisibility flips the input type and the button label', () => {
  const input = { type: 'password' };
  const button = { textContent: '' };
  togglePasswordVisibility(input, button);
  assert.equal(input.type, 'text');
  const hide = button.textContent;
  togglePasswordVisibility(input, button);
  assert.equal(input.type, 'password');
  assert.notEqual(button.textContent, hide);
});
