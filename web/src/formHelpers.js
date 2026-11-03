import { t } from './i18n.js';

// Busy state is identical everywhere a button drives one async action; error display is not
// (each caller shows it in a different place), so that part stays in the caller's own try/catch
// around this call.
export async function withBusyButton(button, action) {
  button.disabled = true;
  button.classList.add('is-loading');
  try {
    return await action();
  } finally {
    button.disabled = false;
    button.classList.remove('is-loading');
  }
}

export function showError(el, message) {
  if (!el) return;
  el.textContent = message;
  el.hidden = false;
}

export function clearAlert(el) {
  if (!el) return;
  el.textContent = '';
  el.hidden = true;
}

export function togglePasswordVisibility(input, button) {
  const visible = input.type === 'text';
  input.type = visible ? 'password' : 'text';
  button.textContent = visible ? t('common.showPassword') : t('common.hidePassword');
}

// `type="email"` in a browser accepts «user@host»; an address people can receive mail at has a dotted domain. A hint
// before the request, not validation: the server decides.
export function looksLikeEmail(value) {
  return /^[^\s@]+@[^\s@.]+(\.[^\s@.]+)+$/.test(String(value || '').trim());
}

const MAX_PASSWORD_BYTES = 72;
const MIN_PASSWORD_LENGTH = 8;

// The server hashes with bcrypt: shorter than 8 or longer than 72 BYTES (not characters) is refused with a 422.
// Returns the i18n key of the problem, or null for an acceptable (or empty, i.e. not entered) password.
export function passwordProblem(password) {
  if (!password) return null;
  // Characters as the server counts them (code points), not UTF-16 units: an emoji is one.
  if (Array.from(password).length < MIN_PASSWORD_LENGTH) return 'admin.passwordTooShort';
  if (new TextEncoder().encode(password).length > MAX_PASSWORD_BYTES) return 'admin.passwordTooLong';
  return null;
}
