import './auth.css';
import { enhanceSelect } from './customSelect.js';
import {
  authFetch,
  getSelectedPortalAccountId,
  hasSessionHint,
  logout,
  requireAuthRedirect,
  setSelectedPortalAccountId,
} from './auth.js';
import * as XLSX from 'xlsx';
import { applyTranslations, getLocale, mountLanguageSwitcher, t } from './i18n.js';

document.documentElement.lang = getLocale();
applyTranslations();
mountLanguageSwitcher(document.getElementById('lang-switcher'))?.addEventListener('change', () => location.reload());

const errorEl = document.getElementById('error');
const successEl = document.getElementById('success');
const createErrorEl = document.getElementById('create-error');
const editErrorEl = document.getElementById('edit-error');
const editStaffErrorEl = document.getElementById('edit-staff-error');
const createStaffAccountErrorEl = document.getElementById('create-staff-account-error');
const tableBody = document.getElementById('users-body');
const usersSearch = document.getElementById('users-search');
const createModal = document.getElementById('create-user-modal');
const editModal = document.getElementById('edit-user-modal');
const editStaffModal = document.getElementById('edit-staff-modal');
const createStaffAccountModal = document.getElementById('create-staff-account-modal');
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
const editStaffForm = document.getElementById('edit-staff-form');
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
const editStaffName = document.getElementById('edit-staff-name');
const editStaffPosition = document.getElementById('edit-staff-position');
const editStaffBranchSelect = document.getElementById('edit-staff-branch-select');
const createStaffAccountName = document.getElementById('create-staff-account-name');
const createStaffAccountEmail = document.getElementById('create-staff-account-email');
const saveBtn = document.getElementById('save-user');
const saveStaffBtn = document.getElementById('save-staff');
const adminRoleLabel = document.getElementById('admin-role-label');
const openCreateUserBtn = document.getElementById('open-create-user');
const tenantSwitcherSection = document.getElementById('tenant-switcher-section');
const tenantSelect = document.getElementById('tenant-select');
const tenantMeta = document.getElementById('tenant-meta');

const createRoleDropdown = enhanceSelect(createRoleSelect, { placeholder: t('admin.selectRole') });
const createBranchDropdown = enhanceSelect(createBranchSelect, { placeholder: t('admin.selectBranches') });
const editRoleDropdown = enhanceSelect(editRoleSelect, { placeholder: t('admin.selectRole') });
const editBranchDropdown = enhanceSelect(editBranchSelect, { placeholder: t('admin.selectBranches') });
const editStaffBranchDropdown = enhanceSelect(editStaffBranchSelect, { placeholder: t('admin.selectBranch') });
const yclientsCredentialBranchDropdown = enhanceSelect(yclientsCredentialBranchSelect, { placeholder: t('admin.selectBranches') });

const ROLE_LABELS = {
  viewer: t('admin.roleViewer'),
  manager: t('admin.roleManager'),
  branch_admin: t('admin.roleBranchAdmin'),
  owner: t('admin.roleOwner'),
  platform_admin: t('admin.rolePlatformAdmin'),
};

const MANAGER_ROLES = new Set(['platform_admin', 'owner', 'branch_admin', 'manager']);
const ADMIN_ROLES = new Set(['platform_admin', 'owner', 'branch_admin']);

let users = [];
let branches = [];
let adminMeta = null;
let currentUserId = null;
let currentUserRole = null;
let editingUserId = null;
let editingStaffId = null;
let pendingStaffAccountId = null;
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

function hideCreateError() {
  createErrorEl.hidden = true;
}

function hideEditError() {
  editErrorEl.hidden = true;
}

function hideEditStaffError() {
  editStaffErrorEl.hidden = true;
}

function hideCreateStaffAccountError() {
  createStaffAccountErrorEl.hidden = true;
}

function showError(message) {
  hideAlerts();
  errorEl.textContent = message;
  errorEl.hidden = false;
}

function showCreateError(message) {
  hideCreateError();
  createErrorEl.textContent = message;
  createErrorEl.hidden = false;
}

function showEditError(message) {
  hideEditError();
  editErrorEl.textContent = message;
  editErrorEl.hidden = false;
}

