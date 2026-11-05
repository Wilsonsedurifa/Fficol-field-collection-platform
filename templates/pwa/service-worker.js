const CACHE_NAME = 'ficol-pwa-v2';
const PRECACHE_ASSETS = [
  '/offline/',
  '/static/img/favicon.svg',
  '/static/img/pwa/icon-192.png',
  '/static/img/pwa/icon-512.png',
  '/static/css/dist/tailwind.css',
  '/static/css/app.css',
  '/static/css/components/pwa.css',
  '/static/js/pwa/ficol-db.js',
  '/static/js/pwa/pwa-sync.js',
  '/static/js/collector/evidence-camera.js',
  '/static/js/components/map.js',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(PRECACHE_ASSETS);
    })
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.map((key) => {
          if (key.startsWith('ficol-pwa-') && key !== CACHE_NAME) {
            return caches.delete(key);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});

// Background Sync Event Listener
self.addEventListener('sync', (event) => {
  if (event.tag === 'ficol-visit-sync') {
    event.waitUntil(
      self.clients.matchAll({ includeUncontrolled: true, type: 'window' }).then((clients) => {
        clients.forEach((client) => {
          client.postMessage({ type: 'TRIGGER_REPLAY' });
        });
      })
    );
  }
});

self.addEventListener('fetch', (event) => {
  // Only handle GET requests; never intercept mutations
  if (event.request.method !== 'GET') {
    return;
  }

  const url = new URL(event.request.url);

  // Navigation requests: Stale-While-Revalidate or Network-First for collector pages
  if (event.request.mode === 'navigate') {
    // For collector pages, try network first, then fall back to cached page, then /offline/
    if (url.pathname.startsWith('/collector/')) {
      event.respondWith(
        fetch(event.request)
          .then((response) => {
            if (response && response.status === 200) {
              const copy = response.clone();
              caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
            }
            return response;
          })
          .catch(() => {
            return caches.match(event.request).then((cached) => {
              return cached || caches.match('/offline/');
            });
          })
      );
      return;
    }

    // Default navigation
    event.respondWith(
      fetch(event.request).catch(() => {
        return caches.match('/offline/');
      })
    );
    return;
  }

  // Same-origin GET static assets under /static/: Cache-First with Network fallback
  if (url.origin === self.location.origin && url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(event.request).then((cached) => {
        if (cached) {
          // Return cached and asynchronously update cache
          fetch(event.request).then((networkRes) => {
            if (networkRes && networkRes.status === 200) {
              caches.open(CACHE_NAME).then((cache) => cache.put(event.request, networkRes));
            }
          }).catch(() => {});
          return cached;
        }

        return fetch(event.request).then((response) => {
          if (response && response.status === 200 && response.type === 'basic') {
            const responseToCache = response.clone();
            caches.open(CACHE_NAME).then((cache) => {
              cache.put(event.request, responseToCache);
            });
          }
          return response;
        });
      })
    );
    return;
  }
});
