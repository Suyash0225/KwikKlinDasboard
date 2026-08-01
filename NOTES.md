# NOTES

Har session ke end mein 2 line likho. Agle din context wapas load karne mein
20 minute bachenge.

---

## Current position

**Phase:** 1 — Foundation + Database
**Next step:** Section 0 paste karna hai, phir Phase 1 group (a)

---

## Daily log

### 2026-08-01
- Spec finalize kiya (payments + staff tables add kiye)
- Build order decide: 1 → 2 → 3 → 3.5 → 5 → 4 → 6 → 7
- Kal: Phase 1 (a) — requirements, config, docker-compose, .env.example

---

## Parked — baad mein, abhi nahi

Ye sab discuss ho chuka hai. Build order mat badlo, bas yahan likha rehne do.

**Phase 3.5 — Staff layer**
- `staff` table already Phase 1 mein hai
- Washer/delivery ko interactive buttons bhejna
- Button callback se status update
- Manager apne number se order create kar sake

**Phase 4 — AI layer**
- Customer ka free-text sawaal → tool use agent
- Staff ka delay reason → Haiku extraction → structured JSON
  - Internal reason `orders.notes` mein, customer ko NEVER
  - Nayi date pehle DB mein likho, tab message bhejo
- Bill ki photo → vision → editable confirm loop → order create
  - Sirf manager ke number se aayi images process karo
  - Confirm loop mein "Badalna hai" ka option zaroori (handwriting risk)
- Manager mode: read tools free, write tools sirf button confirm ke baad

**Phase 7 — Dashboard**
- Read-write, par koi AI nahi — seedha form save
- Password login chahiye (X-API-Key browser ke liye kaafi nahi)
- Phase 3 ka admin API hi backend hai, kuch naya nahi banana

---

## Decisions log

Kyun kiya, taaki baad mein khud se sawaal na karo.

- **Phase 5 se pehle Phase 4 nahi** — 90% system AI ke bina chalta hai, shop ko
  jaldi faayda mile
- **Buttons first, AI last** — button mein order ID attached hoti hai, zero
  ambiguity, zero AI cost, zero galti
- **Docker optional** — 12GB RAM hai to Docker theek hai, par native Postgres bhi
  chalega, `DATABASE_URL` same rehta hai
- **LLM client wrap** — `llm_client.py` ke bahar `anthropic` import nahi, taaki
  baad mein provider switch ek file ka kaam ho
- **No web dashboard till Phase 7** — WhatsApp hi UI hai, staff browser nahi
  kholega

---

## Blockers / doubts

(jo atke, yahan likho)
