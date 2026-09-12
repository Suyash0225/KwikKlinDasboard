# Kwik Klin — Architecture & Scale Plan

Ye document 3 sawaalon ka jawab hai:
1. **10,000 customers** tak ye system kaise handle karega?
2. **Order/message kabhi lost kyon nahi hoga** (crash-safety guarantees)?
3. **50–60 laundry businesses** ek instance par kaise alag-alag aur safe rehte hain?

---

## 1. System overview

```
WhatsApp user ──▶ Meta Cloud API ──▶ cloudflared tunnel ──▶ FastAPI (uvicorn)
                                                              │
                     ┌────────────────────────────────────────┼──────────────┐
                     │                                        │              │
               APScheduler                              PostgreSQL 15   Gemini LLM
       (hourly/nightly/durability jobs)                (single source    (AI replies)
                                                        of truth)
```

- Har inbound/outbound message `conversations` mein, har order `orders` +
  `order_status_history` mein. Dashboard sirf API ke through data dekhta hai
  (X-API-Key), HTML/JS/CSS public static hai.

## 2. Durability — "order kabhi lost nahi hota"

Layered guarantees, har layer independent:

| Layer | Mechanism | Kya guarantee karta hai |
|---|---|---|
| Inbound journal | `webhook_events` — raw payload **process hone se PEHLE** commit hota hai; dedup body-hash se | Crash mid-processing? Scheduler har 5 min journal se replay karta hai (5 attempts, phir `dead` + forensics) |
| Outbound queue | `outbound_queue` — network/5xx/429 par send auto-queue; backoff 5 min → 6 h; 8 attempts, phir dead-letter | Meta down ho to bhi customer message eventually jaata hai, kabhi silently drop nahi hota |
| Atomic transitions | Order status + history + SLA date **ek hi commit** mein | Aadha-updated order kabhi nahi ban sakta |
| Idempotency keys | `sent_events` + unclaim-on-permanent-failure | Scheduled message na double jaata hai, na burn hota hai |
| Nightly backup | `pg_dump -Fc` → `backups/` (14 din retention), scheduler 21:30 pe | Disk crash par bhi kal raat tak ka data wapas aata hai |
| Keepalive | Startup script: Postgres service → `alembic upgrade head` → uvicorn → cloudflared, har 5 min health check | Reboot/crash ke baad system khud khada hota hai, migrations ke saath |

**Restore drill** (kabhi zaroorat pade):
```
pg_restore -h localhost -U laundry -d laundry --clean backups\kwikklin-<latest>.dump
```

## 3. Security model

- **Webhook**: Meta ka `X-Hub-Signature-256` HMAC verify hota hai (raw body,
  app secret), galat signature → 403. Token compares sab `hmac.compare_digest`
  (timing-attack safe).
- **Admin API**: `X-API-Key` constant-time compare + per-IP throttle
  (10 galat attempts / 10 min → 429, sahi key bhi block). Fail attempts IP ke
  saath log hote hain.