function showEditStaffError(message) {
  hideEditStaffError();
  editStaffErrorEl.textContent = message;
  editStaffErrorEl.hidden = false;
}

function showCreateStaffAccountError(message) {
  hideCreateStaffAccountError();
  createStaffAccountErrorEl.textContent = message;
  createStaffAccountErrorEl.hidden = false;
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
    .map((role) => `<option value="${role}">${ROLE_LABELS[role] || role}</option>`)
    .join('');
  dropdown.refresh();
}

function renderBranchOptions(selectEl, dropdown, selectedIds = [], multiple = true) {
  const items = scopedBranches();
  selectEl.innerHTML = items
    .map((branch) => `<option value="${branch.id}">${branch.title}</option>`)
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
  renderRoleOptions(createRoleSelect, createRoleDropdown, roles);
  renderBranchOptions(createBranchSelect, createBranchDropdown);

  if (adminRoleLabel) {
    adminRoleLabel.textContent = adminMeta?.role || 'admin';
  }
  if (openCreateUserBtn) {
    openCreateUserBtn.hidden = !canManage;
  }
  if (provisionAccountsBtn) {
    provisionAccountsBtn.hidden = !canManage;
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

async function reloadTenantScopedAdminData() {
  resetYclientsCredentialForm();
  await loadBranches();
  await Promise.all([loadUsers(), loadInitialPasswords(), loadYclientsCredentials(), loadMetricVisibility()]);
}

function openCreateModal() {
  hideCreateError();
  createForm.reset();
  renderBranchOptions(createBranchSelect, createBranchDropdown);
  if (createRoleSelect.options.length) {
    createRoleSelect.selectedIndex = 0;
    createRoleDropdown.syncFromNative();
  }
  createModal.hidden = false;
  document.body.classList.add('admin-modal-open');
  createEmail.focus();
}

function isAnyModalOpen() {
  return (
    !createModal.hidden ||
    !editModal.hidden ||
    !editStaffModal.hidden ||
    !createStaffAccountModal.hidden ||
    !deleteConfirmModal.hidden ||
    !credentialsModal.hidden
  );
}

function closeCreateModal() {
  createModal.hidden = true;
  if (!isAnyModalOpen()) {
    document.body.classList.remove('admin-modal-open');
  }
  hideCreateError();
}

function openEditModal(user) {
  if (!user?.manageable || !user.is_portal_user) return;
  editingUserId = user.id;
  hideEditError();
  editEmail.value = user.email;
  editName.value = user.full_name || '';
  renderRoleOptions(editRoleSelect, editRoleDropdown, rolesForEdit(user));
  editRoleSelect.value = user.role;
  renderBranchOptions(editBranchSelect, editBranchDropdown, user.company_ids || []);
  editRoleDropdown.syncFromNative();
  editModal.hidden = false;
  document.body.classList.add('admin-modal-open');
  editName.focus();
}

function closeEditModal() {
  editModal.hidden = true;
  editingUserId = null;
  if (!isAnyModalOpen()) {
    document.body.classList.remove('admin-modal-open');
  }
  hideEditError();
}

function openEditStaffModal(staff) {
  if (!staff?.manageable || staff.is_portal_user) return;
  editingStaffId = staff.staff_id;
  hideEditStaffError();
  editStaffName.value = staff.full_name || '';
  editStaffPosition.value = staff.position || '';
  renderBranchOptions(editStaffBranchSelect, editStaffBranchDropdown, staff.company_ids || [], false);
  editStaffModal.hidden = false;
  document.body.classList.add('admin-modal-open');
  editStaffName.focus();
}

function closeEditStaffModal() {
  editStaffModal.hidden = true;
  editingStaffId = null;
  if (!isAnyModalOpen()) {
    document.body.classList.remove('admin-modal-open');
  }
  hideEditStaffError();
}

function openCreateStaffAccountModal(staff) {
  if (!staff?.manageable || staff.is_portal_user) return;
  pendingStaffAccountId = staff.staff_id;
  hideCreateStaffAccountError();
  createStaffAccountForm.reset();
  createStaffAccountName.value = staff.full_name || '';
  createStaffAccountEmail.value = staff.can_create_account ? staff.email : '';
  createStaffAccountModal.hidden = false;
  document.body.classList.add('admin-modal-open');
  createStaffAccountEmail.focus();
}

function closeCreateStaffAccountModal() {
  createStaffAccountModal.hidden = true;
  pendingStaffAccountId = null;
  if (!isAnyModalOpen()) {
    document.body.classList.remove('admin-modal-open');
  }
  hideCreateStaffAccountError();
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function openDeleteConfirm({ type, id, label, subjectType }) {
  pendingDelete = { type, id };
  const safeLabel = escapeHtml(label);
  const lead =
    subjectType === 'staff'
      ? t('admin.deleteStaffLead')
      : t('admin.deleteUserLead');
  deleteConfirmMessage.innerHTML = `
    <p class="admin-modal__confirm-text">${lead}<strong class="admin-modal__confirm-target">${safeLabel}</strong>?</p>
    <p class="admin-modal__confirm-warning">${t('admin.deleteIrreversible')}</p>
  `;
  deleteConfirmModal.hidden = false;
  document.body.classList.add('admin-modal-open');
  confirmDeleteBtn.focus();
}

function closeDeleteConfirm() {
  deleteConfirmModal.hidden = true;
  pendingDelete = null;
  if (!isAnyModalOpen()) {
    document.body.classList.remove('admin-modal-open');
  }
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
    initial_password: item.initial_password,
  }));
  hideCredentialsAlerts();
  const rows = currentCredentialsItems
    .map(
      (item) => `
        <div class="credentials-row">
          <strong>${escapeHtml(item.full_name || item.email)}</strong>
          <div>${t('admin.loginLabel')}: <code>${escapeHtml(item.email)}</code></div>
          <div>${t('common.password')}: <code>${escapeHtml(item.initial_password)}</code></div>
        </div>`
    )
    .join('');
  credentialsMessage.innerHTML = `
    <p class="admin-modal__confirm-text">${t('admin.saveCredentialsHint')}</p>
    <div class="credentials-list">${rows}</div>
  `;
  credentialsModal.hidden = false;
  document.body.classList.add('admin-modal-open');
}

