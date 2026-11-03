import './auth.css';
import { enhanceSelect } from './customSelect.js';
import {
  authFetch,
  getSelectedPortalAccountId,
  hasSessionHint,
  logout,
  requireAuthRedirect,
  setSelectedPortalAccountId,
  USER_ADMIN_ROLES,
} from './auth.js';
import { createModalManager, tenantRequestGuard } from './adminPage.js';
import { buildCsv } from './adminSecurity.js';
import { renderAlert } from './errorDetails.js';
import { looksLikeEmail, passwordProblem, withBusyButton } from './formHelpers.js';
import { escapeHtml } from './html.js';
import { applyTranslations, getLocale, mountLanguageSwitcher, roleLabel, t } from './i18n.js';
import { formatBranchDateTime } from './timestamps.js';

document.documentElement.lang = getLocale();
applyTranslations();
mountLanguageSwitcher(document.getElementById('lang-switcher'))?.addEventListener('change', () => location.reload());

const errorEl = document.getElementById('error');
const successEl = document.getElementById('success');
const createErrorEl = document.getElementById('create-error');
const editErrorEl = document.getElementById('edit-error');
const createStaffAccountErrorEl = document.getElementById('create-staff-account-error');
const tableBody = document.getElementById('users-body');
const usersSearch = document.getElementById('users-search');
const createModal = document.getElementById('create-user-modal');
const editModal = document.getElementById('edit-user-modal');
const createStaffAccountModal = document.getElementById('create-staff-account-modal');
const linkStaffModal = document.getElementById('link-staff-modal');
const linkStaffErrorEl = document.getElementById('link-staff-error');
const linkStaffSuccessEl = document.getElementById('link-staff-success');
const linkStaffLead = document.getElementById('link-staff-lead');
const linkStaffList = document.getElementById('link-staff-list');
const deleteConfirmModal = document.getElementById('delete-confirm-modal');
const credentialsModal = document.getElementById('credentials-modal');
const credentialsMessage = document.getElementById('credentials-message');
const credentialsErrorEl = document.getElementById('credentials-error');
const credentialsSuccessEl = document.getElementById('credentials-success');
const saveCredentialsExcelBtn = document.getElementById('save-credentials-excel');
const distributeCredentialsBtn = document.getElementById('distribute-credentials');
const deleteConfirmMessage = document.getElementById('delete-confirm-message');
const confirmDeleteBtn = document.getElementById('confirm-delete');
const provisionAccountsBtn = document.getElementById('provision-accounts');
const initialPasswordsSection = document.getElementById('initial-passwords-section');
const initialPasswordsBody = document.getElementById('initial-passwords-body');
const initialPasswordsSearch = document.getElementById('initial-passwords-search');
const yclientsCredentialsSection = document.getElementById('yclients-credentials-section');
const metricVisibilitySection = document.getElementById('metric-visibility-section');
const metricVisibilityHead = document.getElementById('metric-visibility-head');
const metricVisibilityBody = document.getElementById('metric-visibility-body');
const metricVisibilityError = document.getElementById('metric-visibility-error');
const metricVisibilitySuccess = document.getElementById('metric-visibility-success');
const yclientsCredentialsBody = document.getElementById('yclients-credentials-body');
const yclientsCredentialsError = document.getElementById('yclients-credentials-error');
const yclientsCredentialsForm = document.getElementById('yclients-credentials-form');
const yclientsCredentialTitle = document.getElementById('yclients-credential-title');
const yclientsCredentialPartnerToken = document.getElementById('yclients-credential-partner-token');
const yclientsCredentialLogin = document.getElementById('yclients-credential-login');
const yclientsCredentialPassword = document.getElementById('yclients-credential-password');
const yclientsCredentialBranchSelect = document.getElementById('yclients-credential-branches');
const yclientsCredentialActive = document.getElementById('yclients-credential-active');
const testYclientsCredentialDraftBtn = document.getElementById('test-yclients-credential-draft');
const cancelYclientsCredentialEditBtn = document.getElementById('cancel-yclients-credential-edit');
const saveYclientsCredentialBtn = document.getElementById('save-yclients-credential');
const createForm = document.getElementById('create-user-form');
const editForm = document.getElementById('edit-user-form');
const createStaffAccountForm = document.getElementById('create-staff-account-form');
const createEmail = document.getElementById('create-email');
const createPassword = document.getElementById('create-password');
const createName = document.getElementById('create-name');
const createRoleSelect = document.getElementById('create-role-select');
const createBranchSelect = document.getElementById('create-branch-select');
const createBtn = document.getElementById('create-user');
const editEmail = document.getElementById('edit-email');
const editName = document.getElementById('edit-name');
const editRoleSelect = document.getElementById('edit-role-select');
const editBranchSelect = document.getElementById('edit-branch-select');
const createStaffAccountName = document.getElementById('create-staff-account-name');
const createStaffAccountEmail = document.getElementById('create-staff-account-email');
const createStaffAccountRoleSelect = document.getElementById('create-staff-account-role');
const createStaffAccountBranchSelect = document.getElementById('create-staff-account-branches');
const createStaffAccountPassword = document.getElementById('create-staff-account-password');
const createStaffAccountPasswordHint = document.getElementById('create-staff-account-password-hint');
const saveBtn = document.getElementById('save-user');
const adminRoleLabel = document.getElementById('admin-role-label');
const openCreateUserBtn = document.getElementById('open-create-user');
const tenantSwitcherSection = document.getElementById('tenant-switcher-section');
const tenantSelect = document.getElementById('tenant-select');
const tenantMeta = document.getElementById('tenant-meta');

const createRoleDropdown = enhanceSelect(createRoleSelect, { placeholder: t('admin.selectRole') });
const createBranchDropdown = enhanceSelect(createBranchSelect, { placeholder: t('admin.selectBranches') });
const editRoleDropdown = enhanceSelect(editRoleSelect, { placeholder: t('admin.selectRole') });
const editBranchDropdown = enhanceSelect(editBranchSelect, { placeholder: t('admin.selectBranches') });
const createStaffAccountRoleDropdown = enhanceSelect(createStaffAccountRoleSelect, { placeholder: t('admin.selectRole') });
const createStaffAccountBranchDropdown = enhanceSelect(createStaffAccountBranchSelect, { placeholder: t('admin.selectBranches') });
const yclientsCredentialBranchDropdown = enhanceSelect(yclientsCredentialBranchSelect, { placeholder: t('admin.selectBranches') });

// One level of access under two names. On this screen the account is provisioned from an
// existing CRM staff row, so the label drives the badge and the per-role money override —
// administrator status keeps coming from the YClients position, which the portal never
// overwrites for a row it did not create.
const STAFF_ACCOUNT_ROLES = ['admin', 'barber'];

// Exactly the roles `/auth/admin/*` accepts. A `manager` used to pass this gate and then get
// 403 from every request on the page, leaving the chrome — search box, "Create user" — on
// screen above an error banner, because `applyAdminMeta()` never ran to hide it.
const ADMIN_ROLES = new Set(USER_ADMIN_ROLES);

