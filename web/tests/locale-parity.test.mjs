import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

// Mirrors the locale set i18n.js imports (LOCALES = Object.keys({ ru, en, it })).
const LOCALES = ['ru', 'en', 'it'];

function leafKeys(node, prefix = '') {
  return Object.entries(node).flatMap(([key, value]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    return value && typeof value === 'object' && !Array.isArray(value)
      ? leafKeys(value, path)
      : [path];
  });
}

async function localeLeafKeys(locale) {
  const raw = await readFile(new URL(`../locales/${locale}.json`, import.meta.url), 'utf8');
  return new Set(leafKeys(JSON.parse(raw)));
}

test('the three locale files translate exactly the same set of keys', async () => {
  const keysByLocale = Object.fromEntries(
    await Promise.all(LOCALES.map(async (locale) => [locale, await localeLeafKeys(locale)])),
  );
  const allKeys = new Set(LOCALES.flatMap((locale) => [...keysByLocale[locale]]));

  // Checking each locale against the union of all three (rather than pairwise) still catches
  // any mismatch between any two files, and names exactly which keys and which file is short —
  // the three files are kept identical by hand and nothing else catches a key added to one
  // and not the others.
  for (const locale of LOCALES) {
    const missing = [...allKeys].filter((key) => !keysByLocale[locale].has(key)).sort();
    assert.deepEqual(missing, [], `${locale}.json is missing: ${missing.join(', ')}`);
  }
});
