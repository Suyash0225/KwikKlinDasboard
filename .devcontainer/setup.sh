#!/usr/bin/env bash
#
# Har NAYE Codespace par ek baar. Prebuild mein ye nahi chalta — yahan
# secrets bante hain, aur prebuild ki image sabke saath saanjha hoti hai.
#
# Iske baad bas:  ./run.sh
#
# Ye script kabhi chup-chaap fail nahi hoti: jo na ho paye wo saaf bolti
# hai, kyunki aadha-taiyar Codespace poore-band Codespace se zyada waqt
# khaata hai — sab kuch theek dikhta hai aur pehli request par phatta hai.

set -uo pipefail
cd "$(dirname "$0")/.."

GRN=$'\e[32m'; YEL=$'\e[33m'; OFF=$'\e[0m'
ok()   { echo "  ${GRN}✓${OFF} $*"; }
warn() { echo "  ${YEL}!${OFF} $*"; }

echo
echo "Kwik Klin — Codespace setup"
echo "─────────────────────────────────────────"

# ── .env ────────────────────────────────────────────────────────────────
if [[ -f .env ]]; then
  ok ".env pehle se hai — chhod raha hoon"
else
  cp .env.example .env
  python3 - <<'PY' >> .env
import secrets
from cryptography.fernet import Fernet
print("ADMIN_API_KEY=" + secrets.token_urlsafe(32))
# Vendor key ALAG. Ek hi rakhne par dukaan ka key /control ka master key
# ban jaata hai — hardening check isi par chillata hai.
print("VENDOR_API_KEY=" + secrets.token_urlsafe(32))
print("TOKEN_ENCRYPTION_KEY=" + Fernet.generate_key().decode())
PY
  ok ".env bana (WhatsApp/Anthropic keys dummy — baad mein bharna)"
fi

# ── Postgres ────────────────────────────────────────────────────────────
if docker compose up -d --wait >/tmp/kk-db.log 2>&1; then
  ok "Postgres chalu"
else
  warn "Postgres start nahi hua — dekho: docker compose logs db"
  tail -5 /tmp/kk-db.log | sed 's/^/      /'
  exit 0        # baaki setup ka koi matlab nahi, par Codespace khulne do
fi

# ── Migrations ──────────────────────────────────────────────────────────
if python3 -m alembic upgrade head >/tmp/kk-alembic.log 2>&1; then
  ok "migrations head par"
else
  warn "migration fail:"
  tail -12 /tmp/kk-alembic.log | sed 's/^/      /'
  exit 0
fi

# ── App ka DB role ──────────────────────────────────────────────────────
# RLS superuser par lagti hi nahi, aur docker-compose ka user wahi hai.
# Iske bina tenant isolation akele ORM filter par tik jaati hai.
if grep -q '^APP_DATABASE_URL=.\+' .env; then
  ok "APP_DATABASE_URL pehle se hai"
else
  line=$(python3 -m scripts.create_app_role 2>/dev/null | grep '^APP_DATABASE_URL=')
  if [[ -n "$line" ]]; then
    # .env mein khali wali line ki jagah
    if grep -q '^APP_DATABASE_URL=$' .env; then
      python3 - "$line" <<'PY'
import pathlib, sys
p = pathlib.Path(".env")
p.write_text("\n".join(
    sys.argv[1] if l.strip() == "APP_DATABASE_URL=" else l
    for l in p.read_text().split("\n")
))
PY
    else
      echo "$line" >> .env
    fi
    ok "kk_app role bana (NOSUPERUSER) — RLS ab sach mein lagegi"
  else
    warn "app role nahi ban paya — app superuser se judegi aur RLS bypass hogi"
    warn "haath se: python3 -m scripts.create_app_role"
  fi
fi

# ── Seed ────────────────────────────────────────────────────────────────
# Rate card khali ho to New Bill "rate card is empty" dikhata hai aur naya
# banda samajhta hai ki app tooti hui hai. Home tenant chahiye pehle.
if python3 -m scripts.seed_rates >/dev/null 2>&1; then
  ok "rate card bhara"
else
  warn "rate card seed nahi hua — pehle owner banao:"
  warn "  python3 -m scripts.bootstrap_home_tenant <email> <password>"
fi

echo "─────────────────────────────────────────"
echo "  Ab chalao:  ./run.sh"
echo
