"""Private session state for the job-management library (``jd.init()``)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from jd.auth import WorkerTokenManager, fetch_worker_token, resolve_hub_url

logger = logging.getLogger("jd.client")

_SESSION: Optional["ClientSession"] = None
_REQUEST_TIMEOUT = 120


@dataclass
class ClientSession:
    exp_id: str
    workspace_parent: Path
    hub_url: str
    token_mgr: WorkerTokenManager
    server_url: str

    def auth_headers(self) -> Dict[str, str]:
        return self.token_mgr.auth_headers()

    def ensure_server_url(self) -> str:
        self.token_mgr.ensure_fresh()
        if self.token_mgr.last_server_url:
            self.server_url = self.token_mgr.last_server_url.rstrip("/")
        if not self.server_url:
            raise RuntimeError(
                "Job server URL is not available. Check that the experiment is "
                "running on the Hub."
            )
        return self.server_url

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
        stream: bool = False,
    ) -> requests.Response:
        base = self.ensure_server_url()
        url = f"{base}{path}"
        headers = self.auth_headers()
        resp = requests.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json,
            timeout=_REQUEST_TIMEOUT,
            stream=stream,
        )
        return resp


def load_env_file(path: str) -> Dict[str, str]:
    """Parse a simple KEY=VALUE env file (``#`` comments, optional quotes)."""
    env: Dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                env[key] = value
    return env


def _merged_config(
    env_file: Optional[str],
    *,
    exp_id: Optional[str],
    hub_url: Optional[str],
) -> Dict[str, str]:
    file_vars: Dict[str, str] = {}
    if env_file:
        file_vars = load_env_file(os.path.expanduser(env_file))
    elif os.path.isfile(".env"):
        file_vars = load_env_file(".env")

    def pick(name: str, default: str = "") -> str:
        return (
            (os.environ.get(name) or "").strip()
            or file_vars.get(name, "").strip()
            or default
        )

    resolved_exp = (exp_id or pick("JD_EXP_ID")).strip().lower()
    resolved_hub = resolve_hub_url(hub_url=hub_url or pick("JD_HUB_URL") or None)
    workspace = pick("JD_WORKSPACE_PATH") or str(Path.home())
    api_key = pick("JD_API_KEY")

    return {
        "JD_API_KEY": api_key,
        "JD_EXP_ID": resolved_exp,
        "JD_WORKSPACE_PATH": workspace,
        "JD_HUB_URL": resolved_hub,
    }


def init_session(
    env_file: Optional[str] = None,
    *,
    exp_id: Optional[str] = None,
    hub_url: Optional[str] = None,
) -> ClientSession:
    """Create and store the global client session."""
    global _SESSION

    cfg = _merged_config(env_file, exp_id=exp_id, hub_url=hub_url)
    api_key = cfg["JD_API_KEY"]
    resolved_exp = cfg["JD_EXP_ID"]
    workspace = Path(cfg["JD_WORKSPACE_PATH"]).expanduser().resolve()
    resolved_hub = cfg["JD_HUB_URL"]

    if not api_key:
        raise ValueError(
            "JD_API_KEY is required. Set it in your .env file or environment."
        )
    if not resolved_exp:
        raise ValueError(
            "JD_EXP_ID is required. Pass exp_id=… to jd.init() or set it in .env."
        )

    token_mgr = WorkerTokenManager(
        resolved_hub,
        api_key,
        resolved_exp,
        logger,
    )
    if not token_mgr.refresh_now():
        raise RuntimeError(
            f"Could not obtain a worker token from Hub ({resolved_hub}). "
            "Check JD_API_KEY and that the experiment exists."
        )

    server_url = (token_mgr.last_server_url or "").strip().rstrip("/")
    if not server_url:
        data = fetch_worker_token(resolved_hub, api_key, resolved_exp, logger=logger)
        server_url = ((data or {}).get("server_url") or "").strip().rstrip("/")
    if not server_url:
        raise RuntimeError(
            "Hub did not return a job server URL for this experiment."
        )

    _SESSION = ClientSession(
        exp_id=resolved_exp,
        workspace_parent=workspace,
        hub_url=resolved_hub,
        token_mgr=token_mgr,
        server_url=server_url,
    )
    return _SESSION


def get_session() -> ClientSession:
    if _SESSION is None:
        raise RuntimeError("Call jd.init() before using the job-management API.")
    return _SESSION


def clear_session() -> None:
    global _SESSION
    _SESSION = None