let users = [];
let branches = [];
let adminMeta = null;
let currentUserId = null;
let currentUserRole = null;
let editingUserId = null;
let pendingStaffAccountId = null;
let linkingUser = null;
let pendingDelete = null;
let initialPasswords = [];
let yclientsCredentials = [];
let editingYclientsCredentialId = null;
let currentCredentialsItems = [];
let portalAccounts = [];

function hideAlerts() {
  errorEl.hidden = true;
  successEl.hidden = true;
}

function hideFieldError(el) {
  el.hidden = true;
}

// What the form can tell before the request: the address looks like one, the password fits what the server accepts.
function formInputProblem(email, password = '') {
  if (!looksLikeEmail(email)) return t('authErrors.invalidEmail');
  const passwordKey = passwordProblem(password);
  return passwordKey ? t(passwordKey) : null;
}

// `source` is a message or a caught error: an error with `details` gets the «i» control (errorDetails.js).
function showFieldError(el, source) {
  renderAlert(el, source);
}

function showError(source) {
  hideAlerts();
  renderAlert(errorEl, source);
}

function showSuccess(message) {
  hideAlerts();
  successEl.textContent = message;
  successEl.hidden = false;
}

function rolesForEdit(user) {
  const assignable = adminMeta?.assignable_roles || [];
  const roles = new Set(assignable);
  roles.add(user.role);
  return [...roles];
}

function scopedBranches() {
  if (!adminMeta?.company_ids) {
    return branches;
  }
  const allowed = new Set(adminMeta.company_ids);
  return branches.filter((branch) => allowed.has(branch.id));
}

function renderRoleOptions(selectEl, dropdown, roles) {
  selectEl.innerHTML = roles
    .map((role) => `<option value="${escapeHtml(role)}">${escapeHtml(roleLabel(role))}</option>`)
    .join('');
  dropdown.refresh();
}

function renderBranchOptions(selectEl, dropdown, selectedIds = [], multiple = true) {
  const items = scopedBranches();
  selectEl.innerHTML = items
    .map((branch) => `<option value="${escapeHtml(branch.id)}">${escapeHtml(branch.title)}</option>`)
    .join('');
  if (multiple) {
    Array.from(selectEl.options).forEach((option) => {
      option.selected = selectedIds.includes(Number(option.value));
    });
  } else if (selectedIds.length) {
    selectEl.value = String(selectedIds[0]);
  }
  dropdown.refresh();
}

function canManageUsers() {
  if (typeof adminMeta?.can_manage_users === 'boolean') {
    return adminMeta.can_manage_users;
  }
  return ADMIN_ROLES.has(currentUserRole || adminMeta?.role);
}

// Only an explicit `false` counts: the server refuses a password-less account itself, this just saves the round trip.
function invitesDeliverable() {
  return adminMeta?.invites_deliverable !== false;
}

function canManageYclientsCredentials() {
  return (currentUserRole || adminMeta?.role) === 'platform_admin';
}

const MONEY_METRIC_LABELS = {
  revenue: () => t('admin.moneyRevenue'),
  avg_check: () => t('admin.moneyAvgCheck'),
  cosmo_sum: () => t('admin.moneyCosmoSum'),
};

function canManageMetricVisibility() {
  return ['owner', 'platform_admin'].includes(currentUserRole || adminMeta?.role);
}

function moneyMetricLabel(metric) {
  const localized = MONEY_METRIC_LABELS[metric.code];
  return localized ? localized() : metric.label || metric.code;
}

function isPlatformAdmin() {
  return (currentUserRole || adminMeta?.role) === 'platform_admin';
}

function selectedPortalAccountId() {
  if (isPlatformAdmin()) {
    return getSelectedPortalAccountId();
  }
  return adminMeta?.portal_account_id ? String(adminMeta.portal_account_id) : '';
}

// Lists of the tenant on screen: an answer that arrives after the platform admin switched away is dropped.
function tenantGuard() {
  return tenantRequestGuard(selectedPortalAccountId);
}

function portalAccountLabel(account) {
  const branchText = t('admin.branchCount', { count: account.branch_count || 0 });
  return `${account.label || `Tenant ${account.id}`} · ${branchText}`;
}

function renderTenantSwitcher() {
  if (!tenantSwitcherSection || !tenantSelect) return;
  const platform = isPlatformAdmin();
  tenantSwitcherSection.hidden = !platform;
  if (!platform) return;

  tenantSelect.innerHTML = '';
  portalAccounts.forEach((account) => {
    const option = document.createElement('option');
    option.value = account.id;
    option.textContent = portalAccountLabel(account);
    tenantSelect.appendChild(option);
  });

  if (!portalAccounts.length) {
    tenantSelect.disabled = true;
    setSelectedPortalAccountId('');
    if (tenantMeta) {
      tenantMeta.textContent = t('admin.noBusinessTenants');
    }
    return;
  }

  let selected = getSelectedPortalAccountId();
  if (!portalAccounts.some((account) => String(account.id) === selected)) {
    selected = String(portalAccounts[0].id);
    setSelectedPortalAccountId(selected);
  }
  tenantSelect.disabled = false;
  tenantSelect.value = selected;
  const account = portalAccounts.find((item) => String(item.id) === selected);
  if (tenantMeta) {
    tenantMeta.textContent = account?.branch_count
      ? t('admin.tenantApplies')
      : t('admin.tenantNoBranchesHint');
  }
}

function applyAdminMeta() {
  const canManage = canManageUsers();
  const roles = canManage ? adminMeta?.assignable_roles || [] : [];
  renderTenantSwitcher();
  // The server refuses these here: a staff account must come from its YClients row, or it reads zeros.
  renderRoleOptions(createRoleSelect, createRoleDropdown, roles.filter((role) => !STAFF_ACCOUNT_ROLES.includes(role)));
  renderBranchOptions(createBranchSelect, createBranchDropdown);

  if (adminRoleLabel) {
    adminRoleLabel.textContent = roleLabel(adminMeta?.role) || 'admin';
  }
  if (openCreateUserBtn) {
    openCreateUserBtn.hidden = !canManage;
  }
  // Bulk creation sends an invite to each address; with mail that never leaves it would only create accounts nobody can open.
  if (provisionAccountsBtn) {
    provisionAccountsBtn.hidden = !canManage || !invitesDeliverable();
  }
  if (yclientsCredentialsSection) {
    yclientsCredentialsSection.hidden = !canManageYclientsCredentials();
  }
  if (metricVisibilitySection) {
    metricVisibilitySection.hidden = !canManageMetricVisibility();
  }
  renderBranchOptions(yclientsCredentialBranchSelect, yclientsCredentialBranchDropdown);
  if (users.length) {
    renderUsers();
  }
  if (initialPasswords.length) {
    renderInitialPasswordsTable();
  }
}

async function loadPortalAccounts() {
  if (!isPlatformAdmin()) {
    setSelectedPortalAccountId('');
    renderTenantSwitcher();
    return;
  }
  const payload = await authFetch('/auth/portal-accounts');
  portalAccounts = payload.data || [];
  renderTenantSwitcher();
}

function loadTenantScopedLists() {
  return Promise.all([
    loadUsers(),
    loadInitialPasswords(),
    loadYclientsCredentials(),
    loadMetricVisibility(),
  ]);
}

