# Public website (`/laundry`)

Kwik Klin's customer-facing page: pickup booking, rate list, Google reviews,
franchise and CRM plans. Kept apart from the dashboard (`app/static/`) so the
two can change and cache independently.

```
app/site/
├── templates/
│   ├── index.html        HTML shell, rendered by GET /laundry (app/main.py)
│   ├── legal.html        shared shell for company/legal pages
│   └── legal/            page bodies: about, contact, privacy, terms,
│                         refund-policy, shipping-policy → GET /<name>
│                         (Google OAuth + Razorpay website verification)
└── assets/               served at /site/assets/… (1-year immutable cache)
    ├── css/site.css
    ├── js/site.js        loaded with `defer`
    └── img/              logo.png (round brand logo), logo-180.png (favicon/apple icon), og-laundry.jpg (1200×630 social preview)
```

## How rendering works

`GET /laundry` reads `templates/index.html` and fills placeholders:

| Placeholder | Filled by |
|---|---|
| `__BASE__` | request origin (canonical, OG, JSON-LD URLs) |
| `{{ASSET_V}}` | hash of `site.css` + `site.js` — cache-busting `?v=` |
| `{{RATE_TABS}}`, `{{RATE_PANELS}}`, `{{FROM_*}}`, `{{FAQ_*}}`, `{{OFFERS_JSON}}`, `{{PRICE_RANGE_JSON}}`, `{{POPULAR_JSON}}` | `app/services/site_rates.py` — the **home shop's** CRM rate card |
| `{{GBP_SUMMARY}}`, `{{GBP_REVIEWS}}` | `app/services/google_business.py` — synced Google reviews |

Server data that JavaScript needs goes in `<script id="site-data" type="application/json">`,
never inside `site.js`, so the JS file stays static and cacheable.

## Rules

- No inline `style=""` attributes — add a class in `site.css`.
- SEO content (rates, FAQ, JSON-LD) stays in the HTML, not injected by JS.
- Changing CSS/JS needs no manual version bump: `{{ASSET_V}}` changes on restart.
- New legal page: add `templates/legal/<name>.html` and one entry in
  `_LEGAL_PAGES` (app/main.py) — route, sitemap and robots follow automatically.
  Also link it in the footers of `index.html`, `legal.html` and `app/static/join.html`.
- Tests: `tests/test_site.py`, `tests/test_google_business.py`, `tests/test_legal_pages.py`.
