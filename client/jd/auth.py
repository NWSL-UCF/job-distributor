"""
Worker JWT management for Hub mode.

The worker parent process holds a WorkerTokenManager that proactively refreshes
the JWT before expiry and writes the current token to the local worker registry
(``workers.db`` column ``worker_token``) so entry scripts (jd_upload,
checkpoints) always read a fresh token.
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
from typing import TYPE_CHECKING, Optional

import requests

if TYPE_CHECKING:
    from jd.worker_registry import WorkerRegistry

# Legacy per-worker token file (pre–1.16); still read/cleaned up for migration.
TOKEN_FILENAME = ".token"
CACHE_DIRNAME = ".cache"


def new_worker_id() -> str:
    """Return a short id for this worker process: ``<pid>-<random_hex>``."""
    return f"{os.getpid()}-{secrets.token_hex(2)}"


def worker_token_file_path(
    workspace_parent: str, exp_id: str, worker_id: str,
) -> str:
    """Legacy path …/<home>/.cache/<expId>/<worker_id>/.token (deprecated)."""
    return os.path.join(
        os.path.abspath(workspace_parent),
        CACHE_DIRNAME,
        exp_id,
        worker_id,
        TOKEN_FILENAME,
    )

# Fallback refresh margin used only when the Hub doesn't return refresh_margin_secs.
# The authoritative value comes from the Hub token response and is stored per-manager.
REFRESH_MARGIN_SECS = 1800  # 30 minutes

# Hub token fetch retries (spreads load when many workers refresh together).
TOKEN_REFRESH_MAX_RETRIES = max(1, int(os.environ.get("JD_TOKEN_REFRESH_RETRIES", "5")))
TOKEN_REFRESH_RETRY_SLEEP_MAX_SECS = max(
    0, int(os.environ.get("JD_TOKEN_REFRESH_RETRY_SLEEP_MAX", "60"))
)

HUB_API_KEYS_URL = "https://hub.jobdistributor.net/api-keys"
# Probe experiment for API-key checks (404 = key valid, experiment absent).
_API_KEY_PROBE_EXPERIMENT = "__api_key_validation__"


def validate_hub_api_key(
    hub_url: str,
    api_key: str,
    *,
    timeout: float = 30,
) -> tuple[bool, str]:
    """Verify an API key with the Hub (no real experiment required).

    Calls ``POST /api/worker/token`` with a probe experiment name. The Hub
    authenticates the key before looking up the experiment:

    - **401** — invalid or unknown API key
    - **403** — account suspended or forbidden
    - **404** — key accepted; probe experiment does not exist (success)
    - **200** — key accepted (probe experiment exists; unlikely)

    Returns ``(valid, error_message)``; *error_message* is empty when valid.
    """
    hub_url = (hub_url or "").strip().rstrip("/")
    api_key = (api_key or "").strip()
    if not hub_url:
        return False, "Hub URL is not configured."
    if not api_key:
        return False, "API key is not set."

    endpoint = f"{hub_url}/api/worker/token"
    try:
        r = requests.post(
            endpoint,
            json={"experiment_name": _API_KEY_PROBE_EXPERIMENT},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return False, f"Could not reach Hub at {hub_url}: {exc}"

    if r.status_code == 401:
        return False, (
            "Your API key isn't found.\n"
            "Please export or enter a correct API key "
            f"(export JD_API_KEY=jd_…), or create a new key at {HUB_API_KEYS_URL}"
        )
    if r.status_code == 403:
        try:
            detail = (r.json() or {}).get("error") or "Account suspended"
        except (ValueError, TypeError, AttributeError):
            detail = "Account suspended"
        return False, (
            f"Hub rejected this API key: {detail}\n"
            f"Create or manage keys at {HUB_API_KEYS_URL}"
        )
    if r.status_code in (200, 404):
        return True, ""

    try:
        detail = (r.json() or {}).get("error") or r.text.strip()
    except (ValueError, TypeError, AttributeError):
        detail = r.text.strip() or f"HTTP {r.status_code}"
    return False, f"Hub API key check failed ({r.status_code}): {detail}"


def resolve_hub_url(
    *,
    hub: Optional[str] = None,
    hub_url: Optional[str] = None,
) -> str:
    """Default Hub base URL from CLI kv or ``JD_HUB_URL``."""
    raw = (
        (hub or "").strip()
        or (hub_url or "").strip()
        or os.environ.get("JD_HUB_URL", "").strip()
        or "https://hub.jobdistributor.net"
    )
    return raw.rstrip("/")


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
        exp    = jwt_exp_unix(token)
        margin = data.get("refresh_margin_secs")
        return {
            "token":               token,
            "server_url":          (data.get("server_url") or "").strip(),
            "expires_at":          exp,
            "refresh_margin_secs": int(margin) if margin is not None else None,
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
    """Legacy: write JWT to a file (mode 600). Prefer registry storage."""
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
    """Legacy: read JWT from token file."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _read_token_from_registry() -> str:
    worker_id = os.environ.get("JD_WORKER_ID", "").strip()
    exp_id = os.environ.get("JD_EXP_ID", "").strip().lower()
    if not worker_id or not exp_id:
        return ""
    try:
        from jd.worker_registry import WorkerRegistry, resolve_cache_parent
        return WorkerRegistry(exp_id, resolve_cache_parent()).get_worker_token(worker_id)
    except Exception:
        return ""


