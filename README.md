# Kwik Klin

WhatsApp-first laundry CRM (multi-tenant SaaS) + Kwik Klin's own public website.
Owner dashboard `/admin`, staff PWA `/staff`, vendor Control panel `/control`,
public site `/` (app/site). Docs: [ARCHITECTURE.md](ARCHITECTURE.md),
[docs/ARCHITECTURE-DETAILED.md](docs/ARCHITECTURE-DETAILED.md),
[docs/QA-TESTING-HANDBOOK.md](docs/QA-TESTING-HANDBOOK.md), [docs/PRD.md](docs/PRD.md).

## Codespaces

Naya Codespace khulte hi taiyar hota hai (`.devcontainer/`): dependencies,
Postgres, migrations, `kk_app` DB role aur rate card — sab apne aap. Bas:

```bash
./run.sh
```

Purana Codespace wapas kholne ke liye: repo par **Code → Codespaces**, ya
`github.com/codespaces`. Us par setup dobara nahi chalta — sirf `./run.sh`.

## Quick start (Linux / macOS)

```bash
pip install -r requirements.txt
./run.sh --seed        # pehli baar: rate card aur staff bhi bhar dega
./run.sh               # uske baad
./run.sh --stop        # band
```

`run.sh` .env banata hai (agar nahi hai), Postgres uthata hai, migrations
chalata hai, purana server band karta hai, aur naya chalu karke `/health`
se **asli jawab** lekar `READY` dikhata hai.

Ye aakhri kadam sabse zaroori hai. Dashboard ek PWA hai: server band ho to
uska service worker cache se purana page de deta hai — bina error, bina
kisi ishaare ke. Browser mein app khuli dikhti rahegi jabki kuch chal hi
nahi raha hoga. Isliye jab bhi lage "code badla par kuch nahi hua", pehle
dekho ki `READY` likha aaya tha ya nahi.

## Nakli data (dev/staging only)

```bash
python -m scripts.seed_demo_data 1000 500 1000   # home dukaan: orders, tasks, customers
python -m scripts.seed_demo_shops 5 1000 1000 500 # 5 aur poori dukaanein (multi-tenant test)
python -m scripts.seed_demo_data --clear          # aur
python -m scripts.seed_demo_shops --clear         # dono saaf
```

Demo dukaanon ka login: `owner1@demo.test` … `owner5@demo.test`, password
`demo12345`.

Testing se pehle dono `--clear` chala lo. Test suite isi DB par chalti hai
aur kai test gintiyon par assert karte hain, isliye nakli rows unhe fail
kara dete hain.

## Setup (Windows)

```bat
:: 1. Virtual env + dependencies
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

:: 2. Config — copy and fill in real values (dummies boot fine in Phase 1)
copy .env.example .env

:: 3. Keys + NOSUPERUSER DB role — ek command, seedha .env mein likhta hai
.venv\Scripts\python.exe -m scripts.secure_setup
```

`secure_setup` `TOKEN_ENCRYPTION_KEY`, `VENDOR_API_KEY` aur `APP_DATABASE_URL`
bharta hai aur purane plaintext tokens encrypt karta hai. Production mein in
teeno ke bina app shuru hi nahi hoti.

`TOKEN_ENCRYPTION_KEY` ka backup rakho — kho gayi to DB ke encrypted
WhatsApp tokens wapas nahi milenge (har dukaan ko number dobara connect
karna padega). Purane deploy par key set karne ke baad ek baar
`python -m scripts.encrypt_tokens` chalao — jo tokens plaintext padhe hain
wo encrypt ho jaayenge.

## Database

Either **native PostgreSQL 15+** (what this machine uses) or Docker — the
`DATABASE_URL` in `.env` is the same for both.

```bat
:: Native: service postgresql-x64-15 runs automatically.
:: One-time user/db creation (already done on this machine):
::   CREATE USER laundry WITH PASSWORD 'laundry';
::   CREATE DATABASE laundry OWNER laundry;

:: Docker alternative:
docker compose up -d --wait
```

## Run migrations

```bat
.venv\Scripts\alembic.exe upgrade head
```

After changing models: `alembic revision --autogenerate -m "..."`, review the
generated file, then `upgrade head`.

## Start the server

```bat
.venv\Scripts\uvicorn.exe app.main:app --reload
```

Check: <http://127.0.0.1:8000/health> → `{"status":"ok","db":"connected"}`

## Run tests

```bat
.venv\Scripts\python.exe -m pytest
```

## Webhook (local development)

Meta must reach your laptop over HTTPS — use a cloudflared quick tunnel:

