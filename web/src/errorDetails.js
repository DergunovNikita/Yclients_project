// What a failed request tells a person who has to report it: the banner shows the translated message, and the «i»
// control of the banner shows these details (see `renderAlert`). Built from the request and the server answer, never
// from the request body: a Pydantic 422 echoes the submitted value in `input`, which for a password form is the password.

import { t } from './i18n.js';

const MAX_SERVER_TEXT = 500;

function pad(value) {
  return String(value).padStart(2, '0');
}

// Local wall-clock time of the failure, with the date: a support message is read a day later.
function localTimestamp(now) {
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} `
    + `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
}

function issueText(item) {
  if (!item || typeof item !== 'object') return String(item);
  const where = Array.isArray(item.loc) ? item.loc.join('.') : '';
  const text = item.msg || item.message || item.type || '';
  return where ? `${where}: ${text}` : String(text);
}

/** The server's `detail` as plain text: field path and message per issue, no echoed input. */
export function serverDetailText(payload) {
  const detail = payload?.detail ?? payload?.message;
  if (detail === undefined || detail === null || detail === '') return '';
  let text;
  if (Array.isArray(detail)) text = detail.map(issueText).join('; ');
  else if (typeof detail === 'object') text = issueText(detail) || JSON.stringify(detail);
  else text = String(detail);
  return text.length > MAX_SERVER_TEXT ? `${text.slice(0, MAX_SERVER_TEXT)}…` : text;
}

/**
 * True when a 422 payload says the email field is not an address (Pydantic `EmailStr`): the raw English text is replaced
 * by a translated hint.
 */
export function isInvalidEmailPayload(payload) {
  const detail = payload?.detail;
  if (!Array.isArray(detail)) return false;
  return detail.some((item) => Array.isArray(item?.loc)
    && item.loc.includes('email')
    && (item.type === 'value_error' || /email/i.test(String(item.msg || ''))));
}

/**
 * @param {{method?: string, path: string, status?: number|null, payload?: object, browserMessage?: string, now?: Date}} source
 * @returns {{method: string, path: string, status: number|null, server: string, browser: string, time: string}}
 */
export function buildErrorDetails({ method, path, status = null, payload, browserMessage = '', now = new Date() }) {
  return {
    method: String(method || 'GET').toUpperCase(),
    // The query string can carry filters or ids; the path is enough to find the route.
    path: String(path || '').split('?')[0],
    status: status ?? null,
    server: serverDetailText(payload),
    browser: String(browserMessage || ''),
    time: localTimestamp(now),
  };
}

/** The details as the lines the banner shows; empty parts are left out. */
export function formatErrorDetails(details) {
  if (!details) return '';
  const lines = [
    `${t('errorDetails.request')}: ${details.method} ${details.path}`,
    `${t('errorDetails.response')}: ${details.status ? `HTTP ${details.status}` : t('errorDetails.noResponse')}`,
  ];
  if (details.server) lines.push(`${t('errorDetails.server')}: ${details.server}`);
  if (details.browser) lines.push(`${t('errorDetails.browser')}: ${details.browser}`);
  lines.push(`${t('errorDetails.time')}: ${details.time}`);
  return lines.join('\n');
}

/**
 * Fills an `.alert` with the message of an error (or a plain string) and shows it. An error that carries `details` turns
 * the round icon on the left into an «i» button that unfolds them under the message; without details the banner is the
 * plain icon and text.
 *
 * @param {HTMLElement} el
 * @param {string|{message: string, details?: object}} source
 */
export function renderAlert(el, source) {
  if (!el) return;
  const message = typeof source === 'string' ? source : String(source?.message ?? '');
  const text = typeof source === 'string' ? '' : formatErrorDetails(source?.details);
  el.classList.remove('alert--has-details');
  if (!text) {
    el.textContent = message;
    el.hidden = false;
    return;
  }

  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'alert__info';
  button.textContent = 'i';
  button.title = text;
  button.setAttribute('aria-label', t('errorDetails.toggle'));
  button.setAttribute('aria-expanded', 'false');

  const body = document.createElement('div');
  body.className = 'alert__body';
  const messageEl = document.createElement('span');
  messageEl.textContent = message;
  const detailsEl = document.createElement('pre');
  detailsEl.className = 'alert__details';
  detailsEl.textContent = text;
  detailsEl.hidden = true;
  body.append(messageEl, detailsEl);

  button.addEventListener('click', () => {
    const open = detailsEl.hidden;
    detailsEl.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
  });

  el.replaceChildren(button, body);
  el.classList.add('alert--has-details');
  el.hidden = false;
}
