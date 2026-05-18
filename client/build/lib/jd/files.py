"""
jd.files — worker-side file/checkpoint helpers
================================================
These functions are designed to be called **from inside your entry script**
while a job is running.  jd_worker automatically injects the required
context (server URL, job ID, experiment ID) as environment variables, so
you normally do not need to pass those arguments explicitly.

Quick-start
-----------
    from jd import jd_upload, jd_update_checkpoint, jd_get_last_checkpoint

    # Save a result file (any format, ≤ 100 MB)
    jd_upload("metrics.csv")

    # Save a checkpoint (any Python / PyTorch object, ≤ 100 MB)
    jd_update_checkpoint({"epoch": 5, "state_dict": model.state_dict()})

    # Restore the latest checkpoint into memory (returns the Python object)
    ckpt = jd_get_last_checkpoint()
    if ckpt:
        model.load_state_dict(ckpt["state_dict"])

Environment variables (set automatically by jd_worker)
-------------------------------------------------------
    JD_SERVER   — job server base URL, e.g. http://10.0.0.1:8000
    JD_JOB_ID   — integer job ID assigned by the server
    JD_EXP_ID   — experiment identifier

You can override any of them via the function's keyword arguments.
"""

import io
import logging
import os
import pickle

import requests

logger = logging.getLogger(__name__)

_MAX_BYTES = 100 * 1024 * 1024   # 100 MB
_TIMEOUT   = 180                  # seconds for upload/download HTTP calls


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


def _check_size(data: bytes, label: str) -> None:
    if len(data) > _MAX_BYTES:
        mb = len(data) / (1024 ** 2)
        raise ValueError(
            f"{label} is {mb:.1f} MB which exceeds the 100 MB limit."
        )


# ── Public API ────────────────────────────────────────────────────────────────

def jd_upload(file_path: str, job_id: int = None, server: str = None) -> dict:
    """
    Upload a result file (≤ 100 MB) to the server.

    The file is stored in the experiment's job directory as::

        result_v{N}_{timestamp}.<original_ext>

    where N auto-increments across calls so every upload is preserved.

    Parameters
    ----------
    file_path : str
        Local path to the file you want to upload.
    job_id : int, optional
        Defaults to the JD_JOB_ID environment variable.
    server : str, optional
        Job server base URL. Defaults to JD_SERVER environment variable.

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
    logger.info(f"[jd_upload] Uploading '{original_name}' ({len(data)/(1024**2):.2f} MB) …")

    resp = requests.post(
        f"{server}/upload",
        data={"job_id": str(job_id)},
        files={"file": (original_name, io.BytesIO(data), "application/octet-stream")},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    result = resp.json()
    logger.info(f"[jd_upload] Saved as '{result.get('filename')}' (version {result.get('version')})")
    return result


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
