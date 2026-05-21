#!/usr/bin/env python3
"""
Send a heartbeat ping to the Hub every PING_INTERVAL seconds.

The Hub marks an experiment offline/inactive if it receives no ping for
10 minutes, so we default to pinging every 3 minutes (180 s).

Requires env vars already populated by hub_bootstrap (via /tmp/jd-hub.env):
  JD_HUB_URL, JD_API_KEY, JD_EXP_NAME
"""
from __future__ import annotations

import os
import signal
import sys
import time

import requests

PING_INTERVAL = int(os.environ.get("JD_PING_INTERVAL", "180"))


def main() -> None:
    hub_url  = os.environ.get("JD_HUB_URL",  "").strip().rstrip("/")
    api_key  = os.environ.get("JD_API_KEY",  "").strip()
    exp_name = os.environ.get("JD_EXP_NAME", "").strip().lower()

    if not hub_url or not api_key or not exp_name:
        print("hub_heartbeat: Hub env not set — exiting (standalone mode)", file=sys.stderr)
        return

    url     = f"{hub_url}/api/experiments/{exp_name}/heartbeat"
    headers = {"Authorization": f"Bearer {api_key}"}

    print(f"hub_heartbeat: starting — will ping {url} every {PING_INTERVAL}s", file=sys.stderr)

    while True:
        try:
            r = requests.post(url, headers=headers, timeout=15)
            if r.status_code == 200:
                status = r.json().get("status", "ACTIVE")
                print(f"hub_heartbeat: OK (status={status})", file=sys.stderr)
            elif r.status_code == 403:
                # Hub signals the experiment is DELETED or EXPIRED — shut down
                # the entire container by sending SIGTERM to PID 1 (gunicorn).
                status = r.json().get("status", "DELETED")
                print(
                    f"hub_heartbeat: experiment is {status} — sending SIGTERM to PID 1.",
                    file=sys.stderr,
                )
                os.kill(1, signal.SIGTERM)
            else:
                print(f"hub_heartbeat: HTTP {r.status_code} — {r.text[:200]}", file=sys.stderr)
        except requests.RequestException as exc:
            print(f"hub_heartbeat: request failed: {exc}", file=sys.stderr)

        time.sleep(PING_INTERVAL)


if __name__ == "__main__":
    main()
