"""Helpers for listing and serving worker upload files from the dashboard."""

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from workspace_layout import job_worker_data_dir

MAX_PREVIEW_BYTES = 2 * 1024 * 1024  # 2 MB

_RESULT_FILENAME_RE = re.compile(r"^result_v(\d+)_(\d+)(\.[^./\\]+)?$")
_VERSION_RE = re.compile(r"_v(\d+)_")

TABULAR_EXTENSIONS = {".csv", ".tsv", ".tab"}
JSON_EXTENSIONS = {".json"}
YAML_EXTENSIONS = {".yaml", ".yml"}


def file_format(ext: str) -> str:
    """Return ``tabular``, ``json``, ``yaml``, or ``other`` for a file extension."""
    ext = (ext or "").lower()
    if ext in TABULAR_EXTENSIONS:
        return "tabular"
    if ext in JSON_EXTENSIONS:
        return "json"
    if ext in YAML_EXTENSIONS:
        return "yaml"
    return "other"


def validate_result_filename(filename: str) -> bool:
    return bool(_RESULT_FILENAME_RE.match(filename or ""))


def parse_result_filename(filename: str) -> Optional[Tuple[int, float, str]]:
    m = _RESULT_FILENAME_RE.match(filename or "")
    if not m:
        return None
    return int(m.group(1)), float(m.group(2)), m.group(3) or ""


def resolve_result_file(workspace: str, exp_id: str, job_id: str, filename: str) -> Optional[str]:
    """Return absolute path if *filename* is a safe result file under the job directory."""
    if not validate_result_filename(filename):
        return None
    job_dir = job_worker_data_dir(workspace, exp_id, str(job_id))
    full = os.path.join(job_dir, filename)
    real_job = os.path.realpath(job_dir)
    real_file = os.path.realpath(full)
    if not real_file.startswith(real_job + os.sep):
        return None
    if not os.path.isfile(real_file):
        return None
    return real_file


def scan_uploads_from_disk(workspace: str, exp_id: str, job_id: str) -> List[Dict[str, Any]]:
    """Scan ``data/<job_id>/`` for ``result_v*`` files (newest version first)."""
    job_dir = job_worker_data_dir(workspace, exp_id, str(job_id))
    if not os.path.isdir(job_dir):
        return []

    uploads: List[Dict[str, Any]] = []
    for fname in os.listdir(job_dir):
        if not fname.startswith("result_v"):
            continue
        parsed = parse_result_filename(fname)
        if not parsed:
            continue
        version, uploaded_at, ext = parsed
        fpath = os.path.join(job_dir, fname)
        try:
            size_bytes = os.path.getsize(fpath)
        except OSError:
            continue
        uploads.append(
            {
                "job_id": int(job_id),
                "version": version,
                "filename": fname,
                "size_bytes": size_bytes,
                "uploaded_at": uploaded_at,
                "format": file_format(ext),
            }
        )

    uploads.sort(key=lambda row: row["version"], reverse=True)
    return uploads


def read_upload_preview(path: str) -> Dict[str, Any]:
    """Read up to ``MAX_PREVIEW_BYTES`` for in-dashboard viewing."""
    ext = os.path.splitext(path)[1]
    fmt = file_format(ext)
    size_bytes = os.path.getsize(path)
    too_large = size_bytes > MAX_PREVIEW_BYTES

    base = {
        "size_bytes": size_bytes,
        "format": fmt,
        "too_large": too_large,
        "viewable": False,
    }

    if too_large or fmt == "other":
        return base

    with open(path, "rb") as handle:
        data = handle.read(MAX_PREVIEW_BYTES)

    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        base["binary"] = True
        return base

    base["viewable"] = True
    base["content"] = content
    return base
