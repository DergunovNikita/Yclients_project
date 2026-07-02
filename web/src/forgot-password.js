import './auth.css';
import { authFetch } from './auth.js';
import { applyTranslations, getLocale, mountLanguageSwitcher, t } from './i18n.js';

document.documentElement.lang = getLocale();
applyTranslations();
const syncTitle = () => {
  document.title = `${t('forgot.title')} — ${t('brand.name')}`;
};
syncTitle();
mountLanguageSwitcher(document.getElementById('lang-switcher'))?.addEventListener('change', syncTitle);

const form = document.getElementById('auth-form');
const errorEl = document.getElementById('error');
const successEl = document.getElementById('success');
const submitBtn = document.getElementById('submit');

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  errorEl.hidden = true;
  successEl.hidden = true;
  submitBtn.disabled = true;
  submitBtn.classList.add('is-loading');
  try {
    const email = document.getElementById('email').value.trim();
    const payload = await authFetch('/auth/forgot-password', {
      method: 'POST',
      body: JSON.stringify({ email }),
    });
    successEl.textContent = payload.message;
    successEl.hidden = false;
  } catch (error) {
    errorEl.textContent = error.message;
    errorEl.hidden = false;
  } finally {
    submitBtn.disabled = false;
    submitBtn.classList.remove('is-loading');
  }
});
