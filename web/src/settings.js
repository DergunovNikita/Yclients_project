import './auth.css';
import './settings.css';
import {
  authFetch,
  loadCurrentUser,
  logout,
  requireAuthRedirect,
  clearLocalSession,
  registerUnsavedWork,
  USER_ADMIN_ROLES,
} from './auth.js';
import { branchOrderChanged, moveBranch } from './branchOrder.js';
import { clearAlert, showError } from './formHelpers.js';
import { escapeHtml } from './html.js';
import { applyTranslations, getLocale, mountLanguageSwitcher, roleLabel, t } from './i18n.js';
import { formatBranchDateTime } from './timestamps.js';

document.documentElement.lang = getLocale();
applyTranslations();
mountLanguageSwitcher(document.getElementById('lang-switcher'))?.addEventListener('change', () => location.reload());

const els = {
  tabs: [...document.querySelectorAll('.settings-tab[data-tab]')],
  panels: [...document.querySelectorAll('.settings-panel')],
  logout: document.getElementById('logout'),
  profileName: document.getElementById('profile-name'),
  profileEmail: document.getElementById('profile-email'),
  profileRole: document.getElementById('profile-role'),
  profileTenant: document.getElementById('profile-tenant'),
  profileBranches: document.getElementById('profile-branches'),
  changePasswordForm: document.getElementById('change-password-form'),
  passwordError: document.getElementById('password-error'),
  passwordSuccess: document.getElementById('password-success'),
  sessionsList: document.getElementById('sessions-list'),
  sessionsError: document.getElementById('sessions-error'),
  refreshSessions: document.getElementById('refresh-sessions'),
  logoutAll: document.getElementById('logout-all'),
  credentialsList: document.getElementById('credentials-list'),
  credentialsError: document.getElementById('credentials-error'),
  refreshCredentials: document.getElementById('refresh-credentials'),
  addCredForm: document.getElementById('add-cred-form'),
  addCredError: document.getElementById('add-cred-error'),
  addCredSuccess: document.getElementById('add-cred-success'),
  branchOrderList: document.getElementById('branch-order-list'),
  branchOrderError: document.getElementById('branch-order-error'),
  branchOrderSuccess: document.getElementById('branch-order-success'),
  branchOrderTenantHint: document.getElementById('branch-order-tenant-hint'),
  branchOrderSave: document.getElementById('branch-order-save'),
  branchOrderReset: document.getElementById('branch-order-reset'),
};


let currentUser = null;

function selectTab(name) {
  els.tabs.forEach((tab) => tab.classList.toggle('is-active', tab.dataset.tab === name));
  els.panels.forEach((panel) => {
    panel.hidden = panel.dataset.panel !== name;
  });
  if (name === 'security') loadSessions();
  if (name === 'sources') loadCredentials();
  // Coming back to the tab keeps a reorder that is not saved yet.
  if (name === 'branches' && !branchOrderChanged(branchOrderSaved, branchOrderWorking)) loadBranchOrder();
}

els.tabs.forEach((tab) => {
  tab.addEventListener('click', (event) => {
    if (tab.tagName === 'A') return;
    event.preventDefault();
    selectTab(tab.dataset.tab);
  });
});

els.logout.addEventListener('click', () => logout());

async function loadProfile() {
  try {
    const me = await loadCurrentUser();
    currentUser = me.data;
    els.profileName.textContent = currentUser.full_name || '—';
    els.profileEmail.textContent = currentUser.email || '—';
    els.profileRole.textContent = roleLabel(currentUser.role);
    els.profileTenant.textContent = currentUser.portal_account_id ? `#${currentUser.portal_account_id}` : '—';
    const branches = (currentUser.company_ids || []).join(', ');
    els.profileBranches.textContent = branches || '—';

    const ownerOrAdmin = currentUser.role === 'owner' || currentUser.role === 'platform_admin';
    document.querySelector('[data-tab="sources"]').hidden = !ownerOrAdmin;
    document.querySelector('[data-tab="branches"]').hidden = !ownerOrAdmin;
    document.querySelector('[data-tab="team"]').hidden = !USER_ADMIN_ROLES.includes(currentUser.role);
  } catch (error) {
    showError(els.passwordError, `${t('settings.profileLoadError')}: ${error.message}`);
  }
}

els.changePasswordForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  clearAlert(els.passwordError);
  clearAlert(els.passwordSuccess);
  const current = document.getElementById('current-password').value;
  const next = document.getElementById('new-password').value;
  const confirm = document.getElementById('confirm-password').value;
  if (next !== confirm) {
    showError(els.passwordError, t('settings.passwordsMismatch'));
    return;
  }
  try {
    await authFetch('/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ current_password: current, new_password: next }),
    });
    els.passwordSuccess.textContent = t('settings.passwordUpdated');
    els.passwordSuccess.hidden = false;
    setTimeout(() => {
      clearLocalSession();
      window.location.href = '/login.html';
    }, 1500);
  } catch (error) {
    showError(els.passwordError, error.message);
  }
});

