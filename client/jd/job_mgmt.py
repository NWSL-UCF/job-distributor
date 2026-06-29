"""
Job-management API for your local machine (not worker entry scripts).

Call ``jd.init()`` once, then use ``create_jobs``, ``list_jobs``, and
``download_result`` to drive experiments from Python.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

import requests

from jd._session import get_session
from jd.job_inputs import CreateSource, normalize_create_payload

PathLike = Union[str, Path]


def data_root() -> Path:
    """``<workspace_parent>/jd_data``."""
    session = get_session()
    return session.workspace_parent / "jd_data"


def exp_path() -> Path:
    """``<workspace_parent>/jd_data/<expId>/``."""
    session = get_session()
    return data_root() / session.exp_id


def job_path(job_id: Union[int, str]) -> Path:
    """``<workspace_parent>/jd_data/<expId>/<job_id>/``."""
    return exp_path() / str(job_id)


def job_download_dir(job_id: Union[int, str]) -> Path:
    """``…/<job_id>/downloaded/`` — default destination for ``download_result``."""
    dest = job_path(job_id) / "downloaded"
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def _raise_for_response(resp: requests.Response) -> Any:
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if resp.ok:
        return payload
    detail = ""
    if isinstance(payload, dict):
        detail = payload.get("error") or payload.get("message") or ""
    if not detail:
        detail = resp.text.strip() or f"HTTP {resp.status_code}"
    raise RuntimeError(detail)


def create_jobs(
    source: CreateSource,
    *,
    replace: bool = False,
    idle_timeout: Optional[int] = None,
    aborted_job_reset_timeout: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Create jobs from a parameter grid, explicit list, or file.

    * **dict with list values** — Cartesian product (same as dashboard grid).
    * **list of dicts** — one job per row (same as CSV upload).
    * **``.json`` / ``.csv`` / ``.tsv`` file** — loaded automatically.

    Set ``replace=True`` to delete existing jobs first; default appends.

    The response always includes ``"start_id"`` (for append operations) — the ID
    of the first job in the batch.  All IDs in the batch are the contiguous range
    ``[start_id, start_id + total_jobs)``.
    """
    payload = normalize_create_payload(source)
    payload["replace"] = replace
    if idle_timeout is not None:
        payload["idle_timeout"] = int(idle_timeout)
    if aborted_job_reset_timeout is not None:
        payload["aborted_job_reset_timeout"] = int(aborted_job_reset_timeout)

    session = get_session()
    resp = session.request("POST", "/api/jobs/create", json=payload)
    data = _raise_for_response(resp)
    if not isinstance(data, dict) or not data.get("success"):
        raise RuntimeError((data or {}).get("error", "create_jobs failed"))
    return data


def list_jobs(
    *,
    status: Optional[str] = None,
    search: Optional[str] = None,
    ids: Optional[List[int]] = None,
    page: int = 1,
    per_page: int = 50,
    fetch_all: bool = False,
) -> Dict[str, Any]:
    """
    List jobs with pagination.

    Set ``fetch_all=True`` to iterate every page and return a single dict with
    all jobs in ``jobs`` (``total_count`` / ``total_pages`` updated).

    Pass ``ids`` as a list of integers to restrict results to those specific job
    IDs.  ``ids`` takes priority over ``search`` when both are provided.
    """
    session = get_session()
    page = max(1, int(page))
    per_page = max(1, min(1000, int(per_page)))

    def _build_params(p: int) -> Dict[str, Any]:
        qp: Dict[str, Any] = {"page": p, "per_page": per_page}
        if status:
            qp["status"] = status
        if ids is not None:
            qp["ids"] = ",".join(str(i) for i in ids)
        elif search:
            qp["search"] = search
        return qp

    if not fetch_all:
        resp = session.request("GET", "/api/jobs", params=_build_params(page))
        return _raise_for_response(resp)

    all_jobs: List[Dict[str, Any]] = []
    current = 1
    total_pages = 1
    total_count = 0
    while current <= total_pages:
        resp = session.request("GET", "/api/jobs", params=_build_params(current))
        chunk = _raise_for_response(resp)
        jobs = chunk.get("jobs") or []
        all_jobs.extend(jobs)
        total_pages = int(chunk.get("total_pages") or 1)
        total_count = int(chunk.get("total_count") or len(all_jobs))
        current += 1
    return {
        "jobs": all_jobs,
        "total_count": total_count,
        "total_pages": 1,
        "current_page": 1,
    }


