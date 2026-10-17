# Kwik Klin — Architecture & Scale Plan

Ye document 3 sawaalon ka jawab hai:
1. **10,000 customers** tak ye system kaise handle karega?
2. **Order/message kabhi lost kyon nahi hoga** (crash-safety guarantees)?
3. **50–60 laundry businesses** ek instance par kaise alag-alag aur safe rehte hain?

---

## 1. System overview

**Production:** WhatsApp/owner browser → HTTPS on `kwikklin.online` → nginx
(reverse proxy) → FastAPI/uvicorn on Ubuntu EC2. FastAPI connects to PostgreSQL;
scheduled jobs use the same database and a PostgreSQL advisory lock elects one
scheduler leader across web workers.

**Local development only:** Meta webhooks can reach a laptop through an optional
`cloudflared` quick tunnel. That tunnel is not the production ingress. See
[README.md](README.md#webhook-local-development) for the dev setup and
[docs/DEPLOY.md](docs/DEPLOY.md) for the production topology.

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
| Nightly backup | `pg_dump -Fc` → `backups/` (14 din retention); optional S3-compatible off-host upload | Local dump stays available if upload is disabled/fails; upload failures alert owner/admin |
| Process supervision | Server par systemd/docker restart policy; `/health` liveness | Reboot/crash ke baad app khud khadi hoti hai |

**Restore drill** (kabhi zaroorat pade):
```
pg_restore -h localhost -U laundry -d laundry --clean backups\kwikklin-<latest>.dump
```

## 3. Security model

- **Webhook**: Meta ka `X-Hub-Signature-256` HMAC verify hota hai (raw body,
  app secret), galat signature → 403. Token compares sab `hmac.compare_digest`
  (timing-attack safe).
- **Admin API**: `X-API-Key` constant-time compare + rate limiting. Client IP
  forwarding is accepted only from configured `TRUSTED_PROXIES`; nginx must
  overwrite forwarding headers and untrusted peers cannot spoof their IP.
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

Capacity depends on query patterns, message volume, LLM latency, EC2 sizing,
PostgreSQL I/O, and connection-pool limits—not just customer count. Indexed
500k-row conversation workloads are a reasonable planning target, not a
performance guarantee; measure production latency and database load before
choosing a worker or instance size.

Production currently uses Ubuntu EC2 + nginx + systemd; the cloudflared tunnel
is a local-development convenience, not a production bottleneck. The scheduler
uses PostgreSQL `pg_try_advisory_lock` leader election, so only one worker
runs scheduled jobs and another can take over after the lock connection is
lost. Scale workers only after load testing and pool sizing. Consider table
partitioning only when measured query/maintenance needs justify it.

## 5. Multi-tenant — ek instance, 50–60 laundry businesses

> **Purana plan (instance-per-tenant) ab code mein nahi hai.** Ye section
> aaj ke code ka sach hai (Sep 2026). Pehle yahan likha tha "har dukaan ka
> alag deployment, shared DB galat trade-off" — wo self-serve signup se
> pehle ki baat thi. Ab ek instance, ek Postgres, `tenants` table, aur har
> dukaan ka data usi DB mein `tenant_id` se alag.

### Isolation — teen layers, koi ek gire to doosri pakadti hai

| Layer | Kahan | Kya karta hai |
|---|---|---|
| 1. Request context | `app/main.py` middleware → `tenant_context.current_tenant_id` (ContextVar) | Valid session → us user ka tenant; staff cookie → staff ki dukaan; anonymous/no-session → no tenant (fail closed); only a valid `ADMIN_API_KEY` on routes that accept it may select the home tenant. Public bill/token routes resolve tenant from the token. |
| 2. ORM | `app/database.py` events | Tenant selected ho to automatic `tenant_id = <ctx>` filter and row stamping. No tenant + no explicit trusted system context means tenant-scoped ORM reads return no rows and new rows are not silently stamped into the home tenant. |
| 3. Postgres RLS | Original tenant policies plus `20261017_rls_fail_closed` | `ENABLE + FORCE ROW LEVEL SECURITY`; rows are visible/writable only when `app.tenant_id` is non-empty and matches `tenant_id`, or trusted code explicitly sets `app.system_context='on'`. Missing/empty tenant without that grant returns no rows. Table owners do not bypass FORCE RLS; superuser/BYPASSRLS roles still must not be used by the runtime app. |

**Kaun sa table kahan** — 23 tables tenant-scoped (customers, orders, payments,
staff, tasks, conversations, campaigns, coupons, rate_card, settings_kv,
audit_log, llm_usage, leads, escalations, faq/corrections/doc_chunks,
invoices, billing_events, credit_ledger, recharge_requests, ...). RLS ke
**bahar** jaan-boojh kar: `tenants`, `users`, `invites`, `login_sessions`
(login ke waqt tenant pata hi nahi hota — email se lookup cross-tenant hai),
`sent_events` (global idempotency; scheduler tenant prefix lagata hai),
`kpi_snapshots` (platform KPI), `webhook_events`/`outbound_queue`
(durability — apna tenant_id carry karte hain).

Account lifecycle ke paanch kaam (signup, login, google callback, invite
accept, session adopt) `tenant_context.system_context()` mein chalte hain:
unmein dukaan pata hi nahi hoti jab tak user/token resolve na ho jaye.
Ye zaroori tha users/invites par RLS lagane se pehle — warna doosri
dukaan ka owner apne hi account se login nahi kar pata.

**System context** is not implied by `current_tenant_id=None` or an unset GUC.
RLS requires an explicit `app.system_context='on'` grant; otherwise a missing
tenant sees zero tenant-scoped rows. Trusted cross-tenant operations opt in
through `tenant_context.system_context(db)` / `as_system_context()`, or a
separate one-shot backup/restore process with the documented system GUC.
Current platform paths include `/control/*`, `/webhooks/razorpay`, account
lifecycle lookups, and scheduler platform-level work. Scheduler shop-level
work runs inside a separate `as_tenant()` block for each active shop.

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
sabse purana tenant). Iska use `.env` WhatsApp credentials ke fallback,
Meta block watcher, aur sirf valid `ADMIN_API_KEY` wali protected request
mein hota hai. Anonymous/no-session requests ko home tenant nahi milta.
Home tenant backward compatibility hai, anonymous access grant nahi.

### Naya vendor onboard — ab 2 minute, code/deploy nahi

Self-serve signup (`/welcome`) ya control panel se tenant banao → owner
invite → `/welcome` par WhatsApp connect (apna WABA/number/token) → rate
card → plan control se. Koi naya process, DB ya `.env` nahi.

### Abhi bhi karne wala (50+ par)

- 2–4 uvicorn workers + pool sizing (ek process, noisy neighbour).
- Per-tenant export/delete (data portability; churn par maangenge).
- Purani single-shop scripts (`seed_staff`, `bootstrap_home_tenant`) home
  par hi likhti hain.

## 6. Monitoring / ops

- `/health` — liveness + DB check (process supervisor/uptime check isi ko poll karta hai; 503 = DB
  down, app restart NAHI hota, Postgres service start hota hai).
- `webhook_events` / `outbound_queue` mein `status='dead'` rows = manual
  dekhne wali cheez (ab tak ka data kabhi delete nahi hota).
- Owner ko WhatsApp par: daily summary (21:00), escalations, Meta-unblock alerts.
