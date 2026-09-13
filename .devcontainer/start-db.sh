#!/usr/bin/env bash
#
# Har baar jab Codespace chalu ho (naya bhi, resume bhi).
#
# docker-compose yeh kaam `restart: unless-stopped` se karta tha. Native
# Postgres apne aap wapas nahi aati, aur uske bina resume ke baad dashboard
# ki har request DB error deti hai — jo "app toot gayi" jaisa lagta hai
# jabki bas daemon so raha hota hai.

set -uo pipefail

# Cluster ka version image ke saath badalta hai (bookworm par 15). Naam
# hardcode karne se ek image upgrade sab kuch chup-chaap tod deta.
V="$(ls /etc/postgresql 2>/dev/null | sort -n | tail -1)"

if [[ -z "$V" ]]; then
  echo "  ! Postgres install nahi hai — chalao: bash .devcontainer/install.sh"
  exit 0
fi

if pg_isready -q 2>/dev/null; then
  echo "  ✓ Postgres pehle se chalu"
  exit 0
fi

if command -v sudo >/dev/null 2>&1; then
  sudo -n pg_ctlcluster "$V" main start 2>/dev/null
else
  pg_ctlcluster "$V" main start 2>/dev/null
fi

for _ in $(seq 1 20); do
  pg_isready -q 2>/dev/null && { echo "  ✓ Postgres chalu (v$V)"; exit 0; }
  sleep 1
done

echo "  ! Postgres chalu nahi hua — dekho:"
echo "      sudo tail -20 /var/log/postgresql/postgresql-$V-main.log"
exit 0
