// Sweat Box — Service Worker
// Handles Web Push notifications for preheat reminders.

self.addEventListener('install', event => {
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(clients.claim());
});

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
    icon: '/icon.svg',
    badge: '/icon.svg',
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
      // Focus an existing window if available
      for (const client of clientList) {
        if ('focus' in client) {
          client.focus();
          if (notifData.bookingId) {
            client.postMessage({ type: 'OPEN_BOOKING', bookingId: notifData.bookingId });
          }
          return;
        }
      }
      // Otherwise open a new window
      return clients.openWindow(targetUrl);
    })
  );
});
