# PROJECT_SPEC.md — Laundry WhatsApp Bot

> Keep this file in the repo root. The coding agent should re-read it whenever
> it needs context. Paste SECTION 0 first, let the agent acknowledge it, then
> paste PHASE 1. Do not paste later phases until the current one runs and is
> committed.

---

# SECTION 0 — MASTER CONTEXT (paste this first)

You are helping me build a production-ready WhatsApp-based order management
system for a laundry business that I own and operate in Varanasi, India.

I am a QA/automation engineer comfortable with Python, so write code I can read
and debug — not clever code. I work a full-time job and build this 1–2 hours a
day, so favour small, complete, testable increments over large sweeps.

This system will run on my real shop with real customers and real staff. Treat
correctness and safety as more important than features.

## GROUND RULES (follow these for every response)

1. Build ONLY what the current phase asks. Do not scaffold future features.
2. Prefer boring, explicit code over abstractions. No design patterns unless I ask.
3. Every function that touches an external service (WhatsApp, LLM, DB) must have
   error handling and a log line.
4. No secrets in code. Everything via environment variables, loaded with
   pydantic-settings.
5. After each file you create, tell me in 2 lines: what it does and how I test it
   manually.
6. If a requirement is ambiguous, ASK me. Do not assume and build.
7. Write type hints on all function signatures.
8. Use async/await for all I/O (FastAPI, httpx, asyncpg).
9. Wrap the LLM client in `app/services/llm_client.py`. No other file may import
   the `anthropic` package directly.

## TECH STACK (fixed — do not substitute)

- Language: Python 3.11+
- API framework: FastAPI
- Server: uvicorn
- Database: PostgreSQL 15+
- ORM: SQLAlchemy 2.0 (async) + Alembic for migrations
- Validation: Pydantic v2
- HTTP client: httpx (async)
- Scheduler: APScheduler
- LLM: Anthropic Claude API (Sonnet for reasoning, Haiku for cheap
  classification and extraction)
- Messaging: WhatsApp Cloud API (Meta Graph API v21.0+) — direct, no BSP wrapper
- Testing: pytest + pytest-asyncio + httpx AsyncClient
- Config: python-dotenv + pydantic-settings
- Logging: structlog (JSON logs)

## BUSINESS DOMAIN

A single laundry shop. Everything happens over WhatsApp — nobody uses a web
dashboard. The system coordinates THREE groups of people:

**CUSTOMER** — messages the business WhatsApp number.
- Asks about order status
- Places new order requests
- Receives status updates, delay notices, and periodic follow-ups

**WASHER (staff)** — the person who actually washes/dries/irons.
- Receives a daily morning message listing orders pending with him
- Replies via interactive buttons (Done / Will finish today / Problem)
- Can reply in free text when buttons don't fit

**DELIVERY (staff)** — the person who picks up and delivers.
- Gets assigned READY orders
- Confirms via buttons
- Confirms delivery completion

**MANAGER** — me. Receives escalations and anything the bot cannot handle.

The bot sits in the middle. When the washer marks an order done, the customer
gets told. When the washer reports a delay, the customer gets a polite delay
notice. I should be able to be at my job all day and have this run itself.

## ORDER LIFECYCLE (status enum, in this exact order)

```
RECEIVED -> IN_WASH -> IN_DRY -> IN_IRON -> READY -> OUT_FOR_DELIVERY -> DELIVERED
```

Plus terminal states: `CANCELLED`, `ON_HOLD`

## CORE DESIGN PRINCIPLES (non-negotiable)

**1. Buttons first, free text second, AI last.**
Staff interactions must use WhatsApp interactive buttons wherever possible.
A button carries the order ID with it, so there is zero ambiguity about which
order the reply refers to. Only fall back to free text (and therefore AI) when
the answer genuinely doesn't fit a button.

**2. The LLM decides WHAT, deterministic Python DOES it.**
The LLM may never invent an order status, price, or date. It may only report
what a tool returned, or convert a human's free text into structured data that
Python then validates and writes.

