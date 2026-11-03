import assert from 'node:assert/strict';
import test from 'node:test';

globalThis.localStorage = { getItem: () => 'en', setItem() {} };

const { buildErrorDetails, formatErrorDetails, isInvalidEmailPayload, renderAlert, serverDetailText } = await import('../src/errorDetails.js');
const { looksLikeEmail, passwordProblem } = await import('../src/formHelpers.js');

const NOW = new Date(2026, 10, 3, 14, 5, 9);

test('a network failure has no status and carries the browser text', () => {
  const details = buildErrorDetails({
    method: 'post',
    path: '/auth/admin/staff/12/create-account',
    browserMessage: 'Failed to fetch',
    now: NOW,
  });
  assert.deepEqual(details, {
    method: 'POST',
    path: '/auth/admin/staff/12/create-account',
    status: null,
    server: '',
    browser: 'Failed to fetch',
    time: '2026-11-03 14:05:09',
  });
  const text = formatErrorDetails(details);
  assert.match(text, /POST \/auth\/admin\/staff\/12\/create-account/);
  assert.match(text, /Failed to fetch/);
  assert.doesNotMatch(text, /HTTP/);
});

test('an HTTP failure names the status and the server text, and the query string stays out', () => {
  const details = buildErrorDetails({
    path: '/auth/admin/users?search=anna@example.com',
    status: 400,
    payload: { detail: 'Invites are not deliverable' },
    now: NOW,
  });
  assert.equal(details.method, 'GET');
  assert.equal(details.path, '/auth/admin/users');
  const text = formatErrorDetails(details);
  assert.match(text, /HTTP 400/);
  assert.match(text, /Invites are not deliverable/);
  assert.doesNotMatch(text, /anna@example\.com/);
});

test('a 422 lists field and message per issue and never echoes the submitted input', () => {
  const payload = {
    detail: [{
      type: 'value_error',
      loc: ['body', 'email'],
      msg: 'value is not a valid email address',
      input: 'secret-password-typed-into-the-wrong-field',
    }],
  };
  const text = serverDetailText(payload);
  assert.equal(text, 'body.email: value is not a valid email address');
  assert.equal(isInvalidEmailPayload(payload), true);
  assert.equal(isInvalidEmailPayload({ detail: [{ loc: ['body', 'password'], msg: 'too short', type: 'string_too_short' }] }), false);
  assert.equal(isInvalidEmailPayload({ detail: 'Invalid email or password' }), false);
});

test('a very long server text is cut', () => {
  assert.equal(serverDetailText({ detail: 'x'.repeat(2000) }).length <= 501, true);
});

test('an alert without details is the plain message; with details it gets the info button and a folded block', () => {
  const makeEl = () => ({ classes: new Set(), hidden: true, textContent: '', children: null,
    classList: { add(name) { this.owner.classes.add(name); }, remove(name) { this.owner.classes.delete(name); } },
    replaceChildren(...nodes) { this.children = nodes; } });
  const withOwner = () => { const el = makeEl(); el.classList.owner = el; return el; };
  globalThis.document = {
    createElement: () => ({ attributes: {}, listeners: {}, children: [], hidden: false, textContent: '',
      setAttribute(name, value) { this.attributes[name] = value; },
      addEventListener(type, fn) { this.listeners[type] = fn; },
      append(...nodes) { this.children.push(...nodes); } }),
  };

  const plain = withOwner();
  renderAlert(plain, 'Something failed');
  assert.equal(plain.textContent, 'Something failed');
  assert.equal(plain.hidden, false);
  assert.equal(plain.classes.has('alert--has-details'), false);

  const detailed = withOwner();
  const error = Object.assign(new Error('No connection'), {
    details: buildErrorDetails({ path: '/auth/me', browserMessage: 'Failed to fetch', now: NOW }),
  });
  renderAlert(detailed, error);
  assert.equal(detailed.classes.has('alert--has-details'), true);
  const [button, body] = detailed.children;
  const [message, details] = body.children;
  assert.equal(message.textContent, 'No connection');
  assert.equal(details.hidden, true);
  assert.match(button.title, /Failed to fetch/);
  button.listeners.click();
  assert.equal(details.hidden, false);
  assert.equal(button.attributes['aria-expanded'], 'true');
  button.listeners.click();
  assert.equal(details.hidden, true);
});

test('an address needs a dotted domain', () => {
  assert.equal(looksLikeEmail('anna@company.ru'), true);
  assert.equal(looksLikeEmail(' anna@mail.company.ru '), true);
  assert.equal(looksLikeEmail('anna@company'), false);
  assert.equal(looksLikeEmail('anna@.ru'), false);
  assert.equal(looksLikeEmail('anna@company.'), false);
  assert.equal(looksLikeEmail('anna company@x.ru'), false);
  assert.equal(looksLikeEmail(''), false);
});

test('the password fits 8 characters and 72 bytes; an empty one is not a problem', () => {
  assert.equal(passwordProblem(''), null);
  assert.notEqual(passwordProblem('short'), null);
  assert.equal(passwordProblem('12345678'), null);
  assert.equal(passwordProblem('a'.repeat(72)), null);
  assert.notEqual(passwordProblem('a'.repeat(73)), null);
  assert.notEqual(passwordProblem('я'.repeat(40)), null, '40 Cyrillic letters are 80 bytes');
  assert.notEqual(passwordProblem('😀'.repeat(7)), null, '7 characters, though 14 UTF-16 units');
  assert.equal(passwordProblem('😀'.repeat(8)), null);
});