- **Headers**: `X-Content-Type-Options`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer` (media URLs ke `?key=` leak se bachata hai), HSTS.
- **Secrets**: `.env` git se bahar; `ig_access_token` API responses mein
  masked (`••••••••`) — mask wapas save karne par real token overwrite nahi hota.
- **Audit**: `audit_log` table — admin/agent/scheduler actions.

## 4. 10,000-customer scale — kya kiya, kya headroom hai

Kiya hua (is commit mein):
- **Indexes** har hot path par: `conversations(customer_id, created_at)`,
  `orders(status)`, `orders(created_at)`, `orders(expected_delivery)`,
  `customers(last_message_at)`, escalations FKs, history `changed_at`.
- **Connection pool**: 10 steady + 20 burst, pre-ping, 30-min recycle.
- **Pagination**: inbox threads capped (200 default, `?limit=`),
  customers `limit/offset`, thread messages pehle se capped.
- **GZip**: bade JSON responses ~10x chhote.

Capacity math (is hardware par): 10k customers × ~50 messages =
500k conversation rows — indexed queries ke liye trivial hai; Postgres
millions tak comfortable. Bottleneck DB nahi, **ye Windows machine + tunnel** hai.

Agla kadam jab load real ho (abhi zaroorat NAHI):
1. Cloud VM (Oracle Free / RackNerd) — 100% uptime, tunnel ki jagah real domain + TLS.
2. Uvicorn workers 2–4 (multi-process) — abhi single process kaafi hai.
3. `webhook_events`/`conversations` partitioning — 10M+ rows ke baad hi sochna.

## 5. Multi-tenant — ek instance, 50–60 laundry businesses

> **Purana plan (instance-per-tenant) ab code mein nahi hai.** Ye section
> aaj ke code ka sach hai (Sep 2026). Pehle yahan likha tha "har dukaan ka
> alag deployment, shared DB galat trade-off" — wo self-serve signup se
> pehle ki baat thi. Ab ek instance, ek Postgres, `tenants` table, aur har
> dukaan ka data usi DB mein `tenant_id` se alag.

### Isolation — teen layers, koi ek gire to doosri pakadti hai

| Layer | Kahan | Kya karta hai |
|---|---|---|
| 1. Request context | `app/main.py` middleware → `tenant_context.current_tenant_id` (ContextVar) | Session cookie → user ka tenant; staff cookie → staff ki dukaan; anonymous/API-key → home tenant. Owner ka phone bhi context mein (`manager_phone()`). |
| 2. ORM | `app/database.py` events | Har SELECT par automatic `tenant_id = <ctx>` filter (`with_loader_criteria`), har naye row par automatic stamp (`before_flush`). Developer WHERE bhool bhi jaye to filter lagta hai. |
| 3. Postgres RLS | migrations `d4c8e2f7a915` (19 tables) + `r2a8c5d3f9e7` (4 billing tables) | `ENABLE + FORCE ROW LEVEL SECURITY`, policy `tenant_isolation` — GUC `app.tenant_id` set ho to sirf usi tenant ki rows (read **aur** write); unset = system context, sab rows. Table owner bhi bypass nahi kar sakta. |

**Kaun sa table kahan** — 23 tables tenant-scoped (customers, orders, payments,
staff, tasks, conversations, campaigns, coupons, rate_card, settings_kv,
audit_log, llm_usage, leads, escalations, faq/corrections/doc_chunks,
invoices, billing_events, credit_ledger, recharge_requests, ...). RLS ke
**bahar** jaan-boojh kar: `tenants`, `users`, `invites`, `login_sessions`
(login ke waqt tenant pata hi nahi hota — email se lookup cross-tenant hai),
`sent_events` (global idempotency; scheduler tenant prefix lagata hai),
`kpi_snapshots` (platform KPI), `webhook_events`/`outbound_queue`
(durability — apna tenant_id carry karte hain).

**System context** (ContextVar = None, GUC unset, RLS pass-through) sirf in
raaston par: `/control/*` (vendor panel), `/webhooks/razorpay`, alembic,
pg_dump/backup, aur scheduler ka platform-level hissa. Scheduler ka
dukaan-level kaam (standup, reminders, summary, nudges, segments)
`_for_each_tenant()` se har chalu dukaan ke liye alag `as_tenant()` block
mein chalta hai — apna data, apna WhatsApp number, apna malik.

### Per-tenant cheezein

- **WhatsApp**: har dukaan ka apna `wa_phone_number_id` + `wa_token`
  (`/api/whatsapp/connect`, Graph-validated). Inbound webhook
  `metadata.phone_number_id` se tenant tay karta hai. Token DB mein
  **encrypted** (`app/services/secrets.py`, Fernet, key `TOKEN_ENCRYPTION_KEY`).
  `.env` wale creds **sirf home tenant** ke — doosri dukaan bina connect ke
  bheje to `SendError("not connected")`, chupke se home ke number se nahi.
- **Plans/limits/credits**: `services/plans.py`, `tenants.limit_overrides`,
  `credit_ledger`; control panel se badalte hain. Per-tenant rate limit
  middleware mein (`RATE_LIMIT_PER_MIN`).
- **Staff**: `staff.phone` per-tenant unique — wahi number do dukaanon mein
  alag aadmi ho sakta hai. Staff panel ka tenant sirf `kk_staff` cookie se.
- **Owner ka number**: `tenants.owner_phone` → `tenant_context.manager_phone()`.
  `settings.MANAGER_PHONE` ab sirf fallback hai (home / system context).

### Keys — do alag

- `ADMIN_API_KEY` — **dukaan** ka: home tenant ka dashboard/API tooling.
- `VENDOR_API_KEY` — **platform** ka: `/control`, vendor session cookie ka
  HMAC. Set ho to `ADMIN_API_KEY` control par nahi chalta aur ulta bhi.
  Per-admin keys (`admin_keys` table, read < write < danger) bhi hain.
- Startup par `_multi_tenant_hardening_check()`: 2+ active tenant aur
  encryption key / vendor key / placeholder key ki kami ho to ERROR log,
  har restart par.

### "Home tenant" — kya bacha hai aur kyun

Home = is deployment ki apni dukaan (`settings_kv.home_tenant_slug`, warna
sabse purana tenant). Sirf teen jagah matlab rakhta hai: `.env` WhatsApp
creds ka fallback, anonymous/`ADMIN_API_KEY` request ka context, Meta
block watcher. Ye single-shop deploy ki backward-compat hai — 60 dukaanon
mein home bhi bas ek tenant hai, koi special access nahi.

### Naya vendor onboard — ab 2 minute, code/deploy nahi

Self-serve signup (`/welcome`) ya control panel se tenant banao → owner
invite → `/welcome` par WhatsApp connect (apna WABA/number/token) → rate
card → plan control se. Koi naya process, DB ya `.env` nahi.

### Abhi bhi karne wala (50+ par)

- 2–4 uvicorn workers + pool sizing (ek process, noisy neighbour).
- Per-tenant export/delete (data portability; churn par maangenge).
- `users`/`invites` par app-level filter hi hai — RLS nahi lag sakta.
- Purani single-shop scripts (`seed_staff`, `bootstrap_home_tenant`) home
  par hi likhti hain.

## 6. Monitoring / ops

- `/health` — liveness + DB check (keepalive isi ko poll karta hai; 503 = DB
  down, app restart NAHI hota, Postgres service start hota hai).
- `webhook_events` / `outbound_queue` mein `status='dead'` rows = manual
  dekhne wali cheez (ab tak ka data kabhi delete nahi hota).
- Owner ko WhatsApp par: daily summary (21:00), escalations, Meta-unblock alerts.
