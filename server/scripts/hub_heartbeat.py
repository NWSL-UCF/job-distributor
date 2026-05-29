#!/usr/bin/env python3
"""
Send a heartbeat ping to the Hub every PING_INTERVAL seconds.

The Hub marks an experiment offline/inactive if it receives no ping for
10 minutes, so we default to pinging every 3 minutes (180 s).

When the Hub marks an experiment DELETED or EXPIRED (HTTP 403), this script
gracefully shuts down the container stack:
  1. POST /admin/shutdown on the local job server (stop all workers)
  2. Wait for workers to poll and receive the stop command
  3. SIGTERM start.py so gunicorn + job_cleaner exit cleanly
  4. SIGTERM PID 1 (entrypoint) as a final fallback
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
import time

import requests

PING_INTERVAL = int(os.environ.get("JD_PING_INTERVAL", "180"))
SHUTDOWN_WAIT = int(os.environ.get("JD_SHUTDOWN_WAIT_SECS", "90"))
START_PID_FILE = "/tmp/jd-start.pid"
DEFAULT_SERVER_PORT = 8000


def _read_admin_token(workspace: str, exp_id: str) -> str | None:
    db_path = os.path.join(workspace, exp_id, "meta", "jobs.db")
    if not os.path.isfile(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        cur = conn.execute(
            "SELECT value FROM server_config WHERE key = 'admin_token' LIMIT 1"
        )
        row = cur.fetchone()
        conn.close()
        return row[0].strip() if row and row[0] else None
    except sqlite3.Error:
        return None


def _local_server_port() -> int:
    raw = os.environ.get("JD_SERVER_PORT", "").strip()
    if raw.isdigit():
        return int(raw)
    return DEFAULT_SERVER_PORT


def _request_local_shutdown(admin_token: str) -> bool:
    port = _local_server_port()
    url = f"http://127.0.0.1:{port}/admin/shutdown"
    try:
        r = requests.post(
            url,
            headers={"X-Admin-Token": admin_token},
            timeout=30,
        )
        if r.status_code == 200:
            body = r.json() if r.content else {}
            print(
                f"hub_heartbeat: local shutdown OK "
                f"(workers_stopped={body.get('workers_stopped', '?')})",
                file=sys.stderr,
            )
            return True
        print(
            f"hub_heartbeat: local shutdown HTTP {r.status_code}: {r.text[:200]}",
            file=sys.stderr,
        )
    except requests.RequestException as exc:
        print(f"hub_heartbeat: local shutdown failed: {exc}", file=sys.stderr)
    return False


def _terminate_start_stack() -> None:
    """SIGTERM start.py (supervises gunicorn + job_cleaner)."""
    if os.path.isfile(START_PID_FILE):
        try:
            with open(START_PID_FILE, encoding="utf-8") as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGTERM)
            print(f"hub_heartbeat: sent SIGTERM to start.py (pid {pid})", file=sys.stderr)
            return
        except (OSError, ValueError) as exc:
            print(f"hub_heartbeat: could not signal start.py: {exc}", file=sys.stderr)

    workspace = os.environ.get("JD_WORKSPACE_PATH", "/workspace").strip()
    exp_name = os.environ.get("JD_EXP_NAME", "").strip().lower()
    if workspace and exp_name:
        pid_file = os.path.join(workspace, exp_name, "meta", "pids.json")
        if os.path.isfile(pid_file):
            try:
                with open(pid_file, encoding="utf-8") as f:
                    pids = json.load(f)
                for name, pid in pids.items():
                    try:
                        os.kill(int(pid), signal.SIGTERM)
                        print(
                            f"hub_heartbeat: sent SIGTERM to {name} (pid {pid})",
                            file=sys.stderr,
                        )
                    except (OSError, ValueError):
                        pass
            except (OSError, json.JSONDecodeError, TypeError):
                pass


def _graceful_shutdown(status: str) -> None:
    print(
        f"hub_heartbeat: experiment is {status} — beginning graceful shutdown.",
        file=sys.stderr,
    )

    workspace = os.environ.get("JD_WORKSPACE_PATH", "/workspace").strip()
    exp_name = os.environ.get("JD_EXP_NAME", "").strip().lower()
    admin_token = _read_admin_token(workspace, exp_name) if exp_name else None

    if admin_token:
        _request_local_shutdown(admin_token)
    else:
        print("hub_heartbeat: no admin token — skipping local shutdown API", file=sys.stderr)

    if SHUTDOWN_WAIT > 0:
        print(
            f"hub_heartbeat: waiting {SHUTDOWN_WAIT}s for workers to stop…",
            file=sys.stderr,
        )
        time.sleep(SHUTDOWN_WAIT)

    _terminate_start_stack()
    time.sleep(3)

    print("hub_heartbeat: sending SIGTERM to PID 1.", file=sys.stderr)
    os.kill(1, signal.SIGTERM)


def main() -> None:
    hub_url = os.environ.get("JD_HUB_URL", "").strip().rstrip("/")
    api_key = os.environ.get("JD_API_KEY", "").strip()
    exp_name = os.environ.get("JD_EXP_NAME", "").strip().lower()

    if not hub_url or not api_key or not exp_name:
        print("hub_heartbeat: Hub env not set — exiting (standalone mode)", file=sys.stderr)
        return

    url = f"{hub_url}/api/experiments/{exp_name}/heartbeat"
    headers = {"Authorization": f"Bearer {api_key}"}

    print(f"hub_heartbeat: starting — will ping {url} every {PING_INTERVAL}s", file=sys.stderr)

    while True:
        try:
            r = requests.post(url, headers=headers, timeout=15)
            if r.status_code == 200:
                status = r.json().get("status", "ACTIVE")
                print(f"hub_heartbeat: OK (status={status})", file=sys.stderr)
            elif r.status_code == 403:
                status = r.json().get("status", "DELETED")
                _graceful_shutdown(status)
                return
            else:
                print(f"hub_heartbeat: HTTP {r.status_code} — {r.text[:200]}", file=sys.stderr)
        except requests.RequestException as exc:
            print(f"hub_heartbeat: request failed: {exc}", file=sys.stderr)

        time.sleep(PING_INTERVAL)


if __name__ == "__main__":
    main()
