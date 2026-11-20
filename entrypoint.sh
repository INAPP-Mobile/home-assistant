#!/bin/sh
# Home Assistant on Railway.
#
# - Sets SETUP_PORT=$PORT (Railway's injected port, default 8080).
#   Home Assistant reads its default listen port from the SETUP_PORT env
#   var (homeassistant/components/http/config.py: default_server_port(),
#   tag 2026.11.2). Do NOT put server_port in configuration.yaml: HA
#   2026.11 migrates a YAML http: block as a 5-minute trial config, then
#   reverts to port 8123 and restarts — the instance would 502 forever
#   on the Railway domain (proven live 2026-11-20).
# - port_config.py heals configuration.yaml (upstream DEFAULT_CONFIG
#   includes) and removes a stale .storage/http store whose stable port
#   no longer matches $PORT.
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
export SETUP_PORT="$PORT"

echo "[wrapper] port $PORT via SETUP_PORT; healing /config/configuration.yaml"
python3 /port_config.py "$PORT"

if [ -n "${HA_ADMIN_PASSWORD:-}" ]; then
  echo "[wrapper] starting background owner seeder"
  (python3 /seeder.py >>/tmp/ha-seeder.log 2>&1 || true) &
else
  echo "[wrapper] HA_ADMIN_PASSWORD unset — skipping owner seeding (use the onboarding wizard to create the owner account)"
fi

exec /init