**3. Internal problems stay internal.**
If the washer says "paani nahi aaya" (no water supply), the customer must NEVER
see that reason. Store it in `orders.notes` for me. The customer sees only a
polite delay notice and a revised date.

**4. Never promise a date that is not in the database.**
A revised delivery date must be written to `orders.expected_delivery` BEFORE any
message quoting that date is sent. No message may contain a date that came only
from an LLM's output.

**5. Degrade, never go silent.**
If the AI is unavailable, fall back to rule-based replies. If a message cannot
be handled, escalate to the manager. A customer must never receive silence or a
raw error.

## CRITICAL WHATSAPP CONSTRAINT (do not violate)

WhatsApp Cloud API has a 24-hour customer service window:
- Within 24h of that person's last message → free-form text allowed, and free.
- Outside 24h → ONLY pre-approved template messages allowed, and they cost money.

So: every outbound message must go through a single function that checks the
window and picks free-form vs template. Never call the send API directly from
business logic.

All proactive messages (daily staff check-ins, follow-ups, status updates,
delay notices) must assume they are outside the window and use templates.

This applies to STAFF as well as customers. Staff are ordinary WhatsApp users to
Meta — the daily morning message to the washer is a template.

## PROJECT STRUCTURE

```
laundry-bot/
├── app/
│   ├── __init__.py
│   ├── main.py                  FastAPI app entry
│   ├── config.py                settings via pydantic-settings
│   ├── database.py              async engine + session factory
│   ├── models/                  SQLAlchemy models
│   ├── schemas/                 Pydantic request/response models
│   ├── routers/
│   │   ├── webhook.py           WhatsApp webhook (GET verify + POST receive)
│   │   ├── orders.py            internal CRUD API
│   │   └── admin.py             admin/staff endpoints
│   ├── services/
│   │   ├── whatsapp.py          send/receive WhatsApp messages
│   │   ├── llm_client.py        the ONLY module importing the anthropic SDK
│   │   ├── ai_agent.py          LLM orchestration
│   │   ├── intent.py            intent classification
│   │   ├── order_service.py     order business logic
│   │   ├── staff_service.py     staff assignment + staff reply handling
│   │   ├── messages.py          ALL human-facing strings (English + Hinglish)
│   │   ├── templates.py         WhatsApp template registry
│   │   └── escalation.py        manager escalation logic
│   ├── jobs/
│   │   └── scheduler.py         daily check-ins, follow-ups, reminders
│   └── utils/
│       ├── logger.py            structlog config
│       └── phone.py             phone normalization (E.164)
├── alembic/
├── tests/
├── scripts/
├── .env.example
├── requirements.txt
├── docker-compose.yml
├── README.md
└── PROJECT_SPEC.md              this file
```

## LANGUAGE

Customers and staff write in English, Hindi, and Hinglish, often mixed and often
misspelled. All human-facing strings live in `app/services/messages.py`, keyed by
language, so I can edit copy without touching logic. Replies should match the
language the person used.

## BUILD ORDER

I am building in this order. Do not jump ahead.

| Phase | What | Needs AI? |
|-------|------|-----------|
| 1 | Foundation + database | No |
| 2 | WhatsApp send/receive | No |
| 3 | Orders, status lifecycle, customer notifications | No |
| 3.5 | Staff roles, assignment, button-based staff replies | No |
| 5 | Scheduler: daily staff check-in, follow-ups, reminders | No |
| 4 | AI layer: customer free-text + staff delay extraction | Yes |
| 6 | Hardening and deploy | No |
| 7 | Owner dashboard (read-only web page over the admin API) | No |

Note the order: Phase 5 comes before Phase 4. Most of this system works without
any AI at all, and I want the shop benefiting before I add the AI layer.

Acknowledge these rules, then wait for me to give you Phase 1.

---

