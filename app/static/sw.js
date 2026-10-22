const CACHE = "habr-madlad-openai-v12";
const ASSETS = [
  "/",
  "/static/app.css?v=12",
  "/static/app.js?v=12",
  "/static/manifest.webmanifest",
  "/public/icon-192.png",
  "/public/icon-512.png",
  "/public/apple-touch-icon.png",
  "/public/favicon.ico"
];
self.addEventListener("install", event => { self.skipWaiting(); event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS))); });
self.addEventListener("activate", event => { event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim())); });
self.addEventListener("fetch", event => {
  if (event.request.method !== "GET" || new URL(event.request.url).pathname.startsWith("/api/")) return;
  event.respondWith(fetch(event.request, {cache:"no-store"}).then(response => { const copy=response.clone(); caches.open(CACHE).then(cache=>cache.put(event.request,copy)); return response; }).catch(()=>caches.match(event.request)));
});
