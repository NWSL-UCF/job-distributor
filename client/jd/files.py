"""
jd.files — worker-side file/checkpoint helpers
================================================
These functions are designed to be called **from inside your entry script**
while a job is running.  jd_worker automatically injects the required
context (server URL, job ID, experiment ID) as environment variables, so
you normally do not need to pass those arguments explicitly.

Quick-start
-----------
    from jd import jd_upload, jd_job_dir, jd_update_checkpoint, jd_get_last_checkpoint

    out = jd_job_dir() / "metrics.csv"
    # … write to out …
    jd_upload(str(out))

    jd_update_checkpoint({"epoch": 5, "state_dict": model.state_dict()})
    ckpt = jd_get_last_checkpoint()
    if ckpt:
        model.load_state_dict(ckpt["state_dict"])

Environment variables (set automatically by jd_worker)
-------------------------------------------------------
    JD_SERVER                   — job server base URL, e.g. http://10.0.0.1:8000
    JD_JOB_ID                   — integer job ID assigned by the server
    JD_EXP_ID                   — experiment identifier
    JD_WORKER_JOB_DIR           — absolute …/<parent>/jd_data/<expId>/<job_id>/
                                  (same as ``--base_path``); prefer ``jd_job_dir()``
    JD_WORKER_WORKSPACE_ROOT    — absolute ``<parent>/jd_data`` (same as ``jd_worker_workspace()``)
    JD_WORKSPACE_PATH           — parent of jd_data (job sandboxes, default logs)
    JD_CACHE_PATH               — optional registry root (``.cache/<expId>/``);
                                  defaults to JD_WORKSPACE_PATH or home
    JD_WORKER_ID                — worker id; entry scripts read JWT from workers.db
    JD_WORKER_TOKEN             — initial JWT at job start (fallback if registry unreadable)
    JD_UPLOAD_MAX_RETRIES         — total jd_upload attempts (default 5)

You can override server/job_id via the upload/checkpoint function keyword arguments.
"""

import io
import logging
import os
import pickle
import random
import time

import requests

from jd.auth import worker_auth_headers

logger = logging.getLogger(__name__)

_MAX_BYTES = 100 * 1024 * 1024   # 100 MB
_TIMEOUT   = 180                  # seconds for upload/download HTTP calls

_UPLOAD_MAX_RETRIES = max(1, int(os.environ.get("JD_UPLOAD_MAX_RETRIES", "5")))
_UPLOAD_RETRY_SLEEP_MIN = 1.0
_UPLOAD_RETRY_SLEEP_MAX = 20.0
# Transient routing / overload errors (e.g. frps 404 during tunnel reconnect).
_UPLOAD_RETRYABLE_STATUS = {404, 408, 429, 500, 502, 503, 504}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _ctx(job_id, server):
    """Resolve job_id and server from kwargs → env vars → error."""
    if job_id is None:
        raw = os.environ.get("JD_JOB_ID", "")
        if not raw:
            raise RuntimeError(
                "job_id not provided and JD_JOB_ID environment variable is not set. "
                "Make sure you are calling this function from inside an entry script "
                "launched by jd_worker."
            )
        job_id = int(raw)
    if server is None:
        server = os.environ.get("JD_SERVER", "")
        if not server:
            raise RuntimeError(
                "server not provided and JD_SERVER environment variable is not set."
            )
    return job_id, server.rstrip("/")


def _auth_headers() -> dict:
    """Return Authorization header (reads proactive token file when set)."""
    return worker_auth_headers()


def _check_size(data: bytes, label: str) -> None:
    if len(data) > _MAX_BYTES:
        mb = len(data) / (1024 ** 2)
        raise ValueError(
            f"{label} is {mb:.1f} MB which exceeds the 100 MB limit."
        )


def _upload_retry_delay() -> float:
    return random.uniform(_UPLOAD_RETRY_SLEEP_MIN, _UPLOAD_RETRY_SLEEP_MAX)


# ── Public API ────────────────────────────────────────────────────────────────