function saveCredentialsAsExcel() {
  if (!currentCredentialsItems.length) return;

  const rows = currentCredentialsItems.map((item) => ({
    ID: item.staff_id ?? item.user_id ?? '',
    [t('admin.colName')]: item.full_name || '',
    [t('admin.colLoginEmail')]: item.email,
    [t('admin.colPassword')]: item.initial_password,
  }));

  const worksheet = XLSX.utils.json_to_sheet(rows);
  worksheet['!cols'] = [{ wch: 12 }, { wch: 28 }, { wch: 36 }, { wch: 18 }];

  const workbook = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(workbook, worksheet, t('admin.passwordsSheet'));

  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
  XLSX.writeFile(workbook, `portal-credentials-${stamp}.xlsx`);
}

async function distributeCredentials() {
  if (!currentCredentialsItems.length) return;
  hideCredentialsAlerts();

  const userIds = currentCredentialsItems.map((item) => item.user_id).filter(Boolean);
  if (!userIds.length) {
    credentialsErrorEl.textContent = t('admin.noUserIdsForDistribution');
    credentialsErrorEl.hidden = false;
    return;
  }

  distributeCredentialsBtn.disabled = true;
  distributeCredentialsBtn.classList.add('is-loading');
  try {
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
      credentialsErrorEl.textContent = errors.map((item) => `${item.email}: ${item.reason}`).join('; ');
      credentialsErrorEl.hidden = false;
    }
  } catch (error) {
    credentialsErrorEl.textContent = error.message;
    credentialsErrorEl.hidden = false;
  } finally {
    distributeCredentialsBtn.disabled = false;
    distributeCredentialsBtn.classList.remove('is-loading');
  }
}

function closeCredentialsModal() {
  credentialsModal.hidden = true;
  currentCredentialsItems = [];
  hideCredentialsAlerts();
  if (!isAnyModalOpen()) {
    document.body.classList.remove('admin-modal-open');
  }
}

async function loadAdminMeta() {
  const payload = await authFetch('/auth/admin/meta');
  adminMeta = payload.data || null;
  applyAdminMeta();
}

