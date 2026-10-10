import './auth.css';
import { authFetch } from './auth.js';
import { initPage, t } from './i18n.js';

initPage({ titleKey: 'verify.title' });

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
    await authFetch('/auth/verify-email', {
      method: 'POST',
      body: JSON.stringify({ token }),
    });
    successEl.textContent = t('verify.success');
    successEl.hidden = false;
  } catch (error) {
    errorEl.textContent = error.message;
    errorEl.hidden = false;
  }
}

verify();
