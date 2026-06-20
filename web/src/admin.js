import './auth.css';
import { enhanceSelect } from './customSelect.js';
import { authFetch, getToken, logout, requireAuthRedirect } from './auth.js';
import * as XLSX from 'xlsx';

const errorEl = document.getElementById('error');
const successEl = document.getElementById('success');
const createErrorEl = document.getElementById('create-error');
const editErrorEl = document.getElementById('edit-error');
const editStaffErrorEl = document.getElementById('edit-staff-error');
const tableBody = document.getElementById('users-body');
const usersSearch = document.getElementById('users-search');
const createModal = document.getElementById('create-user-modal');
const editModal = document.getElementById('edit-user-modal');
const editStaffModal = document.getElementById('edit-staff-modal');
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
const createForm = document.getElementById('create-user-form');
const editForm = document.getElementById('edit-user-form');
const editStaffForm = document.getElementById('edit-staff-form');
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
const saveBtn = document.getElementById('save-user');
const saveStaffBtn = document.getElementById('save-staff');
const adminRoleLabel = document.getElementById('admin-role-label');
const openCreateUserBtn = document.getElementById('open-create-user');

const createRoleDropdown = enhanceSelect(createRoleSelect, { placeholder: 'Выберите роль' });
const createBranchDropdown = enhanceSelect(createBranchSelect, { placeholder: 'Выберите филиалы' });
const editRoleDropdown = enhanceSelect(editRoleSelect, { placeholder: 'Выберите роль' });
const editBranchDropdown = enhanceSelect(editBranchSelect, { placeholder: 'Выберите филиалы' });
const editStaffBranchDropdown = enhanceSelect(editStaffBranchSelect, { placeholder: 'Выберите филиал' });

const ROLE_LABELS = {
  viewer: 'viewer — только просмотр',
  manager: 'manager — метрики филиала',
  branch_admin: 'branch_admin — админ филиала',
  super_admin: 'super_admin — вся сеть',
};

const MANAGER_ROLES = new Set(['super_admin', 'branch_admin', 'manager']);
const ADMIN_ROLES = new Set(['super_admin', 'branch_admin']);

let users = [];
let branches = [];
let adminMeta = null;
let currentUserId = null;
let currentUserRole = null;
let editingUserId = null;
let editingStaffId = null;
let pendingDelete = null;
let initialPasswords = [];
let currentCredentialsItems = [];

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

function applyAdminMeta() {
  const canManage = canManageUsers();
  const roles = canManage ? adminMeta?.assignable_roles || [] : [];
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
  if (users.length) {
    renderUsers();
  }
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
      ? 'Вы уверены, что хотите удалить работника '
      : 'Вы уверены, что хотите удалить пользователя ';
  deleteConfirmMessage.innerHTML = `
    <p class="admin-modal__confirm-text">${lead}<strong class="admin-modal__confirm-target">${safeLabel}</strong>?</p>
    <p class="admin-modal__confirm-warning">Это действие нельзя отменить — восстановить запись будет невозможно.</p>
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
          <div>Логин: <code>${escapeHtml(item.email)}</code></div>
          <div>Пароль: <code>${escapeHtml(item.initial_password)}</code></div>
        </div>`
    )
    .join('');
  credentialsMessage.innerHTML = `
    <p class="admin-modal__confirm-text">Сохраните данные — пароль можно посмотреть позже в разделе «Первичные пароли».</p>
    <div class="credentials-list">${rows}</div>
  `;
  credentialsModal.hidden = false;
  document.body.classList.add('admin-modal-open');
}

function saveCredentialsAsExcel() {
  if (!currentCredentialsItems.length) return;

  const rows = currentCredentialsItems.map((item) => ({
    ID: item.staff_id ?? item.user_id ?? '',
    Имя: item.full_name || '',
    'Логин (email)': item.email,
    Пароль: item.initial_password,
  }));

  const worksheet = XLSX.utils.json_to_sheet(rows);
  worksheet['!cols'] = [{ wch: 12 }, { wch: 28 }, { wch: 36 }, { wch: 18 }];

  const workbook = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(workbook, worksheet, 'Пароли');

  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
  XLSX.writeFile(workbook, `portal-credentials-${stamp}.xlsx`);
}