async function loadSessions() {
  clearAlert(els.sessionsError);
  els.sessionsList.innerHTML = `<div class="settings-empty">${t('common.loading')}</div>`;
  try {
    const response = await authFetch('/auth/sessions');
    const sessions = response.data || [];
    if (!sessions.length) {
      els.sessionsList.innerHTML = `<div class="settings-empty">${t('settings.noSessions')}</div>`;
      return;
    }
    els.sessionsList.innerHTML = sessions
      .map(
        (item) => `
          <div class="settings-session">
            <div class="settings-session__main">
              <span class="settings-session__title">${escapeHtml(item.device_label || t('settings.unknownDevice'))}</span>
              <span class="settings-session__meta">${t('settings.sessionCreated')} ${formatDate(item.created_at)} · ${t('settings.sessionLastUsed')} ${formatDate(item.last_used_at)} · ${t('settings.sessionExpires')} ${formatDate(item.expires_at)}</span>
            </div>
            <button type="button" class="btn btn--ghost btn--sm" data-revoke="${item.id}">${t('settings.sessionRevoke')}</button>
          </div>
        `,
      )
      .join('');
    els.sessionsList.querySelectorAll('[data-revoke]').forEach((btn) => {
      btn.addEventListener('click', () => revokeSession(Number(btn.dataset.revoke)));
    });
  } catch (error) {
    showError(els.sessionsError, error.message);
    els.sessionsList.innerHTML = '';
  }
}

const formatDate = (value) => formatBranchDateTime(value) ?? '—';

function credentialStatus(item) {
  if (item.needs_reauth) return t('settings.credNeedsReauth');
  if (!item.is_active) return t('settings.credDisabled');
  return t('settings.credActive');
}

async function revokeSession(id) {
  try {
    await authFetch(`/auth/sessions/${id}`, { method: 'DELETE' });
    await loadSessions();
  } catch (error) {
    showError(els.sessionsError, error.message);
  }
}

els.refreshSessions.addEventListener('click', loadSessions);

els.logoutAll.addEventListener('click', async () => {
  if (!confirm(t('settings.logoutAllConfirm'))) return;
  try {
    await authFetch('/auth/logout-all', { method: 'POST' });
    clearLocalSession();
    window.location.href = '/login.html';
  } catch (error) {
    showError(els.sessionsError, error.message);
  }
});

async function loadCredentials() {
  clearAlert(els.credentialsError);
  els.credentialsList.innerHTML = `<div class="settings-empty">${t('common.loading')}</div>`;
  try {
    const response = await authFetch('/auth/admin/yclients-credentials');
    const items = response.data || [];
    if (!items.length) {
      els.credentialsList.innerHTML = `<div class="settings-empty">${t('settings.noCredentials')}</div>`;
      return;
    }
    els.credentialsList.innerHTML = items
      .map(
        (item) => `
          <div class="settings-credential">
            <div class="settings-credential__main">
              <span class="settings-credential__title">${escapeHtml(item.title)}</span>
              <span class="settings-credential__meta">${credentialStatus(item)} · ${t('settings.credBranches')}: ${(item.company_ids || []).join(', ') || '—'} · ${t('settings.credCreated')} ${formatDate(item.created_at)} · ${t('settings.credUsed')} ${formatDate(item.last_used_at)}</span>
              ${
                item.last_error
                  ? `<span class="settings-credential__meta">${t('settings.credLastError')} ${formatDate(item.last_error_at)}: ${escapeHtml(item.last_error)}</span>`
                  : ''
              }
            </div>
            <button type="button" class="btn btn--ghost btn--sm" data-toggle="${item.id}" data-active="${item.is_active ? '1' : '0'}">${item.is_active ? t('settings.credDisable') : t('settings.credEnable')}</button>
            <button type="button" class="btn btn--danger btn--sm" data-delete="${item.id}">${t('settings.credDelete')}</button>
          </div>
        `,
      )
      .join('');
    els.credentialsList.querySelectorAll('[data-toggle]').forEach((btn) => {
      btn.addEventListener('click', () => toggleCredential(Number(btn.dataset.toggle), btn.dataset.active !== '1'));
    });
    els.credentialsList.querySelectorAll('[data-delete]').forEach((btn) => {
      btn.addEventListener('click', () => deleteCredential(Number(btn.dataset.delete)));
    });
  } catch (error) {
    showError(els.credentialsError, error.message);
    els.credentialsList.innerHTML = '';
  }
}