async function loadBranches() {
  const payload = await authFetch('/dashboard/branches');
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
    viewer: 'role-badge role-badge--viewer',
  };
  const cls = classes[role] || 'role-badge';
  return `<span class="${cls}">${role}</span>`;
}

function branchTitles(companyIds) {
  if (!companyIds?.length) return '—';
  const titles = companyIds.map((id) => {
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

function renderRowMenu({ editAttr, deleteAttr, createAccountAttr }) {
  const createItem = createAccountAttr
    ? `<button type="button" class="row-menu__item" role="menuitem" ${createAccountAttr}>${t('admin.createAccount')}</button>`
    : '';
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
      ${createItem}
      <button type="button" class="row-menu__item" role="menuitem" ${editAttr}>${t('admin.edit')}</button>
      <button type="button" class="row-menu__item row-menu__item--danger" role="menuitem" ${deleteAttr}>${t('admin.delete')}</button>
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
              actions = renderRowMenu({
                editAttr: `data-edit-user="${user.id}"`,
                deleteAttr: `data-delete-user="${user.id}"`,
              });
            } else {
              actions = renderRowMenu({
                editAttr: `data-edit-staff="${user.staff_id}"`,
                deleteAttr: `data-delete-staff="${user.staff_id}"`,
                createAccountAttr: `data-create-account="${user.staff_id}"`,
              });
            }
          }
          return `
      <tr>
        <td>${user.staff_id ?? user.id ?? '—'}</td>
        <td>${user.is_portal_user ? user.email : '—'}${user.id === currentUserId ? ` <span class="user-you">${t('admin.you')}</span>` : ''}</td>
        <td>${user.full_name || '—'}</td>
        <td>${roleBadge(user.role)}</td>
        <td><span class="status-dot ${user.email_verified ? 'ok' : ''}">${user.is_portal_user ? (user.email_verified ? t('admin.confirmed') : t('admin.pending')) : '—'}</span></td>
        <td>${branchTitles(user.company_ids)}</td>
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
      row.initial_password,
      branchTitles(row.company_ids),
    ]
      .filter(Boolean)
      .join(' ')
      .toLowerCase();
    return haystack.includes(needle);
  });
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
        <td>${row.staff_id ?? row.user_id ?? '—'}</td>
        <td>${escapeHtml(row.email)}</td>
        <td>${escapeHtml(row.full_name || '—')}</td>
        <td>${roleBadge(row.role)}</td>
        <td>${branchTitles(row.company_ids)}</td>
        <td><code class="initial-password">${escapeHtml(row.initial_password)}</code></td>
      </tr>`
        )
        .join('')
    : `<tr><td colspan="6" class="admin-table__empty">${
        initialPasswords.length ? t('admin.nothingFound') : t('admin.noInitialPasswords')
      }</td></tr>`;
}

async function loadInitialPasswords() {
  if (!canManageUsers()) return;
  const payload = await authFetch('/auth/admin/initial-passwords');
  initialPasswords = payload.data || [];
  renderInitialPasswordsTable();
}

function hideYclientsCredentialsError() {
  if (yclientsCredentialsError) {
    yclientsCredentialsError.hidden = true;
  }
}

function showYclientsCredentialsError(message) {
  if (yclientsCredentialsError) {
    yclientsCredentialsError.textContent = message;
    yclientsCredentialsError.hidden = false;
  }
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
        <td>${branchTitles(credential.company_ids)}</td>
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
  const payload = await authFetch('/auth/admin/yclients-credentials');
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
  const payload = await authFetch('/dashboard/metric-visibility');
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
        `<td>${escapeHtml(ROLE_LABELS[role] || role)}</td>` +
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
    if (metricVisibilityError) {
      metricVisibilityError.textContent = error.message;
      metricVisibilityError.hidden = false;
    }
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
  const payload = await authFetch('/auth/admin/users');
  users = payload.data || [];
  renderUsers();
}

