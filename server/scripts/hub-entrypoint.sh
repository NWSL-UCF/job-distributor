#!/bin/sh
# Fetch FRP + worker credentials from Hub, start frpc, then start the server.
set -e

# ── Resolve workspace path ────────────────────────────────────────────────────
# Priority: JD_WORKSPACE env var → default ~/jd_server/<expId>
# Inside the container the host path is already mounted at /workspace, but
# JD_WORKSPACE_PATH lets the server know the logical path for sub-directories.
EXP="${JD_EXP_NAME:-default}"

if [ -n "$JD_WORKSPACE" ]; then
  # User supplied a custom root; append expId
  export JD_WORKSPACE_PATH="${JD_WORKSPACE}/${EXP}"
else
  # Default: /workspace/<expId>  (host maps ~/jd_server/<expId> → /workspace)
  export JD_WORKSPACE_PATH="/workspace/${EXP}"
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
  "$@"
