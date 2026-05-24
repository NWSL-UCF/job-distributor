#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# JobDistributor Hub — quick-start helper
#
# Usage:
#   ./run.sh [command]
#
# Commands:
#   start   Pull the latest images and start (or restart) the full stack
#   stop    Stop and remove all hub containers
#   restart Restart only the hub app container (e.g. after hub.env change)
#   logs    Tail logs from all containers  (or pass a service name)
#   status  Show running containers
#   pull    Pull latest images without restarting
#
# Examples:
#   ./run.sh                       # same as: ./run.sh start
#   ./run.sh start
#   ./run.sh restart               # restart hub_app only (no nginx/frps downtime)
#   ./run.sh restart frps          # restart a specific service
#   ./run.sh logs                  # tail all logs
#   ./run.sh logs hub              # tail hub app logs only
#   ./run.sh stop
#   ./run.sh status
#   ./run.sh pull
#
# Prerequisites:
#   • hub.env must exist in this directory (copy from hub.env.example and fill in)
#   • /etc/letsencrypt certs must exist on the host (see how_to_run/hub.md §4)
#   • MySQL must be running on the host
# ─────────────────────────────────────────────────────────────────────────────
set -e

COMPOSE_FILE="hub-compose.yml"
cd "$(dirname "$0")"

# ── Helpers ──────────────────────────────────────────────────────────────────
die() { echo "Error: $*" >&2; exit 1; }

check_env() {
    [ -f hub.env ] || die "hub.env not found.\n  cp hub.env.example hub.env  and fill in the values."
}

compose() {
    docker compose -f "$COMPOSE_FILE" "$@"
}

# ── Parse command ─────────────────────────────────────────────────────────────
CMD="${1:-start}"
SERVICE="${2:-}"   # optional service name for logs / restart

case "$CMD" in

  start)
    check_env
    echo "Pulling latest images…"
    compose pull
    echo ""
    echo "Starting Hub stack (nginx, hub, frps)…"
    compose up -d
    echo ""
    echo "Hub stack is up. Tail logs with:"
    echo "  ./run.sh logs"
    ;;

  stop)
    echo "Stopping Hub stack…"
    compose down
    echo "Done."
    ;;

  restart)
    check_env
    if [ -n "$SERVICE" ]; then
      echo "Restarting service: $SERVICE …"
      compose restart "$SERVICE"
    else
      echo "Restarting hub app container…"
      compose restart hub
    fi
    echo "Done."
    ;;

  pull)
    echo "Pulling latest images…"
    compose pull
    echo "Run './run.sh restart' or './run.sh start' to apply."
    ;;

  logs)
    if [ -n "$SERVICE" ]; then
      compose logs -f "$SERVICE"
    else
      compose logs -f
    fi
    ;;

  status)
    compose ps
    ;;

  *)
    echo "Unknown command: $CMD" >&2
    echo "Usage: ./run.sh [start|stop|restart|logs|status|pull]" >&2
    exit 1
    ;;

esac