async function reloadTenantScopedAdminData() {
  resetYclientsCredentialForm();
  await loadBranches();
  await loadTenantScopedLists();
}

function openCreateModal() {
  hideFieldError(createErrorEl);
  createForm.reset();
  renderBranchOptions(createBranchSelect, createBranchDropdown);
  if (createRoleSelect.options.length) {
    createRoleSelect.selectedIndex = 0;
    createRoleDropdown.syncFromNative();
  }
  // Without mail the password is the only way into the account, so the form asks for it.
  createPassword.required = !invitesDeliverable();
  createPassword.placeholder = t(invitesDeliverable() ? 'admin.passwordPlaceholder' : 'admin.passwordRequiredPlaceholder');
  modals.open(createModal);
  createEmail.focus();
}

function closeCreateModal() {
  modals.close(createModal);
  hideFieldError(createErrorEl);
}

function openEditModal(user) {
  if (!user?.manageable || !user.is_portal_user) return;
  editingUserId = user.id;
  hideFieldError(editErrorEl);
  editEmail.value = user.email;
  editName.value = user.full_name || '';
  renderRoleOptions(editRoleSelect, editRoleDropdown, rolesForEdit(user));
  editRoleSelect.value = user.role;
  renderBranchOptions(editBranchSelect, editBranchDropdown, user.company_ids || []);
  editRoleDropdown.syncFromNative();
  modals.open(editModal);
  editName.focus();
}

function closeEditModal() {
  modals.close(editModal);
  editingUserId = null;
  hideFieldError(editErrorEl);
}

function openCreateStaffAccountModal(staff) {
  if (!staff?.manageable || staff.is_portal_user) return;
  pendingStaffAccountId = staff.staff_id;
  hideFieldError(createStaffAccountErrorEl);
  createStaffAccountForm.reset();
  createStaffAccountName.value = staff.full_name || '';
  createStaffAccountEmail.value = staff.can_create_account ? staff.email : '';
  renderRoleOptions(createStaffAccountRoleSelect, createStaffAccountRoleDropdown, STAFF_ACCOUNT_ROLES);
  createStaffAccountRoleSelect.value = staff.suggested_role || 'barber';
  createStaffAccountRoleDropdown.syncFromNative();
  renderBranchOptions(createStaffAccountBranchSelect, createStaffAccountBranchDropdown, staff.company_ids || []);
  const passwordRequired = !invitesDeliverable();
  createStaffAccountPassword.required = passwordRequired;
  createStaffAccountPassword.placeholder = t(passwordRequired ? 'admin.passwordRequiredPlaceholder' : 'admin.passwordPlaceholder');
  createStaffAccountPasswordHint.textContent = t(passwordRequired ? 'admin.staffAccountPasswordRequired' : 'admin.staffAccountPasswordOptional');
  modals.open(createStaffAccountModal);
  createStaffAccountEmail.focus();
}

function closeCreateStaffAccountModal() {
  modals.close(createStaffAccountModal);
  pendingStaffAccountId = null;
  hideFieldError(createStaffAccountErrorEl);
}

function canLinkStaff(user) {
  return Boolean(user?.is_portal_user && user.manageable && STAFF_ACCOUNT_ROLES.includes(user.role));
}

function staffLinkLabel(row) {
  const title = [row.name, row.position].filter(Boolean).join(' · ');
  return row.fired ? `${title} (${t('admin.linkStaffFired')})` : title;
}

function renderStaffLinks(branchLinks) {
  linkStaffList.innerHTML = branchLinks
    .map(({ company_id: companyId, linked, candidates }) => {
      let body;
      if (linked.length) {
        body = linked
          .map(
            (row) => `
        <div class="staff-link__row">
          <span>${escapeHtml(staffLinkLabel(row))}</span>
          <button type="button" class="btn btn--ghost" data-unlink-staff="${escapeHtml(row.staff_id)}">${t('admin.unlinkStaffAction')}</button>
        </div>`,
          )
          .join('');
      } else if (candidates.length) {
        body = `
        <div class="staff-link__row">
          <select data-link-select>
            <option value="">${t('admin.linkStaffPick')}</option>
            ${candidates.map((row) => `<option value="${escapeHtml(row.staff_id)}">${escapeHtml(staffLinkLabel(row))}</option>`).join('')}
          </select>
          <button type="button" class="btn btn--primary" data-link-staff>${t('admin.linkStaffAction')}</button>
        </div>`;
      } else {
        body = `<span class="staff-link__empty">${t('admin.linkStaffNone')}</span>`;
      }
      return `
      <section class="staff-link">
        <div class="staff-link__branch">${escapeHtml(branchTitles([companyId]))}</div>
        ${body}
      </section>`;
    })
    .join('');
}

// The answer of an earlier request must not repaint the modal after it was closed or opened for another account.
async function loadStaffLinks() {
  const user = linkingUser;
  if (!user) return;
  const payload = await authFetch(`/auth/admin/users/${user.id}/staff-links`);
  if (linkingUser === user) renderStaffLinks(payload.data.branches);
}

async function openLinkStaffModal(user) {
  if (!canLinkStaff(user)) return;
  linkingUser = user;
  hideFieldError(linkStaffErrorEl);
  linkStaffSuccessEl.hidden = true;
  linkStaffLead.textContent = t('admin.linkStaffLead', { name: user.full_name || user.email });
  linkStaffList.replaceChildren();
  modals.open(linkStaffModal);
  try {
    await loadStaffLinks();
  } catch (error) {
    showFieldError(linkStaffErrorEl, error);
  }
}

function closeLinkStaffModal() {
  modals.close(linkStaffModal);
  linkingUser = null;
  hideFieldError(linkStaffErrorEl);
  linkStaffSuccessEl.hidden = true;
}

// Link and unlink share the refresh: `request` returns the success message. On a refusal (409: somebody took the row
// first) the list is reread too, so the choice on screen is the one the server offers now.
async function changeStaffLink(button, request) {
  const user = linkingUser;
  hideFieldError(linkStaffErrorEl);
  linkStaffSuccessEl.hidden = true;
  const show = (el, source) => {
    if (linkingUser !== user) return;
    renderAlert(el, source);
  };
  try {
    show(linkStaffSuccessEl, await withBusyButton(button, () => request(user)));
  } catch (error) {
    show(linkStaffErrorEl, error);
  }
  try {
    await loadStaffLinks();
  } catch (error) {
    show(linkStaffErrorEl, error);
  }
  // The «not linked» badge of the row follows the links just changed; a failed reread only leaves it stale until the next load.
  loadUsers().catch(() => {});
}

linkStaffList.addEventListener('click', async (event) => {
  const linkButton = event.target.closest('[data-link-staff]');
  const unlinkButton = event.target.closest('[data-unlink-staff]');
  if (linkButton) {
    const select = linkButton.closest('.staff-link__row').querySelector('[data-link-select]');
    const staffId = Number(select.value);
    if (!staffId) return;
    const staff = select.selectedOptions[0].textContent;
    await changeStaffLink(linkButton, async (user) => {
      const payload = await authFetch(`/auth/admin/users/${user.id}/staff-links`, {
        method: 'POST',
        body: JSON.stringify({ staff_id: staffId }),
      });
      return t('admin.staffLinked', { staff, count: payload.data.moved_fact_values });
    });
  } else if (unlinkButton) {
    const staffId = Number(unlinkButton.dataset.unlinkStaff);
    const staff = unlinkButton.closest('.staff-link__row').querySelector('span').textContent;
    await changeStaffLink(unlinkButton, async (user) => {
      await authFetch(`/auth/admin/users/${user.id}/staff-links/${staffId}`, { method: 'DELETE' });
      return t('admin.staffUnlinked', { staff });
    });
  }
});

