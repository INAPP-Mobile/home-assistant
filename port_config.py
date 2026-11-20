#!/usr/bin/env python3
"""Manage /config/configuration.yaml and the HTTP store for Home
Assistant on Railway.

PORT + PROXY MECHANISM (verified against upstream source, tag 2026.11.2):
- The listen port comes from the ``SETUP_PORT`` env var
  (``homeassistant/components/http/config.py: default_server_port()``) —
  the entrypoint exports ``SETUP_PORT=$PORT`` before starting HA.
- The reverse-proxy settings (``use_x_forwarded_for`` +
  ``trusted_proxies``) live in the ``.storage/http`` store, NOT in
  configuration.yaml. They are required: behind Railway's edge every
  request carries ``X-Forwarded-For`` from 100.64.0.1, and HA's
  forwarded middleware answers HTTP 400 to any proxied request while
  ``use_x_forwarded_for`` is false (``http/forwarded.py``), or while
  the connected peer is not in ``trusted_proxies``.

WHY NOT YAML (the 2026.11 trial gotcha, proven live 2026-11-20): HA
2026.11 migrates a YAML ``http:`` block into ``.storage/http`` as a
PENDING (trial) config. It serves on the YAML port for 5 minutes,
then — unless the pending config is confirmed in the UI — reverts to
the stored STABLE config (port 8123) and restarts. The Railway domain
targets $PORT, so the instance 502s forever after its first 5
minutes. Writing the store directly (stable slot, no pending, YAML
migration marked done) never arms a trial: the store IS the source of
truth on every boot.

On every boot this script ensures:
1. The standard includes from upstream's own DEFAULT_CONFIG
   (homeassistant/config.py) are present — ``automation: !include
   automations.yaml``, ``script: !include scripts.yaml``, ``scene:
   !include scenes.yaml``. A minimal config without them loads no
   automation/script/scene integrations at all, so the automation API
   would accept configs that never become entities (proven in e2e
   2026-11-20: the automation config saved and read back, but no
   automation entity ever appeared). Missing keys are appended; an
   existing key is never touched.
2. The included files exist (automations.yaml as "[]", scripts.yaml and
   scenes.yaml empty — exactly what upstream's _write_default_config
   writes), so a fresh volume boots cleanly.
3. A leftover wrapper ``http:`` block from the pre-v3 wrapper (finger-
   printed by its exact trusted_proxies list) is removed from
   configuration.yaml — it would otherwise re-arm the trial on a fresh
   volume. Any other ``http:`` block is the user's own and is left
   alone (HA ignores YAML http: after migration and shows a repair
   notice about it).
4. The ``.storage/http`` store is reachable: stable.server_port ==
   $PORT, use_x_forwarded_for on, and trusted_proxies covering the
   Railway edge (100.64.0.1). If any of those is wrong — e.g. a volume
   poisoned by the pre-v3 trial, whose stable port reverted to 8123 —
   the store is (re)written with the reachability keys forced, keeping
   the user's other HTTP settings (CORS, SSL profile, …) when the
   store is readable. A store that is already reachable is left
   untouched. The store holds only HTTP config, no user data.

When configuration.yaml is missing entirely, the config written is
upstream's DEFAULT_CONFIG verbatim, plus a ``homeassistant: name:``.

Line-based on purpose: the user's file is preserved byte-for-byte
except for the keys this script manages, so hand edits, comments, and
formatting survive a redeploy.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from ipaddress import ip_address, ip_network

CONFIG_DIR = "/config"
CONFIG_PATH = os.path.join(CONFIG_DIR, "configuration.yaml")
HTTP_STORE_PATH = os.path.join(CONFIG_DIR, ".storage", "http")

# Fingerprint of the pre-v3 wrapper's http: block: the exact proxy list
# it wrote. Only a block containing ALL of these is removed.
WRAPPER_PROXIES = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",
)

# The Railway edge connects to HA from the CGNAT range; the peer IP
# seen in the logs is 100.64.0.1.
EDGE_IP = "100.64.0.1"

# HA 2026.11.2 defaults (homeassistant/components/http/const.py and
# config.py: HTTP_STORAGE_SCHEMA({})). The image is pinned by digest,
# so these are fixed for this template version; re-verify them on any
# Home Assistant version bump.
STORE_VERSION = 3
STORE_MINOR_VERSION = 1
DEFAULT_HTTP_CONFIG = {
    "server_port": 8080,
    "cors_allowed_origins": ["https://cast.home-assistant.io"],
    "login_attempts_threshold": -1,
    "ip_ban_enabled": True,
    "ssl_profile": "intermediate_v6",
    "use_x_frame_options": True,
}

# Upstream's DEFAULT_CONFIG (homeassistant/config.py), verbatim.
DEFAULT_CONFIG = """# Loads default set of integrations. Do not remove.
default_config:

# Load frontend themes from the themes folder
frontend:
  themes: !include_dir_merge_named themes

automation: !include automations.yaml
script: !include scripts.yaml
scene: !include scenes.yaml
"""

MINIMAL_CONFIG = """# Home Assistant configuration — created by the Railway wrapper from
# upstream's own DEFAULT_CONFIG (homeassistant/config.py).
# The listen port comes from the SETUP_PORT env var and the proxy
# settings from the .storage/http store (see entrypoint.sh) — NOT from
# this file. HA 2026.11 trials a YAML http: block for 5 min then
# reverts to port 8123. Everything else here is yours: edit freely, it
# survives redeploys. Set your home location and time zone in the UI
# onboarding (Settings -> System -> General) rather than here.
{default_config}
homeassistant:
  name: Home
