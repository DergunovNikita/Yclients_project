import './auth.css';
import {
  authFetch,
  ensureOnboardingComplete,
  isTransientAuthError,
  safeReturnTo,
  setSelectedPortalAccountId,
  wait,
} from './auth.js';
import { togglePasswordVisibility, withBusyButton } from './formHelpers.js';
import { initPage } from './i18n.js';

initPage({ titleKey: 'login.title' });

const form = document.getElementById('auth-form');
const errorEl = document.getElementById('error');
const submitBtn = document.getElementById('submit');
const passwordInput = document.getElementById('password');
const togglePasswordBtn = document.getElementById('toggle-password');

function loginDestination() {
  return safeReturnTo(new URLSearchParams(window.location.search).get('return_to'));
}

togglePasswordBtn?.addEventListener('click', () => togglePasswordVisibility(passwordInput, togglePasswordBtn));

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

// Production runs with public registration off, so the link is a dead end unless the build opts in.
if (import.meta.env.VITE_PUBLIC_REGISTRATION === 'true') {
  document.getElementById('register-link').hidden = false;
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
  try {
    await withBusyButton(submitBtn, async () => {
      const email = document.getElementById('email').value.trim();
      const password = document.getElementById('password').value;
      const payload = await loginWithRetry(email, password);
      const user = payload.data.user;
      if (!(await ensureOnboardingComplete(user))) return;
      window.location.href = loginDestination();
    });
  } catch (error) {
    errorEl.textContent = error.message;
    errorEl.hidden = false;
  }
});