function openDeleteConfirm({ id, label }) {
  pendingDelete = { id };
  const safeLabel = escapeHtml(label);
  deleteConfirmMessage.innerHTML = `
    <p class="admin-modal__confirm-text">${t('admin.deleteUserLead')}<strong class="admin-modal__confirm-target">${safeLabel}</strong>?</p>
    <p class="admin-modal__confirm-warning">${t('admin.deleteIrreversible')}</p>
  `;
  modals.open(deleteConfirmModal);
  confirmDeleteBtn.focus();
}

function closeDeleteConfirm() {
  modals.close(deleteConfirmModal);
  pendingDelete = null;
}

function hideCredentialsAlerts() {
  credentialsErrorEl.hidden = true;
  credentialsSuccessEl.hidden = true;
}

function showCredentialsModal(items) {
  currentCredentialsItems = (items || []).map((item) => ({
    staff_id: item.staff_id ?? item.user_id ?? item.id ?? null,
    user_id: item.user_id ?? item.id ?? null,
    email: item.email,
    full_name: item.full_name || '',
  }));
  hideCredentialsAlerts();
  const rows = currentCredentialsItems
    .map(
      (item) => `
        <div class="credentials-row">
          <strong>${escapeHtml(item.full_name || item.email)}</strong>
          <div>${t('admin.loginLabel')}: <code>${escapeHtml(item.email)}</code></div>
        </div>`
    )
    .join('');
  // Sending is the modal's second action; with mail that never leaves it would only report invites nobody gets.
  distributeCredentialsBtn.hidden = !invitesDeliverable();
  // «Use these emails to send password links» would contradict the note that nothing is sent.
  const hint = t(invitesDeliverable() ? 'admin.saveCredentialsHint' : 'admin.invitesUnavailable');
  credentialsMessage.innerHTML = `
    <p class="admin-modal__confirm-text">${escapeHtml(hint)}</p>
    <div class="credentials-list">${rows}</div>
  `;
  modals.open(credentialsModal);
}

function saveCredentialsAsCsv() {
  if (!currentCredentialsItems.length) return;

  const rows = currentCredentialsItems.map((item) => ({
    id: item.staff_id ?? item.user_id ?? '',
    name: item.full_name || '',
    email: item.email,
  }));
  const csv = buildCsv(rows, [
    { key: 'id', label: 'ID' },
    { key: 'name', label: t('admin.colName') },
    { key: 'email', label: t('admin.colLoginEmail') },
  ]);
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
  link.href = url;
  link.download = `portal-invites-${stamp}.csv`;
  link.click();
  URL.revokeObjectURL(url);
}

async function distributeCredentials() {
  if (!currentCredentialsItems.length || !invitesDeliverable()) return;
  hideCredentialsAlerts();

  const userIds = currentCredentialsItems.map((item) => item.user_id).filter(Boolean);
  if (!userIds.length) {
    renderAlert(credentialsErrorEl, t('admin.noUserIdsForDistribution'));
    return;
  }

  try {
    await withBusyButton(distributeCredentialsBtn, async () => {
      const payload = await authFetch('/auth/admin/distribute-credentials', {
        method: 'POST',
        body: JSON.stringify({ user_ids: userIds }),
      });
      const { sent_count: sentCount, skipped, errors } = payload.data || {};
      const parts = [t('admin.sentEmailsCount', { count: sentCount || 0 })];
      if (skipped?.length) {
        parts.push(t('admin.skippedEmailsCount', { count: skipped.length }));
      }
      if (errors?.length) {
        parts.push(t('admin.errorsCount', { count: errors.length }));
      }
      credentialsSuccessEl.textContent = parts.join('. ');
      credentialsSuccessEl.hidden = false;
      if (errors?.length) {
        renderAlert(credentialsErrorEl, errors.map((item) => `${item.email}: ${item.reason}`).join('; '));
      }
    });
  } catch (error) {
    renderAlert(credentialsErrorEl, error);
  }
}

function closeCredentialsModal() {
  modals.close(credentialsModal);
  currentCredentialsItems = [];
  hideCredentialsAlerts();
}

// Escape closes the first open one, so the list runs from the modal that stacks on top of the others.
const modals = createModalManager([
  { modal: credentialsModal, close: closeCredentialsModal },
  { modal: deleteConfirmModal, close: closeDeleteConfirm },
  { modal: createStaffAccountModal, close: closeCreateStaffAccountModal },
  { modal: linkStaffModal, close: closeLinkStaffModal },
  { modal: editModal, close: closeEditModal },
  { modal: createModal, close: closeCreateModal },
], document.body);

async function loadAdminMeta() {
  const payload = await authFetch('/auth/admin/meta');
  adminMeta = payload.data || null;
  applyAdminMeta();
}

async function loadBranches() {
  const isCurrentTenant = tenantGuard();
  const payload = await authFetch('/dashboard/branches');
  if (!isCurrentTenant()) return;
  branches = payload.data || [];
  applyAdminMeta();
}

function roleBadge(role) {
  if (role === 'staff') {
    return `<span class="role-badge">${t('admin.staffNoAccount')}</span>`;
  }
  const classes = {
    platform_admin: 'role-badge role-badge--super',
    owner: 'role-badge role-badge--super',
    admin: 'role-badge role-badge--staff',
    barber: 'role-badge role-badge--staff',
  };
  const cls = classes[role] || 'role-badge';
  return `<span class="${escapeHtml(cls)}">${escapeHtml(roleLabel(role))}</span>`;
}

// An admin/barber account with no YClients row in some of its branches reads zeros there. The badge is the way to fix it:
// it opens the same link modal as the row menu.
function needsLinkBadge(user) {
  if (!user?.needs_staff_link || !STAFF_ACCOUNT_ROLES.includes(user.role)) return '';
  const label = escapeHtml(t('admin.needsStaffLink'));
  const hint = escapeHtml(t('admin.needsStaffLinkHint'));
  if (user.manageable && canManageUsers() && canLinkStaff(user)) {
    return `<button type="button" class="role-badge role-badge--warn" data-link-user="${escapeHtml(user.id)}" title="${hint}">${label}</button>`;
  }
  return `<span class="role-badge role-badge--warn" title="${hint}">${label}</span>`;
}

function branchTitles(companyIds) {
  if (!companyIds?.length) return '—';
  // Titles follow the tenant's branch order, not the order the ids were stored in.
  const position = (id) => {
    const index = branches.findIndex((item) => item.id === id);
    return index === -1 ? branches.length : index;
  };
  const titles = [...companyIds].sort((a, b) => position(a) - position(b)).map((id) => {
    const branch = branches.find((item) => item.id === id);
    return branch ? branch.title : String(id);
  });
  return titles.join(', ');
}

