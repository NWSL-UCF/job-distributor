"""Parse job definitions from dicts, lists, and tabular/JSON files."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Union

CreateSource = Union[str, Path, Dict[str, Any], List[Dict[str, Any]]]


def _is_parameter_grid(obj: Dict[str, Any]) -> bool:
    if not obj:
        return False
    return all(isinstance(v, list) and len(v) > 0 for v in obj.values())


def jobs_from_dict(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Return API payload fragment for a dict source."""
    if "jobs" in obj or "parameters" in obj:
        if "jobs" in obj and "parameters" in obj:
            raise ValueError("Provide either 'jobs' or 'parameters', not both.")
        return dict(obj)
    if _is_parameter_grid(obj):
        return {"parameters": obj}
    return {"jobs": [obj]}


def _parse_csv_tsv(path: Path, delimiter: str) -> List[Dict[str, Any]]:
    jobs: List[Dict[str, Any]] = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        if not reader.fieldnames:
            raise ValueError(f"No header row found in {path.name}")
        headers = [h.strip() for h in reader.fieldnames if h and h.strip()]
        if not headers:
            raise ValueError(f"No column headers found in {path.name}")
        for row_num, row in enumerate(reader, start=2):
            job: Dict[str, str] = {}
            empty = True
            for header in headers:
                raw = (row.get(header) or "").strip()
                if raw:
                    empty = False
                job[header] = raw
            if empty:
                continue
            jobs.append(job)
    if not jobs:
        raise ValueError(f"No job rows found in {path.name}")
    return jobs


def jobs_from_file(path: Union[str, Path]) -> Dict[str, Any]:
    """Load jobs from ``.json``, ``.csv``, or ``.tsv``."""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Job source file not found: {p}")

    ext = p.suffix.lower()
    if ext == ".json":
        with open(p, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, list):
            if not data:
                raise ValueError(f"Empty jobs list in {p.name}")
            return {"jobs": data}
        if isinstance(data, dict):
            return jobs_from_dict(data)
        raise ValueError(f"Unsupported JSON structure in {p.name}")

    if ext in (".csv",):
        return {"jobs": _parse_csv_tsv(p, ",")}
    if ext in (".tsv", ".tab"):
        return {"jobs": _parse_csv_tsv(p, "\t")}

    raise ValueError(
        f"Unsupported job file type '{ext}'. Use .json, .csv, or .tsv."
    )


def normalize_create_payload(source: CreateSource) -> Dict[str, Any]:
    """Convert any supported *source* into a create-jobs API body."""
    if isinstance(source, (str, Path)):
        return jobs_from_file(source)
    if isinstance(source, list):
        if not source:
            raise ValueError("Job list cannot be empty.")
        return {"jobs": source}
    if isinstance(source, dict):
        return jobs_from_dict(source)
    raise TypeError(
        "source must be a dict, list of dicts, file path, or Path "
        "(.json / .csv / .tsv)."
    )