async function provisionAllAccounts() {
  if (!canManageUsers()) return;
  hideAlerts();
  provisionAccountsBtn.disabled = true;
  provisionAccountsBtn.classList.add('is-loading');
  try {
    const payload = await authFetch('/auth/admin/provision-accounts', { method: 'POST' });
    const { created_count: count, created, errors } = payload.data || {};
    if (created?.length) {
      showCredentialsModal(created);
    }
    const skippedCount = errors?.length || 0;
    showSuccess(t('admin.accountsCreated', { count: count || 0, skipped: skippedCount ? t('admin.skippedNoRealEmail', { count: skippedCount }) : '' }));
    await Promise.all([loadUsers(), loadInitialPasswords()]);
  } catch (error) {
    showError(error.message);
  } finally {
    provisionAccountsBtn.disabled = false;
    provisionAccountsBtn.classList.remove('is-loading');
  }
}

async function createStaffAccount(staffId, email = null) {
  const selected = users.find((user) => user.staff_id === staffId);
  if (!selected?.manageable) return;

  hideAlerts();
  try {
    const payload = await authFetch(`/auth/admin/staff/${staffId}/create-account`, {
      method: 'POST',
      body: JSON.stringify({ role: 'viewer', email: email || selected.email }),
    });
    closeCreateStaffAccountModal();
    showCredentialsModal([payload.data]);
    showSuccess(t('admin.accountForCreated', { name: selected.full_name }));
    await Promise.all([loadUsers(), loadInitialPasswords()]);
  } catch (error) {
    if (!createStaffAccountModal.hidden) {
      showCreateStaffAccountError(error.message);
    } else {
      showError(error.message);
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
    showError(error.message);
  }
}

async function deleteStaff(staffId) {
  const selected = users.find((user) => user.staff_id === staffId);
  if (!selected?.manageable) return;

  hideAlerts();
  try {
    await authFetch(`/auth/admin/staff/${staffId}`, { method: 'DELETE' });
    showSuccess(t('admin.staffDeleted', { name: selected.full_name }));
    await loadUsers();
  } catch (error) {
    showError(error.message);
  }
}

function requestDeleteUser(userId) {
  const selected = users.find((user) => user.id === userId);
  if (!selected?.manageable) return;
  openDeleteConfirm({
    type: 'user',
    id: userId,
    label: selected.email,
    subjectType: 'user',
  });
}

function requestDeleteStaff(staffId) {
  const selected = users.find((user) => user.staff_id === staffId);
  if (!selected?.manageable) return;
  openDeleteConfirm({
    type: 'staff',
    id: staffId,
    label: selected.full_name || `ID ${staffId}`,
    subjectType: 'staff',
  });
}

async function confirmPendingDelete() {
  if (!pendingDelete) return;
  const { type, id } = pendingDelete;
  closeDeleteConfirm();
  confirmDeleteBtn.disabled = true;
  try {
    if (type === 'user') {
      await deleteUser(id);
    } else {
      await deleteStaff(id);
    }
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
  const editStaffBtn = event.target.closest('[data-edit-staff]');
  const createAccountBtn = event.target.closest('[data-create-account]');
  const deleteBtn = event.target.closest('[data-delete-user]');
  const deleteStaffBtn = event.target.closest('[data-delete-staff]');
  if (editBtn) {
    closeAllRowMenus();
    const user = users.find((item) => item.id === Number(editBtn.dataset.editUser));
    openEditModal(user);
    return;
  }
  if (editStaffBtn) {
    closeAllRowMenus();
    const staff = users.find((item) => item.staff_id === Number(editStaffBtn.dataset.editStaff));
    openEditStaffModal(staff);
    return;
  }
  if (createAccountBtn) {
    closeAllRowMenus();
    const staffId = Number(createAccountBtn.dataset.createAccount);
    const staff = users.find((item) => item.staff_id === staffId);
    if (staff?.can_create_account) {
      await createStaffAccount(staffId);
    } else {
      openCreateStaffAccountModal(staff);
    }
    return;
  }
  if (deleteBtn) {
    closeAllRowMenus();
    requestDeleteUser(Number(deleteBtn.dataset.deleteUser));
    return;
  }
  if (deleteStaffBtn) {
    closeAllRowMenus();
    requestDeleteStaff(Number(deleteStaffBtn.dataset.deleteStaff));
  }
});

document.addEventListener('click', (event) => {
  if (!event.target.closest('.row-menu')) {
    closeAllRowMenus();
  }
});

editStaffForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!editingStaffId) return;
  hideEditStaffError();
  saveStaffBtn.disabled = true;
  saveStaffBtn.classList.add('is-loading');
  try {
    await authFetch(`/auth/admin/staff/${editingStaffId}`, {
      method: 'PATCH',
      body: JSON.stringify({
        full_name: editStaffName.value.trim(),
        position: editStaffPosition.value.trim() || null,
        company_id: Number(editStaffBranchSelect.value),
      }),
    });
    closeEditStaffModal();
    showSuccess(t('admin.changesSaved'));
    await loadUsers();
  } catch (error) {
    showEditStaffError(error.message);
  } finally {
    saveStaffBtn.disabled = false;
    saveStaffBtn.classList.remove('is-loading');
  }
});

