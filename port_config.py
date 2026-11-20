#!/usr/bin/env python3
"""Manage /config/configuration.yaml for Home Assistant on Railway.

Home Assistant reads its listen port ONLY from the `http: server_port:`
key (no PORT env support — verified in
homeassistant/components/http/__init__.py, tag 2026.11.2). This script is
line-based on purpose: it preserves the user's file byte-for-byte except
for the keys it manages, so hand edits, comments, and formatting survive
a redeploy.

On every boot it ensures three things:
1. server_port follows Railway's $PORT (created, replaced, or inserted
   inside the http: section).
2. The standard includes from upstream's own DEFAULT_CONFIG
   (homeassistant/config.py) are present — `automation: !include
   automations.yaml`, `script: !include scripts.yaml`, `scene: !include
   scenes.yaml`. A minimal config without them loads no automation/script/
   scene integrations at all, so the automation API would accept configs
   that never become entities (proven in e2e 2026-11-20: the automation
   config saved and read back, but no automation entity ever appeared).
   Missing keys are appended; an existing key is never touched.
3. The included files exist (automations.yaml as "[]", scripts.yaml and
   scenes.yaml empty — exactly what upstream's _write_default_config
   writes), so a fresh volume boots cleanly.

When configuration.yaml is missing entirely, the minimal config written
is upstream's DEFAULT_CONFIG verbatim, plus a `homeassistant: name:` and
the http: block (server_port, server_host, use_x_forwarded_for +
trusted_proxies for Railway's edge — without those, a burst of failed
logins can get the proxy IP banned and lock everyone out).
"""

import os
import re
import sys

CONFIG_DIR = "/config"
CONFIG_PATH = os.path.join(CONFIG_DIR, "configuration.yaml")

HTTP_BLOCK = """http:
  server_host: 0.0.0.0
  server_port: {port}
  use_x_forwarded_for: true
  trusted_proxies:
    - 10.0.0.0/8
    - 172.16.0.0/12
    - 192.168.0.0/16
    - 100.64.0.0/10
"""

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
# server_port is managed by the wrapper (it follows Railway's $PORT on
# every boot). Everything else here is yours: edit freely, it survives
# redeploys. Set your home location and time zone in the UI onboarding
# (Settings -> System -> General) rather than here.
{default_config}
homeassistant:
  name: Home

{http_block}"""

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


def ensure_port(port: str) -> str:
    """Return what configuration.yaml should contain for this port."""
    _ensure_included_files()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            lines = f.read().splitlines(keepends=True)
    except FileNotFoundError:
        return MINIMAL_CONFIG.format(
            default_config=DEFAULT_CONFIG, http_block=HTTP_BLOCK.format(port=port)
        )

    # 1. server_port inside the http: section.
    http_idx = None
    port_idx = None
    for i, line in enumerate(lines):
        if http_idx is None:
            if re.match(r"^http:\s*(#.*)?$", line):
                http_idx = i
        elif _is_top_level_key(line):
            break  # left the http: section
        elif re.match(r"^\s+server_port:\s*.*$", line):
            port_idx = i
            break

    if port_idx is not None:
        indent = re.match(r"^(\s*)server_port:", lines[port_idx]).group(1)
        lines[port_idx] = f"{indent}server_port: {port}\n"
    elif http_idx is not None:
        lines.insert(http_idx + 1, f"  server_port: {port}\n")
    else:
        text = "".join(lines)
        if text and not text.endswith("\n"):
            text += "\n"
        lines = (text + "\n" + HTTP_BLOCK.format(port=port)).splitlines(keepends=True)

    # 2. Standard includes (append only the missing ones).
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
    port = sys.argv[1] if len(sys.argv) > 1 else "8080"
    content = ensure_port(port)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[port_config] server_port={port} in {CONFIG_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
