#!/usr/bin/env bash
#
# Kwik Klin — ek command mein poora dev server.
#
#   ./run.sh              chalao
#   ./run.sh --stop       band karo
#   ./run.sh --seed       chalao + rate card aur staff bhar do (pehli baar)
#
# YE SCRIPT KYUN HAI
#
# Server band ho to bhi dashboard BROWSER MEIN KHULTA RAHTA HAI. Wo ek PWA
# hai aur uska service worker network na milne par cache se purana page de
# deta hai — koi error nahi, koi ishaara nahi. Ek poora ghanta isi mein gaya
# tha: code sahi tha, file sahi thi, browser purana page dikha raha tha,
# aur uvicorn chal hi nahi raha tha.
#
# Isliye ye script chup-chaap kabhi kaamyab nahi hoti. Har padav par jaanch
# karti hai, aur aakhir mein server se ASLI jawab lekar dikhati hai. Agar
# neeche "READY" nahi likha, to server chal nahi raha — chahe browser kuch
# bhi dikha raha ho.

set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8000}"
RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; DIM=$'\e[2m'; OFF=$'\e[0m'
ok()   { echo "  ${GRN}✓${OFF} $*"; }
warn() { echo "  ${YEL}!${OFF} $*"; }
die()  { echo "  ${RED}✗ $*${OFF}" >&2; exit 1; }

# Port khaali hai ya nahi — bash se, kisi tool ke bharose nahi.
#
# /dev/tcp har bash mein hai. lsof/fuser/ss aksar container images mein
# nahi hote (is repo ke apne dev sandbox mein teenon nahi the), aur unke
# bharose "koi pid nahi mila = port khaali" maan lena jhoot bolna hai.
port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

# Pid pata karna best-effort hai — jo tool mile usse. Na mile to khaali.
port_pids() {
  if command -v lsof >/dev/null; then lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null
  elif command -v fuser >/dev/null; then fuser "$1/tcp" 2>/dev/null | tr -s ' ' '\n'
  elif command -v ss >/dev/null; then ss -lptnH "sport = :$1" 2>/dev/null | grep -oP 'pid=\K[0-9]+'
  fi
}

# Purana server hatao.
#
# Sirf `pkill -f "uvicorn app.main:app"` kaafi nahi tha, aur yahi asli bug
# tha: --reload DO process chalata hai, aur socket WORKER pakadta hai. Wo
# worker multiprocessing se spawn hota hai, isliye uski command line mein
# "uvicorn app.main:app" hota hi nahi. pkill parent maar deta tha, script
# "purana server band kiya" likh deti thi, aur anaath worker port pakde
# baitha rehta tha — agla uvicorn "Address already in use" par mar jaata.
#
# Isliye ab: pattern se maaro, phir PORT se poochho ki sach mein khaali
# hua ya nahi, aur na hua to jo mila use maaro. Aur agar port abhi bhi
# busy hai par pid nikalne ka koi tool hi nahi hai, to maan lene ke bajaye
# saaf bata do.
free_port() {
  pkill -f "uvicorn app.main:app" 2>/dev/null || true
  for _ in $(seq 1 12); do
    port_busy "$1" || return 0
    sleep 0.25
  done
  local pids; pids=$(port_pids "$1" | tr '\n' ' ')
  if [[ -n "${pids// }" ]]; then
    kill $pids 2>/dev/null || true; sleep 1
    port_busy "$1" && { kill -9 $pids 2>/dev/null || true; sleep 1; }
  fi
  if port_busy "$1"; then
    warn "port $1 abhi bhi busy hai"
    command -v lsof >/dev/null || command -v fuser >/dev/null || command -v ss >/dev/null \
      || warn "lsof/fuser/ss mein se koi nahi hai, isliye kaun pakde hai ye bata nahi sakta"
    return 1
  fi
  return 0
}

# ── --stop ──────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--stop" ]]; then
  free_port "$PORT" && ok "port $PORT khaali" || die "port $PORT khaali nahi kar paya"
  exit 0
fi

echo
echo "Kwik Klin — dev server"
echo "${DIM}──────────────────────────────────────────${OFF}"

# ── 1. .env ─────────────────────────────────────────────────────────────
# Bina keys ke app boot par hi mar jaati hai, aur uska error samajhna
# mushkil hota hai. Isliye pehle hi bana dete hain.
if [[ ! -f .env ]]; then
  warn ".env nahi mila — .env.example se bana raha hoon"
  cp .env.example .env
  python3 - <<'PY' >> .env
import secrets
from cryptography.fernet import Fernet
print("ADMIN_API_KEY=" + secrets.token_urlsafe(32))
print("VENDOR_API_KEY=" + secrets.token_urlsafe(32))
print("TOKEN_ENCRYPTION_KEY=" + Fernet.generate_key().decode())
PY
  ok ".env bana (WhatsApp/Anthropic keys dummy hain — baad mein bhar lena)"
else
  ok ".env maujood"
fi

# ── 2. Postgres ─────────────────────────────────────────────────────────
DB_URL=$(grep -E '^DATABASE_URL=' .env | cut -d= -f2- || true)
DB_HOST=$(sed -E 's#.*@([^:/]+).*#\1#' <<<"$DB_URL")
DB_PORT=$(sed -E 's#.*:([0-9]+)/.*#\1#' <<<"$DB_URL")

if ! (exec 3<>"/dev/tcp/${DB_HOST}/${DB_PORT}") 2>/dev/null; then
  warn "Postgres ${DB_HOST}:${DB_PORT} par nahi mila — docker compose se utha raha hoon"
  command -v docker >/dev/null || die "docker nahi hai. Ya to Postgres khud chalao, ya DATABASE_URL badlo."
  docker compose up -d --wait || die "Postgres start nahi hua. Dekho: docker compose logs db"