# PHASE 1 — FOUNDATION + DATABASE (paste this second)

PHASE 1: Project skeleton and data layer. No WhatsApp, no AI, no scheduler yet.

Build:

**1.** Project structure exactly as defined above. Empty modules are fine —
create the files with a docstring and nothing else where the phase doesn't need
them yet.

**2.** `requirements.txt` with pinned versions for the stack listed above.

**3.** `app/config.py` — Settings class using pydantic-settings reading from
`.env`:

```
DATABASE_URL
WHATSAPP_TOKEN
WHATSAPP_PHONE_NUMBER_ID
WHATSAPP_VERIFY_TOKEN
WHATSAPP_APP_SECRET
ANTHROPIC_API_KEY
MANAGER_PHONE
ADMIN_API_KEY
SHOP_NAME
DEFAULT_COUNTRY_CODE       (default "91")
QUIET_HOURS_START          (default 21)
QUIET_HOURS_END            (default 9)
FOLLOWUP_DAYS              (default 14)
ENVIRONMENT
LOG_LEVEL
```

Also create `.env.example` with all keys and dummy values.

**4.** `docker-compose.yml` running PostgreSQL 15 on port 5432 with a named
volume. Note: I may run Postgres natively instead — keep the compose file simple
and make sure `DATABASE_URL` works either way.

**5.** `app/database.py` — async SQLAlchemy engine, `async_sessionmaker`, and a
`get_db()` FastAPI dependency that yields a session.

**6.** SQLAlchemy 2.0 models using `Mapped[]` / `mapped_column` style:

```
customers
  id                UUID pk
  phone             str unique, E.164, indexed
  name              str nullable
  address           text nullable
  created_at        timestamptz
  last_message_at   timestamptz nullable    # 24h window check
  last_followup_at  timestamptz nullable    # follow-up throttling
  is_active         bool default true
  opted_out         bool default false      # STOP handling

staff
  id                UUID pk
  phone             str unique, E.164, indexed
  name              str
  role              enum(WASHER, DELIVERY)
  is_active         bool default true
  created_at        timestamptz
  last_message_at   timestamptz nullable

orders
  id                    UUID pk
  order_number          str unique, e.g. "LDY-20260801-0042", indexed
  customer_id           FK -> customers.id, indexed
  status                enum OrderStatus
  items                 JSONB   # [{"type":"shirt","qty":3,"service":"wash_iron"}]
  total_amount          numeric(10,2) nullable
  amount_paid           numeric(10,2) default 0
  payment_status        enum(UNPAID, PARTIAL, PAID) default UNPAID
  payment_method        enum(CASH, UPI, OTHER) nullable
  paid_at               timestamptz nullable
  pickup_date           date nullable
  expected_delivery     date nullable
  actual_delivery       timestamptz nullable
  assigned_washer_id    FK -> staff.id nullable, indexed
  assigned_delivery_id  FK -> staff.id nullable, indexed
  notes                 text nullable    # INTERNAL ONLY — never sent to customer
  created_at            timestamptz
  updated_at            timestamptz

order_status_history
  id           UUID pk
  order_id     FK -> orders.id, indexed
  old_status   enum nullable
  new_status   enum
  changed_by   str      # "staff:ravi", "customer", or "system"
  changed_at   timestamptz

conversations
  id                UUID pk
  customer_id       FK -> customers.id nullable, indexed
  staff_id          FK -> staff.id nullable, indexed
  direction         enum(INBOUND, OUTBOUND)
  message_text      text
  wa_message_id     str nullable, unique      # dedup
  intent            str nullable
  ai_response_meta  JSONB nullable            # model, tokens, latency_ms
  created_at        timestamptz, indexed

escalations
  id             UUID pk
  customer_id    FK -> customers.id nullable
  staff_id       FK -> staff.id nullable
  order_id       FK -> orders.id nullable
  question       text
  status         enum(OPEN, ANSWERED, CLOSED)
  manager_reply  text nullable
  created_at     timestamptz
  resolved_at    timestamptz nullable
```

