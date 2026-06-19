#!/usr/bin/env python3
"""
Register the local dashboard admin_token with the Hub after the stack starts.

Requires: JD_HUB_URL, JD_API_KEY, JD_EXP_NAME, JD_WORKSPACE_PATH
"""
from __future__ import annotations

import os
import sys
import time

import requests

POLL_SECS = 3
MAX_WAIT = 120
DEFAULT_SERVER_PORT = 8000


def _read_admin_token_via_http(port: int) -> str | None:
    """Fetch the admin token from the local job server's /admin/token endpoint."""
    try:
        r = requests.get(f"http://127.0.0.1:{port}/admin/token", timeout=5)
        if r.status_code == 200:
            token = r.json().get("admin_token", "").strip()
            return token or None
    except requests.RequestException:
        pass
    return None


def _server_port() -> int:
    raw = os.environ.get("JD_SERVER_PORT", "").strip()
    return int(raw) if raw.isdigit() else DEFAULT_SERVER_PORT


def main() -> int:
    hub_url = os.environ.get("JD_HUB_URL", "").strip().rstrip("/")
    api_key = os.environ.get("JD_API_KEY", "").strip()
    exp_name = os.environ.get("JD_EXP_NAME", "").strip().lower()

    if not hub_url or not api_key or not exp_name:
        return 0

    port = _server_port()
    print("hub_register: waiting for dashboard admin token…", file=sys.stderr)
    deadline = time.time() + MAX_WAIT
    admin_token = None
    while time.time() < deadline:
        admin_token = _read_admin_token_via_http(port)
        if admin_token:
            break
        time.sleep(POLL_SECS)

    if not admin_token:
        print("hub_register: admin token not available from server", file=sys.stderr)
        return 1

    url = f"{hub_url}/api/experiments/{exp_name}/register"
    try:
        r = requests.post(
            url,
            json={"admin_token": admin_token},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
    except requests.RequestException as exc:
        print(f"hub_register: request failed: {exc}", file=sys.stderr)
        return 1

    if r.status_code == 200:
        print("hub_register: registered with Hub", file=sys.stderr)
        return 0

    print(f"hub_register: Hub returned {r.status_code}: {r.text[:300]}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
