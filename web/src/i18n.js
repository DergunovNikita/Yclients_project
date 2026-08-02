import ru from '../locales/ru.json';
import en from '../locales/en.json';
import it from '../locales/it.json';

const LOCALE_KEY = 'app_locale';
const DEFAULT_LOCALE = 'it';
const messages = { ru, en, it };
export const LOCALES = Object.keys(messages);
export const LOCALE_CHANGE_EVENT = 'app:localechange';

function lookup(dict, key) {
  return key.split('.').reduce((node, part) => (node == null ? undefined : node[part]), dict);
}

export function getLocale() {
  const stored = localStorage.getItem(LOCALE_KEY);
  if (LOCALES.includes(stored)) return stored;
  const candidates = typeof navigator === 'undefined'
    ? []
    : [navigator.language, ...(navigator.languages || [])]
      .filter(Boolean)
      .map((value) => String(value).toLowerCase().split('-')[0]);
  return candidates.find((locale) => LOCALES.includes(locale)) || DEFAULT_LOCALE;
}

/**
 * Translate a dotted key for the active locale.
 * Falls back to Italian, then to the key itself.
 */
export function t(key, paramsOrLocale = {}, maybeLocale) {
  const params = typeof paramsOrLocale === 'string' ? {} : paramsOrLocale;
  const locale = typeof paramsOrLocale === 'string' ? paramsOrLocale : (maybeLocale || getLocale());
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

export function setLocale(locale) {
  if (!LOCALES.includes(locale)) return getLocale();
  const previous = getLocale();
  localStorage.setItem(LOCALE_KEY, locale);
  document.documentElement.lang = locale;
  applyTranslations();
  if (previous !== locale) {
    window.dispatchEvent(new CustomEvent(LOCALE_CHANGE_EVENT, { detail: { locale } }));
  }
  return locale;
}

const INTL_LOCALE = { ru: 'ru-RU', en: 'en-US', it: 'it-IT' };

export function intlLocale() {
  return INTL_LOCALE[getLocale()] || INTL_LOCALE[DEFAULT_LOCALE];
}

export function formatMoney(amount, currency = 'RUB') {
  return new Intl.NumberFormat(intlLocale(), { style: 'currency', currency }).format(amount);
}

export function formatDate(date) {
  return new Intl.DateTimeFormat(intlLocale()).format(date instanceof Date ? date : new Date(date));
}

export function formatNumber(n) {
  return new Intl.NumberFormat(intlLocale()).format(n);
}

export function userDataLoadErrorMessage() {
  return t('common.reloadDataAgain');
}

/**
 * Mount a <select> language switcher into `container` and wire it to setLocale.
 * Reloads translations in place; no page reload.
 */
export function mountLanguageSwitcher(container) {
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
  select.addEventListener('change', (event) => setLocale(event.target.value));
  container.appendChild(select);
  return select;
}
