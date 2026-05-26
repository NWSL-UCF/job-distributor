"""Notify the job server when jd_worker_cli stops workers locally."""

from __future__ import annotations

import logging
import socket
from typing import Any, Dict, List, Optional

import requests

_logger = logging.getLogger("jd_worker_cli.server_sync")


def cli_source_host() -> str:
    try:
        return socket.gethostname().strip() or "unknown"
    except OSError:
        return "unknown"


def _server_headers(exp_id: str, kv: dict) -> tuple[str, dict]:
    """Return (server_base_url, auth_headers) or ('', {}) if unavailable."""
    from jd.worker_commands import _hub_server_url, _worker_token_headers

    _, server = _hub_server_url(exp_id, kv)
    if not server:
        return "", {}
    headers = _worker_token_headers(exp_id, kv)
    if not headers:
        return server.rstrip("/"), {}
    return server.rstrip("/"), headers


def notify_cli_worker_stop(
    exp_id: str,
    kv: dict,
    worker_id: str,
    job_id: Optional[int],
    *,
    action: str = "stop",
) -> bool:
    """
    Tell the server a worker was stopped from the CLI.

    Returns True if the server acknowledged the event. False when the server is
    unreachable or auth fails — local stop should still proceed.
    """
    server, headers = _server_headers(exp_id, kv)
    if not server:
        return False
    payload: Dict[str, Any] = {
        "worker_id": worker_id,
        "source": cli_source_host(),
        "action": action,
    }
    if job_id is not None:
        payload["job_id"] = int(job_id)
    try:
        r = requests.post(
            f"{server}/workers/cli/stop",
            json=payload,
            headers=headers,
            timeout=20,
        )
        if r.status_code == 200:
            return True
        _logger.warning(
            "Server CLI stop notify failed for %s: HTTP %s %s",
            worker_id, r.status_code, r.text[:200],
        )
    except requests.RequestException as exc:
        _logger.warning("Server CLI stop notify failed for %s: %s", worker_id, exc)
    return False


def notify_cli_clear_all(
    workers: List[Dict[str, Any]],
    kv: dict,
) -> bool:
    """
    Batch notify server before clear_all removes local registry DBs.

    *workers* items: ``worker_id``, ``exp_id``, optional ``job_id``.
    Uses any exp_id from the list for Hub auth (same experiment API key).
    """
    if not workers:
        return True
    exp_id = (workers[0].get("exp_id") or "").strip().lower()
    if not exp_id:
        return False
    server, headers = _server_headers(exp_id, kv)
    if not server:
        return False
    batch = [
        {
            "worker_id": w.get("worker_id"),
            "job_id": w.get("job_id"),
        }
        for w in workers
        if w.get("worker_id")
    ]
    try:
        r = requests.post(
            f"{server}/workers/cli/clear_all",
            json={"source": cli_source_host(), "workers": batch},
            headers=headers,
            timeout=60,
        )
        if r.status_code == 200:
            return True
        _logger.warning(
            "Server clear_all notify failed: HTTP %s %s",
            r.status_code, r.text[:200],
        )
    except requests.RequestException as exc:
        _logger.warning("Server clear_all notify failed: %s", exc)
    return False
