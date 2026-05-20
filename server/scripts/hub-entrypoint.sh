#!/bin/sh
# Fetch FRP + worker credentials from Hub, start frpc, then start the server.
set -e

# ── Resolve workspace path ────────────────────────────────────────────────────
# On the host, experiment data lives at ~/jd_server/<expId>/
# The docker-compose volume maps that to /workspace/<expId> inside the container.
# JD_WORKSPACE (optional) lets users override the host root; inside the container
# the mount is always at /workspace so that's what we pass to start.py.
EXP="${JD_EXP_NAME:-default}"

# JD_WORKSPACE_PATH = root dir passed to start.py and hub_register.py.
# Both scripts append /<expId> internally, so this must NOT include the expId.
if [ -n "$JD_WORKSPACE" ]; then
  export JD_WORKSPACE_PATH="${JD_WORKSPACE}"
else
  export JD_WORKSPACE_PATH="/workspace"
fi

# ── Hub bootstrap (fetch frpc.ini + worker secret) ───────────────────────────
if [ -n "$JD_HUB_URL" ] && [ -n "$JD_API_KEY" ] && [ -n "$JD_EXP_NAME" ]; then
  python /app/scripts/hub_bootstrap.py || exit 1

  if [ -f /tmp/jd-hub.env ]; then
    set -a
    # shellcheck disable=SC1091
    . /tmp/jd-hub.env
    set +a
  fi

  # Start frpc in background using the fetched config
  FRPC_INI="${JD_FRPC_CONFIG_PATH:-/tmp/frpc.ini}"
  if [ -f "$FRPC_INI" ]; then
    echo "entrypoint: starting frpc with $FRPC_INI" >&2
    frpc -c "$FRPC_INI" &
  else
    echo "entrypoint: warning — frpc.ini not found at $FRPC_INI, tunnels will not start" >&2
  fi

  # Register admin token with Hub once the server DB is ready
  python /app/scripts/hub_register.py &

  # Send periodic heartbeat pings to the Hub (every 3 min by default)
  python /app/scripts/hub_heartbeat.py &
fi

exec python /app/start.py \
  "--expId=${EXP}" \
  "--workspace_path=${JD_WORKSPACE_PATH}" \
  "$@"
