import './auth.css';
import { authFetch, setToken } from './auth.js';
import { applyTranslations, getLocale, mountLanguageSwitcher, t } from './i18n.js';

document.documentElement.lang = getLocale();
applyTranslations();
const syncTitle = () => {
  document.title = `${t('login.title')} — ${t('brand.name')}`;
};
syncTitle();
mountLanguageSwitcher(document.getElementById('lang-switcher'))?.addEventListener('change', syncTitle);

const form = document.getElementById('auth-form');
const errorEl = document.getElementById('error');
const submitBtn = document.getElementById('submit');

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  errorEl.hidden = true;
  submitBtn.disabled = true;
  submitBtn.classList.add('is-loading');
  try {
    const email = document.getElementById('email').value.trim();
    const password = document.getElementById('password').value;
    const payload = await authFetch('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    });
    setToken(payload.data.access_token);
    const user = payload.data.user;
    if (user?.role === 'owner') {
      try {
        const state = await authFetch('/onboarding/state');
        if (state?.data?.step && state.data.step !== 'done') {
          window.location.href = '/onboarding.html';
          return;
        }
      } catch {
        /* fallback to dashboard on error — main.js will gate again */
      }
    }
    window.location.href = '/';
  } catch (error) {
    errorEl.textContent = error.message;
    errorEl.hidden = false;
  } finally {
    submitBtn.disabled = false;
    submitBtn.classList.remove('is-loading');
  }
});
