"""
Ask a running experiment server to stop all workers before Hub tears down tunnels.
"""
from __future__ import annotations

import logging

import requests

from . import config

log = logging.getLogger(__name__)


def request_server_shutdown(exp) -> dict:
    """POST /admin/shutdown on the experiment job server (best effort).

    Returns a dict with keys: called, ok, workers_stopped, error.
    """
    if not exp.admin_token:
        return {
            "called": False,
            "ok": False,
            "workers_stopped": None,
            "error": "Server not registered (no admin token).",
        }
    if not exp.server_is_online:
        return {
            "called": False,
            "ok": False,
            "workers_stopped": None,
            "error": "Server is offline.",
        }

    host = exp.frpc_subdomain_server or f"{exp.name}-server.{config.JD_BASE_DOMAIN}"
    url = f"https://{host}/admin/shutdown"
    try:
        r = requests.post(
            url,
            headers={"X-Admin-Token": exp.admin_token},
            timeout=30,
        )
    except requests.RequestException as exc:
        log.warning("Could not reach server for shutdown (%s): %s", exp.name, exc)
        return {
            "called": True,
            "ok": False,
            "workers_stopped": None,
            "error": str(exc),
        }

    if r.status_code == 200:
        body = r.json() if r.content else {}
        workers_stopped = body.get("workers_stopped")
        log.info(
            "Server shutdown acknowledged for %s (workers_stopped=%s)",
            exp.name,
            workers_stopped,
        )
        return {
            "called": True,
            "ok": True,
            "workers_stopped": workers_stopped,
            "error": None,
        }

    log.warning(
        "Server shutdown for %s returned HTTP %s: %s",
        exp.name,
        r.status_code,
        r.text[:200],
    )
    return {
        "called": True,
        "ok": False,
        "workers_stopped": None,
        "error": f"HTTP {r.status_code}",
    }
