# Kwik Klin — Architecture & Scale Plan

Ye document 3 sawaalon ka jawab hai:
1. **10,000 customers** tak ye system kaise handle karega?
2. **Order/message kabhi lost kyon nahi hoga** (crash-safety guarantees)?
3. **10–15 laundry businesses ko bechna ho** to deployment model kya hai?

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

## 5. 10–15 laundry businesses ko bechna — deployment model

**Recommendation: instance-per-tenant** (har laundry ka apna deployment).
Shared multi-tenant DB abhi galat trade-off hai — wajah:

- Har laundry ka **apna WhatsApp number + WABA + Meta app** hoga hi
  (WhatsApp policy) — sabse bada "tenant isolation" Meta khud force karta hai.
- Data isolation free milta hai: ek client ka data doosre ko dikh hi nahi
  sakta, koi `tenant_id` bug leak nahi kar sakta. High-security requirement
  ke liye ye सबसे strong model hai.
- Ek client ka crash/upgrade doosron ko nahi girata; per-client backup/restore.
- 10–15 clients ke liye ops bilkul manageable hai (ek VM par 3–4 instances
  bhi chal sakte hain — alag port, alag DB, alag `.env`).

**Naya client onboard karne ka checklist** (~1 ghanta):
1. Naya Postgres DB + user banao (`CREATE DATABASE laundry_<client>`).
2. Repo clone → `.env` bharo: us client ka `WHATSAPP_TOKEN`, `PHONE_NUMBER_ID`,
   `WABA`, `APP_SECRET`, naya random `ADMIN_API_KEY`, `DATABASE_URL`, `SHOP_NAME`.
3. `alembic upgrade head` → uvicorn (alag port) → webhook URL Meta mein set.
4. Settings UI se rate card, review links, staff numbers, SLA days.
5. Templates Meta par submit (Template Studio).

Branding/config sab `settings_kv` + `.env` mein hai — code fork karne ki
zaroorat nahi, ek hi repo sab clients ke liye.

**Shared multi-tenant kab sochna**: 50+ clients ya self-serve signup chahiye
tab. Tab plan hai: `tenants` table + har core table par `tenant_id` +
row-level security — lekin wo ek alag project hai, abhi ka nahi.

## 6. Monitoring / ops

- `/health` — liveness + DB check (keepalive isi ko poll karta hai; 503 = DB
  down, app restart NAHI hota, Postgres service start hota hai).
- `webhook_events` / `outbound_queue` mein `status='dead'` rows = manual
  dekhne wali cheez (ab tak ka data kabhi delete nahi hota).
- Owner ko WhatsApp par: daily summary (21:00), escalations, Meta-unblock alerts.