Note on `conversations`: exactly one of `customer_id` or `staff_id` should be
set. Add a check constraint for this.

Note on payments: `payment_status` should be derived from `amount_paid` vs
`total_amount`, not set independently. Put that logic in one place so it can
never drift.

**7.** Alembic setup (async) + initial migration.

**8.** `app/utils/phone.py` — `normalize_phone(raw: str) -> str` returning E.164.
Default country code from settings (`+91`). Handle inputs like `"9876543210"`,
`"919876543210"`, `"+91 98765 43210"`, `"0919876543210"`. Raise `ValueError` on
invalid input.

**9.** `app/utils/logger.py` — structlog config, JSON output, includes timestamp
and level.

**10.** `app/main.py` — FastAPI app with a `/health` endpoint returning
`{"status":"ok","db":"connected"}` after an actual DB ping.

**11.** `tests/test_phone.py` — pytest tests for `normalize_phone` covering all
the cases above.

Deliver in this order and stop after each group so I can review:

- **(a)** requirements + config + docker-compose + .env.example
- **(b)** database.py + models + alembic
- **(c)** utils + main.py + tests

Also give me the exact commands to: start Postgres, run migrations, start the
server, and run the tests.

---

# PHASE 2 — WHATSAPP SEND/RECEIVE
# (drafted by Claude, approved by owner 2026-08-01 — original phase text was lost)

PHASE 2: The bot can receive WhatsApp messages and send replies. No orders, no
AI, no scheduler yet. After this phase: you message the test number, the bot
stores it and sends a rule-based acknowledgment back.

Build:

**1.** `app/services/whatsapp.py` — the ONLY outbound door:
- `async send_message(db, *, to_phone, text/template/buttons)` — single entry
  point. Checks the 24h window (`last_message_at` of the customer/staff row):
  inside → free-form allowed; outside → template required, free-form raises a
  clear error (never silently drop).
- Interactive buttons: max 3, each id carries context like `order:<uuid>:done`.
- Every send: structlog line + `conversations` OUTBOUND row (with `wa_message_id`
  from Meta's response). Failed send never crashes the caller. Retry once on
  5xx/network, then raise.
- Outbound to a number that is neither customer nor staff (e.g. manager):
  log-only, no conversations row (Phase 3 order flow always creates the
  customer first, so this stays rare).

**2.** `app/services/templates.py` — template registry: name → language +
param builder. Start with `hello_world`. Unregistered template name = error,
so an unapproved template can never be sent.

**3.** `app/services/messages.py` — first real strings: `ack_received`
(EN + Hinglish), `error_fallback`. `get_message(key, lang)` with EN fallback.
Ack text is a placeholder until Phase 3/4 give real replies.

**4.** `app/routers/webhook.py`:
- `GET /webhook` — Meta verification (verify_token match → echo challenge)
- `POST /webhook` — verify `X-Hub-Signature-256` (HMAC-SHA256, app secret,
  403 on mismatch); return 200 fast; parse text/button/status payloads;
  dedup via `wa_message_id` unique constraint; upsert customer by phone
  (staff phones recognized from staff table); update `last_message_at`;
  store INBOUND conversation; reply with rule-based ack.

**5.** Local webhook tunnel documented in README (ngrok/cloudflared), Meta
webhook config + `messages` subscription.

**6.** Permanent token: System User in Business Settings, token with
`whatsapp_business_messaging` + `whatsapp_business_management`, into `.env`.

**7.** Tests: webhook verify GET, signature valid/invalid/missing, sample
payloads (text/button/status/duplicate) with WhatsApp API mocked.

Deliver in this order, stop after each group:
- **(a)** whatsapp.py + templates.py + messages.py (send path; live test)
- **(b)** webhook.py + signature verification (receive path; live test)
- **(c)** ack wiring + tests + permanent token + tunnel docs (full loop)
