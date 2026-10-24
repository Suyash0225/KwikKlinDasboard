/* Kwik Klin — staff app service worker (served at /staff/sw.js, scope /staff).
 *
 * Sirf itna ki app install ho sake aur bina signal ke bhi KHUL jaye.
 * Kaam ka data JAAN-BOOJH KAR cache nahi hota: purana order dikhana kisi
 * ko galat ghar bhej sakta hai, aur shared phone par grahak ka naam/pata
 * phone mein pada nahi rehna chahiye.
 *
 *   - Cache mein sirf SHELL list ki files (HTML/CSS/JS/icons — kisi grahak
 *     ka kuch nahi). Baaki har request browser ke paas seedhi jaati hai.
 *   - /staff/api/* (login, cookie, orders, paisa) kabhi intercept nahi hota.
 *   - Network pehle: deploy turant pahunche. Network na ho tabhi cache.
 *   - Shell bhi cache mein na ho to ek chhota "offline" page.
 *
 * Bina signal ke STATUS dabana staff.js ka outbox sambhalta hai — yahan
 * koi request queue nahi hai.
 */

const PREFIX = "kkstaff-";
const CACHE = PREFIX + "v4";
const PAGE = "/staff";
const SHELL = [
  PAGE,
  "/admin/static/tokens.css",
  "/admin/static/staff.css",
  "/admin/static/staff-ui2.css",
  "/admin/static/staff.js",
  "/admin/static/icons.svg",
  "/staff/manifest.webmanifest",
  "/staff/icon.svg",
  "/staff/icon-192.png",
];
const SHELL_SET = new Set(SHELL);

self.addEventListener("install", (e) => {
  // cache: "reload" — browser ka HTTP cache beech mein na aaye
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(SHELL.map((u) => new Request(u, { cache: "reload" }))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  // Sirf APNE purane cache hatao. Isi origin par /admin ka service worker
  // apna "kk-shell-*" rakhta hai — use chhuna uska offline tod deta tha.
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k.startsWith(PREFIX) && k !== CACHE).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

const OFFLINE_HTML = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kwik Klin Staff — offline</title>
<style>body{margin:0;min-height:100dvh;display:grid;place-content:center;gap:12px;padding:24px;
background:#131820;color:#fff;font:16px/1.45 system-ui,sans-serif;text-align:center}
b{font-size:20px}a{display:inline-block;margin-top:8px;padding:14px 22px;border-radius:12px;
background:#ea580c;color:#fff;font-weight:700;text-decoration:none}</style></head>
<body><b>No internet</b><span>Kwik Klin Staff needs a connection to load your work.<br>
Check your signal and try again.</span><a href="/staff">Try again</a></body></html>`;

function offlinePage() {
  return new Response(OFFLINE_HTML, {
    status: 503,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
      "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'",
    },
  });
}

/* Sirf poora, apne origin ka 200 jawab cache mein jaata hai — 401/500
   ya redirect kabhi "shell" banke offline par na dikhe. */
function keep(key, res) {
  if (res && res.ok && res.type === "basic" && !res.redirected) {
    const copy = res.clone();
    caches.open(CACHE).then((c) => c.put(key, copy));
  }
  return res;
}

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/staff/api")) return;      // live data: hamesha network, kabhi cache nahi

  if (req.mode === "navigate") {
    if (url.pathname !== PAGE) return;
    e.respondWith(
      fetch(req)
        .then((r) => keep(PAGE, r))
        .catch(() => caches.match(PAGE).then((hit) => hit || offlinePage()))
    );
    return;
  }

  if (!SHELL_SET.has(url.pathname)) return;
  // ignoreSearch: shell bina ?v= ke cache hota hai, page ?v= ke saath maangta hai
  e.respondWith(
    fetch(req)
      .then((r) => keep(url.pathname, r))
      .catch(() => caches.match(url.pathname).then((hit) => hit || Response.error()))
  );
});

/* Web Push (services/web_push.py): app band ho tab bhi notification.
   Tap = khula hua staff app aage lao, warna naya kholo. */
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (x) { d = { body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || "Kwik Klin", {
    body: d.body || "",
    tag: d.tag || undefined,
    renotify: !!d.tag,
    icon: "/staff/icon-192.png",
    badge: "/staff/badge-96.png",
    data: { url: d.url || "/staff" },
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/staff";
  e.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then((ws) => {
    for (const w of ws) { if (new URL(w.url).pathname.startsWith("/staff")) return w.focus(); }
    return clients.openWindow(url);
  }));
});