def jd_upload(
    file_path: str,
    job_id: int = None,
    server: str = None,
    max_retries: int = None,
) -> dict:
    """
    Upload a result file (≤ 100 MB) to the server.

    The file is stored in the experiment's job directory as::

        result_v{N}_{timestamp}.<original_ext>

    where N auto-increments across calls so every upload is preserved.

    On transient HTTP or network errors the upload is retried with a random
    1–20 second delay between attempts (default 5 attempts; override with
    ``max_retries`` or ``JD_UPLOAD_MAX_RETRIES``).

    Parameters
    ----------
    file_path : str
        Local path to the file you want to upload.
    job_id : int, optional
        Defaults to the JD_JOB_ID environment variable.
    server : str, optional
        Job server base URL. Defaults to JD_SERVER environment variable.
    max_retries : int, optional
        Total upload attempts (default from ``JD_UPLOAD_MAX_RETRIES``, usually 5).

    Returns
    -------
    dict
        ``{"success": True, "filename": "result_v0_…", "version": 0, "size_bytes": …}``
    """
    job_id, server = _ctx(job_id, server)

    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    with open(file_path, "rb") as fh:
        data = fh.read()

    _check_size(data, f"File '{os.path.basename(file_path)}'")

    original_name = os.path.basename(file_path)
    attempts = max(1, max_retries if max_retries is not None else _UPLOAD_MAX_RETRIES)
    logger.info(
        f"[jd_upload] Uploading '{original_name}' ({len(data)/(1024**2):.2f} MB) "
        f"(up to {attempts} attempt(s)) …"
    )

    for attempt in range(1, attempts + 1):
        try:
            resp = requests.post(
                f"{server}/upload",
                data={"job_id": str(job_id)},
                files={"file": (original_name, io.BytesIO(data), "application/octet-stream")},
                headers=_auth_headers(),
                timeout=_TIMEOUT,
            )
            if resp.status_code == 200:
                result = resp.json()
                if attempt > 1:
                    logger.info(f"[jd_upload] Succeeded on attempt {attempt}/{attempts}")
                logger.info(
                    f"[jd_upload] Saved as '{result.get('filename')}' "
                    f"(version {result.get('version')})"
                )
                return result

            if resp.status_code in _UPLOAD_RETRYABLE_STATUS and attempt < attempts:
                delay = _upload_retry_delay()
                logger.warning(
                    f"[jd_upload] HTTP {resp.status_code} on attempt {attempt}/{attempts}; "
                    f"retrying in {delay:.1f}s …"
                )
                time.sleep(delay)
                continue

            resp.raise_for_status()
        except requests.RequestException as exc:
            if attempt >= attempts:
                raise
            delay = _upload_retry_delay()
            logger.warning(
                f"[jd_upload] Request failed on attempt {attempt}/{attempts} ({exc}); "
                f"retrying in {delay:.1f}s …"
            )
            time.sleep(delay)


def jd_update_checkpoint(obj, job_id: int = None, server: str = None) -> dict:
    """
    Serialise *obj* with pickle and upload it as a versioned checkpoint.

    The checkpoint is stored in the experiment's job directory as::

        checkpoint_v{N}_{timestamp}.pt

    Each call creates a new version, so previous checkpoints are never
    overwritten.  The file uses standard Python pickle serialisation and is
    compatible with PyTorch state-dicts as well as arbitrary Python objects.

    Parameters
    ----------
    obj : any
        The Python object to checkpoint (e.g. a dict containing
        ``model.state_dict()`` and optimizer state).
    job_id : int, optional
    server : str, optional

    Returns
    -------
    dict
        ``{"success": True, "filename": "checkpoint_v0_…", "version": 0, …}``
    """
    job_id, server = _ctx(job_id, server)

    logger.info("[jd_update_checkpoint] Serialising checkpoint …")
    data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    _check_size(data, "Checkpoint")

    logger.info(f"[jd_update_checkpoint] Uploading {len(data)/(1024**2):.2f} MB …")
    resp = requests.post(
        f"{server}/checkpoint",
        data={"job_id": str(job_id)},
        files={"checkpoint": ("checkpoint.pkl", io.BytesIO(data), "application/octet-stream")},
        headers=_auth_headers(),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    result = resp.json()
    logger.info(f"[jd_update_checkpoint] Saved as '{result.get('filename')}' (version {result.get('version')})")
    return result


def jd_get_last_checkpoint(job_id: int = None, server: str = None):
    """
    Download the highest-versioned checkpoint for this job and return it as
    a Python object — **nothing is written to disk**.

    The server sends the raw pickle bytes; this function deserialises them
    directly from an in-memory buffer so your script can resume immediately.

    Parameters
    ----------
    job_id : int, optional
    server : str, optional

    Returns
    -------
    object
        The Python object that was passed to ``jd_update_checkpoint``, or
        ``None`` if no checkpoint exists yet for this job.

    Examples
    --------
    ::

        ckpt = jd_get_last_checkpoint()
        if ckpt is not None:
            model.load_state_dict(ckpt["model"])
            start_epoch = ckpt["epoch"] + 1
    """
    job_id, server = _ctx(job_id, server)

    logger.info(f"[jd_get_last_checkpoint] Fetching latest checkpoint for job {job_id} …")
    resp = requests.get(
        f"{server}/checkpoint/latest",
        params={"job_id": job_id},
        headers=_auth_headers(),
        timeout=_TIMEOUT,
        stream=True,
    )

    if resp.status_code == 404:
        logger.info("[jd_get_last_checkpoint] No checkpoint found.")
        return None

    resp.raise_for_status()

    # Read directly into a BytesIO buffer — no temp file, stays in memory
    buf = io.BytesIO()
    for chunk in resp.iter_content(chunk_size=1024 * 256):
        buf.write(chunk)

    logger.info(f"[jd_get_last_checkpoint] Received {buf.tell()/(1024**2):.2f} MB. Deserialising …")
    buf.seek(0)
    obj = pickle.load(buf)
    logger.info("[jd_get_last_checkpoint] Checkpoint ready.")
    return obj
