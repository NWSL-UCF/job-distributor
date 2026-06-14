"""Shared helpers for creating jobs from API payloads (dashboard + job server)."""

import itertools
import json
import os
from typing import Any, Dict, List, Tuple, Union

from job_files import file_format, scan_uploads_from_disk

ParametersGrid = Dict[str, List[Any]]
JobsList = List[Dict[str, Any]]


def parse_create_jobs_payload(
    data: dict,
) -> Tuple[Union[ParametersGrid, None], Union[JobsList, None], List[str]]:
    """
    Validate a create-jobs JSON body.

    Returns (parameters_grid, explicit_jobs, parameters_list_json_strings).
    Exactly one of parameters_grid or explicit_jobs is non-None.
    """
    parameters = data.get("parameters")
    jobs = data.get("jobs")

    if jobs is not None:
        if parameters:
            raise ValueError("Provide either 'parameters' or 'jobs', not both.")
        if not isinstance(jobs, list) or len(jobs) == 0:
            raise ValueError("Missing or invalid 'jobs'. Must be a non-empty array.")
        for i, job in enumerate(jobs):
            if not isinstance(job, dict) or len(job) == 0:
                raise ValueError(f"Job at index {i} must be a non-empty object.")
        parameters_list = [json.dumps(job) for job in jobs]
        return None, jobs, parameters_list

    if not parameters or not isinstance(parameters, dict):
        raise ValueError("Missing or invalid 'parameters'. Must be a non-empty object.")

    for key, vals in parameters.items():
        if not isinstance(vals, list) or len(vals) == 0:
            raise ValueError(f"Values for '{key}' must be a non-empty array.")

    keys = list(parameters.keys())
    values = list(parameters.values())
    combos = list(itertools.product(*values))
    parameters_list = [json.dumps(dict(zip(keys, combo))) for combo in combos]
    return parameters, None, parameters_list


def upload_rows_for_job(db, workspace: str, exp_id: str, job_id: int) -> List[Dict[str, Any]]:
    """List uploads from SQLite, backfilling from disk when the table is empty."""
    rows = db.list_uploads(job_id)
    if not rows:
        disk_rows = scan_uploads_from_disk(workspace, exp_id, str(job_id))
        if disk_rows:
            db.backfill_uploads(disk_rows)
            rows = db.list_uploads(job_id)
    enriched: List[Dict[str, Any]] = []
    for row in rows:
        ext = os.path.splitext(row["filename"])[1]
        item = dict(row)
        item["format"] = file_format(ext)
        enriched.append(item)
    return enriched
