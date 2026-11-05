/**
 * FiCol Top Navigation Bar Component Script
 * Handles profile disclosure dropdown, keyboard accessibility,
 * outside-click dismissal, and responsive resize focus transitions.
 */
(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    const trigger = document.getElementById('profile-menu-trigger');
    const dropdown = document.getElementById('profile-dropdown-menu');
    const mobileMenuTrigger = document.getElementById('mobile-menu-trigger');
    const topNavbar = document.querySelector('.top-navbar');
    const breakpoint = window.matchMedia('(min-width: 900px)');

    // Exit safely if navbar elements are not present on the page
    if (!trigger || !dropdown) {
      return;
    }

    let isDropdownOpen = false;

    // Ensure initial accessible disclosure state
    trigger.setAttribute('aria-expanded', 'false');
    dropdown.setAttribute('hidden', '');

    function openDropdown() {
      if (!breakpoint.matches) return; // Dropdown is desktop-only
      isDropdownOpen = true;
      trigger.setAttribute('aria-expanded', 'true');
      dropdown.removeAttribute('hidden');
    }

    function closeDropdown(restoreFocus) {
      if (!isDropdownOpen && dropdown.hasAttribute('hidden')) {
        return;
      }
      isDropdownOpen = false;
      trigger.setAttribute('aria-expanded', 'false');
      dropdown.setAttribute('hidden', '');

      if (restoreFocus === true && breakpoint.matches) {
        trigger.focus();
      }
    }

    // Toggle dropdown on trigger click
    trigger.addEventListener('click', function (e) {
      e.preventDefault();
      if (isDropdownOpen) {
        closeDropdown(true);
      } else {
        openDropdown();
      }
    });

    // Close on Escape key and restore focus to trigger
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && isDropdownOpen) {
        e.preventDefault();
        closeDropdown(true);
      }
    });

    // Outside-click dismissal without stealing focus from clicked element
    document.addEventListener('click', function (e) {
      if (!isDropdownOpen) return;
      const isInside = trigger.contains(e.target) || dropdown.contains(e.target);
      if (!isInside) {
        closeDropdown(false);
      }
    });

    // Coordinate with mobile drawer opening:
    // If mobile menu trigger is activated, close dropdown without overriding drawer focus
    if (mobileMenuTrigger) {
      mobileMenuTrigger.addEventListener('click', function () {
        closeDropdown(false);
      });
    }

    // Handle responsive resize:
    function handleResize() {
      if (!breakpoint.matches) {
        // Resizing to mobile (< 900px):
        // If focus is currently inside the disappearing desktop navbar,
        // move focus to the visible mobile menu trigger button.
        if (topNavbar && document.activeElement && topNavbar.contains(document.activeElement)) {
          if (mobileMenuTrigger) {
            mobileMenuTrigger.focus();
          }
        }
        closeDropdown(false);
      }
    }

    if (breakpoint.addEventListener) {
      breakpoint.addEventListener('change', handleResize);
    } else if (breakpoint.addListener) {
      breakpoint.addListener(handleResize);
    }
  });
})();