fi
ok "Postgres ${DB_HOST}:${DB_PORT}"

# ── 3. Migrations ───────────────────────────────────────────────────────
# Har baar chalti hain: pehle se lagi hui ho to alembic kuch nahi karta,
# aur nayi ho to yahin lag jaati hai. Bhoolne ki gunjaish hi na rahe.
# `python3 -m alembic`, bare `alembic` nahi: console scripts har setup mein
# PATH par nahi hote (jo `python -m uvicorn` chalata hai uske paas aksar
# nahi hote), aur tab error "command not found" aata hai — jo migration ki
# dikkat jaisa bilkul nahi dikhta.
if ! python3 -m alembic upgrade head >/tmp/kk-alembic.log 2>&1; then
  echo
  echo "  ${RED}Migration fail:${OFF}"
  # Log ki taraf ishaara karke chhod dena isi script ke maqsad ke khilaf hai.
  # Wajah yahin dikhao.
  sed 's/^/    /' /tmp/kk-alembic.log | tail -20
  die "poora log: /tmp/kk-alembic.log"
fi
ok "Migrations head par"

# ── 3b. App ka DB role ──────────────────────────────────────────────────
# RLS superuser par lagti hi nahi, aur docker-compose ka user wahi hai.
# Role na ho to app chalegi — bas isolation ek parat kam ho jayegi, aur
# startup hardening uspar chillayega.
if ! grep -q '^APP_DATABASE_URL=.\+' .env 2>/dev/null; then
  warn "APP_DATABASE_URL khali — app superuser se judegi aur RLS bypass hogi"
  warn "banane ke liye:  python3 -m scripts.create_app_role"
fi

# ── 4. --seed ───────────────────────────────────────────────────────────
if [[ "${1:-}" == "--seed" ]]; then
  # `-m scripts.x`, `scripts/x.py` nahi — inke apne docstring mein yahi
  # likha hai. Path se chalane par repo root sys.path par nahi aata aur
  # "No module named 'app'" milta hai, jo seed ki dikkat jaisa nahi lagta.
  #
  # Rate card khaali ho to New Bill screen "rate card is empty" dikhati hai
  # aur naya banda samajhta hai ki app tooti hui hai.
  for mod in seed_rates seed_staff; do
    if python3 -m "scripts.$mod" >"/tmp/kk-$mod.log" 2>&1; then
      ok "$mod"
    else
      # Chupana nahi. Yahan ka aam jawab hota hai "pehle bootstrap_home_tenant
      # chalao" — wo padh lena hi kaafi hai, dhoondhne mat bhejo.
      warn "$mod nahi chala:"
      sed 's/^/      /' "/tmp/kk-$mod.log" | tail -6
    fi
  done
fi

# ── 5. Purana server ────────────────────────────────────────────────────
# Do uvicorn ek hi port par = "address already in use", ya isse bura, purana
# wala pakda rehta hai aur naya code kabhi load hi nahi hota.
if port_busy "$PORT"; then
  free_port "$PORT" || die "port $PORT khaali nahi ho raha — dusra PORT do: PORT=8001 ./run.sh"
  ok "purana server band kiya (port $PORT khaali)"
fi

# ── 6. Server ───────────────────────────────────────────────────────────
# --host 0.0.0.0 zaroori hai. Sirf 127.0.0.1 par sunne se Codespaces ka
# port forwarding bahar nahi nikal paata aur phone par kuch nahi khulta.
echo "${DIM}──────────────────────────────────────────${OFF}"
python3 -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --reload \
  >/tmp/kk-server.log 2>&1 &
SERVER_PID=$!

# ── 7. Sach mein chala? ─────────────────────────────────────────────────
# Yahi is script ka asli kaam. /health par asli jawab aane tak intezaar,
# warna saaf bata do ki nahi chala.
printf "  server boot ho raha hai"
for i in $(seq 1 40); do
  [[ $i -gt 1 ]] && printf "."
  # --max-time zaroori hai: bina iske curl anant tak latak sakta hai agar
  # port accept kar le par jawab na de, aur neeche ka 20-second ka bandhan
  # kabhi lagta hi nahi — script chup-chaap tangi reh jaati hai.
  if curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/health" >/tmp/kk-health.json 2>/dev/null; then
    break
  fi
  kill -0 "$SERVER_PID" 2>/dev/null || { echo; cat /tmp/kk-server.log; die "server boot par hi mar gaya"; }
  sleep 0.5
done

printf "\r\033[K"
if ! grep -q '"ok"' /tmp/kk-health.json 2>/dev/null; then
  echo; tail -30 /tmp/kk-server.log
  die "server ne /health par jawab nahi diya. Poora log: /tmp/kk-server.log"
fi

echo
echo "  ${GRN}READY${OFF}  $(cat /tmp/kk-health.json)"
if [[ -n "${CODESPACE_NAME:-}" ]]; then
  echo "  ${DIM}https://${CODESPACE_NAME}-${PORT}.app.github.dev/admin${OFF}"
  echo "  ${DIM}Phone par kholna ho to VS Code ke PORTS tab mein ${PORT} ko Public karo.${OFF}"
else
  echo "  ${DIM}http://localhost:${PORT}/admin${OFF}"
fi
echo "  ${DIM}log: tail -f /tmp/kk-server.log   ·   band: ./run.sh --stop${OFF}"
echo
echo "${DIM}Ctrl+C se yahan se chhoot jao — server peeche chalta rahega.${OFF}"
wait "$SERVER_PID"
