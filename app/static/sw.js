/* Kwik Klin — owner dashboard service worker (served at /admin/sw.js, scope /admin).
 *
 * Purpose: install like an app, fast repeat loads, and a working shell when
 * the counter phone drops offline for a moment.
 *
 *   - Only the SHELL list is cached (HTML/CSS/JS/icons — no customer data).
 *     Every other request goes straight to the browser.
 *   - /admin/api (data, login, payments) and /admin/media (customer photos)
 *     are NEVER intercepted. The app's own outbox (app.js, localStorage)
 *     handles WhatsApp messages typed while offline.
 *   - Network first, so a deploy is picked up immediately; cache only as a
 *     fallback. If the shell was never cached, a small "offline" page.
 */

const PREFIX = "kk-shell-";
const CACHE = PREFIX + "v4";
const PAGE = "/admin";
const SHELL = [
  PAGE,
  "/admin/static/tokens.css",
  "/admin/static/app.css",
  "/admin/static/ui-v2.css",
  "/admin/static/app.js",
  "/admin/static/gbp-poster.js",
  "/admin/static/icons.svg",
  "/admin/manifest.webmanifest",
  "/admin/icon-192.png",
];
const SHELL_SET = new Set(SHELL);

self.addEventListener("install", (e) => {
  // cache: "reload" — skip the browser HTTP cache (static files are immutable for a year)
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(SHELL.map((u) => new Request(u, { cache: "reload" }))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  // Sirf APNE purane cache hatao — isi origin par /staff ka service worker
  // apna "kkstaff-*" rakhta hai, use mitana staff ka offline tod deta tha.
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith(PREFIX) && k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

const OFFLINE_HTML = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kwik Klin — offline</title>
<style>body{margin:0;min-height:100dvh;display:grid;place-content:center;gap:12px;padding:24px;
background:#f4f6fb;color:#0d1526;font:16px/1.45 system-ui,sans-serif;text-align:center}
b{font-size:20px}a{display:inline-block;margin-top:8px;padding:14px 22px;border-radius:12px;
background:#c2410c;color:#fff;font-weight:700;text-decoration:none}</style></head>
<body><b>No internet</b><span>The dashboard needs a connection to load.<br>
Check your signal and try again.</span><a href="/admin">Try again</a></body></html>`;

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

/* Only a complete same-origin 200 goes into the cache — never a 401/500,
   and never the 303 that sends a closed account to /welcome. */
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
  if (url.origin !== self.location.origin) return;            // fonts/CDN: browser cache
  if (url.pathname.startsWith("/admin/api")) return;          // live data, always network
  if (url.pathname.startsWith("/admin/media")) return;        // media: authed, no SW cache

  if (req.mode === "navigate") {
    if (url.pathname !== PAGE && url.pathname !== PAGE + "/") return;
    e.respondWith(
      fetch(req)
        .then((r) => keep(PAGE, r))
        .catch(() => caches.match(PAGE).then((hit) => hit || offlinePage()))
    );
    return;
  }

  if (!SHELL_SET.has(url.pathname)) return;
  // ignoreSearch in effect: shell is keyed without ?v=, the page asks with ?v=<mtime>
  e.respondWith(
    fetch(req)
      .then((r) => keep(url.pathname, r))
      .catch(() => caches.match(url.pathname).then((hit) => hit || Response.error()))
  );
});
