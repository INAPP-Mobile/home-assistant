# Home Assistant 2026.11.2 — open-source home automation that puts local
# control and privacy first (home-assistant/core, Apache-2.0).
#
# Upstream image pinned by manifest-list digest, WITHOUT a tag: Railpack
# 0.43.0 fails to detect a Dockerfile whose FROM carries both a tag and a
# digest, so redeploys die in the plan phase while the first deploy succeeds.
# The digest-only form builds under both builders. Version lives here in the
# comment: homeassistant/home-assistant:2026.11.2 (released 2026-11-16),
# manifest list sha256:493a07e8f6530fc640223f4a0a5248f20a61ca2f718bb1d31efbf08a2c5ea28f
# (amd64 sha256:69193f00e76147a1649a910d39673680e37ce4362dfb410a328a806a824f2000).
#
# The stock image runs as root (no USER directive in the published image
# config) — stated explicitly: Railway mounts persistent volumes root-owned.
# Its entrypoint is s6 /init, which starts the home-assistant service
# (`python3 -P -m homeassistant --config /config`). This wrapper replaces
# the entrypoint with /entrypoint.sh, which prepares /config and then
# execs /init, so s6 still supervises Home Assistant (its finish script
# halts the container when HA exits, and restartPolicy ON_FAILURE
# brings it back).
#
# PORT: Home Assistant does NOT honour a PORT env var — the listen port
# comes only from configuration.yaml `http: server_port:` (verified in
# homeassistant/components/http/__init__.py, HTTP_SCHEMA CONF_SERVER_PORT,
# tag 2026.11.2). The wrapper entrypoint writes/updates that key on every
# boot from Railway's injected $PORT (default 8080); it never sets a
# template-expression URL. The http component also has a bind-failure
# fallback that reverts to a stored config — if the port mapping ever
# fails, the log says "The previous HTTP configuration has been restored".
#
# PROXY: behind Railway's edge, HA must trust X-Forwarded-For or a burst
# of failed logins can get the proxy IP banned (ip_ban is on by default)
# and lock everyone out. The wrapper's configuration.yaml sets
# use_x_forwarded_for + trusted_proxies covering the RFC1918 + CGNAT
# ranges, so the real client IP is seen for auth and rate limiting.
#
# HEALTHCHECK: no in-container HEALTHCHECK — the base image ships none,
# and Railway probes / externally. GET / serves the frontend app shell
# (200) even before login; /manifest.json is the static fallback.
#
# Persistence: everything lives under /config (.storage, configuration.yaml,
# home-assistant_v2.db) on the single Railway volume. SQLite is the
# default recorder DB — no companion database needed.
#
# Accounts: Home Assistant ships NO account — first run is the onboarding
# wizard (name/username/password, no email). This wrapper auto-seeds the
# owner account (admin / HA_ADMIN_PASSWORD) through the unauthenticated
# onboarding API (POST /api/onboarding/users, verified in
# homeassistant/components/onboarding/views.py, tag 2026.11.2) from a
# best-effort background seeder; if seeding fails or the user beats it to
# the wizard, the wizard still works and the generated password is simply
# unused. The remaining onboarding steps (location, units, integrations)
# still show in the UI on first login — they are quick screens, not
# blockers. Registration after onboarding is closed by design (HA only
# ever has the accounts an admin creates).
FROM homeassistant/home-assistant@sha256:493a07e8f6530fc640223f4a0a5248f20a61ca2f718bb1d31efbf08a2c5ea28f

# The stock image carries no USER directive (it runs as root) — state it
# explicitly: Railway mounts persistent volumes root-owned, and the s6
# service writes /config as root.
USER root

# Baked defaults. These literals never appear on the one-click deploy form.
# HA_ADMIN_PASSWORD comes from the template variable (see template-vars.json).
# TZ is UTC unless the deployer overrides it; the in-app time zone setting
# (Settings -> System -> General) takes precedence for display.
ENV TZ=UTC

COPY entrypoint.sh /entrypoint.sh
COPY port_config.py /port_config.py
COPY seeder.py /seeder.py
RUN chmod +x /entrypoint.sh

# /config is the Railway volume (see railway.json). The s6 service runs
# `python3 -P -m homeassistant --config /config` from this directory.
WORKDIR /config

ENTRYPOINT ["/entrypoint.sh"]
