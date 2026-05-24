#!/bin/bash
# Fetch FRP + worker credentials from Hub, start the server stack, then frpc.
#
# Security design:
#   • hub_bootstrap.py writes only non-frpc secrets to /tmp/jd-hub.env
#   • The frpc config (including metadatas.exp_token) is streamed through a
#     named pipe (FIFO) in /dev/shm — never written to a regular file
#   • frpc starts only after gunicorn is listening on 8000/8001 so proxies
#     register against live backends
#   • The env file and fifo are removed immediately after use
set -e

# ── Resolve workspace path ────────────────────────────────────────────────────
EXP="${JD_EXP_NAME:-default}"

if [ -n "$JD_WORKSPACE" ]; then
  export JD_WORKSPACE_PATH="${JD_WORKSPACE}"
else
  export JD_WORKSPACE_PATH="/workspace"
fi

_wait_port() {
  local port="$1"
  python - "$port" <<'PY'
import socket, sys, time
port = int(sys.argv[1])
for _ in range(120):
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
        s.close()
        sys.exit(0)
    except OSError:
        time.sleep(0.5)
print(f"entrypoint: timed out waiting for port {port}", file=sys.stderr)
sys.exit(1)
PY
}

# ── Hub bootstrap (fetch secrets + heartbeat; frpc deferred) ───────────────
if [ -n "$JD_HUB_URL" ] && [ -n "$JD_API_KEY" ] && [ -n "$JD_EXP_NAME" ]; then
  if ! python /app/scripts/hub_bootstrap.py; then
    exit 1
  fi

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

  # Start gunicorn (server + dashboard) before frpc registers HTTP routes.
  python /app/start.py \
    "--expId=${EXP}" \
    "--workspace_path=${JD_WORKSPACE_PATH}" \
    "$@" &
  START_PID=$!

  _wait_port 8000
  _wait_port 8001
  echo "entrypoint: job server and dashboard listening — starting frpc" >&2

  FRPC_FIFO="/dev/shm/frpc-$$.toml"
  mkfifo "$FRPC_FIFO"
  chmod 600 "$FRPC_FIFO"

  frpc -c "$FRPC_FIFO" &
  FRPC_PID=$!

  if ! python /app/scripts/hub_bootstrap.py --frpc-fifo "$FRPC_FIFO" --frpc-only; then
    kill "$FRPC_PID" 2>/dev/null || true
    kill "$START_PID" 2>/dev/null || true
    rm -f "$FRPC_FIFO"
    exit 1
  fi

  sleep 2
  if ! kill -0 "$FRPC_PID" 2>/dev/null; then
    echo "entrypoint: frpc exited immediately after startup — config or auth failed" >&2
    kill "$START_PID" 2>/dev/null || true
    rm -f "$FRPC_FIFO"
    exit 1
  fi

  rm -f "$FRPC_FIFO"
  echo "entrypoint: frpc fifo removed (config was never written to disk)" >&2

  wait "$START_PID"
  exit $?
fi

exec python /app/start.py \
  "--expId=${EXP}" \
  "--workspace_path=${JD_WORKSPACE_PATH}" \
  "$@"
