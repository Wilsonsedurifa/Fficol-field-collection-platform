/**
 * My Accounts Filter and Offline State Enhancer
 */
(() => {
  'use strict';

  function initAccountsFilter() {
    const filterButtons = document.querySelectorAll('.account-filter-bar .filter-pill');
    const cards = document.querySelectorAll('.account-grid .account-card');
    const syncBtn = document.getElementById('btn-manual-sync');

    if (syncBtn) {
      syncBtn.addEventListener('click', async () => {
        syncBtn.disabled = true;
        syncBtn.textContent = 'Downloading accounts...';
        if (window.FiColSync) {
          await window.FiColSync.refreshMorningPack(true);
        }
        syncBtn.textContent = '✅ Naka-save na sa Phone!';
        setTimeout(() => {
          syncBtn.disabled = false;
          syncBtn.textContent = '⚡ I-download para sa Offline Work';
        }, 3000);
      });
    }

    // Enhance cards with local offline status from IndexedDB
    if (window.FiColDB) {
      window.FiColDB.getAllAccounts().then((accounts) => {
        const accMap = new Map();
        accounts.forEach((acc) => accMap.set(Number(acc.assignment_id), acc));

        cards.forEach((card) => {
          const link = card.querySelector('a.account-button[href*="/accounts/"]');
          if (!link) return;

          const match = link.href.match(/\/accounts\/(\d+)\//);
          if (!match) return;

          const assignmentId = Number(match[1]);
          const localAcc = accMap.get(assignmentId);

          if (localAcc) {
            const heading = card.querySelector('.account-card-heading');
            if (localAcc.is_offline_pending) {
              card.dataset.status = 'completed';
              const badge = document.createElement('span');
              badge.className = 'account-status';
              badge.style.cssText = 'background: #ffedd5; color: #9a3412; font-weight: 700; margin-left: 6px;';
              badge.textContent = '⚡ Offline Visited';
              heading?.appendChild(badge);
            } else if (localAcc.local_status && localAcc.local_status !== 'pending') {
              card.dataset.status = 'completed';
              const badge = document.createElement('span');
              badge.className = 'account-status';
              badge.style.cssText = 'background: #dcfce7; color: #166534; font-weight: 700; margin-left: 6px;';
              badge.textContent = '✅ Visited';
              heading?.appendChild(badge);
            } else {
              card.dataset.status = 'pending';
            }
          } else {
            card.dataset.status = 'pending';
          }
        });
      });
    }

    // Filter pill click handling
    filterButtons.forEach((btn) => {
      btn.addEventListener('click', () => {
        filterButtons.forEach((b) => {
          b.classList.remove('active');
          b.style.background = '#ffffff';
          b.style.color = '#1e293b';
        });
        btn.classList.add('active');
        btn.style.background = '#1e293b';
        btn.style.color = '#ffffff';

        const filter = btn.dataset.filter || 'all';

        cards.forEach((card) => {
          const cardStatus = card.dataset.status || 'pending';
          if (filter === 'all') {
            card.style.display = '';
          } else if (filter === 'pending') {
            card.style.display = cardStatus === 'pending' ? '' : 'none';
          } else if (filter === 'completed') {
            card.style.display = cardStatus === 'completed' ? '' : 'none';
          }
        });
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAccountsFilter);
  } else {
    initAccountsFilter();
  }
})();
