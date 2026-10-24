# Kwik Klin — installable apps (PWA)

Two apps install on a phone: the **staff app** (`/staff`, "Kwik Klin") and
the **owner dashboard** (`/admin`, "KK Owner"). They have different icons
(staff: dark frame, owner: solid orange), so both can sit on one phone without
being mixed up. Most of this page covers the staff app; the owner dashboard
has its own section below.

## Staff app

The staff panel at `/staff` installs on a phone like an app ("Kwik Klin
Staff"). There is no build step: it is plain HTML/JS/CSS served by FastAPI,
same as the owner dashboard.

## What is where

| Piece | File | Served at |
|---|---|---|
| Page (app shell) | `app/static/staff.html` | `/staff` (no-store, strict CSP, `?v=<mtime>` on assets) |
| App code | `app/static/staff.js`, `staff.css`, `staff-ui2.css`, `tokens.css` | `/admin/static/…` |
| Manifest | `app/static/staff-manifest.json` | `/staff/manifest.webmanifest` |
| Service worker | `app/static/staff-sw.js` | `/staff/sw.js` (`no-cache`, `Service-Worker-Allowed: /staff`) |
| Icons | `app/static/staff-*.png` (made by `scripts/make_app_icons.py`) | `/staff/icon-192.png`, `/staff/icon-512.png`, `/staff/icon-maskable-512.png`, `/staff/apple-touch-icon.png`, `/staff/badge-96.png`, `/staff/icon.svg` |
| Staff API | `app/routers/staff_panel.py` | `/staff/api/*` (httpOnly `kk_staff` cookie) |

Access control is entirely server-side: tenant comes from the staff
token, the role decides what each endpoint returns (washers never get
amounts or customer contact; customer numbers are masked), and the plan
decides which features are on. The UI only mirrors `/staff/api/me`.

## Service worker rules

- **Scope is `/staff`** (no trailing slash). The default scope for
  `/staff/sw.js` would be `/staff/`, which does not cover the `/staff` page,
  so the old worker never controlled the app (no offline shell, and
  `navigator.serviceWorker.ready` never resolved). `staff.js` registers
  with `{ scope: "/staff" }` and unregisters the stale `/staff/` one.
- **Only an allowlist is cached** (`SHELL` in `staff-sw.js`): the page,
  CSS, JS, icon sprite, manifest and icons. Nothing with customer data.
- **`/staff/api/*` is never intercepted**: login, cookies, orders,
  payments and tokens always go straight to the network and are never
  written to Cache Storage.
- Network first, cache only as a fallback, and only complete same-origin
  `200` responses are stored. If nothing is cached and there is no network,
  a small built-in "No internet" page is shown.
- Each worker deletes only its **own** old caches (`kkstaff-*` for staff,
  `kk-shell-*` for `/admin/sw.js`). Before, each one wiped the other's.
- Push notifications (`services/web_push.py`) are handled in the same worker.

### Offline behaviour (what is and isn't promised)

- With no signal, the installed app still **opens** (cached shell) and shows
  "Can't reach Kwik Klin… it will try again by itself" instead of a
  login form. It retries on the `online` event, every 20 s, or when Retry is tapped.
- No order data is shown offline. That's on purpose: a stale order could
  send someone to the wrong house.
- The existing **status outbox** in `staff.js` (localStorage, per staff
  member) still queues *status-only* taps (picked up, washing, ready,
  delivered, not home) and resends them when the signal returns. If the
  order has already moved on, the server answers 409 and that counts as done.
  **Cash collection is deliberately not queued**, because a lost reply plus a retry
  would record the money twice. Nothing else works offline.

## Installing on a phone

HTTPS is required (service workers only run on HTTPS or `localhost`).

- **Android / Chrome, Edge, Samsung Internet:** open `https://<domain>/staff`.
  An "Install this on your phone like an app" bar appears. Tap **Install**.
  (Or use the browser menu → *Install app* / *Add to Home screen*.)
- **iPhone / iPad (Safari):** Safari has no install prompt. The app shows a
  hint once: tap **Share ⬆ → Add to Home Screen**. Push notifications on
  iOS work only after it is added to the Home Screen (iOS 16.4+).

## Test locally

```bash
# JS syntax (same as CI)
node --check app/static/staff.js && node --check app/static/staff-sw.js && node --check app/static/sw.js

# PWA tests + staff panel tests (needs TEST_DATABASE_URL, see README "Run tests")
python -m pytest tests/test_staff_pwa.py tests/test_admin_pwa.py tests/test_staff_panel.py -q

# Run the app and open http://localhost:8000/staff
# (localhost counts as secure, so the service worker registers)
./run.sh
```

In Chrome DevTools → **Application**:
- *Manifest*: name "Kwik Klin Staff", icons 192/512/maskable, no errors.
- *Service workers*: `/staff/sw.js`, scope `http://localhost:8000/staff`, activated.
- *Cache storage → kkstaff-v4*: only the shell files, no `/staff/api` entries.
- *Network → Offline* then reload: the app opens and shows the offline message.
- Device toolbar at 320 / 375 / 768 px: no horizontal scrolling.

## Deploying

No new services, env vars or secrets. Deploy as usual (`scripts/deploy.sh`).
The nginx config in `DEPLOY.md` needs no change: `/staff/*` and the `/admin` manifest, icons and SW
goes through the app, and the SW precaches with `cache: "reload"`, so the
1-year `immutable` cache on `/admin/static/` can't feed it stale files.

After deploy, already-installed phones pick up the new worker on their next
visit (`no-cache` on both `sw.js`, `skipWaiting` + `clients.claim`).

To change the icon: edit `scripts/make_app_icons.py`, run
`python -m scripts.make_app_icons`, commit the PNGs.

## Owner dashboard (`/admin`)

| Piece | File | Served at |
|---|---|---|
| Page | `app/static/dashboard.html` | `/admin` (`?v=<mtime>` on app.js, app.css, ui-v2.css, tokens.css, gbp-poster.js, icons.svg) |
| Manifest | `app/static/manifest.json` | `/admin/manifest.webmanifest` (1 h cache, **not** `/admin/static/`, which is immutable for a year) |
| Service worker | `app/static/sw.js` | `/admin/sw.js` (`no-cache`, `Service-Worker-Allowed: /admin`), cache `kk-shell-v4` |
| Icons | `app/static/admin-*.png` | `/admin/icon-192.png`, `/admin/icon-512.png`, `/admin/icon-maskable-512.png`, `/admin/apple-touch-icon.png` |

The rules are the same as the staff worker: a fixed allowlist of shell files,
`/admin/api` and `/admin/media` are never intercepted, only complete
same-origin 200s are cached (never the 303 to `/welcome` for a closed
account), and there's a built-in "No internet" page if nothing is cached.

- **Install:** an "Install Kwik Klin on this phone" bar appears at the top
  when the browser offers it (Chrome/Edge/Samsung). On iPhone it shows the
  Share → Add to Home Screen steps instead. ✕ hides the bar on that phone;
  the sidebar keeps a "📲 Install app on this phone" button.
- **Offline:** the shell opens, the "No internet" strip shows, and the owner
  stays signed in (no jump to the login page). A 5xx from `/api/me`, which happens
  while nginx/uvicorn restarts during a deploy, also counts as "no signal"
  now, not as "logged out". WhatsApp messages typed offline go to the
  existing outbox (`kk_outbox`) and send when the signal returns.
- **Phone inputs:** on touch screens every input is 16px (`ui-v2.css`),
  because iPhone Safari zooms the page when you tap a smaller one.

## Known limitations
- No offline viewing of orders and no offline cash entry (see above).
- iOS gives no install event, so the hint is static text. The browser cannot
  tell whether it was followed, except that the hint hides inside the installed app.
- The headless check used for this change covered Chromium only. Check on a
  real iPhone (Safari) and an Android phone before relying on install/push.
