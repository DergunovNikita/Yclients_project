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
