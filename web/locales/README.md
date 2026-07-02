# Frontend i18n

Runtime internationalization for the Vite frontend. Russian (`ru`) is the
**source of truth**; English (`en`) and Italian (`it`) are translations.

## Files

- `ru.json`, `en.json`, `it.json` — translation dictionaries (nested JSON).
- `../src/i18n.js` — ES module: `t`, `applyTranslations`, `getLocale`/`setLocale`,
  `mountLanguageSwitcher`, and `Intl` helpers (`formatMoney`, `formatDate`, `formatNumber`).

The active locale is persisted in `localStorage` under `app_locale`.

## Key naming convention

- Dotted, stable keys grouped by page or scope: `login.title`, `login.aside.feature1`.
- Cross-page/shared strings live under `common.*` (e.g. `common.email`).
- The product name is **never hardcoded** — always `brand.name` (and `brand.mark`
  for the logo glyph). The app is not YClients-branded; the current placeholder
  product name is **"Salon Analytics"**.
- Keys are `camelCase`; nesting reflects UI structure, not deep hierarchies (keep it flat-ish).

## How markup opts in

- `data-i18n="key"` → sets the element's `textContent` from `t(key)`.
- `data-i18n-attr="attr:key,attr2:key2"` → sets attributes (e.g. `placeholder:login.emailPlaceholder`).

Call `applyTranslations(root=document)` once on load (and it re-runs automatically on
`setLocale`). Keep the Russian text inline in the HTML as a no-JS fallback.

## Adding a language

1. Copy `ru.json` to `<code>.json` (e.g. `de.json`) and translate every value.
2. In `../src/i18n.js`: `import de from '../locales/de.json'` and add it to the
   `messages` map and to `INTL_LOCALE` (e.g. `de: 'de-DE'`).
3. Add a `language.<code>` label to **every** locale file so the switcher lists it.
4. Register the page inputs in `vite.config.js` are unaffected — no build change needed.

## Adding/using keys

1. Add the key to `ru.json` first, then mirror it into `en.json` and `it.json`.
2. Reference it via `data-i18n` / `data-i18n-attr` in markup, or `t('key')` in JS.
3. Missing keys fall back to `ru`, then to the literal key string.

## Wired page (proven pattern)

`login.html` + `src/login.js` are fully wired:
- markup carries `data-i18n` / `data-i18n-attr`,
- `login.js` sets `<html lang>`, calls `applyTranslations()`, composes the document
  title from `brand.name`, and mounts the `<select>` switcher via `mountLanguageSwitcher`.

## Rollout checklist (remaining pages)

For each page below, repeat the login pattern:

1. Add translation keys under a page scope (e.g. `register.*`) to `ru.json`, then
   mirror into `en.json` and `it.json`.
2. Add `data-i18n` / `data-i18n-attr` to the page's HTML (keep RU inline as fallback).
   De-hardcode any brand text to `brand.name` / `brand.mark`.
3. In the page's `src/*.js`, add at the top:
   ```js
   import { applyTranslations, getLocale, mountLanguageSwitcher } from './i18n.js';
   document.documentElement.lang = getLocale();
   applyTranslations();
   mountLanguageSwitcher(document.getElementById('lang-switcher')); // if a slot exists
   ```

Pages to convert:

- [ ] `register.html` + `src/register.js`
- [ ] `forgot-password.html` + `src/forgot-password.js`
- [ ] `reset-password.html` + `src/reset-password.js`
- [ ] `verify-email.html` + `src/verify-email.js`
- [ ] `onboarding.html` + `src/onboarding.js`
- [ ] `settings.html` + `src/settings.js`
- [ ] `admin.html` + `src/admin.js`
- [ ] `profile.html` + `src/profile.js`

### Deferred: `index.html` + `src/main.js`

These two are **intentionally deferred**: they had uncommitted work in progress at
the time i18n was introduced and must not be touched until that work lands. Convert
them last, after their pending changes are committed, using the same pattern (note
`main.js` is large — the dashboard/reports strings, so budget for a bigger key set).
