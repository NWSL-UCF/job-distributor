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
#   ./run.sh mnist-v1 stop                       # stop & remove all containers
#   ./run.sh mnist-v1 logs                       # tail server logs
#   ./run.sh mnist-v1 status                     # show running containers
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
PG_IMAGE="postgres:16-alpine"
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

# ── Derive container names, network, and workspace ───────────────────────────
EXP="${JD_EXP_NAME:-server}"
CONTAINER="jd-${EXP}"
DB_CONTAINER="jd-db-${EXP}"
NETWORK="jd-net-${EXP}"
WORKSPACE_ROOT="${JD_WORKSPACE:-$HOME/jd_server}"
HOST_DATA="${WORKSPACE_ROOT}/${EXP}"
# PostgreSQL data lives inside the familiar meta/ folder — delete it for a fresh DB.
PG_DATA_DIR="${HOST_DATA}/meta/pgdata"
DATABASE_URL="postgresql://jd:jd_secret@${DB_CONTAINER}:5432/jobdistributor"

# ── Commands ─────────────────────────────────────────────────────────────────
case "$CMD" in

  stop)
    echo "Stopping $CONTAINER …"
    docker stop "$CONTAINER"    2>/dev/null && docker rm "$CONTAINER"    2>/dev/null || true
    echo "Stopping $DB_CONTAINER …"
    docker stop "$DB_CONTAINER" 2>/dev/null && docker rm "$DB_CONTAINER" 2>/dev/null || true
    docker network rm "$NETWORK" 2>/dev/null || true
    echo "Done."
    ;;

  logs)
    docker logs -f "$CONTAINER"
    ;;

  status)
    docker ps --filter "name=jd-${EXP}" --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"
    ;;

  start|*)
    # Create workspace and pgdata dirs on host
    mkdir -p "$PG_DATA_DIR"

    echo "Starting JobDistributor server"
    echo "  Experiment : $EXP"
    echo "  Hub        : $HUB_URL"
    echo "  Workspace  : $HOST_DATA"
    echo "  Container  : $CONTAINER"
    echo "  Database   : $DB_CONTAINER"
    echo "  Image      : $IMAGE"
    echo ""

    # ── Create shared network ─────────────────────────────────────────────
    docker network inspect "$NETWORK" >/dev/null 2>&1 \
      || docker network create "$NETWORK" >/dev/null

    # ── Start PostgreSQL if not already running ───────────────────────────
    if docker ps --format '{{.Names}}' | grep -q "^${DB_CONTAINER}$"; then
      echo "Database container already running."
    else
      # Remove stopped/crashed instance before re-creating
      docker rm "$DB_CONTAINER" 2>/dev/null || true

      echo "Starting database…"
      docker run -d \
        --name "$DB_CONTAINER" \
        --network "$NETWORK" \
        --restart unless-stopped \
        -e POSTGRES_DB=jobdistributor \
        -e POSTGRES_USER=jd \
        -e POSTGRES_PASSWORD=jd_secret \
        -v "${PG_DATA_DIR}:/var/lib/postgresql/data" \
        "$PG_IMAGE" \
        postgres -c max_connections=250 -c shared_buffers=512MB -c work_mem=4MB >/dev/null
    fi

    # ── Wait for PostgreSQL to accept connections ─────────────────────────
    echo "Waiting for database to be ready…"
    i=0
    while [ "$i" -lt 30 ]; do
      if docker exec "$DB_CONTAINER" pg_isready -U jd -d jobdistributor >/dev/null 2>&1; then
        echo "Database is ready."
        break
      fi
      i=$((i + 1))
      sleep 1
    done
    if [ "$i" -eq 30 ]; then
      echo "Warning: database did not become ready within 30 s — starting server anyway."
    fi

    # ── Pull latest server image ──────────────────────────────────────────
    echo "Pulling latest image…"
    docker pull "$IMAGE"
    echo ""

    # Remove old server container if it exists (allows re-running start)
    docker stop "$CONTAINER" 2>/dev/null || true
    docker rm   "$CONTAINER" 2>/dev/null || true

    # ── Start the server ──────────────────────────────────────────────────
    docker run -d \
      --name "$CONTAINER" \
      --network "$NETWORK" \
      --restart unless-stopped \
      -e JD_HUB_URL="$HUB_URL" \
      -e JD_API_KEY="$JD_API_KEY" \
      -e JD_EXP_NAME="$EXP" \
      -e DATABASE_URL="$DATABASE_URL" \
      -v "${HOST_DATA}:/workspace/${EXP}" \
      "$IMAGE"

    echo ""
    echo "Server started. Follow logs with:"
    echo "  docker logs -f $CONTAINER"
    echo "  (or: JD_EXP_NAME=$EXP ./run.sh logs)"
    echo ""
    echo "To stop everything:"
    echo "  ./run.sh $EXP stop"
    ;;

esac
