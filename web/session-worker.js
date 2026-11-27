self.addEventListener('install', event => event.waitUntil(self.skipWaiting()));
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || !url.pathname.startsWith('/api/')) return;
  event.respondWith((async () => {
    const cache = await caches.open('pruner-browser-session');
    const session = await cache.match('/session');
    if (!session) return fetch(event.request);
    const headers = new Headers(event.request.headers);
    headers.set('X-Pruner-Token', await session.text());
    return fetch(new Request(event.request, { mode: 'same-origin', headers, redirect: 'error', credentials: 'omit' }));
  })());
});