"""

# (top-level key, include line) pairs from upstream's DEFAULT_CONFIG.
STANDARD_INCLUDES = [
    ("automation", "automation: !include automations.yaml"),
    ("script", "script: !include scripts.yaml"),
    ("scene", "scene: !include scenes.yaml"),
]

# Files the includes point at, and what upstream writes for a fresh volume.
INCLUDED_FILES = [
    ("automations.yaml", "[]"),
    ("scripts.yaml", ""),
    ("scenes.yaml", ""),
]


def _is_top_level_key(line: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][^:\s][^:]*:\s*(#.*)?$", line))


def _top_level_keys(lines: list) -> set:
    keys = set()
    for line in lines:
        m = re.match(r"^([A-Za-z_][^:\s][^:]*):", line)
        if m:
            keys.add(m.group(1))
    return keys


def _ensure_included_files() -> None:
    for name, content in INCLUDED_FILES:
        path = os.path.join(CONFIG_DIR, name)
        if not os.path.isfile(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"[port_config] created {path}")


def _remove_wrapper_http_block(lines: list) -> list:
    """Drop the pre-v3 wrapper's http: block (fingerprinted), if present."""
    http_idx = None
    for i, line in enumerate(lines):
        if re.match(r"^http:\s*(#.*)?$", line):
            http_idx = i
            break
    if http_idx is None:
        return lines
    # Section ends at the next top-level key (or EOF).
    end = len(lines)
    for j in range(http_idx + 1, len(lines)):
        if _is_top_level_key(lines[j]):
            end = j
            break
    block = "".join(lines[http_idx:end])
    if all(proxy in block for proxy in WRAPPER_PROXIES) and "server_port:" in block:
        print("[port_config] removed pre-v3 wrapper http: block (would re-arm the 5-min port trial)")
        return lines[:http_idx] + lines[end:]
    return lines


def _proxies_cover_edge(proxies) -> bool:
    """True if any stored trusted proxy network contains the edge IP."""
    try:
        edge = ip_address(EDGE_IP)
        return any(edge in ip_network(p) for p in (proxies or []))
    except ValueError:
        return False


def _store_is_reachable(stable: dict, port: int) -> bool:
    return (
        stable.get("server_port") == port
        and stable.get("use_x_forwarded_for") is True
        and _proxies_cover_edge(stable.get("trusted_proxies"))
    )


def _ensure_http_store(port: int) -> None:
    """Write .storage/http when the stored config would not serve $PORT
    behind Railway's edge. Merges with the existing stable config when
    readable (user settings survive); otherwise starts from HA's
    defaults. Never raises — a store problem must not break boot."""
    try:
        with open(HTTP_STORE_PATH, encoding="utf-8") as f:
            store = json.load(f)
        stable = store.get("data", {}).get("stable") or {}
        if not isinstance(stable, dict):
            stable = {}
    except (FileNotFoundError, ValueError, AttributeError, OSError):
        store, stable = {}, {}

    if _store_is_reachable(stable, port):
        return

    merged = dict(DEFAULT_HTTP_CONFIG)
    merged.update(stable)
    merged["server_port"] = port
    merged["use_x_forwarded_for"] = True
    # Union of the user's proxies and the wrapper's (theirs first).
    proxies = list(stable.get("trusted_proxies") or [])
    for proxy in WRAPPER_PROXIES:
        if proxy not in proxies:
            proxies.append(proxy)
    merged["trusted_proxies"] = proxies
    merged["created_at"] = datetime.now(timezone.utc).isoformat()
    merged["error"] = None
    merged["error_message"] = None

    payload = {
        "version": STORE_VERSION,
        "minor_version": STORE_MINOR_VERSION,
        "key": "http",
        "data": {
            "stable": merged,
            "pending": None,
            "yaml_migration_done": True,
        },
    }
    try:
        os.makedirs(os.path.dirname(HTTP_STORE_PATH), exist_ok=True)
        tmp = HTTP_STORE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, HTTP_STORE_PATH)
        print(
            f"[port_config] wrote .storage/http (stable port {port}, "
            f"proxy trust on, edge {EDGE_IP} covered)"
        )
    except OSError as exc:
        print(f"[port_config] WARNING: could not write .storage/http: {exc}")


def ensure_config() -> str:
    """Return what configuration.yaml should contain."""
    _ensure_included_files()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            lines = f.read().splitlines(keepends=True)
    except FileNotFoundError:
        return MINIMAL_CONFIG.format(default_config=DEFAULT_CONFIG)

    lines = _remove_wrapper_http_block(lines)

    # Standard includes (append only the missing ones).
    keys = _top_level_keys(lines)
    missing = [line for key, line in STANDARD_INCLUDES if key not in keys]
    if missing:
        text = "".join(lines)
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\n" + "\n".join(missing) + "\n"
        lines = text.splitlines(keepends=True)
        print(f"[port_config] appended missing includes: {missing}")

    return "".join(lines)


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    _ensure_http_store(port)
    content = ensure_config()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[port_config] configuration.yaml OK (port via SETUP_PORT={port})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
