import './auth.css';
import { authFetch, getToken, logout, requireAuthRedirect } from './auth.js';

const ADMIN_ROLES = new Set(['super_admin', 'branch_admin']);
const MANAGER_ROLES = new Set(['super_admin', 'branch_admin', 'manager']);

const changePasswordModal = document.getElementById('change-password-modal');
const passwordErrorEl = document.getElementById('password-error');
const passwordSuccessEl = document.getElementById('password-success');
const changePasswordForm = document.getElementById('change-password-form');
const changePasswordBtn = document.getElementById('change-password-btn');
const currentPasswordInput = document.getElementById('current-password');
const newPasswordInput = document.getElementById('new-password');
const confirmPasswordInput = document.getElementById('confirm-password');

const ROLE_LABELS = {
  super_admin: 'Super Admin — вся сеть',
  branch_admin: 'Branch Admin — админ филиала',
  manager: 'Manager — метрики филиала',
  viewer: 'Viewer — только просмотр',
};

function greetingName(user) {
  const fullName = user.full_name?.trim();
  if (!fullName) {
    return user.email.split('@')[0];
  }
  return fullName.split(/\s+/)[0] || fullName;
}

document.getElementById('logout').addEventListener('click', () => logout());
document.getElementById('back-dashboard').addEventListener('click', () => {
  window.location.href = '/';
});

function hidePasswordAlerts() {
  passwordErrorEl.hidden = true;
  passwordSuccessEl.hidden = true;
}

function openChangePasswordModal() {
  hidePasswordAlerts();
  changePasswordForm.reset();
  changePasswordModal.hidden = false;
  document.body.classList.add('admin-modal-open');
  currentPasswordInput.focus();
}

function closeChangePasswordModal() {
  changePasswordModal.hidden = true;
  document.body.classList.remove('admin-modal-open');
  hidePasswordAlerts();
}

document.getElementById('open-change-password').addEventListener('click', openChangePasswordModal);
document.getElementById('close-change-password').addEventListener('click', closeChangePasswordModal);
document.getElementById('cancel-change-password').addEventListener('click', closeChangePasswordModal);
changePasswordModal.querySelector('[data-close-modal="change-password"]')?.addEventListener('click', closeChangePasswordModal);

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !changePasswordModal.hidden) {
    closeChangePasswordModal();
  }
});

changePasswordForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  hidePasswordAlerts();

  if (newPasswordInput.value !== confirmPasswordInput.value) {
    passwordErrorEl.textContent = 'Новый пароль и подтверждение не совпадают';
    passwordErrorEl.hidden = false;
    return;
  }

  changePasswordBtn.disabled = true;
  changePasswordBtn.classList.add('is-loading');
  try {
    await authFetch('/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({
        current_password: currentPasswordInput.value,
        new_password: newPasswordInput.value,
      }),
    });
    changePasswordForm.reset();
    passwordSuccessEl.textContent = 'Пароль успешно изменён';
    passwordSuccessEl.hidden = false;
  } catch (error) {
    passwordErrorEl.textContent = error.message;
    passwordErrorEl.hidden = false;
  } finally {
    changePasswordBtn.disabled = false;
    changePasswordBtn.classList.remove('is-loading');
  }
});

async function init() {
  if (!requireAuthRedirect()) return;
  try {
    const me = await authFetch('/auth/me');
    const user = me.data;
    document.getElementById('profile-name').textContent = `Здравствуйте, ${greetingName(user)}!`;
    document.getElementById('profile-email').textContent = user.email;
    document.getElementById('profile-role').textContent = ROLE_LABELS[user.role] || user.role;

    if (MANAGER_ROLES.has(user.role)) {
      const manageUsersBtn = document.getElementById('manage-users');
      manageUsersBtn.hidden = false;
      manageUsersBtn.textContent = ADMIN_ROLES.has(user.role)
        ? 'Редактирование пользователей'
        : 'Смотреть пользователей';
    }
  } catch {
    if (!getToken()) {
      logout();
    }
  }
}

init();
