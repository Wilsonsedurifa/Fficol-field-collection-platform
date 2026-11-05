(() => {
  const dialog = document.getElementById('signout-confirmation');
  if (!dialog || typeof dialog.showModal !== 'function') return;
  const logoutUrl = new URL(dialog.dataset.logoutUrl, window.location.href);
  const confirmButton = dialog.querySelector('[data-signout-confirm]');
  let pendingForm = null;
  let trigger = null;

  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== 'post') return;
    const target = new URL(form.action, window.location.href);
    if (target.origin !== logoutUrl.origin || target.pathname !== logoutUrl.pathname) return;
    event.preventDefault();
    pendingForm = form;
    trigger = event.submitter || document.activeElement;
    if (!dialog.open) dialog.showModal();
  }, true);

  dialog.querySelector('[data-signout-cancel]').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', (event) => {
    const bounds = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom)) dialog.close();
  });
  dialog.addEventListener('close', () => {
    pendingForm = null;
    if (trigger && trigger.isConnected) trigger.focus();
    trigger = null;
  });
  confirmButton.addEventListener('click', () => {
    if (!pendingForm || confirmButton.disabled) return;
    confirmButton.disabled = true;
    // Keep the original POST action and CSRF token; bypass only this confirmation listener.
    HTMLFormElement.prototype.submit.call(pendingForm);
  });
})();
