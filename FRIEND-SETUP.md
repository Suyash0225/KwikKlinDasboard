# Kwik Klin — Dev Setup (for the partner/UI developer)

Suyash will send you a `.env.friend` file separately (WhatsApp/USB —
it is NOT in this repo on purpose). Rename it to `.env` in the project
root. Its WhatsApp token is a dummy so your machine can never
accidentally message real customers — everything else works.

## Run it (Windows)

1. Install Python 3.12 + PostgreSQL 15.
2. Create DB + user (psql):
   ```sql
   CREATE USER laundry WITH PASSWORD 'laundry';
   CREATE DATABASE laundry OWNER laundry;
   ```
3. In the project folder:
   ```
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   copy .env.friend .env        (the file Suyash sends you)
   .venv\Scripts\alembic upgrade head
   .venv\Scripts\python scripts\seed_rates.py
   .venv\Scripts\python scripts\seed_staff.py
   .venv\Scripts\uvicorn app.main:app --port 8000
   ```
4. Open http://127.0.0.1:8000/admin — sign in with the ADMIN_API_KEY
   from the .env.

## UI work lives in exactly three files

- `app/static/dashboard.html` — structure
- `app/static/app.css` — the whole design system (CSS variables at top)
- `app/static/app.js` — logic + all UI strings (top `T` object)

Backend needs no changes for UI work. Please work on a branch
(`git checkout -b ui-redesign`) and PR back to main.

## Ground rules

- Never commit `.env` / `.env.friend` (gitignored — keep it that way).
- The dummy WhatsApp token must stay dummy on your machine.
- Tests: `.venv\Scripts\python -m pytest` — keep them green (157).
