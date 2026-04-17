// PosturoSPS Service Worker - PWA offline support
const CACHE_NAME = 'posturosps-v1';
const STATIC_ASSETS = [
  '/',
  '/static/app.css',
  '/static/app.js',
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  // API calls: always network, fallback to cached
  const isAPI = ['/status', '/exercise', '/sot/', '/esp/', '/tare', '/center',
                 '/log/', '/videos/', '/system/', '/patients', '/presets', '/sessions']
    .some(p => url.pathname.startsWith(p));

  if (isAPI) {
    event.respondWith(
      fetch(event.request).catch(() =>
        new Response(JSON.stringify({ error: 'offline' }), {
          headers: { 'Content-Type': 'application/json' }
        })
      )
    );
    return;
  }

  // Static assets: cache first
  event.respondWith(
    caches.match(event.request).then(cached => cached || fetch(event.request).then(resp => {
      const clone = resp.clone();
      caches.open(CACHE_NAME).then(c => c.put(event.request, clone));
      return resp;
    }))
  );
});
