(function() {
  // Accessible Dropdown Management
  let activeTrigger = null;
  let activeMenu = null;

  function closeAllMenus() {
    document.querySelectorAll('.sa-dropdown-menu').forEach(menu => {
      menu.hidden = true;
      menu.style.position = '';
      menu.style.top = '';
      menu.style.right = '';
      menu.style.left = '';
    });
    document.querySelectorAll('.sa-dropdown-trigger').forEach(btn => {
      btn.setAttribute('aria-expanded', 'false');
    });
    activeTrigger = null;
    activeMenu = null;
  }

  function positionMenu(trigger, menu) {
    const rect = trigger.getBoundingClientRect();
    menu.hidden = false;
    menu.style.position = 'fixed';
    menu.style.zIndex = '9999';

    const menuWidth = menu.offsetWidth || 220;
    const menuHeight = menu.offsetHeight || 180;

    // Align right edge with trigger right edge
    let right = window.innerWidth - rect.right;
    if (right < 8) right = 8;
    if (rect.right - menuWidth < 8) {
      menu.style.left = '8px';
      menu.style.right = 'auto';
    } else {
      menu.style.right = right + 'px';
      menu.style.left = 'auto';
    }

    // Check bottom space
    if (rect.bottom + menuHeight > window.innerHeight && rect.top - menuHeight > 8) {
      // Open upwards
      menu.style.top = (rect.top - menuHeight - 4) + 'px';
    } else {
      // Open downwards
      menu.style.top = (rect.bottom + 4) + 'px';
    }
  }

  function openMenu(trigger, menu) {
    closeAllMenus();
    activeTrigger = trigger;
    activeMenu = menu;
    trigger.setAttribute('aria-expanded', 'true');
    positionMenu(trigger, menu);

    // Focus first focusable element inside menu
    const firstItem = menu.querySelector('a, button, [tabindex="0"]');
    if (firstItem) {
      firstItem.focus();
    }
  }

  document.querySelectorAll('.sa-dropdown-trigger').forEach(trigger => {
    const dropdown = trigger.closest('.sa-dropdown');
    const menu = dropdown ? dropdown.querySelector('.sa-dropdown-menu') : null;
    if (!menu) return;

    trigger.addEventListener('click', function(e) {
      e.stopPropagation();
      const isExpanded = trigger.getAttribute('aria-expanded') === 'true';
      if (isExpanded) {
        closeAllMenus();
      } else {
        openMenu(trigger, menu);
      }
    });

    trigger.addEventListener('keydown', function(e) {
      if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        openMenu(trigger, menu);
      } else if (e.key === 'Escape') {
        closeAllMenus();
      }
    });
  });

  // Keyboard navigation inside dropdown menu
  document.querySelectorAll('.sa-dropdown-menu').forEach(menu => {
    menu.addEventListener('keydown', function(e) {
      const items = Array.from(menu.querySelectorAll('a, button:not([disabled])'));
      const currentIndex = items.indexOf(document.activeElement);

      if (e.key === 'Escape') {
        e.preventDefault();
        const trigger = activeTrigger;
        closeAllMenus();
        if (trigger) trigger.focus();
      } else if (e.key === 'ArrowDown') {
        e.preventDefault();
        const nextIndex = (currentIndex + 1) % items.length;
        items[nextIndex].focus();
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        const prevIndex = (currentIndex - 1 + items.length) % items.length;
        items[prevIndex].focus();
      } else if (e.key === 'Home') {
        e.preventDefault();
        if (items.length) items[0].focus();
      } else if (e.key === 'End') {
        e.preventDefault();
        if (items.length) items[items.length - 1].focus();
      } else if (e.key === 'Tab') {
        closeAllMenus();
      }
    });
  });

  // Close menus on click outside or window resize / scroll
  document.addEventListener('click', function(e) {
    if (!e.target.closest('.sa-dropdown')) {
      closeAllMenus();
    }
  });

  window.addEventListener('resize', closeAllMenus);
  window.addEventListener('scroll', closeAllMenus, true);

})();