createStaffAccountForm?.addEventListener('submit', async (event) => {
  event.preventDefault();
  hideCreateStaffAccountError();
  if (!pendingStaffAccountId) return;
  const email = createStaffAccountEmail.value.trim();
  if (!email) {
    showCreateStaffAccountError(t('admin.realStaffEmailRequired'));
    return;
  }
  await createStaffAccount(pendingStaffAccountId, email);
});

editForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!editingUserId) return;
  hideEditError();
  saveBtn.disabled = true;
  saveBtn.classList.add('is-loading');
  try {
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
  } catch (error) {
    showEditError(error.message);
  } finally {
    saveBtn.disabled = false;
    saveBtn.classList.remove('is-loading');
  }
});

async function testDraftYclientsCredential() {
  hideYclientsCredentialsError();
  testYclientsCredentialDraftBtn.disabled = true;
  testYclientsCredentialDraftBtn.classList.add('is-loading');
  try {
    await authFetch('/auth/admin/yclients-credentials/test', {
      method: 'POST',
      body: JSON.stringify({
        partner_token: yclientsCredentialPartnerToken.value.trim(),
        login: yclientsCredentialLogin.value.trim(),
        password: yclientsCredentialPassword.value,
      }),
    });
    showSuccess(t('admin.credentialsValid'));
  } catch (error) {
    showYclientsCredentialsError(error.message);
  } finally {
    testYclientsCredentialDraftBtn.disabled = false;
    testYclientsCredentialDraftBtn.classList.remove('is-loading');
  }
}

async function saveYclientsCredential(event) {
  event.preventDefault();
  hideYclientsCredentialsError();
  saveYclientsCredentialBtn.disabled = true;
  saveYclientsCredentialBtn.classList.add('is-loading');
  try {
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
  } catch (error) {
    showYclientsCredentialsError(error.message);
  } finally {
    saveYclientsCredentialBtn.disabled = false;
    saveYclientsCredentialBtn.classList.remove('is-loading');
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
      showYclientsCredentialsError(error.message);
    } finally {
      testBtn.disabled = false;
    }
  }
  if (deleteBtn) {
    hideYclientsCredentialsError();
    deleteBtn.disabled = true;
    try {
      await authFetch(`/auth/admin/yclients-credentials/${deleteBtn.dataset.deleteYclientsCredential}`, {
        method: 'DELETE',
      });
      showSuccess(t('admin.credentialsDeleted'));
      await loadYclientsCredentials();
    } catch (error) {
      showYclientsCredentialsError(error.message);
    } finally {
      deleteBtn.disabled = false;
    }
  }
});

createForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  hideCreateError();
  createBtn.disabled = true;
  createBtn.classList.add('is-loading');
  try {
    const company_ids = Array.from(createBranchSelect.selectedOptions).map((option) => Number(option.value));
    const portal_account_id = selectedPortalAccountId();
    const payload = await authFetch('/auth/admin/users', {
      method: 'POST',
      body: JSON.stringify({
        email: createEmail.value.trim(),
        password: createPassword.value,
        full_name: createName.value.trim() || null,
        role: createRoleSelect.value,
        portal_account_id: portal_account_id ? Number(portal_account_id) : null,
        company_ids,
      }),
    });
    closeCreateModal();
    if (payload.data?.initial_password) {
      showCredentialsModal([payload.data]);
    }
    showSuccess(t('admin.userCreated', { email: payload.data.email }));
    await Promise.all([loadUsers(), loadInitialPasswords()]);
  } catch (error) {
    showCreateError(error.message);
  } finally {
    createBtn.disabled = false;
    createBtn.classList.remove('is-loading');
  }
});

