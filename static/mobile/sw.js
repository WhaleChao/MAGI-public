const MAGI_MOBILE_CACHE = "magi-mobile-v20260815-ia-v1";

const PRE_CACHE = [
    "/static/mobile/magi-mobile.svg",
    "/static/mobile/mobile.css",
    "/static/mobile/mobile.js",
];

const NON_CACHE_ROUTES = new Set([
    "/login",
    "/register",
]);

function isAuthPath(urlPath) {
    return NON_CACHE_ROUTES.has(urlPath);
}

function shouldSkipCache(urlPath) {
    return !urlPath.startsWith("/static/mobile/");
}

function cacheKey(request) {
    return new Request(request.url);
}

async function networkFirst(event) {
    const request = event.request;
    const cache = await caches.open(MAGI_MOBILE_CACHE);
    try {
        const response = await fetch(request);
        if (response && response.ok && !shouldSkipCache(new URL(request.url).pathname)) {
            await cache.put(cacheKey(request), response.clone());
        }
        return response;
    } catch (_err) {
        const cached = await cache.match(cacheKey(request));
        if (cached) {
            return cached;
        }
        if (request.mode === "navigate") {
            return new Response("MAGI Mobile offline", {
                status: 503,
                headers: {
                    "Content-Type": "text/plain; charset=utf-8",
                },
            });
        }
        throw _err;
    }
}

self.addEventListener("install", (event) => {
    event.waitUntil(
        caches.open(MAGI_MOBILE_CACHE).then((cache) => cache.addAll(PRE_CACHE))
    );
    self.skipWaiting();
});

self.addEventListener("activate", (event) => {
    event.waitUntil(
        caches.keys().then(async (keys) => {
            await Promise.all(
                keys.map((key) => key.startsWith("magi-mobile-") && key !== MAGI_MOBILE_CACHE ? caches.delete(key) : null)
            );
            await self.clients.claim();
        })
    );
});

self.addEventListener("fetch", (event) => {
    const request = event.request;
    if (request.method !== "GET") return;
    const url = new URL(request.url);
    if (url.origin !== self.location.origin) return;
    if (shouldSkipCache(url.pathname)) return;
    if (isAuthPath(url.pathname)) {
        event.respondWith(fetch(request));
        return;
    }
    event.respondWith(networkFirst(event));
});
