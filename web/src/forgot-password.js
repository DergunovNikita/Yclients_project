import './auth.css';
import { authFetch } from './auth.js';
import { withBusyButton } from './formHelpers.js';
import { initPage, t } from './i18n.js';

initPage({ titleKey: 'forgot.title' });

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
      await authFetch('/auth/forgot-password', {
        method: 'POST',
        body: JSON.stringify({ email }),
      });
      successEl.textContent = t('forgot.success');
      successEl.hidden = false;
    });
  } catch (error) {
    errorEl.textContent = error.message;
    errorEl.hidden = false;
  }
});
