#!/bin/sh
# Home Assistant on Railway.
#
# - Maps Railway's injected PORT onto configuration.yaml `http:
#   server_port:` via port_config.py (line-based edit — the user's file is
#   preserved, only the port key is managed). Home Assistant reads the
#   port only from that key (no PORT env support, verified in upstream
#   source); when PORT is unset it defaults to 8080, Railway's default
#   service port.
# - Starts the background owner seeder (seeder.py) when HA_ADMIN_PASSWORD
#   is set: it waits for the onboarding API, then creates the owner
#   account (admin / HA_ADMIN_PASSWORD) through POST /api/onboarding/users.
#   Best-effort only — if it fails, or the user reaches the wizard first,
#   the wizard still works and the generated password is unused. The
#   seeder never blocks startup and never fails this script.
# - Hands off to the upstream entrypoint (/init, s6), which supervises
#   Home Assistant (`python3 -P -m homeassistant --config /config`).
set -e

PORT="${PORT:-8080}"
export PORT

echo "[wrapper] configuring /config/configuration.yaml for port $PORT"
python3 /port_config.py "$PORT"

if [ -n "${HA_ADMIN_PASSWORD:-}" ]; then
  echo "[wrapper] starting background owner seeder"
  (python3 /seeder.py >>/tmp/ha-seeder.log 2>&1 || true) &
else
  echo "[wrapper] HA_ADMIN_PASSWORD unset — skipping owner seeding (use the onboarding wizard to create the owner account)"
fi

exec /init
