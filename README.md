# Laundry WhatsApp Bot

WhatsApp-based order management for a laundry shop in Varanasi. Customers,
washer, and delivery staff all interact over WhatsApp; the bot coordinates.
Full context: [PROJECT_SPEC.md](PROJECT_SPEC.md).

## Setup (Windows)

```bat
:: 1. Virtual env + dependencies
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

:: 2. Config — copy and fill in real values (dummies boot fine in Phase 1)
copy .env.example .env
```

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

See PROJECT_SPEC.md → "PROJECT STRUCTURE". Rules that matter:

- All outbound WhatsApp messages go through `app/services/whatsapp.py`. Never
  call the Graph API anywhere else.
- Only `app/services/llm_client.py` may import `anthropic`.
- All human-facing strings live in `app/services/messages.py`.
- `orders.notes` is internal-only — never sent to a customer.
- Payment status is derived in exactly one place: `app/models/order.py`.
