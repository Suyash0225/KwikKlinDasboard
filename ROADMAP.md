# ROADMAP — Full CRM vision (north star)

> Owner ne 2026-08-02 ko ek detailed CRM spec diya. Owner ki turant zaroorat:
> **(1) chat dekh sakun, (2) khud message kar sakun, (3) dashboard se data
> samjhun, (4) modern SaaS-jaisa UI.** Baaki modules yahan parked hain —
> priorities owner tay karega. Stack FIXED hai: FastAPI + Postgres
> (PROJECT_SPEC.md) — koi rewrite nahi.

## Ban chuka (2026-08-02 tak)
- Orders: state machine + audit history, auto number `KK-YYYYMMDD-NN`,
  payments (derived), internal-only notes, milestone WhatsApp notifications
- Webhook: idempotent (wa_message_id dedup), signature-verified, rule-based
  status replies; DotPe adapter + provider switch
- Admin API (X-API-Key) + dashboard: orders/filters/actions, naya-order form,
  customers, baat-cheet log
- **Inbox v1** (isi commit mein): thread list + chat view + manual send +
  24h window indicator + bot/manager labels

## Agla (chhote, kaam ke tukde — owner priority se)
1. Rate card (service × garment, editable) + order form auto-fill
2. Extended statuses map (Pickup Requested/Assigned, QC, Re-wash, Damaged/
   claim, Delivery Failed, Walk-in pickup) — enum migration + transitions
3. Staff WhatsApp commands (`KK-... ready`, `mera kaam`) + role permissions
   — Phase 3.5 ka core
4. Customer 360: tags (Naya/Regular/VIP/Blacklist), preferences (starch,
   separate wash...), multiple addresses, udhaar/ledger
5. Bot control panel: keyword→reply table, FAQ KB, escalation rules
   ("Needs Human"), bot ON/OFF per conversation, test console
6. Dashboard home KPIs: aaj ka collection, udhaar, late orders, 7/30-din
   revenue chart
7. Pre-existing damage photos on orders (media storage)
8. Proper login (password) — X-API-Key sirf local dev ke liye hai

## Scale-engineering — TABHI jab zaroorat ho
(single shop ~200-500 msg/din pe ye overkill hai; SaaS/multi-shop hua to karo)
- messages month-wise partitioning / archive jobs
- WebSockets push (abhi: 10s delta polling kaafi)
- Outbound queue w/ retry+dead-letter (abhi: inline + 1 retry)
- Frontend virtualization (react-window jaisa)
- 100k-message seed benchmarks + EXPLAIN ANALYZE targets

## Already-followed principles (spec se match)
- Keyset pagination (OFFSET nahi), indexes on hot paths
- Status events out-of-order safe (rank: sent < delivered < read) — TODO
  jab statuses store karne lagein (abhi log-only)
- Webhook: 200 turant, processing crash-safe; sab transactional