function closeAllRowMenus() {
  const openMenus = document.querySelectorAll('.row-menu[data-open="true"]');
  openMenus.forEach((menu) => {
    menu.dataset.open = 'false';
    const trigger = menu.querySelector('[data-row-menu-toggle]');
    const dropdown = menu.querySelector('.row-menu__dropdown');
    if (trigger) {
      trigger.setAttribute('aria-expanded', 'false');
    }
    if (dropdown) {
      dropdown.hidden = true;
    }
  });
  return openMenus.length > 0;
}

function renderRowMenu(items) {
  return `<div class="row-menu" data-row-menu>
    <button
      type="button"
      class="row-menu__trigger"
      data-row-menu-toggle
      aria-label="${t('admin.colActions')}"
      aria-haspopup="menu"
      aria-expanded="false"
    >
      <span class="row-menu__dots" aria-hidden="true"></span>
    </button>
    <div class="row-menu__dropdown" role="menu" hidden>
      ${items}
    </div>
  </div>`;
}

function filterUsers(rows, query) {
  const needle = query.trim().toLowerCase();
  if (!needle) return rows;
  return rows.filter((user) => {
    const haystack = [
      user.id,
      user.staff_id,
      user.email,
      user.full_name,
      user.role,
      user.is_portal_user ? (user.email_verified ? t('admin.confirmed') : t('admin.pending')) : '',
      branchTitles(user.company_ids),
    ]
      .filter((value) => value !== undefined && value !== null && value !== '—')
      .join(' ')
      .toLowerCase();
    return haystack.includes(needle);
  });
}

function renderUsers() {
  const query = usersSearch?.value || '';
  const filtered = filterUsers(users, query);
  tableBody.innerHTML = filtered.length
    ? filtered
        .map((user) => {
          let actions = '<span class="admin-table__muted">—</span>';
          if (user.manageable && canManageUsers()) {
            if (user.is_portal_user) {
              actions = renderRowMenu(`
      <button type="button" class="row-menu__item" role="menuitem" data-edit-user="${escapeHtml(user.id)}">${t('admin.edit')}</button>
      ${canLinkStaff(user) ? `<button type="button" class="row-menu__item" role="menuitem" data-link-user="${escapeHtml(user.id)}">${t('admin.linkToYclients')}</button>` : ''}
      <button type="button" class="row-menu__item row-menu__item--danger" role="menuitem" data-delete-user="${escapeHtml(user.id)}">${t('admin.delete')}</button>`);
            } else {
              actions = renderRowMenu(`
      <button type="button" class="row-menu__item" role="menuitem" data-create-account="${escapeHtml(user.staff_id)}">${t('admin.createAccount')}</button>`);
            }
          }
          return `
      <tr>
        <td>${escapeHtml(user.staff_id ?? user.id ?? '—')}</td>
        <td>${user.is_portal_user ? escapeHtml(user.email) : '—'}${user.id === currentUserId ? ` <span class="user-you">${escapeHtml(t('admin.you'))}</span>` : ''}</td>
        <td>${escapeHtml(user.full_name || '—')}</td>
        <td>${roleBadge(user.role)}${needsLinkBadge(user)}</td>
        <td><span class="status-dot ${user.email_verified ? 'ok' : ''}">${user.is_portal_user ? escapeHtml(user.email_verified ? t('admin.confirmed') : t('admin.pending')) : '—'}</span></td>
        <td>${escapeHtml(branchTitles(user.company_ids))}</td>
        <td class="admin-table__cell-actions">${actions}</td>
      </tr>`;
        })
        .join('')
    : `<tr><td colspan="7" class="admin-table__empty">${
        users.length ? t('admin.nothingFound') : t('admin.usersNotFound')
      }</td></tr>`;
}

function filterInitialPasswords(rows, query) {
  const needle = query.trim().toLowerCase();
  if (!needle) return rows;
  return rows.filter((row) => {
    const haystack = [
      row.staff_id,
      row.user_id,
      row.email,
      row.full_name,
      row.role,
      row.password_reset_sent_at,
      branchTitles(row.company_ids),
    ]
      .filter(Boolean)
      .join(' ')
      .toLowerCase();
    return haystack.includes(needle);
  });
}

function resendCell(row) {
  if (!invitesDeliverable()) return `<span class="admin-card__hint">${escapeHtml(t('admin.invitesUnavailableShort'))}</span>`;
  return `<button type="button" class="btn btn--ghost btn--small" data-resend-invite="${escapeHtml(row.user_id)}">${t('admin.resendInvite')}</button>`;
}

function renderInitialPasswordsTable() {
  if (!canManageUsers()) {
    initialPasswordsSection.hidden = true;
    return;
  }
  initialPasswordsSection.hidden = false;
  const query = initialPasswordsSearch?.value || '';
  const filtered = filterInitialPasswords(initialPasswords, query);
  initialPasswordsBody.innerHTML = filtered.length
    ? filtered
        .map(
          (row) => `
      <tr>
        <td>${escapeHtml(row.staff_id ?? row.user_id ?? '—')}</td>
        <td>${escapeHtml(row.email)}</td>
        <td>${escapeHtml(row.full_name || '—')}</td>
        <td>${roleBadge(row.role)}</td>
        <td>${escapeHtml(branchTitles(row.company_ids))}</td>
        <td>${escapeHtml(formatBranchDateTime(row.password_reset_sent_at) || '—')}</td>
        <td class="admin-table__cell-actions">
          ${resendCell(row)}
        </td>
      </tr>`
        )
        .join('')
    : `<tr><td colspan="7" class="admin-table__empty">${
        initialPasswords.length ? t('admin.nothingFound') : t('admin.noInitialPasswords')
      }</td></tr>`;
}

async function loadInitialPasswords() {
  if (!canManageUsers()) return;
  const isCurrentTenant = tenantGuard();
  const payload = await authFetch('/auth/admin/initial-passwords');
  if (!isCurrentTenant()) return;
  initialPasswords = payload.data || [];
  renderInitialPasswordsTable();
}

async function resendInvite(userId, button) {
  if (!userId || !invitesDeliverable()) return;
  try {
    await withBusyButton(button, async () => {
      const payload = await authFetch('/auth/admin/distribute-credentials', {
        method: 'POST',
        body: JSON.stringify({ user_ids: [userId] }),
      });
      const { sent_count: sentCount, errors } = payload.data || {};
      if (errors?.length) {
        showError(errors.map((item) => `${item.email || item.user_id}: ${item.reason}`).join('; '));
      } else {
        showSuccess(t('admin.sentEmailsCount', { count: sentCount || 0 }));
      }
      await loadInitialPasswords();
    });
  } catch (error) {
    showError(error);
  }
}

function hideYclientsCredentialsError() {
  if (yclientsCredentialsError) {
    yclientsCredentialsError.hidden = true;
  }
}

function showYclientsCredentialsError(source) {
  renderAlert(yclientsCredentialsError, source);
}

