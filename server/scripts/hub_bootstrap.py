#!/usr/bin/env python3
"""
Fetch experiment runtime config from the Hub.

Writes worker secrets to a short-lived env file (no frpc config on disk).
When --frpc-fifo is passed, streams the frpc INI into a named pipe so the
entrypoint can start frpc without ever writing the token to a file.

Requires: JD_HUB_URL, JD_API_KEY, JD_EXP_NAME
Optional: JD_HUB_ENV_FILE (default /tmp/jd-hub.env)
"""
from __future__ import annotations

import argparse
import os
import sys

import requests

ENV_FILE = os.environ.get("JD_HUB_ENV_FILE", "/tmp/jd-hub.env").strip()


def _fetch_runtime_config() -> dict:
    hub_url = os.environ.get("JD_HUB_URL", "").strip().rstrip("/")
    api_key = os.environ.get("JD_API_KEY", "").strip()
    exp_name = os.environ.get("JD_EXP_NAME", "").strip().lower()

    if not hub_url or not api_key or not exp_name:
        print("hub_bootstrap: Hub env not set — skipping (standalone mode)", file=sys.stderr)
        return {}

    url = f"{hub_url}/api/experiments/{exp_name}/runtime-config"
    print(f"hub_bootstrap: fetching {url}", file=sys.stderr)

    try:
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
    except requests.RequestException as exc:
        print(f"hub_bootstrap: request failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if r.status_code != 200:
        print(f"hub_bootstrap: Hub returned {r.status_code}: {r.text[:500]}", file=sys.stderr)
        raise SystemExit(1)

    data = r.json()
    frpc_config = data.get("frpc_config", "")
    worker_secret = data.get("worker_shared_secret", "")

    if not frpc_config or not worker_secret:
        print("hub_bootstrap: incomplete response from Hub", file=sys.stderr)
        raise SystemExit(1)

    return {
        "hub_url": hub_url,
        "api_key": api_key,
        "exp_name": exp_name,
        "frpc_config": frpc_config,
        "worker_secret": worker_secret,
        "server_url": data.get("server_url", ""),
    }


def _write_env_file(data: dict) -> None:
    """Write non-frpc secrets only — frpc config never touches disk."""
    env_dir = os.path.dirname(ENV_FILE)
    if env_dir:
        os.makedirs(env_dir, mode=0o700, exist_ok=True)
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write(f"JD_WORKER_SHARED_SECRET={data['worker_secret']}\n")
        f.write(f"JD_HUB_URL={data['hub_url']}\n")
        f.write(f"JD_API_KEY={data['api_key']}\n")
        f.write(f"JD_EXP_NAME={data['exp_name']}\n")
    os.chmod(ENV_FILE, 0o600)
    print(
        f"hub_bootstrap: wrote {ENV_FILE} (frpc config excluded — streamed via fifo)",
        file=sys.stderr,
    )
    print(f"hub_bootstrap: server_url={data['server_url']}", file=sys.stderr)


def _send_initial_heartbeat(data: dict) -> None:
    hb_url = f"{data['hub_url']}/api/experiments/{data['exp_name']}/heartbeat"
    try:
        r = requests.post(
            hb_url,
            headers={"Authorization": f"Bearer {data['api_key']}"},
            timeout=15,
        )
        print(f"hub_bootstrap: initial heartbeat → HTTP {r.status_code}", file=sys.stderr)
    except requests.RequestException as exc:
        print(f"hub_bootstrap: initial heartbeat failed: {exc}", file=sys.stderr)


def _write_frpc_fifo(fifo_path: str, frpc_config: str) -> None:
    """Stream frpc INI into a named pipe — never a regular file."""
    with open(fifo_path, "w", encoding="utf-8") as f:
        f.write(frpc_config)
    print(f"hub_bootstrap: frpc config streamed to fifo (not stored on disk)", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Hub runtime config")
    parser.add_argument(
        "--frpc-fifo",
        metavar="PATH",
        help="Write frpc INI to this named pipe (entrypoint creates the fifo)",
    )
    args = parser.parse_args()

    data = _fetch_runtime_config()
    if not data:
        return 0

    _write_env_file(data)

    if args.frpc_fifo:
        _write_frpc_fifo(args.frpc_fifo, data["frpc_config"])

    _send_initial_heartbeat(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
