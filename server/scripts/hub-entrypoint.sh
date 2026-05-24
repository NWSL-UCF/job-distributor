#!/bin/bash
# Fetch FRP + worker credentials from Hub, start frpc, then start the server.
#
# Security design:
#   • hub_bootstrap.py writes only non-frpc secrets to /tmp/jd-hub.env
#   • The frpc config (including meta_exp_token) is streamed through a named
#     pipe (FIFO) in /dev/shm — never written to a regular file
#   • The env file and fifo are removed immediately after use
set -e

# ── Resolve workspace path ────────────────────────────────────────────────────
EXP="${JD_EXP_NAME:-default}"

if [ -n "$JD_WORKSPACE" ]; then
  export JD_WORKSPACE_PATH="${JD_WORKSPACE}"
else
  export JD_WORKSPACE_PATH="/workspace"
fi

# ── Hub bootstrap (fetch frpc config + worker secret) ────────────────────────
if [ -n "$JD_HUB_URL" ] && [ -n "$JD_API_KEY" ] && [ -n "$JD_EXP_NAME" ]; then
  FRPC_FIFO="/dev/shm/frpc-$$.ini"
  mkfifo "$FRPC_FIFO"
  chmod 600 "$FRPC_FIFO"

  # frpc blocks until the fifo is written; config exists only in kernel pipe buffers
  frpc -c "$FRPC_FIFO" &
  FRPC_PID=$!

  if ! python /app/scripts/hub_bootstrap.py --frpc-fifo "$FRPC_FIFO"; then
    kill "$FRPC_PID" 2>/dev/null || true
    rm -f "$FRPC_FIFO"
    exit 1
  fi

  rm -f "$FRPC_FIFO"
  echo "entrypoint: frpc fifo removed (config was never written to disk)" >&2

  if [ -f /tmp/jd-hub.env ]; then
    set -a
    # shellcheck disable=SC1091
    . /tmp/jd-hub.env
    set +a
    rm -f /tmp/jd-hub.env
    echo "entrypoint: env file loaded and removed from disk" >&2
  fi

  python /app/scripts/hub_register.py &
  python /app/scripts/hub_heartbeat.py &
fi

exec python /app/start.py \
  "--expId=${EXP}" \
  "--workspace_path=${JD_WORKSPACE_PATH}" \
  "$@"
