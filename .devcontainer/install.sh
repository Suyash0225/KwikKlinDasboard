#!/usr/bin/env bash
#
# Ek baar, container banate waqt (prebuild on ho to prebuild mein).
# Yahan sirf wo cheezein jo SAB ke liye ek jaisi hain — koi secret nahi.
#
# Secrets aur DB ka data setup.sh mein bante hain, kyunki prebuild ki image
# sabke saath saanjha hoti hai.

set -uo pipefail
cd "$(dirname "$0")/.."

GRN=$'\e[32m'; YEL=$'\e[33m'; OFF=$'\e[0m'
ok()   { echo "  ${GRN}✓${OFF} $*"; }
warn() { echo "  ${YEL}!${OFF} $*"; }

echo
echo "Kwik Klin — install"
echo "─────────────────────────────────────────"

# ── Toota hua apt source ────────────────────────────────────────────────
# Base image mein yarn ka repo pada hai jiski GPG key ab valid nahi hai:
#
#   W: GPG error: https://dl.yarnpkg.com/debian stable InRelease:
#      NO_PUBKEY 62D54FD4003F6525
#
# `apt-get update` iske baad non-zero deta hai, aur jo bhi install us par
# tika ho wo mar jaata hai. Docker-in-docker feature isi par marta tha aur
# poora container build le doobta tha.
#
# Hum yarn use karte hi nahi (koi build step nahi — vanilla JS hai), to
# source hata dena sabse saaf hal hai. Kisi key ko chase karne ki zaroorat
# nahi.
for f in /etc/apt/sources.list.d/yarn.list /etc/apt/sources.list.d/nodesource.list; do
  if [[ -f "$f" ]]; then
    (command -v sudo >/dev/null 2>&1 && sudo -n rm -f "$f") || rm -f "$f"
    ok "hataya: $f (iski key toothi hui hai)"
  fi
done

# ── Postgres ────────────────────────────────────────────────────────────
if command -v psql >/dev/null 2>&1; then
  ok "Postgres pehle se hai"
else
  SUDO=""; command -v sudo >/dev/null 2>&1 && SUDO="sudo -n"
  if $SUDO apt-get update -qq >/tmp/kk-apt.log 2>&1 \
     && $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        postgresql postgresql-contrib >>/tmp/kk-apt.log 2>&1; then
    ok "Postgres install ho gaya ($(ls /etc/postgresql 2>/dev/null | head -1))"
  else
    warn "Postgres install fail — dekho: /tmp/kk-apt.log"
    tail -5 /tmp/kk-apt.log | sed 's/^/      /'
  fi
fi

# ── Python dependencies ─────────────────────────────────────────────────
if pip install --no-cache-dir -q -r requirements.txt >/tmp/kk-pip.log 2>&1; then
  ok "Python dependencies lag gayi"
else
  warn "pip install fail — dekho: /tmp/kk-pip.log"
  tail -5 /tmp/kk-pip.log | sed 's/^/      /'
fi

echo "─────────────────────────────────────────"
echo
