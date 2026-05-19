#!/bin/sh
# Fetch FRP + worker credentials from Hub, then start the server stack.
set -e

if [ -n "$JD_HUB_URL" ] && [ -n "$JD_API_KEY" ] && [ -n "$JD_EXP_NAME" ]; then
  python /app/scripts/hub_bootstrap.py || exit 1
  if [ -f /tmp/jd-hub.env ]; then
    set -a
    # shellcheck disable=SC1091
    . /tmp/jd-hub.env
    set +a
  fi
  python /app/scripts/hub_register.py &
fi

exec python /app/start.py "$@"
