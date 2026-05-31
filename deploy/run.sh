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
#   restart Restart hub app, or pass a service name (e.g. nginx, frps)
#   nginx-test  Validate nginx config + TLS cert paths (before restart)
#   build    Build hub image from local source (needs ../hub or HUB_SRC)
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

check_certs() {
    CERT="/etc/letsencrypt/live/jobdistributor.net/fullchain.pem"
    KEY="/etc/letsencrypt/live/jobdistributor.net/privkey.pem"
    DHP="/etc/letsencrypt/ssl-dhparams.pem"
    if [ ! -r "$CERT" ] || [ ! -r "$KEY" ]; then
        die "TLS cert not found at $CERT

  Nginx needs a cert for jobdistributor.net AND *.jobdistributor.net (DNS-01).
  See how_to_run/hub.md §4, then:
    sudo certbot certificates
    ./run.sh start"
    fi
    if [ ! -r "$DHP" ]; then
        die "DH params not found at $DHP
  Run: sudo openssl dhparam -out $DHP 2048"
    fi
}

check_landing() {
    [ -f landing/index.html ] || die "landing/index.html not found.
  Copy the deploy/landing/ folder from the repo before starting nginx."
}

nginx_test() {
    check_certs
    check_landing
    echo "Testing nginx config…"
    compose run --rm --no-deps nginx nginx -t
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
    check_certs
    check_landing
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
      if [ "$SERVICE" = "nginx" ]; then
        check_certs
        check_landing
      fi
      echo "Restarting service: $SERVICE …"
      compose restart "$SERVICE"
    else
      echo "Restarting hub app container…"
      compose restart hub
    fi
    echo "Done."
    ;;

  nginx-test)
    nginx_test
    echo "Nginx config OK."
    ;;

  build)
    check_env
    HUB_SRC="${HUB_SRC:-$(cd .. && pwd)/hub}"
    if [ ! -f "$HUB_SRC/Dockerfile" ]; then
      die "Hub source not found at $HUB_SRC

  deploy-only servers should pull the published image instead:
    ./run.sh pull && ./run.sh restart

  To build locally, clone the full repo so deploy/../hub exists, or run:
    HUB_SRC=/path/to/hub ./run.sh build"
    fi
    export HUB_SRC
    echo "Building hub image from $HUB_SRC …"
    compose -f "$COMPOSE_FILE" -f hub-compose.build.yml build hub
    echo "Recreating hub container with new image…"
    compose -f "$COMPOSE_FILE" -f hub-compose.build.yml up -d hub
    echo "Done. Tail plugin logs with: ./run.sh logs hub | grep 'frp plugin'"
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
    echo "Usage: ./run.sh [start|stop|restart|build|logs|status|pull|nginx-test]" >&2
    exit 1
    ;;

esac
