import './auth.css';
import { authFetch, isTransientAuthError, setSelectedPortalAccountId, wait } from './auth.js';
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
const passwordInput = document.getElementById('password');
const togglePasswordBtn = document.getElementById('toggle-password');

function safeReturnTo(value) {
  const raw = String(value || '').trim();
  if (!raw || raw.startsWith('//') || /^[a-z][a-z0-9+.-]*:/i.test(raw)) {
    return '/';
  }
  return raw.startsWith('/') ? raw : '/';
}

function loginDestination() {
  return safeReturnTo(new URLSearchParams(window.location.search).get('return_to'));
}

togglePasswordBtn?.addEventListener('click', () => {
  const visible = passwordInput.type === 'text';
  passwordInput.type = visible ? 'password' : 'text';
  togglePasswordBtn.textContent = visible ? t('common.showPassword') : t('common.hidePassword');
});

async function loginWithRetry(email, password) {
  let lastError;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      return await authFetch('/auth/login', {
        method: 'POST',
        body: JSON.stringify({ email, password }),
      });
    } catch (error) {
      lastError = error;
      if (!isTransientAuthError(error) || attempt === 1) break;
      await wait(450);
    }
  }
  throw lastError;
}

const DEMO_ORIGIN = import.meta.env.VITE_DEMO_ORIGIN;
const DEMO_AUTOLOGIN = import.meta.env.VITE_DEMO_AUTOLOGIN;
const demoBtn = document.getElementById('demo-login');

async function runDemoLogin() {
  errorEl.hidden = true;
  demoBtn?.classList.add('is-loading');
  if (demoBtn) demoBtn.disabled = true;
  try {
    setSelectedPortalAccountId('');
    await authFetch('/auth/demo-login', { method: 'POST' });
    setSelectedPortalAccountId('');
    window.location.href = loginDestination();
  } catch (error) {
    errorEl.textContent = error.message;
    errorEl.hidden = false;
  } finally {
    demoBtn?.classList.remove('is-loading');
    if (demoBtn) demoBtn.disabled = false;
  }
}

// On the production site VITE_DEMO_ORIGIN points at the dedicated demo instance,
// so the button sends the visitor there (that build auto-logs in via
// VITE_DEMO_AUTOLOGIN). On the demo instance itself VITE_DEMO_ORIGIN is unset, so
// the button performs the passwordless login same-origin.
demoBtn?.addEventListener('click', () => {
  if (DEMO_ORIGIN) {
    window.location.href = DEMO_ORIGIN;
    return;
  }
  runDemoLogin();
});

if (DEMO_AUTOLOGIN && !DEMO_ORIGIN) {
  runDemoLogin();
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  errorEl.hidden = true;
  submitBtn.disabled = true;
  submitBtn.classList.add('is-loading');
  try {
    const email = document.getElementById('email').value.trim();
    const password = document.getElementById('password').value;
    const payload = await loginWithRetry(email, password);
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
    window.location.href = loginDestination();
  } catch (error) {
    errorEl.textContent = error.message;
    errorEl.hidden = false;
  } finally {
    submitBtn.disabled = false;
    submitBtn.classList.remove('is-loading');
  }
});
