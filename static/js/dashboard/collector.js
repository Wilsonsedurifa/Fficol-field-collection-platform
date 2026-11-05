/* Collector dashboard: explicit GPS detection and confirmed server saving. */
(() => {
  const panel = document.getElementById('gps-telemetry');
  const button = document.getElementById('gps-action-btn');
  if (!panel || !button) return;

  const statusText = document.getElementById('gps-status-text');
  const metaText = document.getElementById('gps-meta-text');
  const freshnessText = document.getElementById('gps-freshness-text');
  const icon = document.getElementById('gps-status-icon');
  let busy = false;
  let savedObservation = null;
  let freshnessTimer = null;

  function setUI(title, message, tone = '') {
    if (statusText) statusText.textContent = title;
    if (metaText) metaText.textContent = message;
    if (icon) icon.className = 'telemetry-icon ' + tone;
  }

  function setBusy(value) {
    busy = value;
    button.disabled = value;
    button.setAttribute('aria-busy', String(value));
    if (!value) button.textContent = savedObservation ? 'Update Location' : 'Detect Location';
  }

  function updateAge() {
    if (!savedObservation || !freshnessText) return;
    // Use the server's observation age plus elapsed time, avoiding device clock skew.
    const ageSeconds = Math.floor(savedObservation.ageSeconds +
      Math.max(0, performance.now() - savedObservation.confirmedAt) / 1000);
    const ageDisplay = ageSeconds < 60 ? `${ageSeconds}s ago` : `${Math.floor(ageSeconds / 60)}m ago`;
    freshnessText.textContent = `Saved observation captured ${ageDisplay}. Update your location before new collection work.`;
  }

  function clearSavedDisplay() {
    savedObservation = null;
    if (freshnessTimer !== null) clearInterval(freshnessTimer);
    freshnessTimer = null;
    if (freshnessText) freshnessText.textContent = '';
  }

  function showFailure(code, status, serverMessage) {
    const messages = {
      inaccurate_location: ['Location accuracy too low', 'Move to an open area and try again.'],
      stale_location: ['Location too old', 'Detect your location again to get a current observation.'],
      future_location: ['Check your device clock', 'Set your device time correctly and try again.'],
      older_observation: ['Newer location already saved', 'Detect your location again to submit a newer observation.'],
      conflicting_observation: ['Please update your location', 'Detect your location again to get a new observation.'],
      approval_required: ['Approval required', 'Contact your supervisor about your collector approval.'],
      collector_required: ['Collector access required', 'Sign in with an active collector account.'],
      collector_portal_forbidden: ['Collector portal unavailable', 'Use the Back Office portal with this account.'],
      authentication_required: ['Sign in again', 'Your session may have expired. Sign in and try again.'],
      invalid_observation: ['Location could not be accepted', 'Your location could not be accepted. Update your location and try again.'],
      invalid_json: ['Location could not be accepted', 'Your location could not be accepted. Update your location and try again.'],
    };
    let fallback;
    if (status === 401) {
      fallback = ['Sign in again', 'Your session may have expired. Sign in and try again.'];
    } else if (status === 403) {
      fallback = ['Please reload this page', 'Your session or access may have changed. Reload and try again.'];
    } else if (status === 400) {
      fallback = ['Location could not be accepted', 'Your location could not be accepted. Update your location and try again.'];
    } else {
      fallback = ['Save could not be confirmed', 'The server could not accept your location fix. Please try again.'];
    }
    const entry = messages[code] || fallback;
    const title = entry[0];
    const validServerMessage = typeof serverMessage === 'string' && serverMessage.trim().length > 0 ? serverMessage.trim() : null;
    const description = validServerMessage || entry[1];
    setUI(title, description, 'is-error');
  }

  async function saveLocation(position) {
    let timeout;
    try {
      button.textContent = 'Saving...';
      setUI('Saving location...', 'Please wait while your location is saved.', 'is-warning');
      const coords = position.coords;
      if (!Number.isFinite(position.timestamp) ||
          !Number.isFinite(coords.longitude) || !Number.isFinite(coords.latitude) ||
          !Number.isFinite(coords.accuracy)) {
        showFailure('invalid_observation', 400, 'Your location could not be accepted. Update your location and try again.');
        return;
      }
      const csrfToken = panel.querySelector('input[name="csrfmiddlewaretoken"]')?.value;
      const url = panel.dataset.gpsSubmitUrl;
      if (!csrfToken || !url) {
        setUI('Please reload this page', 'Reload the dashboard and try again.', 'is-error');
        return;
      }
      const controller = new AbortController();
      timeout = setTimeout(() => controller.abort(), 15000);
      const response = await fetch(url, {
        method: 'POST',
        credentials: 'same-origin',
        mode: 'same-origin',
        signal: controller.signal,
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify({
          longitude: coords.longitude,
          latitude: coords.latitude,
          accuracy_m: coords.accuracy,
          captured_at: new Date(position.timestamp).toISOString(),
        }),
      });
      if (response.redirected || response.status === 401) {
        showFailure('authentication_required', response.status, 'Your session may have expired. Sign in and try again.');
        return;
      }
      let data = null;
      if (response.headers.get('content-type')?.includes('application/json')) {
        try {
          data = await response.json();
        } catch {
          // Response was not valid JSON; server was reached but response is malformed.
        }
      }
      if (!response.ok) {
        const rawMessage = data?.error?.message;
        const validMessage = typeof rawMessage === 'string' && rawMessage.trim().length > 0 ? rawMessage.trim() : null;
        showFailure(data?.error?.code, response.status, validMessage);
        return;
      }
      if (!data || data.saved !== true || typeof data.changed !== 'boolean' ||
          typeof data.fresh_for_submission !== 'boolean' ||
          !Number.isFinite(data.age_seconds) || data.age_seconds < 0 ||
          !Number.isFinite(data.location?.accuracy_m) || data.location.accuracy_m < 0 ||
          !Number.isFinite(Date.parse(data.location?.captured_at)) ||
          !Number.isFinite(Date.parse(data.location?.received_at))) {
        setUI('Save could not be confirmed', 'The server response could not be understood. Please try again.', 'is-error');
        return;
      }
      savedObservation = {
        ageSeconds: data.age_seconds,
        confirmedAt: performance.now(),
      };
      const accuracy = Math.ceil(data.location.accuracy_m);
      if (!data.fresh_for_submission) {
        setUI('Location saved; update needed', 'Detect your location again for a current observation.', 'is-warning');
      } else {
        setUI(`Location saved (±${accuracy}m)`,
          'Your location is saved. Open Account Offers to check nearby accounts.', 'is-active');
      }
      updateAge();
      freshnessTimer = setInterval(updateAge, 15000);
    } catch (error) {
      setUI(
        error.name === 'AbortError' ? 'Request timed out' : 'Connection error',
        error.name === 'AbortError'
          ? 'The request timed out. Check your connection and try again.'
          : 'Could not connect to the server. Check your network connection and try again.',
        'is-error'
      );
    } finally {
      if (timeout !== undefined) clearTimeout(timeout);
      setBusy(false);
    }
  }

  function requestLocation() {
    if (busy) return;
    clearSavedDisplay();
    if (!navigator.geolocation) {
      setUI('Location unsupported', 'This browser does not support location detection.', 'is-error');
      return;
    }
    setBusy(true);
    button.textContent = 'Detecting...';
    setUI('Detecting location...', 'Allow location access when your browser asks.', 'is-warning');
    try {
      navigator.geolocation.getCurrentPosition(saveLocation, (error) => {
        if (error.code === 1) {
          setUI('Permission denied', 'Allow location access in your browser settings and try again.', 'is-error');
        } else if (error.code === 2) {
          setUI('Location unavailable', 'Move to an open area and try again.', 'is-error');
        } else {
          setUI('Location detection timed out', 'Try again in an area with a clearer location signal.', 'is-warning');
        }
        setBusy(false);
      }, { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 });
    } catch (error) {
      setUI('Location unavailable', 'Please check browser location access and try again.', 'is-error');
      setBusy(false);
    }
  }

  button.addEventListener('click', requestLocation);
  window.addEventListener('pagehide', () => {
    if (freshnessTimer !== null) clearInterval(freshnessTimer);
  });
})();
