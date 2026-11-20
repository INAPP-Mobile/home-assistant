#!/usr/bin/env python3
"""Manage the `http: server_port:` key in /config/configuration.yaml.

Home Assistant reads its listen port ONLY from that key (no PORT env
support — verified in homeassistant/components/http/__init__.py,
tag 2026.11.2). This script is line-based on purpose: it preserves the
user's file byte-for-byte except for the one managed key, so hand edits,
comments, and formatting survive a redeploy.

Behaviour for the given PORT:
- configuration.yaml missing -> write the minimal wrapper config
  (default_config, homeassistant name, http block with server_port,
  server_host, use_x_forwarded_for + trusted_proxies for Railway's edge).
- http: section exists with a server_port line -> replace its value.
- http: section exists without server_port -> insert the key after the
  `http:` line.
- no http: section -> append a full http: block.
- server_host / use_x_forwarded_for / trusted_proxies are only written
  on first creation; an existing http: section is left alone apart from
  the port, so a deployer who tuned the proxy settings keeps them.
"""

import re
import sys

CONFIG_PATH = "/config/configuration.yaml"

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

MINIMAL_CONFIG = """# Home Assistant configuration — created by the Railway wrapper.
# server_port is managed by the wrapper (it follows Railway's $PORT on
# every boot). Everything else here is yours: edit freely, it survives
# redeploys. Set your home location and time zone in the UI onboarding
# (Settings -> System -> General) rather than here.
default_config:

homeassistant:
  name: Home

{http_block}"""


def _is_top_level_key(line: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][^:\s][^:]*:\s*(#.*)?$", line))


def ensure_port(port: str) -> str:
    """Return what configuration.yaml should contain for this port."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            lines = f.read().splitlines(keepends=True)
    except FileNotFoundError:
        return MINIMAL_CONFIG.format(http_block=HTTP_BLOCK.format(port=port))

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
        return "".join(lines)

    if http_idx is not None:
        lines.insert(http_idx + 1, f"  server_port: {port}\n")
        return "".join(lines)

    text = "".join(lines)
    if text and not text.endswith("\n"):
        text += "\n"
    return text + "\n" + HTTP_BLOCK.format(port=port)


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "8080"
    content = ensure_port(port)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[port_config] server_port={port} in {CONFIG_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
