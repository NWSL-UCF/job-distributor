#!/bin/bash
# Fetch FRP + worker credentials from Hub, start frpc, then start the server.
#
# Security design:
#   hub_bootstrap.py stores the frpc config as a base64-encoded env var
#   (FRPC_CONFIG_B64) inside /tmp/jd-hub.env.  This entrypoint sources that
#   file into memory, deletes it from disk immediately, then starts frpc via
#   bash process substitution (<(...)) so the decoded config — including the
#   FRPS token — is passed directly to frpc as a named pipe and never written
#   to the container filesystem.
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

# ── Hub bootstrap (fetch frpc config + worker secret) ────────────────────────
if [ -n "$JD_HUB_URL" ] && [ -n "$JD_API_KEY" ] && [ -n "$JD_EXP_NAME" ]; then
  python /app/scripts/hub_bootstrap.py || exit 1

  if [ -f /tmp/jd-hub.env ]; then
    set -a
    # shellcheck disable=SC1091
    . /tmp/jd-hub.env
    set +a
    # Delete the env file immediately — all secrets are now in process memory
    # only. Anyone doing "docker exec ... cat /tmp/jd-hub.env" will find nothing.
    rm -f /tmp/jd-hub.env
    echo "entrypoint: env file loaded and removed from disk" >&2
  fi

  # Start frpc via bash process substitution: the config is decoded from the
  # base64 env var and piped directly to frpc through /dev/fd/<N>.
  # Nothing is written to disk — the FRPS token never appears in the filesystem.
  if [ -n "$FRPC_CONFIG_B64" ]; then
    echo "entrypoint: starting frpc (config decoded in memory, never on disk)" >&2
    frpc -c <(echo "$FRPC_CONFIG_B64" | base64 -d) &
    # Clear the env var so it is no longer visible in /proc/<pid>/environ
    unset FRPC_CONFIG_B64
  else
    echo "entrypoint: warning — FRPC_CONFIG_B64 not set, tunnels will not start" >&2
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
