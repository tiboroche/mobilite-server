// Service worker minimal : la page reste ouvrable hors ligne ; l'API n'est jamais mise en cache.
const CACHE = "mobilite-shell-v2";
const SHELL = ["/", "/manifest.webmanifest", "/icon-192.png", "/content.json"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  // Réseau d'abord, cache en secours
  e.respondWith(
    fetch(e.request)
      .then((r) => {
        if (r.ok && (SHELL.includes(url.pathname) || e.request.mode === "navigate")) {
          const copy = r.clone();
          caches.open(CACHE).then((c) => c.put(url.pathname === "/" || e.request.mode === "navigate" ? "/" : e.request, copy));
        }
        return r;
      })
      .catch(() => caches.match(e.request.mode === "navigate" ? "/" : e.request))
  );
});
