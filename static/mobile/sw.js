const MAGI_MOBILE_CACHE = "magi-mobile-v1";

self.addEventListener("install", (event) => {
    event.waitUntil(
        caches.open(MAGI_MOBILE_CACHE).then((cache) => cache.addAll([
            "/mobile",
            "/static/mobile/mobile.css",
            "/static/mobile/mobile.js",
            "/static/mobile/magi-mobile.svg"
        ]))
    );
    self.skipWaiting();
});

self.addEventListener("activate", (event) => {
    event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
    const request = event.request;
    if (request.method !== "GET") return;
    event.respondWith(
        fetch(request).catch(() => caches.match(request).then((cached) => cached || caches.match("/mobile")))
    );
});
