#!/usr/bin/env bash
#
# Kwik Klin — server par update (har naye code ke liye yahi ek command).
#
#   cd /opt/kwikklin && ./scripts/deploy.sh
#
# Kya karta hai: DB backup -> git pull -> dependencies -> migrations ->
# security check (secure_setup) -> app restart -> /health se ASLI jawab.
# Kisi bhi padav par fail = ruk jaata hai, aadha deploy kabhi nahi.
#
# Pehli baar ka setup docs/DEPLOY.md mein hai (.env, systemd, nginx).

set -euo pipefail
cd "$(dirname "$0")/.."

SERVICE="${SERVICE:-kwikklin}"        # systemd unit ka naam
PORT="${PORT:-8000}"
PY=".venv/bin/python"

ok()   { echo "  ✓ $*"; }
die()  { echo "  ✗ $*" >&2; exit 1; }

[[ -f .env ]] || die ".env nahi mila — docs/DEPLOY.md ka 'Pehli baar' hissa dekho"
[[ -x $PY ]]  || die ".venv nahi mila — python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"

echo "1/6 backup"
if $PY -c "import asyncio; from app.services.backup import run_backup; import sys; sys.exit(0 if asyncio.run(run_backup()) else 1)" 2>/dev/null; then
  ok "pg_dump backups/ mein"
else
  echo "  ! backup nahi bana (pg_dump missing?) — aage badh rahe hain"
fi

echo "2/6 code"
git pull --ff-only
ok "$(git log -1 --format='%h %s')"

echo "3/6 dependencies"
.venv/bin/pip install -q -r requirements.txt
ok "requirements"

echo "4/6 migrations"
.venv/bin/alembic upgrade head
ok "alembic head"

echo "5/6 security"
$PY -m scripts.secure_setup
ok "keys / DB role / tokens"

echo "6/6 restart"
if command -v systemctl >/dev/null && systemctl list-unit-files | grep -q "^$SERVICE.service"; then
  sudo systemctl restart "$SERVICE"
else
  echo "  ! systemd unit '$SERVICE' nahi — app khud restart karo (docker/pm2)"
fi

for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    ok "READY — $(curl -fsS "http://127.0.0.1:$PORT/health")"
    exit 0
  fi
  sleep 1
done
die "/health ne jawab nahi diya — journalctl -u $SERVICE -n 50 dekho"
