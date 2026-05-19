#!/usr/bin/env python3
"""
Fetch experiment runtime config from the Hub and write frpc.ini + env file.

Requires: JD_HUB_URL, JD_API_KEY, JD_EXP_NAME
Optional: JD_FRPC_CONFIG_PATH (default /frp-config/frpc.ini)
          JD_HUB_ENV_FILE (default /tmp/jd-hub.env)
"""
from __future__ import annotations

import os
import sys

import requests

FRPC_PATH = os.environ.get("JD_FRPC_CONFIG_PATH", "/tmp/frpc.ini").strip()
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

    frpc_dir = os.path.dirname(FRPC_PATH)
    if frpc_dir:
        os.makedirs(frpc_dir, mode=0o755, exist_ok=True)
    with open(FRPC_PATH, "w", encoding="utf-8") as f:
        f.write(frpc_config)
        if not frpc_config.endswith("\n"):
            f.write("\n")
    os.chmod(FRPC_PATH, 0o644)

    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write(f"JD_WORKER_SHARED_SECRET={worker_secret}\n")
        f.write(f"JD_HUB_URL={hub_url}\n")
        f.write(f"JD_API_KEY={api_key}\n")
        f.write(f"JD_EXP_NAME={exp_name}\n")

    print(f"hub_bootstrap: wrote {FRPC_PATH} and {ENV_FILE}", file=sys.stderr)
    print(f"hub_bootstrap: server_url={data.get('server_url')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