function renderYclientsCredentialsTable() {
  if (!canManageYclientsCredentials()) {
    yclientsCredentialsSection.hidden = true;
    return;
  }
  yclientsCredentialsSection.hidden = false;
  yclientsCredentialsBody.innerHTML = yclientsCredentials.length
    ? yclientsCredentials
        .map((credential) => {
          const secretStatus = [
            credential.has_partner_token ? 'token' : null,
            credential.has_login ? 'login' : null,
            credential.has_password ? 'password' : null,
          ].filter(Boolean).join(', ');
          return `
      <tr>
        <td>${credential.id}</td>
        <td>${escapeHtml(credential.title)}</td>
        <td><span class="status-dot ${credential.is_active ? 'ok' : ''}">${credential.is_active ? t('admin.activeStatus') : t('admin.disabledStatus')}</span></td>
        <td>${escapeHtml(branchTitles(credential.company_ids))}</td>
        <td>${escapeHtml(secretStatus || '—')}</td>
        <td class="admin-table__cell-actions">
          <button type="button" class="btn btn--ghost btn--small" data-edit-yclients-credential="${credential.id}">${t('admin.edit')}</button>
          <button type="button" class="btn btn--ghost btn--small" data-test-yclients-credential="${credential.id}">${t('admin.test')}</button>
          <button type="button" class="btn btn--ghost btn--small" data-delete-yclients-credential="${credential.id}">${t('admin.delete')}</button>
        </td>
      </tr>`;
        })
        .join('')
    : `<tr><td colspan="6" class="admin-table__empty">${t('admin.noCredentials')}</td></tr>`;
}

async function loadYclientsCredentials() {
  if (!canManageYclientsCredentials()) return;
  const isCurrentTenant = tenantGuard();
  const payload = await authFetch('/auth/admin/yclients-credentials');
  if (!isCurrentTenant()) return;
  yclientsCredentials = payload.data || [];
  renderYclientsCredentialsTable();
}

let metricVisibilityData = null;

async function loadMetricVisibility() {
  if (!metricVisibilitySection) return;
  if (!canManageMetricVisibility()) {
    metricVisibilitySection.hidden = true;
    return;
  }
  metricVisibilitySection.hidden = false;
  const isCurrentTenant = tenantGuard();
  const payload = await authFetch('/dashboard/metric-visibility');
  if (!isCurrentTenant()) return;
  metricVisibilityData = payload.data || null;
  renderMetricVisibility();
}

function renderMetricVisibility() {
  if (!metricVisibilityData || !metricVisibilityHead || !metricVisibilityBody) return;
  const metrics = metricVisibilityData.money_metrics || [];
  const roles = metricVisibilityData.roles || {};

  metricVisibilityHead.innerHTML =
    `<th>${escapeHtml(t('admin.colRole'))}</th>` +
    metrics.map((metric) => `<th>${escapeHtml(moneyMetricLabel(metric))}</th>`).join('') +
    `<th>${escapeHtml(t('admin.colActions'))}</th>`;

  metricVisibilityBody.innerHTML = Object.keys(roles)
    .map((role) => {
      const visible = new Set(roles[role] || []);
      const cells = metrics
        .map(
          (metric) =>
            `<td><label class="admin-checkbox"><input type="checkbox" data-mv-role="${escapeHtml(role)}" ` +
            `data-mv-code="${escapeHtml(metric.code)}"${visible.has(metric.code) ? ' checked' : ''} /></label></td>`
        )
        .join('');
      return (
        '<tr>' +
        `<td>${escapeHtml(roleLabel(role))}</td>` +
        cells +
        `<td><button type="button" class="btn btn--primary" data-mv-save="${escapeHtml(role)}">${escapeHtml(t('admin.save'))}</button></td>` +
        '</tr>'
      );
    })
    .join('');
}

async function saveMetricVisibility(role) {
  if (metricVisibilityError) metricVisibilityError.hidden = true;
  if (metricVisibilitySuccess) metricVisibilitySuccess.hidden = true;
  const codes = Array.from(
    metricVisibilityBody.querySelectorAll(`input[data-mv-role="${role}"]:checked`)
  ).map((input) => input.dataset.mvCode);
  try {
    await authFetch('/dashboard/metric-visibility', {
      method: 'PUT',
      body: JSON.stringify({ role, visible_codes: codes }),
    });
    if (metricVisibilitySuccess) {
      metricVisibilitySuccess.textContent = t('admin.metricVisibilitySaved');
      metricVisibilitySuccess.hidden = false;
    }
    await loadMetricVisibility();
  } catch (error) {
    renderAlert(metricVisibilityError, error);
  }
}

metricVisibilityBody?.addEventListener('click', (event) => {
  const button = event.target.closest('[data-mv-save]');
  if (!button) return;
  saveMetricVisibility(button.dataset.mvSave);
});

function resetYclientsCredentialForm() {
  editingYclientsCredentialId = null;
  yclientsCredentialsForm?.reset();
  if (yclientsCredentialActive) {
    yclientsCredentialActive.checked = true;
  }
  renderBranchOptions(yclientsCredentialBranchSelect, yclientsCredentialBranchDropdown);
  if (cancelYclientsCredentialEditBtn) {
    cancelYclientsCredentialEditBtn.hidden = true;
  }
  if (saveYclientsCredentialBtn) {
    saveYclientsCredentialBtn.textContent = t('admin.save');
  }
}

function editYclientsCredential(credentialId) {
  const credential = yclientsCredentials.find((item) => item.id === credentialId);
  if (!credential) return;
  editingYclientsCredentialId = credentialId;
  hideYclientsCredentialsError();
  yclientsCredentialTitle.value = credential.title || '';
  yclientsCredentialPartnerToken.value = '';
  yclientsCredentialLogin.value = '';
  yclientsCredentialPassword.value = '';
  yclientsCredentialActive.checked = Boolean(credential.is_active);
  renderBranchOptions(yclientsCredentialBranchSelect, yclientsCredentialBranchDropdown, credential.company_ids || []);
  if (cancelYclientsCredentialEditBtn) {
    cancelYclientsCredentialEditBtn.hidden = false;
  }
  saveYclientsCredentialBtn.textContent = t('admin.update');
  yclientsCredentialTitle.focus();
}

async function loadUsers() {
  const isCurrentTenant = tenantGuard();
  const payload = await authFetch('/auth/admin/users');
  if (!isCurrentTenant()) return;
  users = payload.data || [];
  renderUsers();
}

async function provisionAllAccounts() {
  if (!canManageUsers() || !invitesDeliverable()) return;
  hideAlerts();
  try {
    await withBusyButton(provisionAccountsBtn, async () => {
      const payload = await authFetch('/auth/admin/provision-accounts', { method: 'POST' });
      const { created_count: count, created, errors } = payload.data || {};
      if (created?.length) {
        showCredentialsModal(created);
      }
      const skippedCount = errors?.length || 0;
      showSuccess(t('admin.accountsCreated', { count: count || 0, skipped: skippedCount ? t('admin.skippedNoRealEmail', { count: skippedCount }) : '' }));
      await Promise.all([loadUsers(), loadInitialPasswords()]);
    });
  } catch (error) {
    showError(error);
  }
}