def get_job_statuses(job_ids: Iterable[Union[int, str]]) -> Dict[int, str]:
    """Return a ``{job_id: status}`` map for the given IDs.

    Uses ``GET /api/jobs/statuses`` which fetches only ``id`` and ``status`` —
    no ``SELECT *``, no COUNT, no pagination overhead.
    """
    ids = [int(j) for j in job_ids]
    if not ids:
        return {}
    session = get_session()
    resp = session.request(
        "GET", "/api/jobs/statuses", params={"ids": ",".join(str(i) for i in ids)}
    )
    data = _raise_for_response(resp)
    return {int(k): str(v) for k, v in (data.get("statuses") or {}).items()}


def list_job_uploads(job_id: Union[int, str]) -> List[Dict[str, Any]]:
    """Return upload metadata for a job (newest versions first)."""
    session = get_session()
    resp = session.request("GET", f"/api/jobs/{int(job_id)}/uploads")
    data = _raise_for_response(resp)
    return list((data or {}).get("uploads") or [])


def download_result(
    job_id: Union[int, str],
    filename: str,
    dest: Optional[PathLike] = None,
) -> Path:
    """
    Download the latest version of a logical result name (e.g. ``metrics.csv``).

    Default destination: ``job_download_dir(job_id) / filename`` (basename only).
    Returns the local path written.
    """
    logical = Path(filename).name
    session = get_session()
    if dest is None:
        out = job_download_dir(job_id) / logical
    else:
        out = Path(dest).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)

    resp = session.request(
        "GET",
        f"/api/jobs/{int(job_id)}/uploads/download",
        params={"filename": logical},
        stream=True,
    )
    if not resp.ok:
        _raise_for_response(resp)

    with open(out, "wb") as handle:
        for chunk in resp.iter_content(chunk_size=1024 * 256):
            if chunk:
                handle.write(chunk)
    return out


def wait_for_jobs(
    job_ids: Iterable[Union[int, str]],
    *,
    status: str = "DONE",
    timeout: Optional[float] = None,
    poll_interval: float = 30.0,
    include_aborted: bool = True,
) -> Dict[int, Dict[str, Any]]:
    """
    Poll until every *job_id* reaches *status* (default ``DONE``).

    Returns a map ``job_id → job record``. Raises ``TimeoutError`` if
    *timeout* seconds elapse first.
    """
    targets = {int(j) for j in job_ids}
    if not targets:
        return {}

    terminal_ok = {status.upper()}
    if include_aborted and status.upper() == "DONE":
        terminal_ok.add("ABORTED")

    deadline = None if timeout is None else time.time() + float(timeout)
    remaining = set(targets)
    found: Dict[int, Dict[str, Any]] = {}

    while remaining:
        if deadline is not None and time.time() > deadline:
            pending = sorted(remaining)
            raise TimeoutError(
                f"Timed out waiting for jobs {pending} to reach {status!r}."
            )

        for jid in list(remaining):
            chunk = list_jobs(search=str(jid), per_page=1)
            jobs = chunk.get("jobs") or []
            if not jobs or int(jobs[0]["id"]) != jid:
                continue
            if (jobs[0].get("status") or "").upper() in terminal_ok:
                found[jid] = jobs[0]
                remaining.discard(jid)

        if not remaining:
            break
        time.sleep(max(1.0, float(poll_interval)))

    return found
