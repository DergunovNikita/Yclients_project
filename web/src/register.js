import './auth.css';
import { authFetch } from './auth.js';
import { withBusyButton } from './formHelpers.js';
import { initPage, t } from './i18n.js';

initPage({ titleKey: 'register.title' });

const form = document.getElementById('auth-form');
const errorEl = document.getElementById('error');
const successEl = document.getElementById('success');
const submitBtn = document.getElementById('submit');

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  errorEl.hidden = true;
  successEl.hidden = true;
  try {
    await withBusyButton(submitBtn, async () => {
      const email = document.getElementById('email').value.trim();
      const password = document.getElementById('password').value;
      const full_name = document.getElementById('full_name').value.trim() || null;
      const payload = await authFetch('/auth/register', {
        method: 'POST',
        body: JSON.stringify({ email, password, full_name }),
      });
      successEl.textContent = payload.message_key
        ? t(payload.message_key)
        : (payload.message || t('register.success'));
      successEl.hidden = false;
      form.reset();
    });
  } catch (error) {
    errorEl.textContent = error.message;
    errorEl.hidden = false;
  }
});
