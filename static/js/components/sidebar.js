/**
 * FiCol Sidebar Component Script
 * Manages responsive sidebar drawer, focus transitions on resize, keyboard navigation,
 * and accessibility. Synchronized with CSS at 900px breakpoint.
 */
(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    const sidebar = document.getElementById('app-sidebar');
    // Safely exit if page does not include sidebar
    if (!sidebar) {
      return;
    }

    const triggerBtn = document.getElementById('mobile-menu-trigger');
    const closeBtn = document.getElementById('sidebar-close-btn');
    const backdrop = document.getElementById('mobile-sidebar-backdrop');
    const breakpoint = window.matchMedia('(min-width: 900px)');

    let isDrawerOpen = false;

    // Focusable selector for keyboard focus trap inside the mobile drawer
    const focusableSelector = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

    function getFocusableElements() {
      return Array.from(sidebar.querySelectorAll(focusableSelector));
    }

    function openDrawer() {
      if (breakpoint.matches) return; // Never open as modal drawer on desktop
      isDrawerOpen = true;

      sidebar.classList.add('open');
      sidebar.setAttribute('aria-hidden', 'false');

      if (backdrop) {
        backdrop.classList.add('active');
        backdrop.setAttribute('aria-hidden', 'false');
      }

      if (triggerBtn) {
        triggerBtn.setAttribute('aria-expanded', 'true');
      }

      document.body.classList.add('drawer-open');

      // Move focus into the drawer (close button or first focusable element)
      requestAnimationFrame(function () {
        if (closeBtn && !closeBtn.disabled) {
          closeBtn.focus();
        } else {
          const focusables = getFocusableElements();
          if (focusables.length > 0) {
            focusables[0].focus();
          }
        }
      });
    }

    function closeDrawer(restoreFocus) {
      if (!isDrawerOpen && !sidebar.classList.contains('open')) {
        handleResize();
        return;
      }

      isDrawerOpen = false;
      sidebar.classList.remove('open');

      if (breakpoint.matches) {
        sidebar.setAttribute('aria-hidden', 'false');
      } else {
        sidebar.setAttribute('aria-hidden', 'true');
      }

      if (backdrop) {
        backdrop.classList.remove('active');
        backdrop.setAttribute('aria-hidden', 'true');
      }

      if (triggerBtn) {
        triggerBtn.setAttribute('aria-expanded', 'false');
      }

      document.body.classList.remove('drawer-open');

      // Return focus to menu trigger button
      if (restoreFocus !== false && triggerBtn) {
        triggerBtn.focus();
      }
    }

    // Keyboard focus trap inside mobile drawer
    sidebar.addEventListener('keydown', function (e) {
      if (!isDrawerOpen || breakpoint.matches) return;

      if (e.key === 'Tab') {
        const focusables = getFocusableElements();
        if (focusables.length === 0) {
          e.preventDefault();
          return;
        }

        const first = focusables[0];
        const last = focusables[focusables.length - 1];

        if (e.shiftKey) {
          if (document.activeElement === first) {
            e.preventDefault();
            last.focus();
          }
        } else {
          if (document.activeElement === last) {
            e.preventDefault();
            first.focus();
          }
        }
      }
    });

    // Close on Escape key
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && isDrawerOpen && !breakpoint.matches) {
        e.preventDefault();
        closeDrawer(true);
      }
    });

    // Trigger button click listener
    if (triggerBtn) {
      triggerBtn.addEventListener('click', function (e) {
        e.preventDefault();
        if (isDrawerOpen) {
          closeDrawer(true);
        } else {
          openDrawer();
        }
      });
    }

    // Close button click listener
    if (closeBtn) {
      closeBtn.addEventListener('click', function (e) {
        e.preventDefault();
        closeDrawer(true);
      });
    }

    // Backdrop click listener
    if (backdrop) {
      backdrop.addEventListener('click', function (e) {
        e.preventDefault();
        closeDrawer(true);
      });
    }

    // Handle responsive state & focus transitions on viewport resizing
    function handleResize() {
      if (breakpoint.matches) {
        // Desktop view (>= 900px):
        // 1. If focus was on the disappearing close button or mobile menu trigger, move focus to a visible navigation link
        const isDisappearingElementFocused = document.activeElement && (
          document.activeElement === closeBtn ||
          (closeBtn && closeBtn.contains(document.activeElement)) ||
          document.activeElement === triggerBtn ||
          (triggerBtn && triggerBtn.contains(document.activeElement))
        );

        if (isDisappearingElementFocused) {
          const targetNav = sidebar.querySelector('a.nav-item.active') || sidebar.querySelector('a.nav-item');
          if (targetNav) {
            targetNav.focus();
          }
        }

        // 2. Clear mobile scroll locks, backdrop state, and focus trapping
        isDrawerOpen = false;
        sidebar.classList.remove('open');
        sidebar.setAttribute('aria-hidden', 'false');

        if (backdrop) {
          backdrop.classList.remove('active');
          backdrop.setAttribute('aria-hidden', 'true');
        }

        if (triggerBtn) {
          triggerBtn.setAttribute('aria-expanded', 'false');
        }

        document.body.classList.remove('drawer-open');
      } else {
        // Mobile view (< 900px):
        // If drawer is closed, sidebar is off-canvas (visibility: hidden).
        // If focus would otherwise remain inside the newly hidden sidebar, move focus to the visible menu button!
        if (!isDrawerOpen) {
          sidebar.setAttribute('aria-hidden', 'true');

          if (backdrop) {
            backdrop.setAttribute('aria-hidden', 'true');
          }

          if (document.activeElement && sidebar.contains(document.activeElement)) {
            if (triggerBtn) {
              triggerBtn.focus();
            }
          }
        }
      }
    }

    // Listen for viewport resize crossing the 900px breakpoint
    if (breakpoint.addEventListener) {
      breakpoint.addEventListener('change', handleResize);
    } else if (breakpoint.addListener) {
      breakpoint.addListener(handleResize);
    }

    // Initialize state on page load
    handleResize();
  });
})();
