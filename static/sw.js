// Sweat Box — Service Worker
// Offline caching + Web Push notifications.
//
// Cache strategy:
//   Static assets (shell + CDN libs) → cache-first, background update
//   API GET requests                 → network-first, cache as offline fallback
//   API non-GET requests             → network only (never cache mutations)
//   Background sync                  → retry queued bookings on reconnect

const CACHE_VERSION = 'sweatbox-v4';

const PRECACHE_URLS = [
  '/',
  '/manifest.json',
  '/icon.svg',
  '/icon-192.png',
  '/icon-512.png',
];

// CDN resources to cache on first use (cache-first after that)
const CDN_ORIGINS = [
  'cdn.tailwindcss.com',
  'unpkg.com',
];

// ─── lifecycle ───────────────────────────────────────────────────────────────

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_VERSION)
      .then(cache => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => k !== CACHE_VERSION).map(k => caches.delete(k))
      ))
      .then(() => clients.claim())
  );
});

// ─── fetch ───────────────────────────────────────────────────────────────────

self.addEventListener('fetch', event => {
  const { request } = event;
  const url = new URL(request.url);

  // Never intercept non-GET API mutations (POST, DELETE, PUT, PATCH)
  if (url.pathname.startsWith('/api/') && request.method !== 'GET') return;

  // CDN resources — cache-first (they're versioned, safe to cache long-term)
  if (CDN_ORIGINS.some(o => url.hostname.includes(o))) {
    event.respondWith(cacheFirst(request));
    return;
  }

  // Same-origin GET API calls — network-first with cache fallback
  if (url.origin === self.location.origin && url.pathname.startsWith('/api/')) {
    event.respondWith(networkFirst(request));
    return;
  }

  // App shell and static assets — cache-first with background revalidation
  if (url.origin === self.location.origin) {
    event.respondWith(staleWhileRevalidate(request));
    return;
  }
});

async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(CACHE_VERSION);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    return cached || new Response('Offline', { status: 503 });
  }
}

async function networkFirst(request) {
  const cache = await caches.open(CACHE_VERSION);
  try {
    const response = await fetch(request);
    if (response.ok) cache.put(request, response.clone());
    return response;
  } catch {
    const cached = await cache.match(request);
    return cached || new Response(JSON.stringify({ error: 'Offline' }), {
      status: 503,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(CACHE_VERSION);
  const cached = await cache.match(request);
  const fetchPromise = fetch(request).then(response => {
    if (response.ok) cache.put(request, response.clone());
    return response;
  }).catch(() => null);
  return cached || await fetchPromise || new Response('Offline', { status: 503 });
}

// ─── background sync ─────────────────────────────────────────────────────────

self.addEventListener('sync', event => {
  if (event.tag === 'sync-bookings') {
    event.waitUntil(syncQueuedBookings());
  }
});

async function syncQueuedBookings() {
  // Notify open clients to flush their localStorage queue
  const clientList = await clients.matchAll({ type: 'window', includeUncontrolled: true });
  for (const client of clientList) {
    client.postMessage({ type: 'FLUSH_BOOKING_QUEUE' });
  }
}

// ─── push notifications ───────────────────────────────────────────────────────

self.addEventListener('push', event => {
  if (!event.data) return;

  let data;
  try {
    data = event.data.json();
  } catch {
    data = { title: 'Sweat Box', body: event.data.text() };
  }

  const options = {
    body: data.body || '',
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    tag: data.tag || 'sauna-reminder',
    data: { url: data.url || '/', bookingId: data.bookingId || null },
    requireInteraction: true,
    actions: data.actions || [],
  };

  event.waitUntil(self.registration.showNotification(data.title || 'Sweat Box', options));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();

  const notifData = event.notification.data || {};
  const targetUrl = notifData.url || '/';

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
      for (const client of clientList) {
        if ('focus' in client) {
          client.focus();
          if (notifData.bookingId) {
            client.postMessage({ type: 'OPEN_BOOKING', bookingId: notifData.bookingId });
          }
          return;
        }
      }
      return clients.openWindow(targetUrl);
    })
  );
});
