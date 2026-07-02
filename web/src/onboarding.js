import './auth.css';
import './onboarding.css';
import { authFetch, hasSessionHint, loadCurrentUser } from './auth.js';
import { applyTranslations, getLocale, mountLanguageSwitcher, t } from './i18n.js';

if (!hasSessionHint()) {
  window.location.href = '/login.html';
}

document.documentElement.lang = getLocale();
applyTranslations();
mountLanguageSwitcher(document.getElementById('lang-switcher'))?.addEventListener('change', () => refreshState());

const els = {
  loading: document.getElementById('step-loading'),
  progress: document.getElementById('progress'),
  steps: {
    pending_credentials: document.getElementById('step-credentials'),
    pending_branches: document.getElementById('step-branches'),
    done: document.getElementById('step-done'),
  },
  credsForm: document.getElementById('creds-form'),
  credsSubmit: document.getElementById('creds-submit'),
  credsError: document.getElementById('creds-error'),
  branchesForm: document.getElementById('branches-form'),
  branchesSubmit: document.getElementById('branches-submit'),
  branchesError: document.getElementById('branches-error'),
  branchesList: document.getElementById('branches-list'),
};

let currentUser = null;
let credentialId = null;
let availableCompanies = [];

function showStep(step) {
  els.loading.hidden = true;
  Object.entries(els.steps).forEach(([key, el]) => {
    if (el) el.hidden = key !== step;
  });
  els.progress.querySelectorAll('li').forEach((li) => {
    const liStep = li.dataset.step;
    li.classList.remove('is-active', 'is-done');
    if (step === 'done') {
      li.classList.add('is-done');
    } else if (liStep === step) {
      li.classList.add('is-active');
    } else if (isStepBefore(liStep, step)) {
      li.classList.add('is-done');
    }
  });
}

const STEP_ORDER = ['pending_credentials', 'pending_branches', 'done'];
function isStepBefore(a, b) {
  return STEP_ORDER.indexOf(a) < STEP_ORDER.indexOf(b);
}

function showError(el, message) {
  if (!el) return;
  el.textContent = message;
  el.hidden = false;
}

function clearAlert(el) {
  if (!el) return;
  el.textContent = '';
  el.hidden = true;
}

function renderBranches(companies) {
  if (!companies.length) {
    els.branchesList.innerHTML = `<div class="empty compact">${t('onboarding.noBranches')}</div>`;
    return;
  }
  els.branchesList.innerHTML = companies
    .map(
      (item) => `
        <label>
          <input type="checkbox" data-company-id="${item.company_id}" checked />
          <span class="branch-title">${escapeHtml(item.title || `${t('onboarding.branchFallback')} ${item.company_id}`)}</span>
          <span class="branch-meta">${escapeHtml(item.group_title || '')} · id ${item.company_id}</span>
        </label>
      `,
    )
    .join('');
  els.branchesSubmit.disabled = false;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

async function refreshState() {
  els.loading.hidden = false;
  Object.values(els.steps).forEach((el) => {
    if (el) el.hidden = true;
  });

  try {
    if (!currentUser) {
      const me = await loadCurrentUser();
      currentUser = me.data;
      if (currentUser.role !== 'owner') {
        window.location.href = '/';
        return;
      }
    }
    const response = await authFetch('/onboarding/state');
    const data = response.data || {};
    if (data.credentials && data.credentials.length && !credentialId) {
      credentialId = data.credentials[0].id;
    }
    showStep(data.step || 'done');
  } catch (error) {
    els.loading.textContent = `${t('common.errorPrefix')}: ${error.message}`;
  }
}

els.credsForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  clearAlert(els.credsError);
  els.credsSubmit.disabled = true;
  els.credsSubmit.textContent = t('onboarding.credsSubmitLoading');
  try {
    const body = {
      title: document.getElementById('title').value.trim() || 'YClients integration',
      partner_token: document.getElementById('partner_token').value.trim(),
      login: document.getElementById('login').value.trim(),
      password: document.getElementById('password').value,
    };
    const response = await authFetch('/onboarding/credentials', {
      method: 'POST',
      body: JSON.stringify(body),
    });
    credentialId = response.data.credential_id;
    availableCompanies = response.data.companies || [];
    renderBranches(availableCompanies);
    showStep('pending_branches');
  } catch (error) {
    showError(els.credsError, error.message);
  } finally {
    els.credsSubmit.disabled = false;
    els.credsSubmit.textContent = t('onboarding.credsSubmit');
  }
});

els.branchesForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  clearAlert(els.branchesError);
  const company_ids = [...els.branchesList.querySelectorAll('input[type="checkbox"]:checked')].map((cb) =>
    Number(cb.dataset.companyId),
  );
  if (!company_ids.length) {
    showError(els.branchesError, t('onboarding.selectOne'));
    return;
  }
  if (!credentialId) {
    showError(els.branchesError, t('onboarding.needCreds'));
    return;
  }
  els.branchesSubmit.disabled = true;
  els.branchesSubmit.textContent = t('onboarding.branchesSubmitLoading');
  try {
    await authFetch('/onboarding/branches', {
      method: 'POST',
      body: JSON.stringify({ credential_id: credentialId, company_ids }),
    });
    showStep('done');
  } catch (error) {
    showError(els.branchesError, error.message);
  } finally {
    els.branchesSubmit.disabled = false;
    els.branchesSubmit.textContent = t('onboarding.branchesSubmit');
  }
});

refreshState();