// Role and branches are always passed in: a staff member whose email is already on file is
// created straight from the row menu without opening the modal, and reading the modal's
// controls there would hand them whatever the last opened modal happened to leave behind.
async function createStaffAccount(staffId, { email = null, role, companyIds, password = null } = {}) {
  const selected = users.find((user) => user.staff_id === staffId);
  if (!selected?.manageable) return;

  hideAlerts();
  try {
    const payload = await authFetch(`/auth/admin/staff/${staffId}/create-account`, {
      method: 'POST',
      body: JSON.stringify({
        // Omitted on the quick path — the server derives it from the CRM position.
        role: role || null,
        email: email || selected.email,
        password: password || null,
        company_ids: companyIds || [],
      }),
    });
    closeCreateStaffAccountModal();
    showCredentialsModal([payload.data]);
    showSuccess(t('admin.accountForCreated', { name: selected.full_name }));
    await Promise.all([loadUsers(), loadInitialPasswords()]);
  } catch (error) {
    if (!createStaffAccountModal.hidden) {
      showFieldError(createStaffAccountErrorEl, error);
    } else {
      showError(error);
    }
  }
}

async function deleteUser(userId) {
  const selected = users.find((user) => user.id === userId);
  if (!selected?.manageable) return;

  hideAlerts();
  try {
    await authFetch(`/auth/admin/users/${userId}`, { method: 'DELETE' });
    showSuccess(t('admin.userDeleted', { email: selected.email }));
    await loadUsers();
  } catch (error) {
    showError(error);
  }
}

function requestDeleteUser(userId) {
  const selected = users.find((user) => user.id === userId);
  if (!selected?.manageable) return;
  openDeleteConfirm({ id: userId, label: selected.email });
}

async function confirmPendingDelete() {
  if (!pendingDelete) return;
  const { id } = pendingDelete;
  closeDeleteConfirm();
  confirmDeleteBtn.disabled = true;
  try {
    await deleteUser(id);
  } finally {
    confirmDeleteBtn.disabled = false;
  }
}

tableBody.addEventListener('click', async (event) => {
  const menuToggle = event.target.closest('[data-row-menu-toggle]');
  if (menuToggle) {
    const menu = menuToggle.closest('.row-menu');
    const dropdown = menu?.querySelector('.row-menu__dropdown');
    const isOpen = menu?.dataset.open === 'true';
    closeAllRowMenus();
    if (menu && dropdown && !isOpen) {
      menu.dataset.open = 'true';
      menuToggle.setAttribute('aria-expanded', 'true');
      dropdown.hidden = false;
    }
    return;
  }

  const editBtn = event.target.closest('[data-edit-user]');
  const createAccountBtn = event.target.closest('[data-create-account]');
  const deleteBtn = event.target.closest('[data-delete-user]');
  const linkBtn = event.target.closest('[data-link-user]');
  if (linkBtn) {
    closeAllRowMenus();
    await openLinkStaffModal(users.find((item) => item.id === Number(linkBtn.dataset.linkUser)));
    return;
  }
  if (editBtn) {
    closeAllRowMenus();
    const user = users.find((item) => item.id === Number(editBtn.dataset.editUser));
    openEditModal(user);
    return;
  }
  if (createAccountBtn) {
    closeAllRowMenus();
    const staffId = Number(createAccountBtn.dataset.createAccount);
    const staff = users.find((item) => item.staff_id === staffId);
    // The invite is the only way into an account made without a password, so the quick path exists only while it can be
    // delivered; otherwise the modal asks for the password.
    if (staff?.can_create_account && invitesDeliverable()) {
      // No modal on this path, so nobody picked a role: leave it out and let the server
      // derive it from the CRM position, the same rule bulk provisioning uses.
      await createStaffAccount(staffId, { companyIds: staff.company_ids || [] });
    } else {
      openCreateStaffAccountModal(staff);
    }
    return;
  }
  if (deleteBtn) {
    closeAllRowMenus();
    requestDeleteUser(Number(deleteBtn.dataset.deleteUser));
  }
});

document.addEventListener('click', (event) => {
  if (!event.target.closest('.row-menu')) {
    closeAllRowMenus();
  }
});

createStaffAccountForm?.addEventListener('submit', async (event) => {
  event.preventDefault();
  hideFieldError(createStaffAccountErrorEl);
  if (!pendingStaffAccountId) return;
  const email = createStaffAccountEmail.value.trim();
  if (!email) {
    showFieldError(createStaffAccountErrorEl, t('admin.realStaffEmailRequired'));
    return;
  }
  const problem = formInputProblem(email, createStaffAccountPassword.value);
  if (problem) {
    showFieldError(createStaffAccountErrorEl, problem);
    return;
  }
  await createStaffAccount(pendingStaffAccountId, {
    email,
    password: createStaffAccountPassword.value,
    role: createStaffAccountRoleSelect.value,
    companyIds: Array.from(createStaffAccountBranchSelect.selectedOptions).map((option) => Number(option.value)),
  });
});

editForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!editingUserId) return;
  hideFieldError(editErrorEl);
  const problem = formInputProblem(editEmail.value.trim());
  if (problem) {
    showFieldError(editErrorEl, problem);
    return;
  }
  try {
    await withBusyButton(saveBtn, async () => {
      const company_ids = Array.from(editBranchSelect.selectedOptions).map((option) => Number(option.value));
      await authFetch(`/auth/admin/users/${editingUserId}`, {
        method: 'PATCH',
        body: JSON.stringify({
          email: editEmail.value.trim(),
          full_name: editName.value.trim() || null,
          role: editRoleSelect.value,
          company_ids,
        }),
      });
      closeEditModal();
      showSuccess(t('admin.changesSaved'));
      await loadUsers();
    });
  } catch (error) {
    showFieldError(editErrorEl, error);
  }
});

async function testDraftYclientsCredential() {
  hideYclientsCredentialsError();
  try {
    await withBusyButton(testYclientsCredentialDraftBtn, async () => {
      await authFetch('/auth/admin/yclients-credentials/test', {
        method: 'POST',
        body: JSON.stringify({
          partner_token: yclientsCredentialPartnerToken.value.trim(),
          login: yclientsCredentialLogin.value.trim(),
          password: yclientsCredentialPassword.value,
        }),
      });
      showSuccess(t('admin.credentialsValid'));
    });
  } catch (error) {
    showYclientsCredentialsError(error);
  }
}

async function saveYclientsCredential(event) {
  event.preventDefault();
  hideYclientsCredentialsError();
  try {
    await withBusyButton(saveYclientsCredentialBtn, async () => {
      const company_ids = Array.from(yclientsCredentialBranchSelect.selectedOptions)
        .map((option) => Number(option.value));
      const body = {
        title: yclientsCredentialTitle.value.trim(),
        is_active: yclientsCredentialActive.checked,
        company_ids,
      };
      if (isPlatformAdmin()) {
        const portalAccountId = selectedPortalAccountId();
        if (!portalAccountId) {
          throw new Error(t('admin.selectTenantForCredentials'));
        }
        body.portal_account_id = Number(portalAccountId);
      }
      if (yclientsCredentialPartnerToken.value.trim()) {
        body.partner_token = yclientsCredentialPartnerToken.value.trim();
      }
      if (yclientsCredentialLogin.value.trim()) {
        body.login = yclientsCredentialLogin.value.trim();
      }
      if (yclientsCredentialPassword.value) {
        body.password = yclientsCredentialPassword.value;
      }
      if (!editingYclientsCredentialId && (!body.partner_token || !body.login || !body.password)) {
        throw new Error(t('admin.newCredentialsRequired'));
      }
      const editedCredentialId = editingYclientsCredentialId;
      const url = editedCredentialId
        ? `/auth/admin/yclients-credentials/${editedCredentialId}`
        : '/auth/admin/yclients-credentials';
      await authFetch(url, {
        method: editedCredentialId ? 'PATCH' : 'POST',
        body: JSON.stringify(body),
      });
      resetYclientsCredentialForm();
      showSuccess(editedCredentialId ? t('admin.credentialsUpdated') : t('admin.credentialsSaved'));
      await loadYclientsCredentials();
    });
  } catch (error) {
    showYclientsCredentialsError(error);
  }
}

