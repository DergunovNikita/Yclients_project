import './auth.css';
import { authFetch } from './auth.js';
import { applyTranslations, getLocale, mountLanguageSwitcher, t } from './i18n.js';

document.documentElement.lang = getLocale();
applyTranslations();
const syncTitle = () => {
  document.title = `${t('register.title')} — ${t('brand.name')}`;
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
    const password = document.getElementById('password').value;
    const full_name = document.getElementById('full_name').value.trim() || null;
    await authFetch('/auth/register', {
      method: 'POST',
      body: JSON.stringify({ email, password, full_name }),
    });
    successEl.textContent = t('register.success');
    successEl.hidden = false;
    form.reset();
  } catch (error) {
    errorEl.textContent = error.message;
    errorEl.hidden = false;
  } finally {
    submitBtn.disabled = false;
    submitBtn.classList.remove('is-loading');
  }
});
