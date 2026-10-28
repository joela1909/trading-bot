// Service Worker: haelt die App-Huelle bereit, damit sie vom Startbildschirm schnell oeffnet.
// Daten (/api/...) kommen immer frisch vom Bot und werden nie zwischengespeichert.
const CACHE = 'tradingbot-v25';
const SHELL = [
  '/', '/static/app.css', '/static/app.js', '/static/icon.svg', '/manifest.webmanifest',
  '/static/vendor/lightweight-charts.standalone.production.js'

];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/')) return;
  // erst das Netz (neueste Fassung), ohne Verbindung die gespeicherte
  event.respondWith(
    fetch(event.request)
      .then((res) => { const copy = res.clone(); if (res.ok) caches.open(CACHE).then((c) => c.put(event.request, copy)); return res; })
      .catch(() => caches.match(event.request))
  );
});
