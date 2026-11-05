document.querySelectorAll('[data-permission-section]').forEach((section) => {
  const counter = section.querySelector('[data-permission-count]');
  const updateCount = () => {
    counter.textContent = `${section.querySelectorAll('input[name="modules"]:checked').length} selected`;
  };
  section.querySelectorAll('[data-group-controls]').forEach((controls) => {
    controls.hidden = false;
  });
  section.addEventListener('change', updateCount);
  section.addEventListener('click', (event) => {
    const button = event.target.closest('[data-permission-select]');
    if (!button) return;
    button.closest('[data-permission-group]').querySelectorAll('input[name="modules"]').forEach((input) => {
      input.checked = button.dataset.permissionSelect === 'all';
    });
    updateCount();
  });
  updateCount();
});

document.querySelectorAll('[data-client-section]').forEach((section) => {
  const input = section.querySelector('[data-client-search]');
  if (!input) return;
  section.querySelector('[data-client-search-wrapper]').hidden = false;
  const cards = Array.from(section.querySelectorAll('[data-client-card]'));
  input.addEventListener('input', () => {
    const query = input.value.trim().toLocaleLowerCase();
    cards.forEach((card) => {
      card.hidden = !card.textContent.toLocaleLowerCase().includes(query);
    });
    section.querySelector('[data-client-empty]').hidden = cards.some((card) => !card.hidden);
  });
});
