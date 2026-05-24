"""
Worker JWT management for Hub mode.

The worker parent process holds a WorkerTokenManager that proactively refreshes
the JWT before expiry and writes the current token to
``<home>/.cache/<expId>/<worker_id>/.token`` so entry scripts
(jd_upload, checkpoints) always read a fresh token.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import random
import secrets
import threading
import time
from typing import Optional

import requests

# Filename for the per-worker JWT cache file; parent updates, child reads.
TOKEN_FILENAME = ".token"
CACHE_DIRNAME = ".cache"


def new_worker_id() -> str:
    """Return a short id for this worker process: ``<pid>-<random_hex>``."""
    return f"{os.getpid()}-{secrets.token_hex(2)}"


def worker_token_file_path(
    workspace_parent: str, exp_id: str, worker_id: str,
) -> str:
    """Return …/<home>/.cache/<expId>/<worker_id>/.token"""
    return os.path.join(
        os.path.abspath(workspace_parent),
        CACHE_DIRNAME,
        exp_id,
        worker_id,
        TOKEN_FILENAME,
    )

# Refresh this many seconds before JWT exp (1h Hub TTL → refresh at ~55m).
REFRESH_MARGIN_SECS = 300

# Hub token fetch retries (spreads load when many workers refresh together).
TOKEN_REFRESH_MAX_RETRIES = max(1, int(os.environ.get("JD_TOKEN_REFRESH_RETRIES", "5")))
TOKEN_REFRESH_RETRY_SLEEP_MAX_SECS = max(
    0, int(os.environ.get("JD_TOKEN_REFRESH_RETRY_SLEEP_MAX", "60"))
)


def jwt_exp_unix(token: str) -> Optional[float]:
    """Return JWT exp claim as Unix timestamp, or None if unreadable."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1]
        pad = (-len(payload)) % 4
        if pad:
            payload += "=" * pad
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        return float(exp) if exp is not None else None
    except Exception:
        return None


