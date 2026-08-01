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

## Layout

See PROJECT_SPEC.md → "PROJECT STRUCTURE". Rules that matter:

- All outbound WhatsApp messages go through `app/services/whatsapp.py`. Never
  call the Graph API anywhere else.
- Only `app/services/llm_client.py` may import `anthropic`.
- All human-facing strings live in `app/services/messages.py`.
- `orders.notes` is internal-only — never sent to a customer.
- Payment status is derived in exactly one place: `app/models/order.py`.