async function toggleCredential(id, isActive) {
  try {
    await authFetch(`/auth/admin/yclients-credentials/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ is_active: isActive }),
    });
    await loadCredentials();
  } catch (error) {
    showError(els.credentialsError, error.message);
  }
}

async function deleteCredential(id) {
  if (!confirm(t('settings.credDeleteConfirm'))) return;
  try {
    await authFetch(`/auth/admin/yclients-credentials/${id}`, { method: 'DELETE' });
    await loadCredentials();
  } catch (error) {
    showError(els.credentialsError, error.message);
  }
}

els.refreshCredentials.addEventListener('click', loadCredentials);

els.addCredForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  clearAlert(els.addCredError);
  clearAlert(els.addCredSuccess);
  try {
    await authFetch('/auth/admin/yclients-credentials', {
      method: 'POST',
      body: JSON.stringify({
        title: document.getElementById('cred-title').value.trim(),
        partner_token: document.getElementById('cred-token').value.trim(),
        login: document.getElementById('cred-login').value.trim(),
        password: document.getElementById('cred-password').value,
      }),
    });
    els.addCredSuccess.textContent = t('settings.credCreated2');
    els.addCredSuccess.hidden = false;
    els.addCredForm.reset();
    document.getElementById('cred-title').value = 'YClients integration';
    await loadCredentials();
  } catch (error) {
    showError(els.addCredError, error.message);
  }
});

// «Порядок филиалов»: the owner reorders the tenant's branches; the server stores the position of each.
let branchOrderSaved = [];
let branchOrderWorking = [];

registerUnsavedWork(() => branchOrderChanged(branchOrderSaved, branchOrderWorking));

function renderBranchOrder() {
  const last = branchOrderWorking.length - 1;
  els.branchOrderList.innerHTML = branchOrderWorking
    .map(
      (branch, index) =>
        '<li class="branch-order-item">' +
        `<span class="branch-order-item__title">${escapeHtml(branch.title)}</span>` +
        '<span class="branch-order-item__buttons">' +
        `<button type="button" class="btn btn--ghost btn--sm" data-bo-move="-1" data-bo-index="${index}"` +
        ` aria-label="${escapeHtml(t('settings.branchOrderUp'))}"${index === 0 ? ' disabled' : ''}>↑</button>` +
        `<button type="button" class="btn btn--ghost btn--sm" data-bo-move="1" data-bo-index="${index}"` +
        ` aria-label="${escapeHtml(t('settings.branchOrderDown'))}"${index === last ? ' disabled' : ''}>↓</button>` +
        '</span></li>',
    )
    .join('');
  els.branchOrderSave.disabled = !branchOrderChanged(branchOrderSaved, branchOrderWorking);
  els.branchOrderReset.disabled = !branchOrderSaved.some((branch) => branch.sort_order !== null);
}

function setBranchOrderTenantMissing(missing) {
  els.branchOrderTenantHint.hidden = !missing;
  els.branchOrderList.hidden = missing;
  els.branchOrderSave.hidden = missing;
  els.branchOrderReset.hidden = missing;
}

// Resolves to whether the list was read: the caller must not announce a success over a failed reload.
async function loadBranchOrder() {
  clearAlert(els.branchOrderError);
  try {
    const payload = await authFetch('/dashboard/branch-order');
    branchOrderSaved = payload.data || [];
    branchOrderWorking = [...branchOrderSaved];
    setBranchOrderTenantMissing(false);
    renderBranchOrder();
    return true;
  } catch (error) {
    // 400 on read: a platform admin has no tenant selected; the server's detail is English.
    if (error.status === 400 && currentUser?.role === 'platform_admin') {
      branchOrderSaved = [];
      branchOrderWorking = [];
      setBranchOrderTenantMissing(true);
      return false;
    }
    showError(els.branchOrderError, error.message);
    return false;
  }
}

async function saveBranchOrder(companyIds, successKey = 'settings.branchOrderSaved') {
  clearAlert(els.branchOrderError);
  clearAlert(els.branchOrderSuccess);
  try {
    await authFetch('/dashboard/branch-order', {
      method: 'PUT',
      body: JSON.stringify({ company_ids: companyIds }),
    });
    if (await loadBranchOrder()) {
      els.branchOrderSuccess.textContent = t(successKey);
      els.branchOrderSuccess.hidden = false;
    }
  } catch (error) {
    // 400: the tenant's branches changed since this tab loaded; 403: the read-only demo owner. Both details are English.
    if (error.status === 400) await loadBranchOrder();
    const known = { 400: 'settings.branchOrderStale', 403: 'settings.branchOrderForbidden' }[error.status];
    showError(els.branchOrderError, known ? t(known) : error.message);
  }
}

els.branchOrderList.addEventListener('click', (event) => {
  const button = event.target.closest('[data-bo-move]');
  if (!button) return;
  branchOrderWorking = moveBranch(branchOrderWorking, Number(button.dataset.boIndex), Number(button.dataset.boMove));
  renderBranchOrder();
});

els.branchOrderSave.addEventListener('click', () => saveBranchOrder(branchOrderWorking.map((branch) => branch.id)));

els.branchOrderReset.addEventListener('click', () => saveBranchOrder([], 'settings.branchOrderResetDone'));

if (requireAuthRedirect()) loadProfile();
