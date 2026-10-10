import assert from 'node:assert/strict';
import test from 'node:test';

globalThis.localStorage = { getItem: () => 'ru', setItem() {} };

const switcherListeners = [];
const options = [];
const container = {
  querySelector: () => null,
  appendChild: (node) => { container.child = node; },
};
globalThis.document = {
  documentElement: { lang: '' },
  title: '',
  querySelectorAll: () => [],
  getElementById: (id) => (id === 'lang-switcher' ? container : null),
  createElement: () => ({
    setAttribute() {},
    appendChild: (option) => options.push(option),
    addEventListener: (type, listener) => switcherListeners.push([type, listener]),
  }),
};

const { initPage } = await import('../src/i18n.js');

test('a public auth page gets its language, its title and a switcher that keeps the title in step', () => {
  initPage({ titleKey: 'login.title' });
  assert.equal(document.documentElement.lang, 'ru');
  assert.equal(document.title, 'Вход — Salon Analytics');
  assert.equal(options.length, 3, 'one option per locale');
  assert.deepEqual(switcherListeners.map(([type]) => type), ['change', 'change']);

  document.title = '';
  switcherListeners[1][1]();
  assert.equal(document.title, 'Вход — Salon Analytics', 'the second listener is the title sync');
});

test('every title key the five pages pass exists in every locale', async () => {
  const { readFileSync } = await import('node:fs');
  for (const code of ['ru', 'en', 'it']) {
    const locale = JSON.parse(readFileSync(new URL(`../locales/${code}.json`, import.meta.url), 'utf8'));
    for (const [group, key] of [['login', 'title'], ['register', 'title'], ['forgot', 'title'], ['reset', 'title'], ['verify', 'title']]) {
      assert.equal(typeof locale[group][key], 'string', `${code}: ${group}.${key}`);
    }
    assert.equal(typeof locale.brand.name, 'string');
  }
});

test('each public auth page starts through initPage with its own title key', async () => {
  const { readFileSync } = await import('node:fs');
  const pages = {
    login: 'login.title',
    register: 'register.title',
    'forgot-password': 'forgot.title',
    'reset-password': 'reset.title',
    'verify-email': 'verify.title',
  };
  for (const [page, key] of Object.entries(pages)) {
    const source = readFileSync(new URL(`../src/${page}.js`, import.meta.url), 'utf8');
    assert.ok(source.includes(`initPage({ titleKey: '${key}' });`), `${page} passes ${key}`);
    assert.equal(/document\.documentElement\.lang|mountLanguageSwitcher\(/.test(source), false, `${page} no longer boots by hand`);
  }
});
