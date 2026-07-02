import './auth.css';
import { authFetch } from './auth.js';
import { applyTranslations, getLocale, mountLanguageSwitcher, t } from './i18n.js';

document.documentElement.lang = getLocale();
applyTranslations();
const syncTitle = () => {
  document.title = `${t('verify.title')} — ${t('brand.name')}`;
};
syncTitle();
mountLanguageSwitcher(document.getElementById('lang-switcher'))?.addEventListener('change', syncTitle);

const params = new URLSearchParams(window.location.search);
const token = params.get('token');
const errorEl = document.getElementById('error');
const successEl = document.getElementById('success');

async function verify() {
  if (!token) {
    errorEl.textContent = t('verify.missingToken');
    errorEl.hidden = false;
    return;
  }
  try {
    const payload = await authFetch('/auth/verify-email', {
      method: 'POST',
      body: JSON.stringify({ token }),
    });
    successEl.textContent = payload.message;
    successEl.hidden = false;
  } catch (error) {
    errorEl.textContent = error.message;
    errorEl.hidden = false;
  }
}

verify();
