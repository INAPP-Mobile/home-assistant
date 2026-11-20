#!/usr/bin/env python3
"""Manage /config/configuration.yaml for Home Assistant on Railway.

PORT MECHANISM (verified against upstream source, tag 2026.11.2):
Home Assistant's default listen port comes from
``homeassistant/components/http/config.py: default_server_port()``, which
reads the ``SETUP_PORT`` environment variable — the entrypoint exports
``SETUP_PORT=$PORT`` before starting HA. That is the upstream-supported
way to set the port; do NOT write ``http: server_port:`` into
configuration.yaml.

WHY NOT YAML (the 2026.11 trial gotcha, proven live 2026-11-20): HA
2026.11 migrates a YAML ``http:`` block into its ``.storage/http``
store as a PENDING (trial) config. It serves on the YAML port for 5
minutes, then — unless the pending config is confirmed in the UI —
reverts to the stored STABLE config (port 8123) and restarts. The
Railway domain targets $PORT, so the instance 502s forever after the
first 5 minutes. With no YAML ``http:`` block at all, the store's
stable config IS the default (port = SETUP_PORT) and no trial ever
starts.

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
   printed by its exact trusted_proxies list) is removed — it would
   otherwise re-arm the trial on a fresh volume. Any other ``http:``
   block is the user's own and is left alone.
4. The ``.storage/http`` store is healed: if its stable server_port
   differs from $PORT (e.g. a volume poisoned by the pre-v3 trial, whose
   stable port reverted to 8123), the store file is deleted so HA
   rebuilds it from the default (port = SETUP_PORT). The store holds
   only HTTP config, no user data.

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
# The listen port comes from the SETUP_PORT env var (see entrypoint.sh),
# NOT from this file — HA 2026.11 trials a YAML http: block for 5 min
# then reverts to port 8123. Everything else here is yours: edit freely,
# it survives redeploys. Set your home location and time zone in the UI
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


def _heal_http_store(port: int) -> None:
    """Delete a stale .storage/http store whose stable port != $PORT.

    A volume poisoned by the pre-v3 YAML trial has stable.server_port
    8123; with the store deleted HA rebuilds it from the default
    (port = SETUP_PORT). Unparseable store: leave alone, never break boot.
    """
    try:
        with open(HTTP_STORE_PATH, encoding="utf-8") as f:
            store = json.load(f)
        stable_port = store["data"]["stable"]["server_port"]
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return
    if stable_port != port:
        os.remove(HTTP_STORE_PATH)
        print(f"[port_config] removed stale .storage/http (stable port {stable_port} != {port})")


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
    _heal_http_store(port)
    content = ensure_config()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[port_config] configuration.yaml OK (port via SETUP_PORT={port})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
