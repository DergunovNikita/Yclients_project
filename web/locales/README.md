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
`setLocale`). Current HTML uses Italian inline text as the no-JS fallback; `ru.json`
remains the source of truth for the translation-key set.

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

## Wired pages

All Vite entrypoints are wired to the runtime i18n module:

- dashboard/reports: `index.html` + `src/main.js`;
- auth: login, register, forgot/reset password and email verification;
- portal: onboarding, profile, settings and admin.

Each page sets `<html lang>`, applies translations and mounts a language switcher
where the layout provides one. New entrypoints should follow the same pattern and
must add every new key to all three dictionaries.