def read_worker_token() -> str:
    """
    Token for entry-script API calls.

    Prefers the local worker registry (``workers.db``) when ``JD_WORKER_ID`` and
    ``JD_EXP_ID`` are set — the parent worker refreshes that row on each Hub
    fetch. Falls back to legacy ``JD_WORKER_TOKEN_FILE``, then ``JD_WORKER_TOKEN``.
    """
    token = _read_token_from_registry()
    if token:
        return token
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
    Thread-safe worker JWT with proactive refresh and registry persistence.

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
        self._refresh_margin = REFRESH_MARGIN_SECS  # overwritten by Hub response
        self._registry: Optional["WorkerRegistry"] = None
        self._registry_worker_id: Optional[str] = None
        self._legacy_token_file: Optional[str] = None
        self.last_server_url = ""

    @property
    def enabled(self) -> bool:
        return bool(self.hub_url and self.api_key)

    def set_token_registry(
        self,
        registry: Optional["WorkerRegistry"],
        worker_id: str,
    ) -> None:
        """Persist tokens in ``workers.db`` for this worker row."""
        with self._lock:
            self._registry = registry
            self._registry_worker_id = worker_id.strip() if worker_id else None
            if registry and self._registry_worker_id and self._token:
                registry.set_worker_token(self._registry_worker_id, self._token)

    def set_token_file(self, path: Optional[str]) -> None:
        """Deprecated alias — stores legacy file path for migration cleanup only."""
        with self._lock:
            self._legacy_token_file = path

    def clear_token_store(self) -> None:
        """Clear registry token and remove legacy token file if present."""
        with self._lock:
            registry = self._registry
            wid = self._registry_worker_id
            legacy = self._legacy_token_file
            self._registry = None
            self._registry_worker_id = None
            self._legacy_token_file = None
        if registry and wid:
            registry.clear_worker_token(wid)
        if legacy and os.path.isfile(legacy):
            try:
                os.remove(legacy)
            except OSError:
                pass
            cache_dir = os.path.dirname(legacy)
            if cache_dir:
                try:
                    os.rmdir(cache_dir)
                except OSError:
                    pass

    def clear_token_file(self) -> None:
        """Deprecated alias for ``clear_token_store``."""
        self.clear_token_store()

    def _persist_token(self, token: str) -> None:
        if self._registry and self._registry_worker_id:
            self._registry.set_worker_token(self._registry_worker_id, token)

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
                if now < self._expires_at - self._refresh_margin:
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
        # Use the Hub-provided refresh margin if present; fall back to module default.
        hub_margin = data.get("refresh_margin_secs")
        if hub_margin is not None and int(hub_margin) > 0:
            self._refresh_margin = int(hub_margin)

        self._persist_token(self._token)

        remaining_m = max(0, (self._expires_at - time.time()) / 60)
        self.logger.info(
            f"Worker token refreshed (valid ~{remaining_m:.0f}m, "
            f"refresh margin {self._refresh_margin}s)"
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
