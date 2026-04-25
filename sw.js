const CACHE_NAME = 'fleet-c2-v2'; // Incrementiamo la versione
const urlsToCache = [
  '/',
  '/manifest.json',
  '/icon-512.png'
];

// --- 1. GESTIONE CACHE (Il tuo codice originale) ---
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(urlsToCache))
  );
});

self.addEventListener('fetch', event => {
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});

// --- 2. GESTIONE NOTIFICHE PUSH (Il nuovo codice) ---
self.addEventListener('push', function(event) {
    console.log('[Service Worker] Notifica Push ricevuta.');

    let data = { title: 'Fleet Alert', body: 'Nuovo messaggio dal sistema.' };

    if (event.data) {
        try {
            data = event.data.json();
        } catch (e) {
            data.body = event.data.text();
        }
    }

    const options = {
        body: data.body,
        icon: '/icon-512.png',
        badge: '/icon-512.png',
        vibrate: [200, 100, 200],
        data: {
            dateOfArrival: Date.now(),
            primaryKey: '1'
        },
        actions: [
            { action: 'explore', title: 'Apri Dashboard' },
            { action: 'close', title: 'Chiudi' }
        ]
    };

    event.waitUntil(
        self.registration.showNotification(data.title, options)
    );
});

self.addEventListener('notificationclick', function(event) {
    event.notification.close();
    if (event.action === 'explore') {
        event.waitUntil(
            clients.openWindow('/')
        );
    }
});
