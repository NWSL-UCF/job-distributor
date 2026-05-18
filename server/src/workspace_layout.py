"""
Central layout under ``<workspace_path>/<expId>/``:

- ``meta/`` — SQLite DB, application logs, orchestrator files (e.g. ``pids.json``).
- ``data/`` — worker uploads and checkpoints under ``data/<job_id>/``.
"""

from __future__ import annotations

import os

META_SUBDIR = "meta"
DATA_SUBDIR = "data"


def exp_root(workspace: str, exp_id: str) -> str:
    return os.path.join(workspace, exp_id)


def exp_meta_dir(workspace: str, exp_id: str) -> str:
    return os.path.join(exp_root(workspace, exp_id), META_SUBDIR)


def exp_data_dir(workspace: str, exp_id: str) -> str:
    return os.path.join(exp_root(workspace, exp_id), DATA_SUBDIR)


def jobs_db_path(workspace: str, exp_id: str, filename: str = "jobs.db") -> str:
    return os.path.join(exp_meta_dir(workspace, exp_id), filename)


def job_worker_data_dir(workspace: str, exp_id: str, job_id: str) -> str:
    """Per-job directory for ``/upload`` and ``/checkpoint`` payloads."""
    return os.path.join(exp_data_dir(workspace, exp_id), str(job_id))


def ensure_exp_layout(workspace: str, exp_id: str) -> None:
    """Create experiment root, ``meta/``, and ``data/``."""
    os.makedirs(exp_root(workspace, exp_id), exist_ok=True)
    os.makedirs(exp_meta_dir(workspace, exp_id), exist_ok=True)
    os.makedirs(exp_data_dir(workspace, exp_id), exist_ok=True)
