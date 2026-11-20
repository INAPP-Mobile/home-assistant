#!/usr/bin/env python3
"""Seed the Home Assistant owner account via the onboarding API.

Runs in the background from entrypoint.sh (its output goes to
/tmp/ha-seeder.log). Best-effort by design: if seeding fails, or the
deployer reaches the onboarding wizard first, the wizard still works and
the generated password is simply unused — this script must never break a
boot.

Flow (verified against homeassistant/components/onboarding/views.py,
tag 2026.11.2):
- GET /api/onboarding (unauthenticated) lists the onboarding steps with
  done flags. If the "user" step is already done, exit: someone created
  the owner account already.
- POST /api/onboarding/users {name, username, password, client_id,
  language} creates the owner (admin group), creates the Person, creates
  the default areas, and returns an auth_code. A 403 here means the step
  completed between the poll and the POST — a benign race, exit quietly.
Only the user step is seeded: the remaining onboarding steps (location,
units, integrations) still show in the UI on first login, which is the
normal first-run experience.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

PORT = os.environ.get("PORT", "8080")
PASSWORD = os.environ.get("HA_ADMIN_PASSWORD", "")
USERNAME = os.environ.get("HA_ADMIN_USERNAME", "admin")
BASE = f"http://127.0.0.1:{PORT}"
MARKER = "/config/.railway-admin-seeded"
POLL_SECONDS = 600
POLL_INTERVAL = 10


def log(msg: str) -> None:
    print(f"[seeder] {msg}", flush=True)


def http_json(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body) if body else {}
        except json.JSONDecodeError:
            return e.code, {"raw": body}
    except Exception as e:  # connection refused while HA is still booting
        return 0, {"error": str(e)}


def main() -> int:
    if not PASSWORD:
        log("HA_ADMIN_PASSWORD unset, nothing to seed")
        return 0
    if os.path.exists(MARKER):
        log("marker exists, owner already seeded")
        return 0

    deadline = time.time() + POLL_SECONDS
    while time.time() < deadline:
        status, body = http_json("GET", "/api/onboarding")
        if status == 200 and isinstance(body, list):
            steps = {s.get("step"): s.get("done") for s in body}
            log(f"onboarding steps: {steps}")
            if steps.get("user"):
                log("user step already done, exiting")
                return 0
            break
        time.sleep(POLL_INTERVAL)
    else:
        log(f"onboarding API did not answer within {POLL_SECONDS}s, giving up")
        return 1

    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    client_id = f"https://{domain}" if domain else "https://home-assistant.local"
    status, body = http_json(
        "POST",
        "/api/onboarding/users",
        {
            "name": "Home Assistant",
            "username": USERNAME,
            "password": PASSWORD,
            "client_id": client_id,
            "language": "en",
        },
    )
    if status == 200 and body.get("auth_code"):
        with open(MARKER, "w", encoding="utf-8") as f:
            f.write("seeded\n")
        log(f"owner account '{USERNAME}' created")
        return 0
    if status == 403:
        log("user step completed by someone else (race), exiting quietly")
        return 0
    log(f"user creation failed: HTTP {status} {body}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
