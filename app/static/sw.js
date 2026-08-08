/* Kwik Klin — minimal app-shell service worker.
   Purpose: fast repeat loads + a working shell when the counter phone drops
   offline for a moment. API calls and the webhook are NEVER intercepted —
   the app's own offline outbox handles unsent messages. */

const CACHE = "kk-shell-v1";
const SHELL = ["/admin/static/app.css", "/admin/static/app.js"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  if (url.origin !== location.origin) return;                 // fonts/CDN: browser cache
  if (url.pathname.startsWith("/admin/api")) return;          // live data, always network
  if (url.pathname.startsWith("/admin/media")) return;        // media: authed, no SW cache
  const isShell =
    url.pathname === "/admin" || url.pathname === "/admin/" ||
    url.pathname.startsWith("/admin/static/");
  if (!isShell) return;

  // network first (so a deploy is picked up immediately), cache as fallback
  e.respondWith(
    fetch(e.request)
      .then((r) => {
        if (r.ok) {
          const copy = r.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
        }
        return r;
      })
      .catch(() =>
        caches.match(e.request, { ignoreSearch: true })
          .then((hit) => hit || caches.match("/admin"))
      )
  );
});
