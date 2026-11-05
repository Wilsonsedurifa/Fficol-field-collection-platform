/**
 * FiCol Simple Login Presentation Logic
 */

document.addEventListener('DOMContentLoaded', () => {
  const toggleBtn = document.getElementById('password-toggle');
  const passwordInput = document.getElementById('id_password');
  const toggleText = document.getElementById('password-toggle-text');
  const form = document.querySelector('.login-form');
  const submitBtn = document.getElementById('submit-btn');

  // Accessible Password Show/Hide Toggle
  if (toggleBtn && passwordInput) {
    toggleBtn.addEventListener('click', () => {
      const isShowing = passwordInput.getAttribute('type') === 'text';

      if (isShowing) {
        passwordInput.setAttribute('type', 'password');
        toggleBtn.setAttribute('aria-label', 'Show password');
        toggleBtn.setAttribute('aria-pressed', 'false');
        if (toggleText) toggleText.textContent = 'Show';
      } else {
        passwordInput.setAttribute('type', 'text');
        toggleBtn.setAttribute('aria-label', 'Hide password');
        toggleBtn.setAttribute('aria-pressed', 'true');
        if (toggleText) toggleText.textContent = 'Hide';
      }

      passwordInput.focus();
    });
  }

  // Provide immediate visual feedback when submitting valid credentials
  if (form && submitBtn) {
    form.addEventListener('submit', () => {
      if (form.checkValidity && form.checkValidity()) {
        submitBtn.disabled = true;
        submitBtn.textContent = 'Signing In...';
        submitBtn.style.opacity = '0.85';
        submitBtn.style.cursor = 'wait';
      }
    });
  }
});