"""Helpers for listing and serving worker upload files from the dashboard."""

import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from workspace_layout import job_worker_data_dir

MAX_PREVIEW_BYTES = 2 * 1024 * 1024  # 2 MB

_LEGACY_RESULT_FILENAME_RE = re.compile(r"^result_v(\d+)_(\d+)(\.[^./\\]+)?$")
_CHECKPOINT_PREFIX = "checkpoint_v"


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


def is_checkpoint_filename(filename: str) -> bool:
    return bool(filename) and filename.startswith(_CHECKPOINT_PREFIX)


def is_legacy_result_filename(filename: str) -> bool:
    return bool(_LEGACY_RESULT_FILENAME_RE.match(filename or ""))


def sanitize_upload_basename(filename: str) -> str:
    """Return a safe basename for a worker upload (no directories)."""
    name = os.path.basename((filename or "").replace("\\", "/")).strip()
    name = name.replace("\x00", "")
    if not name or name in (".", ".."):
        return "upload.bin"
    if len(name) > 200:
        stem, ext = os.path.splitext(name)
        ext = ext or ".bin"
        name = stem[: max(1, 200 - len(ext))] + ext
    return name


def validate_result_filename(filename: str) -> bool:
    """Return True if *filename* is a safe worker upload name."""
    return validate_upload_filename(filename)


def validate_upload_filename(filename: str) -> bool:
    if not filename or not isinstance(filename, str):
        return False
    if "/" in filename or "\\" in filename or "\x00" in filename:
        return False
    if filename.startswith(".") or filename in (".", ".."):
        return False
    if is_checkpoint_filename(filename):
        return False
    if is_legacy_result_filename(filename):
        return True
    return bool(re.match(r'^[^/\\<>:"|?*]+$', filename))


def parse_legacy_result_filename(filename: str) -> Optional[Tuple[int, float, str]]:
    m = _LEGACY_RESULT_FILENAME_RE.match(filename or "")
    if not m:
        return None
    return int(m.group(1)), float(m.group(2)), m.group(3) or ""


def parse_result_filename(filename: str) -> Optional[Tuple[int, float, str]]:
    """Parse legacy ``result_v{N}_{timestamp}.ext`` names (backward compatible)."""
    return parse_legacy_result_filename(filename)


def _file_version_suffix(stem: str, ext: str, filename: str) -> Optional[int]:
    """Return 0 for the unversioned name, N for ``stem_vN.ext``, else None."""
    plain = f"{stem}{ext}"
    if filename == plain:
        return 0
    m = re.match(rf"^{re.escape(stem)}_v(\d+){re.escape(ext)}$", filename)
    if m:
        return int(m.group(1))
    return None


def resolve_upload_filename(directory: str, original_name: str) -> Tuple[str, int]:
    """
    Pick the on-disk name for a new upload.

    First upload of a logical name uses the original basename; repeats use
    ``name_v1.ext``, ``name_v2.ext``, …
    """
    safe = sanitize_upload_basename(original_name)
    stem, ext = os.path.splitext(safe)
    if not stem:
        stem = "upload"
    ext = ext.lower() or ".bin"

    plain = f"{stem}{ext}"
    versions: List[int] = []

    if os.path.isfile(os.path.join(directory, plain)):
        versions.append(0)

    if os.path.isdir(directory):
        for fname in os.listdir(directory):
            if fname == plain:
                continue
            file_v = _file_version_suffix(stem, ext, fname)
            if file_v is not None and file_v > 0:
                versions.append(file_v)

    if not versions:
        return plain, 0

    next_v = max(versions) + 1
    return f"{stem}_v{next_v}{ext}", next_v


def _upload_timestamp(filename: str, fpath: str) -> float:
    parsed = parse_legacy_result_filename(filename)
    if parsed:
        return parsed[1]
    try:
        return os.path.getmtime(fpath)
    except OSError:
        return time.time()


def scan_uploads_from_disk(workspace: str, exp_id: str, job_id: str) -> List[Dict[str, Any]]:
    """Scan ``data/<job_id>/`` for worker uploads (newest first after sort)."""
    job_dir = job_worker_data_dir(workspace, exp_id, str(job_id))
    if not os.path.isdir(job_dir):
        return []

    uploads: List[Dict[str, Any]] = []
    for fname in os.listdir(job_dir):
        fpath = os.path.join(job_dir, fname)
        if not os.path.isfile(fpath):
            continue
        if not validate_upload_filename(fname):
            continue
        try:
            size_bytes = os.path.getsize(fpath)
        except OSError:
            continue
        ext = os.path.splitext(fname)[1]
        uploads.append(
            {
                "job_id": int(job_id),
                "filename": fname,
                "size_bytes": size_bytes,
                "uploaded_at": _upload_timestamp(fname, fpath),
                "format": file_format(ext),
            }
        )

    uploads.sort(key=lambda row: row["uploaded_at"])
    for i, row in enumerate(uploads):
        row["version"] = i

    uploads.sort(key=lambda row: row["uploaded_at"], reverse=True)
    return uploads


def resolve_result_file(workspace: str, exp_id: str, job_id: str, filename: str) -> Optional[str]:
    """Return absolute path if *filename* is a safe result file under the job directory."""
    if not validate_upload_filename(filename):
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