initialPasswordsSearch?.addEventListener('input', renderInitialPasswordsTable);
usersSearch?.addEventListener('input', renderUsers);
yclientsCredentialsForm?.addEventListener('submit', saveYclientsCredential);
testYclientsCredentialDraftBtn?.addEventListener('click', testDraftYclientsCredential);
cancelYclientsCredentialEditBtn?.addEventListener('click', resetYclientsCredentialForm);
document.getElementById('open-create-user').addEventListener('click', openCreateModal);
provisionAccountsBtn?.addEventListener('click', provisionAllAccounts);
document.getElementById('close-create-user').addEventListener('click', closeCreateModal);
saveCredentialsExcelBtn?.addEventListener('click', saveCredentialsAsExcel);
distributeCredentialsBtn?.addEventListener('click', distributeCredentials);
document.getElementById('close-credentials').addEventListener('click', closeCredentialsModal);
document.getElementById('close-credentials-btn').addEventListener('click', closeCredentialsModal);
credentialsModal.querySelector('[data-close-modal="credentials"]')?.addEventListener('click', closeCredentialsModal);
document.getElementById('cancel-create-user').addEventListener('click', closeCreateModal);
document.getElementById('close-edit-user').addEventListener('click', closeEditModal);
document.getElementById('cancel-edit-user').addEventListener('click', closeEditModal);
document.getElementById('close-edit-staff').addEventListener('click', closeEditStaffModal);
document.getElementById('cancel-edit-staff').addEventListener('click', closeEditStaffModal);
document.getElementById('close-create-staff-account').addEventListener('click', closeCreateStaffAccountModal);
document.getElementById('cancel-create-staff-account').addEventListener('click', closeCreateStaffAccountModal);
document.getElementById('close-delete-confirm').addEventListener('click', closeDeleteConfirm);
document.getElementById('cancel-delete-confirm').addEventListener('click', closeDeleteConfirm);
document.getElementById('confirm-delete').addEventListener('click', confirmPendingDelete);
deleteConfirmModal.querySelector('[data-close-modal="delete"]').addEventListener('click', closeDeleteConfirm);

createModal.querySelector('[data-close-modal="create"]').addEventListener('click', closeCreateModal);
editModal.querySelector('[data-close-modal="edit"]').addEventListener('click', closeEditModal);
editStaffModal.querySelector('[data-close-modal="edit-staff"]').addEventListener('click', closeEditStaffModal);
createStaffAccountModal.querySelector('[data-close-modal="create-staff-account"]').addEventListener('click', closeCreateStaffAccountModal);

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  if (closeAllRowMenus()) return;
  if (!credentialsModal.hidden) closeCredentialsModal();
  else if (!deleteConfirmModal.hidden) closeDeleteConfirm();
  else if (!createStaffAccountModal.hidden) closeCreateStaffAccountModal();
  else if (!editStaffModal.hidden) closeEditStaffModal();
  else if (!editModal.hidden) closeEditModal();
  else if (!createModal.hidden) closeCreateModal();
});

document.getElementById('back-profile').addEventListener('click', () => {
  window.location.href = '/profile.html';
});

async function init() {
  if (!requireAuthRedirect()) return;
  try {
    const me = await authFetch('/auth/me');
    currentUserId = me.data.id;
    currentUserRole = me.data.role;
    if (!MANAGER_ROLES.has(me.data.role)) {
      window.location.href = '/profile.html';
      return;
    }
    await loadAdminMeta();
    await loadPortalAccounts();
    if (isPlatformAdmin() && !selectedPortalAccountId()) {
      showError(t('admin.selectTenantForSettings'));
      return;
    }
    await loadBranches();
    await Promise.all([loadUsers(), loadInitialPasswords(), loadYclientsCredentials(), loadMetricVisibility()]);
  } catch (error) {
    if (!hasSessionHint()) {
      logout();
      return;
    }
    showError(error.message);
  }
}

tenantSelect?.addEventListener('change', async () => {
  setSelectedPortalAccountId(tenantSelect.value);
  hideAlerts();
  try {
    await reloadTenantScopedAdminData();
    showSuccess(t('admin.businessTenantSelected'));
  } catch (error) {
    showError(error.message);
  }
});

init();
