import './auth.css';
import { authFetch } from './auth.js';

const params = new URLSearchParams(window.location.search);
const token = params.get('token');
const form = document.getElementById('auth-form');
const errorEl = document.getElementById('error');
const successEl = document.getElementById('success');
const submitBtn = document.getElementById('submit');

if (!token) {
  errorEl.textContent = 'Missing reset token in URL';
  errorEl.hidden = false;
  submitBtn.disabled = true;
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  errorEl.hidden = true;
  successEl.hidden = true;
  submitBtn.disabled = true;
  submitBtn.classList.add('is-loading');
  try {
    const password = document.getElementById('password').value;
    const payload = await authFetch('/auth/reset-password', {
      method: 'POST',
      body: JSON.stringify({ token, password }),
    });
    successEl.textContent = `${payload.message} Redirecting to login...`;
    successEl.hidden = false;
    setTimeout(() => {
      window.location.href = '/login.html';
    }, 1500);
  } catch (error) {
    errorEl.textContent = error.message;
    errorEl.hidden = false;
  } finally {
    submitBtn.disabled = false;
    submitBtn.classList.remove('is-loading');
  }
});
