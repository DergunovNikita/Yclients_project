import ru from '../locales/ru.json';
import en from '../locales/en.json';
import it from '../locales/it.json';

const LOCALE_KEY = 'app_locale';
const DEFAULT_LOCALE = 'it';
const messages = { ru, en, it };
export const LOCALES = Object.keys(messages);
let cachedLocale = null;

function lookup(dict, key) {
  return key.split('.').reduce((node, part) => (node == null ? undefined : node[part]), dict);
}

// Browsers with site data blocked throw on any localStorage access; the locale must still resolve.
function readStoredLocale() {
  try {
    return localStorage.getItem(LOCALE_KEY);
  } catch {
    return null;
  }
}

function resolveLocale() {
  const stored = readStoredLocale();
  if (LOCALES.includes(stored)) return stored;
  const candidates = typeof navigator === 'undefined'
    ? []
    : [navigator.language, ...(navigator.languages || [])]
      .filter(Boolean)
      .map((value) => String(value).toLowerCase().split('-')[0]);
  return candidates.find((locale) => LOCALES.includes(locale)) || DEFAULT_LOCALE;
}

// t() runs thousands of times per render; resolve once and let setLocale invalidate.
export function getLocale() {
  if (cachedLocale === null) cachedLocale = resolveLocale();
  return cachedLocale;
}

/**
 * Translate a dotted key for the active locale.
 * Falls back to Italian, then to the key itself.
 */
export function t(key, params = {}) {
  const locale = getLocale();
  const value = lookup(messages[locale], key);
  const fallback = lookup(messages[DEFAULT_LOCALE], key);
  const template = value != null ? value : (fallback != null ? fallback : key);
  if (!params || typeof template !== 'string') return template;
  return template.replace(/\{(\w+)\}/g, (match, name) => (
    params[name] === undefined || params[name] === null ? match : String(params[name])
  ));
}

export function applyTranslations(root = document) {
  root.querySelectorAll('[data-i18n]').forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  root.querySelectorAll('[data-i18n-attr]').forEach((el) => {
    // Format: "placeholder:login.emailPlaceholder,title:common.email"
    el.dataset.i18nAttr.split(',').forEach((pair) => {
      const [attr, key] = pair.split(':').map((part) => part.trim());
      if (attr && key) el.setAttribute(attr, t(key));
    });
  });
}

function setLocale(locale) {
  if (!LOCALES.includes(locale)) return getLocale();
  try {
    localStorage.setItem(LOCALE_KEY, locale);
  } catch {
    /* the choice then lasts until the page reloads */
  }
  cachedLocale = locale;
  document.documentElement.lang = locale;
  applyTranslations();
  return locale;
}

const INTL_LOCALE = { ru: 'ru-RU', en: 'en-US', it: 'it-IT' };

export function intlLocale() {
  return INTL_LOCALE[getLocale()] || INTL_LOCALE[DEFAULT_LOCALE];
}

export function userDataLoadErrorMessage() {
  return t('common.reloadDataAgain');
}

/**
 * Mount a <select> language switcher into `container` and wire it to setLocale.
 * Reloads translations in place; no page reload.
 */
export function mountLanguageSwitcher(container, { beforeChange, afterChange } = {}) {
  if (!container) return null;
  const existing = container.querySelector('select.lang-switcher');
  if (existing) return existing;
  const select = document.createElement('select');
  select.className = 'lang-switcher';
  select.setAttribute('aria-label', t('language.label'));
  LOCALES.forEach((locale) => {
    const option = document.createElement('option');
    option.value = locale;
    option.textContent = t(`language.${locale}`);
    if (locale === getLocale()) option.selected = true;
    select.appendChild(option);
  });
  select.addEventListener('change', async (event) => {
    const previousLocale = getLocale();
    const nextLocale = event.target.value;
    select.disabled = true;
    try {
      const allowed = beforeChange
        ? await beforeChange({ previousLocale, nextLocale })
        : true;
      if (!allowed) {
        select.value = previousLocale;
        return;
      }
      setLocale(nextLocale);
      if (afterChange) await afterChange({ previousLocale, nextLocale });
    } finally {
      select.disabled = false;
    }
  });
  container.appendChild(select);
  return select;
}

/**
 * Start of a public auth page: document language, translated markup, the page title and a language switcher
 * that keeps the title in step.
 */
export function initPage({ titleKey }) {
  document.documentElement.lang = getLocale();
  applyTranslations();
  const syncTitle = () => {
    document.title = `${t(titleKey)} — ${t('brand.name')}`;
  };
  syncTitle();
  mountLanguageSwitcher(document.getElementById('lang-switcher'))?.addEventListener('change', syncTitle);
}