yclientsCredentialsBody?.addEventListener('click', async (event) => {
  const editBtn = event.target.closest('[data-edit-yclients-credential]');
  const testBtn = event.target.closest('[data-test-yclients-credential]');
  const deleteBtn = event.target.closest('[data-delete-yclients-credential]');
  if (editBtn) {
    editYclientsCredential(Number(editBtn.dataset.editYclientsCredential));
    return;
  }
  if (testBtn) {
    hideYclientsCredentialsError();
    testBtn.disabled = true;
    try {
      await authFetch(`/auth/admin/yclients-credentials/${testBtn.dataset.testYclientsCredential}/test`, {
        method: 'POST',
      });
      showSuccess(t('admin.credentialsValid'));
    } catch (error) {
      showYclientsCredentialsError(error);
    } finally {
      testBtn.disabled = false;
    }
  }
  if (deleteBtn && confirm(t('settings.credDeleteConfirm'))) {
    hideYclientsCredentialsError();
    deleteBtn.disabled = true;
    try {
      await authFetch(`/auth/admin/yclients-credentials/${deleteBtn.dataset.deleteYclientsCredential}`, {
        method: 'DELETE',
      });
      showSuccess(t('admin.credentialsDeleted'));
      await loadYclientsCredentials();
    } catch (error) {
      showYclientsCredentialsError(error);
    } finally {
      deleteBtn.disabled = false;
    }
  }
});

createForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  hideFieldError(createErrorEl);
  const problem = formInputProblem(createEmail.value.trim(), createPassword.value);
  if (problem) {
    showFieldError(createErrorEl, problem);
    return;
  }
  try {
    await withBusyButton(createBtn, async () => {
      const company_ids = Array.from(createBranchSelect.selectedOptions).map((option) => Number(option.value));
      const portal_account_id = selectedPortalAccountId();
      const payload = await authFetch('/auth/admin/users', {
        method: 'POST',
        body: JSON.stringify({
          email: createEmail.value.trim(),
          password: createPassword.value || null,
          full_name: createName.value.trim() || null,
          role: createRoleSelect.value,
          portal_account_id: portal_account_id ? Number(portal_account_id) : null,
          company_ids,
        }),
      });
      closeCreateModal();
      showCredentialsModal([payload.data]);
      showSuccess(t('admin.userCreated', { email: payload.data.email }));
      await Promise.all([loadUsers(), loadInitialPasswords()]);
    });
  } catch (error) {
    showFieldError(createErrorEl, error);
  }
});

initialPasswordsSearch?.addEventListener('input', renderInitialPasswordsTable);
initialPasswordsBody?.addEventListener('click', (event) => {
  const button = event.target.closest('[data-resend-invite]');
  if (!button) return;
  resendInvite(Number(button.dataset.resendInvite), button);
});
usersSearch?.addEventListener('input', renderUsers);
yclientsCredentialsForm?.addEventListener('submit', saveYclientsCredential);
testYclientsCredentialDraftBtn?.addEventListener('click', testDraftYclientsCredential);
cancelYclientsCredentialEditBtn?.addEventListener('click', resetYclientsCredentialForm);
document.getElementById('open-create-user').addEventListener('click', openCreateModal);
provisionAccountsBtn?.addEventListener('click', provisionAllAccounts);
document.getElementById('close-create-user').addEventListener('click', closeCreateModal);
saveCredentialsExcelBtn?.addEventListener('click', saveCredentialsAsCsv);
distributeCredentialsBtn?.addEventListener('click', distributeCredentials);
document.getElementById('close-credentials').addEventListener('click', closeCredentialsModal);
document.getElementById('close-credentials-btn').addEventListener('click', closeCredentialsModal);
credentialsModal.querySelector('[data-close-modal="credentials"]')?.addEventListener('click', closeCredentialsModal);
document.getElementById('cancel-create-user').addEventListener('click', closeCreateModal);
document.getElementById('close-edit-user').addEventListener('click', closeEditModal);
document.getElementById('cancel-edit-user').addEventListener('click', closeEditModal);
document.getElementById('close-create-staff-account').addEventListener('click', closeCreateStaffAccountModal);
document.getElementById('cancel-create-staff-account').addEventListener('click', closeCreateStaffAccountModal);
document.getElementById('close-delete-confirm').addEventListener('click', closeDeleteConfirm);
document.getElementById('cancel-delete-confirm').addEventListener('click', closeDeleteConfirm);
document.getElementById('confirm-delete').addEventListener('click', confirmPendingDelete);
deleteConfirmModal.querySelector('[data-close-modal="delete"]').addEventListener('click', closeDeleteConfirm);

createModal.querySelector('[data-close-modal="create"]').addEventListener('click', closeCreateModal);
editModal.querySelector('[data-close-modal="edit"]').addEventListener('click', closeEditModal);
createStaffAccountModal.querySelector('[data-close-modal="create-staff-account"]').addEventListener('click', closeCreateStaffAccountModal);
linkStaffModal.querySelector('[data-close-modal="link-staff"]').addEventListener('click', closeLinkStaffModal);
document.getElementById('close-link-staff').addEventListener('click', closeLinkStaffModal);

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  if (closeAllRowMenus()) return;
  modals.closeTopmost();
});

document.getElementById('back-settings').addEventListener('click', () => {
  window.location.href = '/settings.html';
});

async function init() {
  if (!requireAuthRedirect()) return;
  try {
    const me = await authFetch('/auth/me');
    currentUserId = me.data.id;
    currentUserRole = me.data.role;
    if (!ADMIN_ROLES.has(me.data.role)) {
      window.location.href = '/settings.html';
      return;
    }
    await loadAdminMeta();
    await loadPortalAccounts();
    if (isPlatformAdmin() && !selectedPortalAccountId()) {
      showError(t('admin.selectTenantForSettings'));
      return;
    }
    await loadBranches();
    await loadTenantScopedLists();
  } catch (error) {
    if (!hasSessionHint()) {
      logout();
      return;
    }
    showError(error);
  }
}

tenantSelect?.addEventListener('change', async () => {
  setSelectedPortalAccountId(tenantSelect.value);
  hideAlerts();
  const isCurrentTenant = tenantGuard();
  try {
    await reloadTenantScopedAdminData();
    if (isCurrentTenant()) showSuccess(t('admin.businessTenantSelected'));
  } catch (error) {
    if (isCurrentTenant()) showError(error);
  }
});

init();
