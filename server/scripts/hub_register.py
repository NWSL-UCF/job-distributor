#!/usr/bin/env python3
"""
Register the local dashboard admin_token with the Hub after the stack starts.

Requires: JD_HUB_URL, JD_API_KEY, JD_EXP_NAME, JD_WORKSPACE_PATH
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

import requests

POLL_SECS = 3
MAX_WAIT = 120


def _read_admin_token(workspace: str, exp_id: str) -> str | None:
    db_path = os.path.join(workspace, exp_id, "meta", "jobs.db")
    if not os.path.isfile(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        cur = conn.execute(
            "SELECT value FROM config WHERE key = 'admin_token' LIMIT 1"
        )
        row = cur.fetchone()
        conn.close()
        return row[0].strip() if row and row[0] else None
    except sqlite3.Error:
        return None


def main() -> int:
    hub_url = os.environ.get("JD_HUB_URL", "").strip().rstrip("/")
    api_key = os.environ.get("JD_API_KEY", "").strip()
    exp_name = os.environ.get("JD_EXP_NAME", "").strip().lower()
    workspace = os.environ.get("JD_WORKSPACE_PATH", "/workspace").strip()

    if not hub_url or not api_key or not exp_name:
        return 0

    print("hub_register: waiting for dashboard admin token…", file=sys.stderr)
    deadline = time.time() + MAX_WAIT
    admin_token = None
    while time.time() < deadline:
        admin_token = _read_admin_token(workspace, exp_name)
        if admin_token:
            break
        time.sleep(POLL_SECS)

    if not admin_token:
        print("hub_register: admin token not found in jobs.db", file=sys.stderr)
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
