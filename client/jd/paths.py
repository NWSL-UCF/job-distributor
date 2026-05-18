"""
Paths injected by jd_worker for entry scripts
==============================================
Use these instead of hard-coding directories. Layout on the worker::

    <workspace_root>/<expId>/<job_id>/   ← save job outputs here (jd_job_dir)

``workspace_root`` is the absolute path passed as ``workspace_path=`` to ``jd_worker``.
"""

from __future__ import annotations

import os
from pathlib import Path


def jd_job_dir() -> Path:
    """
    Absolute directory for **this job's** local files — same as ``--base_path``.

    Equivalent to ``JD_WORKER_JOB_DIR``. Prefer this for CSVs, checkpoints on
    disk, temp files, etc.
    """
    raw = os.environ.get("JD_WORKER_JOB_DIR", "").strip()
    if not raw:
        raise RuntimeError(
            "JD_WORKER_JOB_DIR is not set. Run your script via jd_worker "
            "(or set JD_WORKER_JOB_DIR to your job sandbox directory)."
        )
    return Path(raw).expanduser().resolve()


def jd_worker_workspace() -> Path:
    """
    Absolute **workspace root** from ``jd_worker workspace_path=…`` (before
    ``expId`` / ``job_id`` segments).

    Equivalent to ``JD_WORKER_WORKSPACE_ROOT`` when set by current jd_worker.
    If only ``JD_WORKER_JOB_DIR`` is present (older workers), derives
    ``…/<workspace>/<expId>/<job_id>`` → parent.parent as the workspace root.
    """
    raw = os.environ.get("JD_WORKER_WORKSPACE_ROOT", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    job_raw = os.environ.get("JD_WORKER_JOB_DIR", "").strip()
    if job_raw:
        p = Path(job_raw).expanduser().resolve()
        return p.parent.parent
    raise RuntimeError(
        "Cannot resolve worker workspace: set JD_WORKER_WORKSPACE_ROOT or "
        "run inside jd_worker so JD_WORKER_JOB_DIR is set."
    )


def jd_exp_dir() -> Path:
    """Absolute ``<workspace>/<expId>/`` for this run (parent of ``jd_job_dir()``)."""
    return jd_job_dir().parent
