// Cache strategy:
//  - STATIC_CACHE: vendored, version/hash-named assets (immutable) + icons.
//    Served cache-first with background revalidation (stale-while-revalidate).
//  - Navigations: network-first with the offline page as fallback.
//  - NEVER cached: authenticated HTML, HTMX fragments, signed Storage URLs,
//    or anything outside /static — legal client data must not persist in
//    browser caches.
const STATIC_CACHE = 'athena-static-v4';
const OFFLINE_CACHE = 'athena-offline-v2';
const OFFLINE_URL = '/offline';

const PRECACHE = [
  '/static/vendor/app.af95b30d.css',
  '/static/vendor/htmx-2.0.4.min.js',
  '/static/vendor/alpinejs-3.15.12.min.js',
  '/static/vendor/firebase-app-compat-10.12.2.js',
  '/static/vendor/firebase-app-check-compat-10.12.2.js',
  '/static/vendor/firebase-auth-compat-10.12.2.js',
  '/static/vendor/appcheck-boot.fee929af.js',
  '/static/owl.png',
  '/static/icons/icon-192.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    Promise.all([
      caches.open(OFFLINE_CACHE).then((cache) => cache.add(OFFLINE_URL)),
      // Precache failures (e.g. offline install) must not block activation.
      caches.open(STATIC_CACHE).then((cache) =>
        Promise.allSettled(PRECACHE.map((url) => cache.add(url)))
      ),
    ])
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  const keep = [STATIC_CACHE, OFFLINE_CACHE];
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => !keep.includes(k)).map((k) => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

function isCacheableStatic(url) {
  return url.origin === self.location.origin &&
    (url.pathname.startsWith('/static/vendor/') ||
     url.pathname.startsWith('/static/icons/') ||
     url.pathname === '/static/owl.png');
}

self.addEventListener('fetch', (event) => {
  const request = event.request;

  // Navigations: network first, offline fallback. Never cached.
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() => caches.match(OFFLINE_URL))
    );
    return;
  }

  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (!isCacheableStatic(url)) return; // everything else: straight to network

  // Stale-while-revalidate for immutable static assets.
  event.respondWith(
    caches.open(STATIC_CACHE).then(async (cache) => {
      const cached = await cache.match(request);
      const refresh = fetch(request).then((response) => {
        if (response && response.ok) cache.put(request, response.clone());
        return response;
      }).catch(() => cached);
      return cached || refresh;
    })
  );
});
