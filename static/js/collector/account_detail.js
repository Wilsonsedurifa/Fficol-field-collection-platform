(() => {
  const configEl = document.getElementById('account-map-config');
  if (!configEl) return;
  const config = JSON.parse(configEl.textContent);
  const map = window.FiColMap?.create('account-map', config, null, (message) => {
    document.getElementById('detail-map-message').textContent = message;
  });
  map?.showAssigned(config);
  window.currentFiColMap = map;

  if (map) {
    const observer = new ResizeObserver(() => map.resize());
    observer.observe(document.getElementById('account-map'));
    window.addEventListener('pagehide', () => observer.disconnect());
  } else {
    document.getElementById('detail-map-message').textContent = 'Map unavailable. Use Get Directions to open the saved collection location.';
  }
})();

/* Visit requests use server-confirmed state; directions never start a visit. */
(() => {
  const configEl = document.getElementById('visit-config');
  if (!configEl) return;
  const config = JSON.parse(configEl.textContent);
  const el = id => document.getElementById(id);
  const form = el('visit-form');
  const csrf = form.querySelector('[name="csrfmiddlewaretoken"]')?.value || '';
  let ongoing = config.ongoing, gate = config.gate, busy = false, eventId = null;
  const buttons = ['start-visit', 'start-without-location', 'submit-visit', 'cancel-visit-open', 'cancel-visit-confirm', 'cancel-visit-back', 'schedule-revisit'];
  const message = (text, error = false) => {
    el('visit-feedback').textContent = text;
    el('visit-feedback').classList.toggle('is-error', error);
  };
  const formatDate = value => new Intl.DateTimeFormat('en-PH', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Manila' }).format(new Date(value));

  function uuid() {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    const bytes = window.crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  // Initialize Evidence Camera Pipeline
  const camera = window.FiColCamera?.initEvidenceCamera({
    inputId: 'evidence-camera-input',
    triggerBtnId: 'evidence-camera-btn',
    previewContainerId: 'evidence-photo-preview',
    hintId: 'evidence-photo-hint',
  });

  // Check if this account already has an offline pending visit saved on the phone
  if (window.FiColDB && config.assignment_id) {
    window.FiColDB.getAccount(config.assignment_id).then(acc => {
      if (acc && acc.is_offline_pending) {
        message(`⚡ Offline Visit Naka-save: ${acc.latest_visit?.outcome?.toUpperCase() || 'SUBMITTED'}. Ia-upload pag nagka-signal.`);
        if (window.currentFiColMap) {
          window.currentFiColMap.updateAssignedStatus(acc.latest_visit?.outcome || 'contacted', true);
        }
      }
    });
  }

  function showOngoing(value) {
    const active = value?.status === 'in_progress';
    ongoing = active ? value : null;
    el('visit-start-panel').hidden = active || !gate.allowed;
    el('visit-wait-panel').hidden = active || gate.allowed;
    el('revisit-form').hidden = active || gate.state !== 'schedule_required';

    if (gate.state === 'waiting') el('visit-wait-message').textContent = `Your next visit is scheduled for ${formatDate(gate.next_due_at)} (Philippine time). Reload at that time to start.`;
    else if (gate.state === 'schedule_required') el('visit-wait-message').textContent = 'Your visit is submitted. Request another visit for Back Office to schedule.';
    else if (gate.state === 'requested') el('visit-wait-message').textContent = 'Your follow-up request is awaiting a schedule from Back Office.';
    else if (gate.state === 'another_visit') el('visit-wait-message').textContent = 'Finish or cancel your current visit before starting work on another account.';
    else if (gate.state === 'overdue') el('visit-wait-message').textContent = 'The follow-up deadline has passed. Contact Back Office before another visit.';

    form.hidden = !active;
    if (!active) {
      el('visit-reload').hidden = !value && gate.allowed;
      return;
    }

    el('visit-started').textContent = `Visit started ${formatDate(value.started_at)} · Philippine time`;
    const evidence = value.distance_m === null
      ? (value.location_issue_reason ? `Location explanation: ${value.location_issue_reason}` : 'No GPS evidence captured.')
      : `${Math.round(Number(value.distance_m))} m from the collection pin · GPS accuracy ±${Math.ceil(Number(value.accuracy_m))} m.`;
    el('visit-location-summary').textContent = evidence + ' Location is supporting evidence, not proof of customer contact.';
  }

  async function api(url, payload) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(url, {
        method: 'POST',
        credentials: 'same-origin',
        mode: 'same-origin',
        signal: controller.signal,
        headers: {
          Accept: 'application/json',
          'Content-Type': 'application/json',
          'X-CSRFToken': csrf,
        },
        body: JSON.stringify(payload),
      });

      if (response.redirected || response.status === 401) {
        throw new Error('Please sign in again. Your visit may already be saved; check its history after signing in.');
      }
      let data = null;
      if (response.headers.get('content-type')?.includes('application/json')) data = await response.json();
      if (!response.ok) {
        const error = new Error(data?.error?.message || 'The request could not be confirmed. Reload to check your access and visit history.');
        error.code = data?.error?.code;
        throw error;
      }
      if (data?.saved !== true || (!data.visit && !data.task)) {
        throw new Error('The save could not be confirmed. Reload the account to check its visit history.');
      }
      return data;
    } catch (error) {
      if (error.name === 'AbortError' || error instanceof TypeError || error instanceof SyntaxError) {
        const netErr = new Error('The save could not be confirmed. Check your connection and reload to check the visit before retrying.');
        netErr.isNetwork = true;
        throw netErr;
      }
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  async function run(work) {
    if (busy) return;
    busy = true;
    buttons.forEach(id => {
      const btn = el(id);
      if (btn) btn.disabled = true;
    });
    try {
      await work();
    } catch (error) {
      message(error.message, true);
      if (error.code === 'location_reason_required') el('visit-location-reason').focus();
      el('visit-reload').hidden = false;
    } finally {
      busy = false;
      buttons.forEach(id => {
        const btn = el(id);
        if (btn) btn.disabled = false;
      });
    }
  }

  function detect() {
    return new Promise((resolve, reject) => {
      if (!navigator.geolocation) {
        reject(new Error('Location detection is unavailable. Explain the issue and choose Start without location.'));
        return;
      }
      navigator.geolocation.getCurrentPosition(
        position => resolve({
          longitude: position.coords.longitude,
          latitude: position.coords.latitude,
          accuracy_m: position.coords.accuracy,
          captured_at: new Date(position.timestamp).toISOString(),
        }),
        () => reject(new Error('Location could not be detected. Retry, or explain the issue and choose Start without location.')),
        { enableHighAccuracy: true, maximumAge: 0, timeout: 15000 }
      );
    });
  }

  async function start(withLocation) {
    if (!gate.allowed) throw new Error('Reload the account to check when your next visit is available.');
    const reason = el('visit-location-reason').value.trim();
    if (!withLocation && !reason) {
      el('visit-location-reason').focus();
      throw new Error('Explain why location evidence is unavailable.');
    }

    eventId = eventId || uuid();

    // OFFLINE HANDLING: If offline, start visit locally without network error
    if (!navigator.onLine) {
      const localVisit = {
        id: 0,
        status: 'in_progress',
        started_at: new Date().toISOString(),
        distance_m: null,
        accuracy_m: null,
        location_issue_reason: reason || 'Offline Visit (No cellular data)',
      };
      showOngoing(localVisit);
      message('⚡ Naka-offline mode. Nagsimula na ang visit sa phone mo. Punan ang report at i-submit.');
      el('visit-outcome').focus();
      return;
    }

    message(withLocation ? 'Detecting location. Allow access when asked.' : 'Starting visit with your location explanation...');
    let location = null;
    try {
      location = withLocation ? await detect() : null;
    } catch (locErr) {
      if (withLocation) throw locErr;
    }

    try {
      const result = await api(config.startUrl, { event_id: eventId, location, location_issue_reason: reason });
      gate = result.gate;
      showOngoing(result.visit);
      message(result.visit.status === 'in_progress' ? 'Visit saved. Record the outcome when your visit is finished.' : 'This visit request was already completed. Reload to view its history.');
      if (result.visit.status === 'in_progress') el('visit-outcome').focus();
    } catch (err) {
      if (err.isNetwork || !navigator.onLine) {
        // Fallback to local offline start
        const localVisit = {
          id: 0,
          status: 'in_progress',
          started_at: new Date().toISOString(),
          distance_m: null,
          accuracy_m: null,
          location_issue_reason: reason || 'Offline Visit (Weak signal fallback)',
        };
        showOngoing(localVisit);
        message('⚡ Mahina ang signal. Nagsimula na ang visit sa phone mo. Punan ang report at i-submit.');
        el('visit-outcome').focus();
        return;
      }
      throw err;
    }
  }

  el('start-visit')?.addEventListener('click', () => run(() => start(true)));
  el('start-without-location')?.addEventListener('click', () => run(() => start(false)));

  function syncFields() {
    const promised = el('visit-outcome').value === 'promise_to_pay';
    el('visit-promise').hidden = !promised;
    el('visit-promise').disabled = !promised;
    const notesRequired = ['wrong_address', 'other', 'disputed'].includes(el('visit-outcome').value);
    el('visit-notes').required = notesRequired;
    el('notes-hint').textContent = notesRequired ? 'Explain what happened (required)' : 'Add useful details about the visit';
    const follow = el('schedule-followup').checked;
    el('visit-followup').hidden = !follow;
    el('visit-followup').disabled = !follow;
  }

  el('visit-outcome').addEventListener('change', syncFields);
  el('schedule-followup').addEventListener('change', syncFields);

  form.addEventListener('submit', event => {
    event.preventDefault();
    if (!ongoing || !form.reportValidity()) return;

    const promise = el('visit-outcome').value === 'promise_to_pay' ? { amount: el('promise-amount').value, due_date: el('promise-date').value } : null;

    run(async () => {
      const due = el('schedule-followup').checked ? new Date(el('followup-date').value + '+08:00') : null;
      if (due && !Number.isFinite(due.getTime())) throw new Error('Choose a valid follow-up date and time.');
      const follow_up = due ? { kind: el('followup-kind').value, due_at: due.toISOString() } : null;
      const photo = camera?.getPhoto();

      const offlinePayload = {
        event_id: eventId || uuid(),
        assignment_id: config.assignment_id,
        outcome: el('visit-outcome').value,
        notes: el('visit-notes').value,
        occurred_at: ongoing?.started_at || new Date().toISOString(),
        ended_at: new Date().toISOString(),
        location_issue_reason: el('visit-location-reason')?.value || ongoing?.location_issue_reason || 'Offline Visit Submission',
        promise,
        follow_up,
        photoBlob: photo?.blob || null,
        photoName: photo?.fileName || null,
      };

      // If phone is offline: write directly to IndexedDB outbox
      if (!navigator.onLine) {
        if (window.FiColDB) {
          await window.FiColDB.queueVisit(offlinePayload);
        }
        if (window.currentFiColMap) {
          window.currentFiColMap.updateAssignedStatus(offlinePayload.outcome, true);
        }
        if ('serviceWorker' in navigator && 'SyncManager' in window) {
          navigator.serviceWorker.ready.then(reg => reg.sync.register('ficol-visit-sync')).catch(() => {});
        }

        showOngoing({ status: 'submitted' });
        camera?.clear();
        message('✅ Naka-save sa Phone (Offline). Berde na ang pin mo at kusang ia-upload pag nagka-signal.', false);
        el('visit-reload').hidden = false;
        if (window.FiColSync) {
          window.FiColSync.updateSyncBadge(1, false);
        }
        return;
      }

      // If online: attempt server submission
      message('Saving your visit report...');
      try {
        const result = await api(config.submitUrl.replace('/visits/0/', '/visits/' + ongoing.id + '/'), {
          outcome: el('visit-outcome').value,
          notes: el('visit-notes').value,
          promise,
          follow_up,
        });

        if (result.visit.status !== 'submitted') throw new Error('Submission could not be confirmed. Reload to check the visit.');
        gate = result.gate;
        eventId = null;
        showOngoing(result.visit);
        el('visit-cancel-form').hidden = true;

        if (window.currentFiColMap) {
          window.currentFiColMap.updateAssignedStatus(result.visit.outcome, false);
        }

        // If there was a captured photo, upload it as well
        if (photo?.blob) {
          try {
            const photoForm = new FormData();
            photoForm.append('event_id', uuid());
            photoForm.append('assignment_id', String(config.assignment_id));
            photoForm.append('outcome', result.visit.outcome);
            photoForm.append('notes', el('visit-notes').value);
            photoForm.append('evidence_photo', photo.blob, photo.fileName);
            await fetch('/collector/visits/sync/', {
              method: 'POST',
              body: photoForm,
              headers: { 'X-CSRFToken': csrf },
            });
          } catch (e) {
            console.warn('Photo upload queued for background sync.');
          }
        }

        camera?.clear();
        message('Visit submitted for Back Office review. Reload to see its status and any scheduled follow-up.');
        el('visit-reload').focus();
      } catch (err) {
        // If connection dropped during submit, gracefully fallback to offline save
        if (err.isNetwork || !navigator.onLine) {
          if (window.FiColDB) {
            await window.FiColDB.queueVisit(offlinePayload);
          }
          if (window.currentFiColMap) {
            window.currentFiColMap.updateAssignedStatus(offlinePayload.outcome, true);
          }
          showOngoing({ status: 'submitted' });
          camera?.clear();
          message('✅ Naantala ang internet. Naka-save sa Phone (Offline) at ia-upload pag nagka-signal.', false);
          return;
        }
        throw err;
      }
    });
  });

  el('cancel-visit-open')?.addEventListener('click', () => {
    el('visit-cancel-form').hidden = false;
    el('visit-cancel-reason').focus();
  });

  el('cancel-visit-back')?.addEventListener('click', () => {
    el('visit-cancel-form').hidden = true;
    el('cancel-visit-open').focus();
  });

  el('visit-cancel-form')?.addEventListener('submit', event => {
    event.preventDefault();
    if (!ongoing || !event.currentTarget.reportValidity()) return;

    if (!navigator.onLine || ongoing.id === 0) {
      showOngoing(null);
      el('visit-cancel-form').hidden = true;
      message('Visit cancelled locally on phone.');
      return;
    }

    const payload = { reason: el('visit-cancel-reason').value };
    run(async () => {
      message('Cancelling visit...');
      const result = await api(config.cancelUrl.replace('/visits/0/', '/visits/' + ongoing.id + '/'), payload);
      if (result.visit.status !== 'cancelled') throw new Error('Cancellation could not be confirmed. Reload to check the visit.');
      gate = result.gate;
      eventId = null;
      showOngoing(result.visit);
      el('visit-cancel-form').hidden = true;
      message('Visit cancelled. The attempt and reason remain in its history.');
      el('visit-reload').focus();
    });
  });

  el('revisit-form')?.addEventListener('submit', event => {
    event.preventDefault();
    if (!event.currentTarget.reportValidity() || !gate.latest_id) return;
    run(async () => {
      const due = new Date(el('revisit-date').value + '+08:00');
      if (!Number.isFinite(due.getTime())) throw new Error('Choose a valid next visit date and time.');
      message('Sending your follow-up request...');
      const result = await api(config.scheduleUrl.replace('/visits/0/', '/visits/' + gate.latest_id + '/'), { due_at: due.toISOString() });
      if (result.task?.status !== 'requested' || result.task.kind !== 'revisit') throw new Error('The schedule could not be confirmed. Reload the account.');
      gate = result.gate;
      showOngoing(null);
      message('Follow-up requested. Back Office will set the schedule before you can start another visit.');
      el('visit-reload').focus();
    });
  });

  const todayParts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Manila', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date());
  const datePart = type => todayParts.find(part => part.type === type).value;
  const promiseDateEl = el('promise-date');
  if (promiseDateEl) {
    promiseDateEl.min = `${datePart('year')}-${datePart('month')}-${datePart('day')}`;
  }

  showOngoing(ongoing);
  syncFields();
})();

/* Customer payment reporting */
(() => {
  const form = document.getElementById('payment-report-form');
  if (!form) return;
  const feedback = document.getElementById('payment-feedback');
  const btn = document.getElementById('submit-payment-btn');
  const eventIdInput = document.getElementById('payment-event-id');

  function uuid() {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    const bytes = window.crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, v => v.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  const paidAtInput = document.getElementById('payment-paid-at');
  if (paidAtInput && !paidAtInput.value) {
    const now = new Date();
    const offset = now.getTimezoneOffset() * 60000;
    const local = new Date(now.getTime() - offset).toISOString().slice(0, 16);
    paidAtInput.value = local;
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!form.reportValidity()) return;

    if (!eventIdInput.value) {
      eventIdInput.value = uuid();
    }

    feedback.hidden = false;
    feedback.textContent = 'Submitting customer payment report...';
    feedback.classList.remove('is-error');
    btn.disabled = true;

    const formData = new FormData(form);
    const rawPaidAt = formData.get('paid_at');
    if (rawPaidAt) {
      const paidDate = new Date(rawPaidAt + '+08:00');
      formData.set('paid_at', paidDate.toISOString());
    }

    try {
      const response = await fetch(form.action, {
        method: 'POST',
        headers: {
          'X-Requested-With': 'XMLHttpRequest',
        },
        body: formData,
      });

      const data = await response.json().catch(() => null);

      if (!response.ok) {
        const errorMsg = data?.error?.message || 'Failed to submit payment report. Please check your inputs.';
        throw new Error(errorMsg);
      }

      feedback.textContent = 'Payment reported successfully. Back Office will verify the proof.';
      form.reset();
      eventIdInput.value = '';
      setTimeout(() => {
        window.location.reload();
      }, 1500);
    } catch (err) {
      feedback.textContent = err.message;
      feedback.classList.add('is-error');
      btn.disabled = false;
    }
  });
})();

/* Contact attempt logging */
(() => {
  const form = document.getElementById('contact-attempt-form');
  if (!form) return;
  const feedback = document.getElementById('contact-attempt-feedback');
  const btn = document.getElementById('save-contact-attempt-btn');
  const methodSelect = document.getElementById('contact-attempt-method');
  const outcomeSelect = document.getElementById('contact-attempt-outcome');
  const notesTextarea = document.getElementById('contact-attempt-notes');

  function uuid() {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    const bytes = window.crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, v => v.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  let pendingEventId = null;

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!form.reportValidity()) return;

    const kind = methodSelect.value;
    const outcome = outcomeSelect.value;
    const notes = notesTextarea.value.trim();

    if (!notes) {
      notesTextarea.focus();
      feedback.hidden = false;
      feedback.textContent = 'Provide notes for this contact attempt.';
      feedback.classList.add('is-error');
      return;
    }

    if (!pendingEventId) {
      pendingEventId = uuid();
    }

    feedback.hidden = false;
    feedback.textContent = 'Saving contact attempt...';
    feedback.classList.remove('is-error');
    btn.disabled = true;

    const csrf = form.querySelector('[name="csrfmiddlewaretoken"]')?.value || '';
    const payload = {
      event_id: pendingEventId,
      kind: kind,
      outcome: outcome,
      notes: notes,
    };

    try {
      const response = await fetch(form.action, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Accept': 'application/json',
          'Content-Type': 'application/json',
          'X-CSRFToken': csrf,
        },
        body: JSON.stringify(payload),
      });

      const data = await response.json().catch(() => null);

      if (!response.ok) {
        const errorMsg = data?.error?.message || 'Failed to save contact attempt. Please check your inputs.';
        throw new Error(errorMsg);
      }

      if (data?.saved !== true) {
        throw new Error('Save could not be confirmed. Reload the account.');
      }

      feedback.textContent = 'Contact attempt saved.';
      pendingEventId = null;
      form.reset();
      window.location.reload();
    } catch (err) {
      feedback.textContent = err.message;
      feedback.classList.add('is-error');
      btn.disabled = false;
    }
  });
})();
