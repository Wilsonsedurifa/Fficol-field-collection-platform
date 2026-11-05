/**
 * FiCol Offline Storage Manager (IndexedDB)
 * Manages daily assigned accounts cache, local visit outbox queue, and offline sync metadata.
 */
(() => {
  'use strict';

  const DB_NAME = 'FiColOfflineDB';
  const DB_VERSION = 1;

  function openDB() {
    return new Promise((resolve, reject) => {
      if (!window.indexedDB) {
        return reject(new Error('IndexedDB is not supported on this device.'));
      }
      const request = indexedDB.open(DB_NAME, DB_VERSION);

      request.onerror = () => reject(request.error);
      request.onsuccess = () => resolve(request.result);

      request.onupgradeneeded = (event) => {
        const db = event.target.result;

        // 1. Store for daily assigned accounts
        if (!db.objectStoreNames.contains('assigned_accounts')) {
          const store = db.createObjectStore('assigned_accounts', { keyPath: 'assignment_id' });
          store.createIndex('reference', 'reference', { unique: false });
          store.createIndex('local_status', 'local_status', { unique: false });
        }

        // 2. Store for queued offline visits (Outbox)
        if (!db.objectStoreNames.contains('visit_outbox')) {
          const outbox = db.createObjectStore('visit_outbox', { keyPath: 'local_id', autoIncrement: true });
          outbox.createIndex('event_id', 'event_id', { unique: true });
          outbox.createIndex('assignment_id', 'assignment_id', { unique: false });
          outbox.createIndex('status', 'status', { unique: false });
        }

        // 3. Store for synchronization metadata
        if (!db.objectStoreNames.contains('sync_meta')) {
          db.createObjectStore('sync_meta', { keyPath: 'key' });
        }
      };
    });
  }

  const FiColDB = {
    /**
     * Save/refresh assigned accounts from the morning bootstrap pack.
     * Preserves locally updated visit status if not yet synced on the server.
     */
    async saveAccounts(accounts) {
      const db = await openDB();
      const existing = await this.getAllAccounts();
      const localStatusMap = new Map();
      existing.forEach((acc) => {
        if (acc.is_offline_pending) {
          localStatusMap.set(acc.assignment_id, {
            local_status: acc.local_status,
            latest_visit: acc.latest_visit,
            is_offline_pending: true,
          });
        }
      });

      return new Promise((resolve, reject) => {
        const tx = db.transaction(['assigned_accounts', 'sync_meta'], 'readwrite');
        const store = tx.objectStore('assigned_accounts');
        store.clear();

        accounts.forEach((acc) => {
          // Preserve local pending state if present
          if (localStatusMap.has(acc.assignment_id)) {
            const preserved = localStatusMap.get(acc.assignment_id);
            acc.local_status = preserved.local_status;
            acc.latest_visit = preserved.latest_visit;
            acc.is_offline_pending = true;
          }
          store.put(acc);
        });

        tx.objectStore('sync_meta').put({
          key: 'last_account_sync',
          value: new Date().toISOString(),
          total_accounts: accounts.length,
        });

        tx.oncomplete = () => resolve(true);
        tx.onerror = () => reject(tx.error);
      });
    },

    /**
     * Retrieve a single assigned account by its assignment ID.
     */
    async getAccount(assignmentId) {
      const db = await openDB();
      return new Promise((resolve) => {
        const tx = db.transaction('assigned_accounts', 'readonly');
        const req = tx.objectStore('assigned_accounts').get(Number(assignmentId));
        req.onsuccess = () => resolve(req.result || null);
        req.onerror = () => resolve(null);
      });
    },

    /**
     * Retrieve all cached accounts.
     */
    async getAllAccounts() {
      const db = await openDB();
      return new Promise((resolve) => {
        const tx = db.transaction('assigned_accounts', 'readonly');
        const req = tx.objectStore('assigned_accounts').getAll();
        req.onsuccess = () => resolve(req.result || []);
        req.onerror = () => resolve([]);
      });
    },

    /**
     * Update the local status of an account (e.g., mark as 'contacted' or 'unavailable').
     */
    async updateAccountStatus(assignmentId, updateData) {
      const db = await openDB();
      return new Promise((resolve, reject) => {
        const tx = db.transaction('assigned_accounts', 'readwrite');
        const store = tx.objectStore('assigned_accounts');
        const req = store.get(Number(assignmentId));

        req.onsuccess = () => {
          const account = req.result;
          if (account) {
            account.local_status = updateData.status || account.local_status;
            account.is_offline_pending = Boolean(updateData.is_offline_pending);
            account.latest_visit = {
              outcome: updateData.status,
              occurred_at: updateData.occurred_at || new Date().toISOString(),
              notes: updateData.notes || '',
              status: 'submitted',
            };
            store.put(account);
          }
          resolve(account);
        };
        req.onerror = () => reject(req.error);
      });
    },

    /**
     * Add a visit submission to the outbox queue.
     */
    async queueVisit(visitPayload) {
      const db = await openDB();
      return new Promise((resolve, reject) => {
        const tx = db.transaction(['visit_outbox', 'assigned_accounts'], 'readwrite');
        const outbox = tx.objectStore('visit_outbox');
        const record = {
          ...visitPayload,
          status: 'pending',
          created_at: new Date().toISOString(),
          retry_count: 0,
          error_message: null,
        };

        const addReq = outbox.add(record);
        addReq.onsuccess = (e) => {
          const localId = e.target.result;
          // Also mark account in local store as pending sync
          const accStore = tx.objectStore('assigned_accounts');
          const accReq = accStore.get(Number(visitPayload.assignment_id));
          accReq.onsuccess = () => {
            const acc = accReq.result;
            if (acc) {
              acc.local_status = visitPayload.outcome;
              acc.is_offline_pending = true;
              acc.latest_visit = {
                outcome: visitPayload.outcome,
                occurred_at: visitPayload.occurred_at || new Date().toISOString(),
                notes: visitPayload.notes || '',
                status: 'submitted',
              };
              accStore.put(acc);
            }
          };
          resolve(localId);
        };
        addReq.onerror = () => reject(addReq.error);
      });
    },

    /**
     * Retrieve all pending or failed visits in the outbox.
     */
    async getPendingVisits() {
      const db = await openDB();
      return new Promise((resolve) => {
        const tx = db.transaction('visit_outbox', 'readonly');
        const store = tx.objectStore('visit_outbox');
        const req = store.getAll();
        req.onsuccess = () => {
          const list = req.result || [];
          resolve(list.filter((item) => item.status === 'pending' || item.status === 'failed'));
        };
        req.onerror = () => resolve([]);
      });
    },

    /**
     * Retrieve total count of pending visits waiting in outbox.
     */
    async getOutboxCount() {
      const pending = await this.getPendingVisits();
      return pending.length;
    },

    /**
     * Mark a visit in outbox as syncing.
     */
    async markVisitSyncing(localId) {
      const db = await openDB();
      const tx = db.transaction('visit_outbox', 'readwrite');
      const store = tx.objectStore('visit_outbox');
      const req = store.get(localId);
      req.onsuccess = () => {
        const item = req.result;
        if (item) {
          item.status = 'syncing';
          store.put(item);
        }
      };
    },

    /**
     * Mark a visit as successfully synced and update the corresponding account.
     */
    async markVisitSynced(localId, assignmentId) {
      const db = await openDB();
      return new Promise((resolve) => {
        const tx = db.transaction(['visit_outbox', 'assigned_accounts'], 'readwrite');
        const outbox = tx.objectStore('visit_outbox');
        outbox.delete(localId);

        if (assignmentId) {
          const accStore = tx.objectStore('assigned_accounts');
          const accReq = accStore.get(Number(assignmentId));
          accReq.onsuccess = () => {
            const acc = accReq.result;
            if (acc) {
              acc.is_offline_pending = false;
              accStore.put(acc);
            }
          };
        }

        tx.oncomplete = () => resolve(true);
        tx.onerror = () => resolve(false);
      });
    },

    /**
     * Mark a visit as failed with an error message.
     */
    async markVisitFailed(localId, errorMsg) {
      const db = await openDB();
      const tx = db.transaction('visit_outbox', 'readwrite');
      const store = tx.objectStore('visit_outbox');
      const req = store.get(localId);
      req.onsuccess = () => {
        const item = req.result;
        if (item) {
          item.status = 'failed';
          item.retry_count = (item.retry_count || 0) + 1;
          item.error_message = errorMsg;
          store.put(item);
        }
      };
    },

    /**
     * Metadata getters and setters.
     */
    async getMeta(key) {
      const db = await openDB();
      return new Promise((resolve) => {
        const tx = db.transaction('sync_meta', 'readonly');
        const req = tx.objectStore('sync_meta').get(key);
        req.onsuccess = () => resolve(req.result ? req.result.value : null);
        req.onerror = () => resolve(null);
      });
    },

    async setMeta(key, value) {
      const db = await openDB();
      const tx = db.transaction('sync_meta', 'readwrite');
      tx.objectStore('sync_meta').put({ key, value });
    },
  };

  window.FiColDB = FiColDB;
})();
