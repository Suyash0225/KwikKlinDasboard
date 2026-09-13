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
# Container ke andar seedha, docker ke bina — kyun, wo devcontainer.json
# mein likha hai. Credentials wahi jo docker-compose.yml deta tha, isliye
# .env.example ka DATABASE_URL bina badle chalta hai.
bash "$(dirname "$0")/start-db.sh"
if ! pg_isready -q 2>/dev/null; then
  warn "Postgres nahi mili — baaki setup ka koi matlab nahi"
  exit 0        # Codespace phir bhi khulne do
fi

# Postgres superuser tak pahunchne ka rasta image ke hisaab se badalta hai.
#
# `sudo -u postgres` yahan NAHI chalta: Codespaces ke is image mein sudoers
# sirf root banne deta hai, kisi aur user ka nahi. Wo chupke se fail nahi
# hota — password maang kar setup ko hamesha ke liye rok deta hai, jo saaf
# fail hone se badtar hai.
#
# -n har jagah: sudo kabhi prompt na kare, seedha fail ho.
pg_su() {
  if [[ $(id -u) -eq 0 ]]; then
    su postgres -c "psql -qtA"
  else
    sudo -n su postgres -c "psql -qtA"
  fi
}

if pg_su <<<"SELECT 1 FROM pg_roles WHERE rolname='laundry'" 2>/dev/null | grep -q 1; then
  ok "laundry role/DB pehle se hai"
elif pg_su <<'SQL' >/dev/null 2>&1
CREATE ROLE laundry LOGIN SUPERUSER PASSWORD 'laundry';
CREATE DATABASE laundry OWNER laundry;
SQL
then
  ok "laundry role + DB bane"
else
  warn "laundry role/DB nahi bane — haath se:"
  warn "  sudo su postgres -c \"psql -c \\\"CREATE ROLE laundry LOGIN SUPERUSER PASSWORD 'laundry';\\\" -c 'CREATE DATABASE laundry OWNER laundry;'\""
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

# ── Home tenant ─────────────────────────────────────────────────────────
# Ye pehle yahan nahi tha, aur naya Codespace isi par atakta tha: seed_rates
# ko home tenant chahiye, home tenant sirf haath se banta tha, aur warning
# padhne se pehle log bahut aage nikal chuka hota tha.
#
# Password yahan banta hai aur neeche chhapta hai. Dev Codespace hai —
# `.env` mein already teen keys padi hain; ek aur secret yahan koi nayi
# baat nahi.
if [[ -f .kk-owner ]]; then
  ok "home tenant pehle se hai (login: $(head -1 .kk-owner))"
else
  OWNER_EMAIL="owner@kwikklin.local"
  OWNER_PASS="$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"
  if python3 -m scripts.bootstrap_home_tenant "$OWNER_EMAIL" "$OWNER_PASS" \
       >/tmp/kk-bootstrap.log 2>&1; then
    printf '%s\n%s\n' "$OWNER_EMAIL" "$OWNER_PASS" > .kk-owner
    ok "home tenant bana"
  else
    warn "home tenant nahi bana — dekho: /tmp/kk-bootstrap.log"
    tail -5 /tmp/kk-bootstrap.log | sed 's/^/      /'
  fi
fi

# ── Seed ────────────────────────────────────────────────────────────────
# Rate card khali ho to New Bill "rate card is empty" dikhata hai aur naya
# banda samajhta hai ki app tooti hui hai.
if python3 -m scripts.seed_rates >/dev/null 2>&1; then
  ok "rate card bhara"
else
  warn "rate card seed nahi hua — dekho: python3 -m scripts.seed_rates"
fi

if python3 -m scripts.seed_staff >/dev/null 2>&1; then
  ok "staff bhara"
else
  warn "staff seed nahi hua — dekho: python3 -m scripts.seed_staff"
fi

echo "─────────────────────────────────────────"
if [[ -f .kk-owner ]]; then
  echo "  Login  :  $(sed -n 1p .kk-owner)  /  $(sed -n 2p .kk-owner)"
  echo "            (yeh .kk-owner mein bhi rakha hai, gitignored)"
fi
echo "  Ab chalao:  ./run.sh"
echo