```bat
:: terminal 1 — the app
.venv\Scripts\uvicorn.exe app.main:app --port 8000

:: terminal 2 — the tunnel (prints a https://xxx.trycloudflare.com URL)
"C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://127.0.0.1:8000
```

Then in developers.facebook.com → app → WhatsApp → **Configuration**:

- Callback URL: `https://<tunnel-url>/webhook`
- Verify token: the `WHATSAPP_VERIFY_TOKEN` from `.env`
- Webhook fields → subscribe to **`messages`**

⚠️ The trycloudflare URL is random **per tunnel run** — after every tunnel
restart, paste the new URL into Meta's Configuration again. (Production gets
a fixed domain in Phase 6.)

⚠️ The API Setup access token expires every ~24h during development —
regenerate and update `WHATSAPP_TOKEN` in `.env`, then restart uvicorn
(settings load at startup). A permanent System User token replaces this.

## Layout

- `app/routers` HTTP, `app/services` business logic, `app/models` SQLAlchemy,
  `app/static` dashboard/staff/control UI, `app/site` public website
  (see `app/site/README.md`), `alembic/` migrations, `scripts/` ops.

Rules that matter:

- All outbound WhatsApp messages go through `app/services/whatsapp.py`. Never
  call the Graph API anywhere else.
- Only `app/services/llm_client.py` talks to an LLM (Anthropic or Gemini).
- All human-facing strings live in `app/services/messages.py`.
- `orders.notes` is internal-only — never sent to a customer.
- Payment status is derived in exactly one place: `app/models/order.py`.

## Overnight build (03 Aug 2026) — agents + marketing + English UI

**What runs now**
- **Service agent (WhatsApp)**: owner/staff commands — bill by text or *photo*
  (draft → 'haan' confirm), delay/status updates, relay ("Ravi ko bolo…",
  works for customers too and closes their open questions), set priority /
  assign staff / add note / record payment (confirm-gated), business Q&A from
  live DB aggregates, standup reply parsing ("1 aur 2 ho gaya" → statuses).
- **Customer agent**: answers only from DB facts + owner-taught FAQ/corrections
  (AI training page), remembers the thread, escalates unknowns into the
  Teach-me queue, pauses itself when a customer complains (owner takes over).
- **Scheduler** (Asia/Kolkata): 10AM staff standup with real pending lists,
  delivery-day nudges, payment reminders (3d polite / 15d firm + admin flag),
  nightly RFM segments, Monday campaign suggestion. Quiet hours + idempotent.
- **Marketing**: segments, campaign engine (opted-in only, STOP/"band karo"
  honored instantly, frequency cap, monthly budget, 1 msg/sec), delivered/
  read/replied tracking, revenue attribution, coupons redeemable on New Bill.
- **Dashboard** (127.0.0.1:8000/admin): full English, mobile-first, sign-in
  required; new pages: Campaigns, AI training, Activity (audit trail).

**Env**: see `.env` — `LLM_PROVIDER=gemini` + `GEMINI_API_KEY` (Claude path
kept: set `LLM_PROVIDER=anthropic` + `ANTHROPIC_API_KEY`).
`ESCALATION_CC_PHONE` CCs escalations to Ravi.

**Migrations**: `alembic upgrade head` (latest: payments ledger, audit_log,
campaigns/coupons, open_questions, faq/corrections, settings_kv).

**Tests**: `.venv\Scripts\python -m pytest` — all LLM/WhatsApp mocked. Suite runs
against the local DB; seed demo data makes a few count-based tests fail.

## Deploy (server)

Poora kram (EC2, systemd, nginx, HTTPS): [docs/DEPLOY.md](docs/DEPLOY.md).
Har update: `./scripts/deploy.sh`. `.env` git mein nahi jaati — server par ek
baar banti hai aur rehti hai.

```bash
# pehli baar
git clone <repo> && cd KwikKlinDasboard
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # DATABASE_URL, keys, SITE_URL=https://kwikklin.online, ENVIRONMENT=production
.venv/bin/alembic upgrade head
.venv/bin/python -m scripts.secure_setup   # keys + kk_app role, .env mein likhta hai
.venv/bin/python -m scripts.bootstrap_home_tenant

# har update par
git pull && .venv/bin/pip install -r requirements.txt && .venv/bin/alembic upgrade head
# phir app restart (systemd/pm2/docker — jo bhi use ho)
```

Server ki `.env` ka backup password manager mein rakho — khaaskar
`TOKEN_ENCRYPTION_KEY`.
