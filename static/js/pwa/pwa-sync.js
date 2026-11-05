/**
 * FiCol Auto-Sync Engine (Two-Tier Offline Replay & Morning Auto-Cache)
 * Automatically syncs queued visits upon network reconnection and keeps
 * assigned accounts cached in IndexedDB.
 */
(() => {
  'use strict';

  function getCookie(name) {
    let cookieValue = null;
    if (document.cookie && document.cookie !== '') {
      const cookies = document.cookie.split(';');
      for (let i = 0; i < cookies.length; i++) {
        const cookie = cookies[i].trim();
        if (cookie.substring(0, name.length + 1) === name + '=') {
          cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
          break;
        }
      }
    }
    return cookieValue;
  }

  function showSyncToast(message, isError = false) {
    let toast = document.getElementById('ficol-sync-toast');
    if (!toast) {
      toast = document.createElement('div');
      toast.id = 'ficol-sync-toast';
      toast.style.cssText = `
        position: fixed;
        bottom: 24px;
        left: 50%;
        transform: translateX(-50%);
        background: #1e293b;
        color: #ffffff;
        padding: 10px 18px;
        border-radius: 9999px;
        font-size: 13px;
        font-weight: 600;
        box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.2);
        z-index: 9999;
        display: flex;
        align-items: center;
        gap: 8px;
        transition: opacity 0.3s ease, transform 0.3s ease;
        pointer-events: none;
      `;
      document.body.appendChild(toast);
    }

    toast.style.background = isError ? '#991b1b' : '#0f172a';
    toast.textContent = message;
    toast.style.opacity = '1';

    setTimeout(() => {
      toast.style.opacity = '0';
    }, 4000);
  }

  const FiColSync = {
    isSyncing: false,

    /**
     * Download and cache assigned accounts for offline use in the morning.
     */
    async refreshMorningPack(force = false) {
      if (!navigator.onLine || !window.FiColDB) return;

      const lastSync = await window.FiColDB.getMeta('last_account_sync');
      const now = Date.now();
      // Auto-cache if never synced or if older than 30 minutes
      if (!force && lastSync && now - new Date(lastSync).getTime() < 30 * 60 * 1000) {
        return;
      }

      try {
        const res = await fetch('/collector/accounts/offline-pack/', {
          headers: { 'Accept': 'application/json' },
        });
        if (res.ok) {
          const data = await res.json();
          if (data && data.accounts) {
            await window.FiColDB.saveAccounts(data.accounts);
            console.log(`[FiCol Offline] Cached ${data.accounts.length} assigned accounts for offline use.`);
          }
        }
      } catch (err) {
        console.warn('[FiCol Offline] Morning pack refresh skipped (offline or network error).');
      }
    },

    /**
     * Replay queued offline visits to the Django backend.
     */
    async replayOutbox() {
      if (this.isSyncing || !navigator.onLine || !window.FiColDB) return;

      const pending = await window.FiColDB.getPendingVisits();
      if (pending.length === 0) return;

      this.isSyncing = true;
      this.updateSyncBadge(pending.length, true);

      let successCount = 0;
      const csrftoken = getCookie('csrftoken') || '';

      for (const item of pending) {
        try {
          await window.FiColDB.markVisitSyncing(item.local_id);

          const formData = new FormData();
          formData.append('event_id', item.event_id);
          formData.append('assignment_id', String(item.assignment_id));
          formData.append('outcome', item.outcome);
          formData.append('notes', item.notes || '');
          formData.append('occurred_at', item.occurred_at || new Date().toISOString());
          formData.append('ended_at', item.ended_at || new Date().toISOString());
          formData.append('location_issue_reason', item.location_issue_reason || '');

          if (item.latitude != null && item.longitude != null) {
            formData.append('latitude', String(item.latitude));
            formData.append('longitude', String(item.longitude));
            formData.append('accuracy_m', String(item.accuracy_m || '10'));
          }

          if (item.promise) {
            formData.append('promise_amount', item.promise.amount);
            formData.append('promise_date', item.promise.due_date);
          }

          if (item.follow_up) {
            formData.append('followup_date', item.follow_up.due_at);
          }

          if (item.photoBlob) {
            formData.append('evidence_photo', item.photoBlob, item.photoName || 'evidence.jpg');
          }

          const res = await fetch('/collector/visits/sync/', {
            method: 'POST',
            body: formData,
            headers: {
              'X-CSRFToken': csrftoken,
            },
          });

          if (res.ok || res.status === 409) {
            await window.FiColDB.markVisitSynced(item.local_id, item.assignment_id);
            successCount++;

            // If map is currently loaded on this page, update its visual pin
            if (window.currentFiColMap) {
              window.currentFiColMap.updateAssignedStatus?.(item.outcome, false);
              window.currentFiColMap.updateStatus?.(item.assignment_id, item.outcome, false);
            }
          } else {
            const errData = await res.json().catch(() => ({}));
            await window.FiColDB.markVisitFailed(item.local_id, errData?.error?.message || 'Server error');
          }
        } catch (err) {
          console.error('[FiCol Sync] Error replaying visit:', err);
          await window.FiColDB.markVisitFailed(item.local_id, err.message);
        }
      }

      this.isSyncing = false;
      const remaining = await window.FiColDB.getOutboxCount();
      this.updateSyncBadge(remaining, false);

      if (successCount > 0) {
        showSyncToast(`✅ ${successCount} offline visit${successCount > 1 ? 's' : ''} uploaded to server!`);
      }
    },

    /**
     * UI Status Badge indicating network state and pending outbox count.
     */
    updateSyncBadge(count, isSyncing = false) {
      let badge = document.getElementById('ficol-sync-indicator');
      if (!badge) {
        badge = document.createElement('div');
        badge.id = 'ficol-sync-indicator';
        badge.style.cssText = `
          position: fixed;
          top: 12px;
          right: 12px;
          z-index: 8000;
          font-size: 11px;
          font-weight: 700;
          padding: 4px 10px;
          border-radius: 9999px;
          box-shadow: 0 2px 4px rgba(0,0,0,0.1);
          transition: all 0.2s ease;
          display: flex;
          align-items: center;
          gap: 6px;
        `;
        document.body.appendChild(badge);
      }

      const isOnline = navigator.onLine;

      if (!isOnline) {
        badge.style.background = '#ea580c';
        badge.style.color = '#ffffff';
        badge.innerHTML = `<span>⚡ Offline Mode</span>${count > 0 ? `<span style="background:rgba(255,255,255,0.25); border-radius:10px; padding:1px 6px;">${count} unsynced</span>` : ''}`;
        badge.style.display = 'flex';
      } else if (isSyncing) {
        badge.style.background = '#2563eb';
        badge.style.color = '#ffffff';
        badge.innerHTML = `<span>🔄 Syncing (${count})...</span>`;
        badge.style.display = 'flex';
      } else if (count > 0) {
        badge.style.background = '#f59e0b';
        badge.style.color = '#78350f';
        badge.innerHTML = `<span>⏳ ${count} pending sync</span>`;
        badge.style.display = 'flex';
      } else {
        // Online and everything is synced
        badge.style.background = '#ecfdf5';
        badge.style.color = '#065f46';
        badge.innerHTML = `<span>🟢 Online</span>`;
        badge.style.display = 'none'; // Keep clean when online and empty
      }
    },

    init() {
      // Refresh morning cache if online
      this.refreshMorningPack();

      // Check outbox immediately
      if (window.FiColDB) {
        window.FiColDB.getOutboxCount().then((count) => {
          this.updateSyncBadge(count, false);
          if (navigator.onLine && count > 0) {
            this.replayOutbox();
          }
        });
      }

      // Listen for network state changes
      window.addEventListener('online', () => {
        showSyncToast('📶 Connection restored. Syncing offline visits...');
        this.updateSyncBadge(0, true);
        this.refreshMorningPack(true);
        this.replayOutbox();
      });

      window.addEventListener('offline', () => {
        if (window.FiColDB) {
          window.FiColDB.getOutboxCount().then((count) => this.updateSyncBadge(count, false));
        }
      });

      // Regular heartbeat check every 45 seconds
      setInterval(() => {
        if (navigator.onLine) {
          this.replayOutbox();
        }
      }, 45000);

      // Listen for Background Sync trigger from Service Worker
      if ('serviceWorker' in navigator) {
        navigator.serviceWorker.addEventListener('message', (event) => {
          if (event.data && event.data.type === 'TRIGGER_REPLAY') {
            this.replayOutbox();
          }
        });
      }
    },
  };

  window.FiColSync = FiColSync;

  // Auto-start sync engine once DOM is loaded
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => FiColSync.init());
  } else {
    FiColSync.init();
  }
})();
