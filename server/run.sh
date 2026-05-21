#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# JobDistributor Server — quick-start helper
#
# Usage:
#   ./run.sh <expName> [command]
#
# Examples:
#   JD_API_KEY=jd_xxx ./run.sh mnist-v1         # start experiment
#   JD_API_KEY=jd_xxx ./run.sh mnist-v1 start   # same as above
#   ./run.sh mnist-v1 stop                       # stop & remove container
#   ./run.sh mnist-v1 logs                       # tail container logs
#   ./run.sh mnist-v1 status                     # show running status
#
# You can also pass the experiment name via the env var (legacy):
#   JD_API_KEY=jd_xxx JD_EXP_NAME=mnist-v1 ./run.sh
#
# Optional env vars:
#   JD_HUB_URL    Hub base URL  (default: https://hub.jobdistributor.net)
#   JD_WORKSPACE  Host root dir (default: ~/jd_server)
#                 Data stored at <JD_WORKSPACE>/<expName>/
# ─────────────────────────────────────────────────────────────────────────────
set -e

IMAGE="${JD_IMAGE:-jobdistributor/jd-server:latest}"
HUB_URL="${JD_HUB_URL:-https://hub.jobdistributor.net}"

# ── Parse args — support both positional and env-var style ──────────────────
# If first arg is a known command keyword, treat it as CMD with no expName arg.
# Otherwise first arg is expName and second (optional) arg is CMD.
case "${1:-}" in
  start|stop|logs|status|"")
    CMD="${1:-start}"
    # expName must come from env
    ;;
  *)
    # First arg is the experiment name
    JD_EXP_NAME="${1}"
    CMD="${2:-start}"
    ;;
esac

if [ "$CMD" != "stop" ] && [ "$CMD" != "logs" ] && [ "$CMD" != "status" ]; then
  if [ -z "$JD_API_KEY" ]; then
    echo "Error: JD_API_KEY is required." >&2
    echo "  export JD_API_KEY=<your api key from Hub Profile>" >&2
    exit 1
  fi
  if [ -z "$JD_EXP_NAME" ]; then
    echo "Error: experiment name is required." >&2
    echo "  Usage: JD_API_KEY=<key> ./run.sh <expName>" >&2
    exit 1
  fi
fi

# ── Derive container name and workspace ─────────────────────────────────────
CONTAINER="jd-${JD_EXP_NAME:-server}"
WORKSPACE_ROOT="${JD_WORKSPACE:-$HOME/jd_server}"
HOST_DATA="${WORKSPACE_ROOT}/${JD_EXP_NAME}"

# ── Commands ─────────────────────────────────────────────────────────────────
case "$CMD" in

  stop)
    echo "Stopping $CONTAINER…"
    docker stop "$CONTAINER" 2>/dev/null && docker rm "$CONTAINER" 2>/dev/null || true
    echo "Done."
    ;;

  logs)
    docker logs -f "$CONTAINER"
    ;;

  status)
    docker ps --filter "name=$CONTAINER" --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"
    ;;

  start|*)
    # Create workspace dir on host so the mount works even before first run
    mkdir -p "$HOST_DATA"

    echo "Starting JobDistributor server"
    echo "  Experiment : $JD_EXP_NAME"
    echo "  Hub        : $HUB_URL"
    echo "  Workspace  : $HOST_DATA"
    echo "  Container  : $CONTAINER"
    echo "  Image      : $IMAGE"
    echo ""

    echo "Pulling latest image…"
    docker pull "$IMAGE"
    echo ""

    docker run -d \
      --name "$CONTAINER" \
      --restart unless-stopped \
      -e JD_HUB_URL="$HUB_URL" \
      -e JD_API_KEY="$JD_API_KEY" \
      -e JD_EXP_NAME="$JD_EXP_NAME" \
      -v "$HOST_DATA:/workspace/$JD_EXP_NAME" \
      "$IMAGE"

    echo ""
    echo "Container started. Follow logs with:"
    echo "  docker logs -f $CONTAINER"
    echo "  (or: JD_EXP_NAME=$JD_EXP_NAME ./run.sh logs)"
    ;;

esac
