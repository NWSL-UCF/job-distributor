"""
Paths injected by jd_worker for entry scripts
==============================================
Layout on the worker::

    <parent>/jd_data/<expId>/<job_id>/   ← save job outputs here (jd_job_dir)

``parent`` is ``JD_WORKSPACE_PATH`` if set, otherwise your home directory.
``jd_worker_workspace()`` returns the resolved ``…/jd_data`` directory.
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
    Absolute **jd_data root**: ``<parent>/jd_data`` where ``parent`` is
    ``JD_WORKSPACE_PATH`` or ``~``.

    Set by ``JD_WORKER_WORKSPACE_ROOT`` for the entry-script process.
    If only ``JD_WORKER_JOB_DIR`` is present, derives
    ``…/jd_data/<expId>/<job_id>`` → parent.parent as that root.
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
    """Absolute ``<parent>/jd_data/<expId>/`` for this run (parent of ``jd_job_dir()``)."""
    return jd_job_dir().parent