async function distributeCredentials() {
  if (!currentCredentialsItems.length) return;
  hideCredentialsAlerts();

  const userIds = currentCredentialsItems.map((item) => item.user_id).filter(Boolean);
  if (!userIds.length) {
    credentialsErrorEl.textContent = 'Нет идентификаторов пользователей для рассылки';
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
    const parts = [`Отправлено писем: ${sentCount || 0}`];
    if (skipped?.length) {
      parts.push(`Пропущено: ${skipped.length} (нет реального email или пароль уже изменён)`);
    }
    if (errors?.length) {
      parts.push(`Ошибок: ${errors.length}`);
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
    return '<span class="role-badge">staff · без аккаунта</span>';
  }
  const classes = {
    super_admin: 'role-badge role-badge--super',
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
    ? `<button type="button" class="row-menu__item" role="menuitem" ${createAccountAttr}>Создать аккаунт</button>`
    : '';
  return `<div class="row-menu" data-row-menu>
    <button
      type="button"
      class="row-menu__trigger"
      data-row-menu-toggle
      aria-label="Действия"
      aria-haspopup="menu"
      aria-expanded="false"
    >
      <span class="row-menu__dots" aria-hidden="true"></span>
    </button>
    <div class="row-menu__dropdown" role="menu" hidden>
      ${createItem}
      <button type="button" class="row-menu__item" role="menuitem" ${editAttr}>Редактировать</button>
      <button type="button" class="row-menu__item row-menu__item--danger" role="menuitem" ${deleteAttr}>Удалить</button>
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
      user.is_portal_user ? (user.email_verified ? 'подтверждён' : 'ожидает') : '',
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
        <td>${user.is_portal_user ? user.email : '—'}${user.id === currentUserId ? ' <span class="user-you">(вы)</span>' : ''}</td>
        <td>${user.full_name || '—'}</td>
        <td>${roleBadge(user.role)}</td>
        <td><span class="status-dot ${user.email_verified ? 'ok' : ''}">${user.is_portal_user ? (user.email_verified ? 'подтверждён' : 'ожидает') : '—'}</span></td>
        <td>${branchTitles(user.company_ids)}</td>
        <td class="admin-table__cell-actions">${actions}</td>
      </tr>`;
        })
        .join('')
    : `<tr><td colspan="7" class="admin-table__empty">${
        users.length ? 'Ничего не найдено' : 'Пользователи не найдены'
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
        initialPasswords.length ? 'Ничего не найдено' : 'Нет сохранённых первичных паролей'
      }</td></tr>`;
}

async function loadInitialPasswords() {
  if (!canManageUsers()) return;
  const payload = await authFetch('/auth/admin/initial-passwords');
  initialPasswords = payload.data || [];
  renderInitialPasswordsTable();
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
    const { created_count: count, created } = payload.data || {};
    if (created?.length) {
      showCredentialsModal(created);
    }
    showSuccess(`Создано аккаунтов: ${count || 0}`);
    await Promise.all([loadUsers(), loadInitialPasswords()]);
  } catch (error) {
    showError(error.message);
  } finally {
    provisionAccountsBtn.disabled = false;
    provisionAccountsBtn.classList.remove('is-loading');
  }
}

async function createStaffAccount(staffId) {
  const selected = users.find((user) => user.staff_id === staffId);
  if (!selected?.manageable) return;

  hideAlerts();
  try {
    const payload = await authFetch(`/auth/admin/staff/${staffId}/create-account`, {
      method: 'POST',
      body: JSON.stringify({ role: 'viewer' }),
    });
    showCredentialsModal([payload.data]);
    showSuccess(`Аккаунт для ${selected.full_name} создан`);
    await Promise.all([loadUsers(), loadInitialPasswords()]);
  } catch (error) {
    showError(error.message);
  }
}

async function deleteUser(userId) {
  const selected = users.find((user) => user.id === userId);
  if (!selected?.manageable) return;

  hideAlerts();
  try {
    await authFetch(`/auth/admin/users/${userId}`, { method: 'DELETE' });
    showSuccess(`Пользователь ${selected.email} удалён`);
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
    showSuccess(`Работник ${selected.full_name} удалён`);
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
    await createStaffAccount(Number(createAccountBtn.dataset.createAccount));
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
    showSuccess('Изменения сохранены');
    await loadUsers();
  } catch (error) {
    showEditStaffError(error.message);
  } finally {
    saveStaffBtn.disabled = false;
    saveStaffBtn.classList.remove('is-loading');
  }
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
        full_name: editName.value.trim() || null,
        role: editRoleSelect.value,
        company_ids,
      }),
    });
    closeEditModal();
    showSuccess('Изменения сохранены');
    await loadUsers();
  } catch (error) {
    showEditError(error.message);
  } finally {
    saveBtn.disabled = false;
    saveBtn.classList.remove('is-loading');
  }
});

createForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  hideCreateError();
  createBtn.disabled = true;
  createBtn.classList.add('is-loading');
  try {
    const company_ids = Array.from(createBranchSelect.selectedOptions).map((option) => Number(option.value));
    const payload = await authFetch('/auth/admin/users', {
      method: 'POST',
      body: JSON.stringify({
        email: createEmail.value.trim(),
        password: createPassword.value,
        full_name: createName.value.trim() || null,
        role: createRoleSelect.value,
        company_ids,
      }),
    });
    closeCreateModal();
    if (payload.data?.initial_password) {
      showCredentialsModal([payload.data]);
    }
    showSuccess(`Пользователь ${payload.data.email} создан`);
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
document.getElementById('close-delete-confirm').addEventListener('click', closeDeleteConfirm);
document.getElementById('cancel-delete-confirm').addEventListener('click', closeDeleteConfirm);
document.getElementById('confirm-delete').addEventListener('click', confirmPendingDelete);
deleteConfirmModal.querySelector('[data-close-modal="delete"]').addEventListener('click', closeDeleteConfirm);

createModal.querySelector('[data-close-modal="create"]').addEventListener('click', closeCreateModal);
editModal.querySelector('[data-close-modal="edit"]').addEventListener('click', closeEditModal);
editStaffModal.querySelector('[data-close-modal="edit-staff"]').addEventListener('click', closeEditStaffModal);

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  if (closeAllRowMenus()) return;
  if (!credentialsModal.hidden) closeCredentialsModal();
  else if (!deleteConfirmModal.hidden) closeDeleteConfirm();
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
    await Promise.all([loadBranches(), loadUsers(), loadInitialPasswords()]);
  } catch (error) {
    if (!getToken()) {
      logout();
      return;
    }
    showError(error.message);
  }
}

init();
