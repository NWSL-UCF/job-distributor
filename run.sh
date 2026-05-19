#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# JobDistributor Server — quick-start helper
#
# Usage:
#   JD_API_KEY=<key> JD_EXP_NAME=<name> ./run.sh
#
# Optional env vars:
#   JD_HUB_URL    Hub base URL  (default: https://hub.jobdistributor.net)
#   JD_WORKSPACE  Host root dir (default: ~/jd_server)
#                 Data will be stored at <JD_WORKSPACE>/<JD_EXP_NAME>/
#
# Commands (first arg):
#   (none)  Start the server (default)
#   stop    Stop & remove the container
#   logs    Follow container logs
#   status  Show container status
# ─────────────────────────────────────────────────────────────────────────────
set -e

IMAGE="${JD_IMAGE:-abdurrouf/jd-server:latest}"
HUB_URL="${JD_HUB_URL:-https://hub.jobdistributor.net}"

# ── Validate required args ───────────────────────────────────────────────────
CMD="${1:-start}"

if [ "$CMD" != "stop" ] && [ "$CMD" != "logs" ] && [ "$CMD" != "status" ]; then
  if [ -z "$JD_API_KEY" ]; then
    echo "Error: JD_API_KEY is required." >&2
    echo "  export JD_API_KEY=<your api key from Hub Profile>" >&2
    exit 1
  fi
  if [ -z "$JD_EXP_NAME" ]; then
    echo "Error: JD_EXP_NAME is required." >&2
    echo "  export JD_EXP_NAME=<your experiment name>" >&2
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
