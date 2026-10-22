import assert from 'node:assert/strict';
import test from 'node:test';

let storedLocale = 'ru';
globalThis.localStorage = { getItem: () => storedLocale, setItem: (key, value) => { storedLocale = value; } };

const switcherListeners = [];
const container = {
  querySelector: () => null,
  appendChild: () => {},
};
globalThis.document = {
  documentElement: { lang: '' },
  title: '',
  querySelectorAll: () => [],
  getElementById: (id) => (id === 'lang-switcher' ? container : null),
  createElement: () => ({
    setAttribute() {},
    appendChild() {},
    addEventListener: (type, listener) => { if (type === 'change') switcherListeners.push(listener); },
  }),
};

const { initPage } = await import('../src/i18n.js');

test('a public auth page gets its language and title, and the title follows a language switch', async () => {
  initPage({ titleKey: 'login.title' });
  assert.equal(document.documentElement.lang, 'ru');
  assert.equal(document.title, 'Вход — Salon Analytics');

  const select = { value: 'en', disabled: false };
  await Promise.all(switcherListeners.map((listener) => listener({ target: select })));
  assert.equal(document.documentElement.lang, 'en');
  assert.equal(document.title, 'Sign in — Salon Analytics');
});
