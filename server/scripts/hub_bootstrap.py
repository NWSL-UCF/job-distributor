#!/usr/bin/env python3
"""
Fetch experiment runtime config from the Hub and write an env file.

The frpc config is base64-encoded and stored as FRPC_CONFIG_B64 in the env
file.  The entrypoint reads the env file into memory, deletes it from disk,
and starts frpc via bash process substitution — so the FRPS token never
touches the container filesystem.

Requires: JD_HUB_URL, JD_API_KEY, JD_EXP_NAME
Optional: JD_HUB_ENV_FILE (default /tmp/jd-hub.env)
"""
from __future__ import annotations

import base64
import os
import sys

import requests

ENV_FILE = os.environ.get("JD_HUB_ENV_FILE", "/tmp/jd-hub.env").strip()


def main() -> int:
    hub_url = os.environ.get("JD_HUB_URL", "").strip().rstrip("/")
    api_key = os.environ.get("JD_API_KEY", "").strip()
    exp_name = os.environ.get("JD_EXP_NAME", "").strip().lower()

    if not hub_url or not api_key or not exp_name:
        print("hub_bootstrap: Hub env not set — skipping (standalone mode)", file=sys.stderr)
        return 0

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
        return 1

    if r.status_code != 200:
        print(f"hub_bootstrap: Hub returned {r.status_code}: {r.text[:500]}", file=sys.stderr)
        return 1

    data = r.json()
    frpc_config = data.get("frpc_config", "")
    worker_secret = data.get("worker_shared_secret", "")

    if not frpc_config or not worker_secret:
        print("hub_bootstrap: incomplete response from Hub", file=sys.stderr)
        return 1

    # Encode the frpc config as base64 so it can be stored as a single-line
    # env var and decoded in memory by the entrypoint — no config file on disk.
    frpc_config_b64 = base64.b64encode(frpc_config.encode()).decode()

    env_dir = os.path.dirname(ENV_FILE)
    if env_dir:
        os.makedirs(env_dir, mode=0o700, exist_ok=True)
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write(f"JD_WORKER_SHARED_SECRET={worker_secret}\n")
        f.write(f"JD_HUB_URL={hub_url}\n")
        f.write(f"JD_API_KEY={api_key}\n")
        f.write(f"JD_EXP_NAME={exp_name}\n")
        f.write(f"FRPC_CONFIG_B64={frpc_config_b64}\n")
    os.chmod(ENV_FILE, 0o600)

    print(f"hub_bootstrap: wrote {ENV_FILE} (frpc config base64-encoded, no file on disk)", file=sys.stderr)
    print(f"hub_bootstrap: server_url={data.get('server_url')}", file=sys.stderr)

    # Send an initial heartbeat NOW, before frpc starts, so the Hub considers
    # this experiment online when frps fires the NewProxy authorisation hook.
    # hub_heartbeat.py handles all subsequent periodic pings.
    hb_url = f"{hub_url}/api/experiments/{exp_name}/heartbeat"
    try:
        r = requests.post(
            hb_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        print(f"hub_bootstrap: initial heartbeat → HTTP {r.status_code}", file=sys.stderr)
    except requests.RequestException as exc:
        # Non-fatal: frpc will retry on reconnect; log and continue.
        print(f"hub_bootstrap: initial heartbeat failed: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
