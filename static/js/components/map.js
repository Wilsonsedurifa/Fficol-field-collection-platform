/* Provider-specific map rendering with enhanced offline and visit status visual states. */
(() => {
  'use strict';

  function createMarkerIcon(selected = false, status = 'pending', isOffline = false) {
    let color = selected ? '#312e81' : '#4f46e5';
    let innerSvg = '<circle cx="16" cy="15" r="5" fill="white"/>';

    if (status === 'contacted' || status === 'promise_to_pay') {
      color = selected ? '#14532d' : '#16a34a'; // Green
      innerSvg = '<path d="M11 15l3.5 3.5 7-7" stroke="white" stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>';
    } else if (status === 'unavailable') {
      color = selected ? '#78350f' : '#d97706'; // Amber / Yellow
      innerSvg = '<circle cx="16" cy="15" r="5" fill="white"/><path d="M16 12v3l2 2" stroke="#d97706" stroke-width="1.8" fill="none" stroke-linecap="round"/>';
    } else if (status === 'wrong_address' || status === 'disputed' || status === 'other') {
      color = selected ? '#334155' : '#64748b'; // Slate / Gray
      innerSvg = '<path d="M12 11l8 8M20 11l-8 8" stroke="white" stroke-width="2.2" stroke-linecap="round"/>';
    }

    const offlineBadge = isOffline
      ? '<circle cx="25" cy="8" r="5.5" fill="#ea580c" stroke="white" stroke-width="1.5"/><circle cx="25" cy="8" r="2" fill="white"/>'
      : '';

    return L.divIcon({
      className: 'map-offer-pin',
      html: `
        <div style="position:relative; width:32px; height:40px;">
          <svg width="32" height="40" viewBox="0 0 32 40" aria-hidden="true">
            <path d="M16 39C12 32 2 24 2 16a14 14 0 1 1 28 0c0 8-10 16-14 23Z" fill="${color}" stroke="white" stroke-width="2"/>
            ${innerSvg}
          </svg>
          ${offlineBadge}
        </div>
      `,
      iconSize: [32, 40],
      iconAnchor: [16, 40],
    });
  }

  window.FiColMap = {
    createMarkerIcon,

    create(elementId, config, onSelect, onError) {
      if (!window.L) {
        onError?.('Map unavailable. You can still use the account list.');
        return null;
      }
      const map = L.map(elementId, { zoomControl: false }).setView([14.5995, 120.9842], 12);
      L.control.zoom({ position: 'topright' }).addTo(map);

      const tiles = L.tileLayer(config.tileUrl, { maxZoom: 19, attribution: config.attribution }).addTo(map);
      tiles.on('tileerror', () => onError?.('Map tiles could not load. Offline mode remains active.'));

      const offers = L.layerGroup().addTo(map);
      const own = L.layerGroup().addTo(map);
      const markers = new Map();
      let location = null;
      let assignedPoint = null;
      let currentAssignedMarker = null;
      const mobile = window.matchMedia('(min-width:900px)');

      function padding() {
        if (elementId !== 'offers-map') return { padding: [32, 32], maxZoom: 16 };
        const sheet = document.getElementById('selected-offer');
        const bottom = !mobile.matches && sheet && !sheet.hidden ? sheet.offsetHeight + 70 : 65;
        const size = map.getSize();
        return {
          paddingTopLeft: mobile.matches ? [Math.min(300, size.x * 0.3), 75] : [40, 80],
          paddingBottomRight: mobile.matches ? [Math.min(370, size.x * 0.35), 85] : [45, Math.min(bottom, size.y - 140)],
          maxZoom: 15,
        };
      }

      function fit(points) {
        if (points.length) map.fitBounds(L.latLngBounds(points), padding());
      }

      return {
        setLocation(value, centre = false) {
          own.clearLayers();
          location = value;
          if (!value) return;
          const point = [value.latitude, value.longitude];
          L.circle(point, { radius: value.accuracy_m, color: '#2563eb', weight: 1, fillOpacity: 0.08, interactive: false }).addTo(own);
          L.circleMarker(point, { radius: 7, color: '#fff', weight: 2, fillColor: '#2563eb', fillOpacity: 1 }).addTo(own);
          if (centre) map.setView(point, 14);
        },

        recenter() {
          if (location) map.setView([location.latitude, location.longitude], 14);
        },

        setOffers(items) {
          offers.clearLayers();
          markers.clear();
          assignedPoint = null;
          for (const item of items) {
            const area = item.approximate_area;
            const point = [area.latitude, area.longitude];
            L.circle(point, { radius: area.radius_m, color: '#818cf8', weight: 1, fillColor: '#818cf8', fillOpacity: 0.15, interactive: false }).addTo(offers);
            const marker = L.marker(point, {
              icon: createMarkerIcon(false, item.status || 'pending', item.is_offline_pending),
              title: item.reference + ' (approximate area)',
              keyboard: true,
            }).addTo(offers);
            marker.on('click', () => onSelect?.(item.id));
            markers.set(item.id, { marker, data: item });
          }
        },

        select(id) {
          for (const [key, obj] of markers) {
            const isSel = key === id;
            obj.marker.setIcon(createMarkerIcon(isSel, obj.data.status || 'pending', obj.data.is_offline_pending));
          }
        },

        updateStatus(id, status, isOffline = false) {
          const obj = markers.get(id);
          if (obj) {
            obj.data.status = status;
            obj.data.is_offline_pending = isOffline;
            obj.marker.setIcon(createMarkerIcon(false, status, isOffline));
          }
        },

        filterMarkers(filterType = 'all') {
          for (const [, obj] of markers) {
            const st = obj.data.status || 'pending';
            const isCompleted = st === 'contacted' || st === 'promise_to_pay' || st === 'wrong_address' || st === 'disputed' || st === 'other';

            if (filterType === 'pending') {
              if (isCompleted) {
                obj.marker.remove();
              } else {
                obj.marker.addTo(offers);
              }
            } else if (filterType === 'completed') {
              if (!isCompleted) {
                obj.marker.remove();
              } else {
                obj.marker.addTo(offers);
              }
            } else {
              obj.marker.addTo(offers);
            }
          }
        },

        fitOffers(id) {
          const obj = markers.get(id);
          const points = obj ? [obj.marker.getLatLng()] : Array.from(markers.values(), (item) => item.marker.getLatLng());
          if (location) points.push([location.latitude, location.longitude]);
          if (assignedPoint) points.push(assignedPoint);
          fit(points);
        },

        showAssigned(value, status = 'pending', isOffline = false) {
          offers.clearLayers();
          markers.clear();
          const point = [value.latitude, value.longitude];
          assignedPoint = point;
          currentAssignedMarker = L.marker(point, {
            icon: createMarkerIcon(true, status, isOffline),
            title: 'Verified collection location',
          }).addTo(offers);
          fit([point]);
        },

        updateAssignedStatus(status, isOffline = false) {
          if (currentAssignedMarker) {
            currentAssignedMarker.setIcon(createMarkerIcon(true, status, isOffline));
          }
        },

        resize() {
          map.invalidateSize();
        },
      };
    },
  };
})();