def _fetch_worker_token_once(hub_url: str, api_key: str, exp_id: str) -> Optional[dict]:
    """Single attempt to request a worker JWT from the Hub."""
    endpoint = f"{hub_url.rstrip('/')}/api/worker/token"
    try:
        r = requests.post(
            endpoint,
            json={"experiment_name": exp_id},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        token = (data.get("worker_token") or "").strip()
        if not token:
            return None
        exp = jwt_exp_unix(token)
        return {
            "token":      token,
            "server_url": (data.get("server_url") or "").strip(),
            "expires_at": exp,
        }
    except requests.RequestException:
        return None


def fetch_worker_token(
    hub_url: str,
    api_key: str,
    exp_id: str,
    logger: Optional[logging.Logger] = None,
    max_retries: Optional[int] = None,
) -> Optional[dict]:
    """
    Request a new worker JWT from the Hub, with retries and random backoff.

    Between failed attempts sleeps a random duration in [0, 60] seconds
    (configurable via JD_TOKEN_REFRESH_RETRY_SLEEP_MAX) to spread load when
    many workers refresh at once.

    Returns {"token", "server_url", "expires_at"} or None if all attempts fail.
    """
    attempts = max(1, max_retries if max_retries is not None else TOKEN_REFRESH_MAX_RETRIES)
    for attempt in range(1, attempts + 1):
        data = _fetch_worker_token_once(hub_url, api_key, exp_id)
        if data:
            if attempt > 1 and logger:
                logger.info(
                    f"Worker token obtained from Hub on attempt {attempt}/{attempts}"
                )
            return data

        if attempt >= attempts:
            break

        delay = random.uniform(0, TOKEN_REFRESH_RETRY_SLEEP_MAX_SECS)
        if logger:
            logger.warning(
                f"Worker token refresh failed (attempt {attempt}/{attempts}); "
                f"retrying in {delay:.1f}s"
            )
        time.sleep(delay)

    return None


def write_token_file(path: str, token: str) -> None:
    """Write the current JWT to a file (mode 600) for the entry script to read."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(token.strip())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def read_token_file(path: str) -> str:
    """Read JWT from token file; return empty string if missing."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def read_worker_token() -> str:
    """
    Token for entry-script API calls.

    Prefers JD_WORKER_TOKEN_FILE (updated by parent on proactive refresh),
    falls back to JD_WORKER_TOKEN env (initial value at job start).
    """
    path = os.environ.get("JD_WORKER_TOKEN_FILE", "").strip()
    if path:
        token = read_token_file(path)
        if token:
            return token
    return os.environ.get("JD_WORKER_TOKEN", "").strip()


def worker_auth_headers() -> dict:
    """Authorization headers for job-server requests from entry scripts."""
    token = read_worker_token()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


class WorkerTokenManager:
    """
    Thread-safe worker JWT with proactive refresh and optional token file sync.

    Call ensure_fresh() before server API calls; refresh happens automatically
    when within REFRESH_MARGIN_SECS of expiry.
    """

    def __init__(
        self,
        hub_url: str,
        api_key: str,
        exp_id: str,
        logger: logging.Logger,
        initial_token: str = "",
    ) -> None:
        self.hub_url = hub_url.rstrip("/")
        self.api_key = api_key.strip()
        self.exp_id = exp_id.strip().lower()
        self.logger = logger
        self._lock = threading.Lock()
        self._token = initial_token.strip()
        self._expires_at = jwt_exp_unix(self._token) or 0.0
        self._token_file: Optional[str] = None
        self.last_server_url = ""

    @property
    def enabled(self) -> bool:
        return bool(self.hub_url and self.api_key)

    def set_token_file(self, path: Optional[str]) -> None:
        """Point at the per-worker token file; write current token immediately."""
        with self._lock:
            self._token_file = path
            if path and self._token:
                write_token_file(path, self._token)

    def clear_token_file(self) -> None:
        """Remove the per-worker token file and cache dir when the worker exits."""
        with self._lock:
            path = self._token_file
            self._token_file = None
        if not path:
            return
        cache_dir = os.path.dirname(path)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
        if cache_dir:
            try:
                os.rmdir(cache_dir)
            except OSError:
                pass

    def ensure_fresh(self) -> bool:
        """
        Proactively refresh if the token is missing or near expiry.

        Returns True if a usable token is available after this call.
        """
        if not self.enabled:
            return bool(self._token)
        with self._lock:
            now = time.time()
            if self._token and self._expires_at:
                if now < self._expires_at - REFRESH_MARGIN_SECS:
                    return True
            return self._refresh_locked()

    def refresh_now(self) -> bool:
        """Force a Hub token fetch (e.g. initial startup)."""
        if not self.enabled:
            return bool(self._token)
        with self._lock:
            return self._refresh_locked()

    def _refresh_locked(self) -> bool:
        """Hub fetch; caller must hold self._lock."""
        data = fetch_worker_token(
            self.hub_url, self.api_key, self.exp_id, logger=self.logger,
        )
        if not data:
            if self._token:
                self.logger.warning(
                    "Worker token refresh failed — continuing with existing token "
                    f"(expires in {max(0, int(self._expires_at - time.time()))}s)"
                )
                return True
            self.logger.error("Worker token refresh failed and no token available.")
            return False

        self._token = data["token"]
        self._expires_at = data.get("expires_at") or jwt_exp_unix(self._token) or 0.0
        if data.get("server_url"):
            self.last_server_url = data["server_url"]

        if self._token_file:
            write_token_file(self._token_file, self._token)

        remaining_m = max(0, (self._expires_at - time.time()) / 60)
        self.logger.info(
            f"Worker token refreshed proactively (valid ~{remaining_m:.0f}m more)"
        )
        return True

    def get_token(self) -> str:
        self.ensure_fresh()
        with self._lock:
            return self._token

    def auth_headers(self) -> dict:
        token = self.get_token()
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}
