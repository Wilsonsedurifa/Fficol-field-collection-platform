(function () {
  'use strict';

  if (!('serviceWorker' in navigator)) {
    return;
  }

  function isUserEditingForm() {
    var activeEl = document.activeElement;
    if (activeEl && (activeEl.tagName === 'INPUT' || activeEl.tagName === 'TEXTAREA' || activeEl.tagName === 'SELECT')) {
      return true;
    }
    var inputs = document.querySelectorAll('form input:not([type="hidden"]):not([type="submit"]):not([type="button"]), form textarea');
    for (var i = 0; i < inputs.length; i++) {
      if (inputs[i].value && inputs[i].value.trim() !== '') {
        return true;
      }
    }
    return false;
  }

  window.addEventListener('load', function () {
    var userRequestedReload = false;
    var refreshing = false;

    navigator.serviceWorker
      .register('/service-worker.js', {
        scope: '/',
        updateViaCache: 'none',
      })
      .then(function (registration) {
        var updateNotice = document.getElementById('pwa-update-notice');
        var reloadButton = document.getElementById('pwa-reload-btn');

        function setupUpdatePrompt(worker) {
          if (!updateNotice) return;
          updateNotice.removeAttribute('hidden');
          updateNotice.classList.add('is-visible');

          if (reloadButton) {
            reloadButton.onclick = function (e) {
              e.preventDefault();
              userRequestedReload = true;
              if (worker) {
                worker.postMessage({ type: 'SKIP_WAITING' });
              }
            };
          }
        }

        if (registration.waiting) {
          setupUpdatePrompt(registration.waiting);
        }

        registration.addEventListener('updatefound', function () {
          var newWorker = registration.installing;
          if (!newWorker) return;

          newWorker.addEventListener('statechange', function () {
            if (newWorker.state === 'installed' && navigator.serviceWorker.controller) {
              setupUpdatePrompt(newWorker);
            }
          });
        });
      })
      .catch(function (error) {
        console.warn('PWA service worker registration failed:', error);
      });

    navigator.serviceWorker.addEventListener('controllerchange', function () {
      if (refreshing) return;
      if (!userRequestedReload && isUserEditingForm()) {
        return;
      }
      refreshing = true;
      window.location.reload();
    });
  });
})();
