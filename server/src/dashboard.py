import argparse
import itertools
import json
import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytz
from database import JobDatabase, job_worker_id
from flask import Flask, jsonify, make_response, redirect, render_template, request, send_file
from workspace_layout import ensure_exp_layout, exp_meta_dir, jobs_db_path
from job_files import (
    MAX_PREVIEW_BYTES,
    file_format,
    read_upload_preview,
    resolve_result_file,
    scan_uploads_from_disk,
    validate_result_filename,
)

app = Flask(__name__)

# -------------------------- CONFIG --------------------------
BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DB_FILE = ""
LOG_FILENAME = "dashboard.log"
EXP_ID = "sim100"

# Initialize database connection
db = None

def setup_log(args):
    LOG_FILE = os.path.join(exp_meta_dir(args.workspacePath, args.expId), LOG_FILENAME)
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )


STATUS_PENDING = "PENDING"
STATUS_SERVED = "SERVED"
STATUS_DONE = "DONE"
STATUS_ABORTED = "ABORTED"
STATUS_DELETED = "DELETED"

def worker_machine_label(worker_id: str) -> str:
    """Derive a host label from a full worker id for charts and machine stats."""
    if not worker_id or not str(worker_id).strip():
        return "Unassigned"
    rb = str(worker_id).strip()
    # Legacy: user@host(type)_slot_suffix
    if "@" in rb:
        return rb.split("_")[0]
    # Current: {host}_{instance}_{slot}
    parts = rb.split("_")
    if len(parts) == 3 and parts[-1].isdigit() and re.fullmatch(
        r"(?:[a-z]{1,6}|[0-9A-Za-z]{6})", parts[1]
    ):
        return parts[0]
    if len(parts) == 3 and parts[-1].isdigit() and re.fullmatch(
        r"[0-9a-fA-F]{4}", parts[1]
    ):
        return parts[0]
    # Legacy hyphen ids: {instance}-{host}-{slot} or {host}-{slot}-{instance}
    hparts = rb.split("-")
    if len(hparts) >= 3 and hparts[-1].isdigit() and (
        re.fullmatch(r"[0-9A-Za-z]{6}", hparts[0])
        or re.fullmatch(r"[0-9a-fA-F]{4}", hparts[0])
    ):
        return hparts[1]
    if hparts:
        return hparts[0]
    return parts[0] if parts else rb


def _hub_dashboard_url() -> Optional[str]:
    """Hub web UI root URL when running in Hub mode (JD_HUB_URL set)."""
    hub = os.environ.get("JD_HUB_URL", "").strip().rstrip("/")
    if not hub:
        return None
    return hub

# ── Auth / PIN constants ──────────────────────────────────────────────────
SESSION_COOKIE = 'jd_session'
MAX_ATTEMPTS   = 3
BLOCK_DURATION = 5 * 60  # seconds

# In-memory rate limiter: {ip: {'attempts': int, 'blocked_until': float}}
_rate_limit: dict = {}

# Paths that do not require authentication
_AUTH_EXEMPT = frozenset([
    '/auth', '/auth/login', '/auth/logout', '/admin/override_pin',
])


def _init_pin_and_token() -> None:
    """Seed default PIN (000000) and admin token on first startup."""
    if db is None:
        return
    if not db.pin_is_set():
        db.set_pin('000000')
        logging.warning("=" * 60)
        logging.warning("DEFAULT DASHBOARD PIN SET TO: 000000")
        logging.warning("Change it via Settings → Change PIN after first login.")
        logging.warning("=" * 60)
    token = db.get_or_create_admin_token()
    logging.info("=" * 60)
    logging.info(f"ADMIN OVERRIDE TOKEN: {token}")
    logging.info("Use this token with POST /admin/override_pin to reset the PIN.")
    logging.info("=" * 60)


# ── Gunicorn worker init ──────────────────────────────────────────────────
# start.py sets JD_WORKSPACE_PATH and JD_EXP_ID in the subprocess environment.
_jd_workspace = os.environ.get("JD_WORKSPACE_PATH", "")
_jd_exp_id    = os.environ.get("JD_EXP_ID", "")
if _jd_workspace and _jd_exp_id:
    BASE_DIR = _jd_workspace
    EXP_ID   = _jd_exp_id
    ensure_exp_layout(BASE_DIR, EXP_ID)
    _meta = exp_meta_dir(BASE_DIR, EXP_ID)
    logging.basicConfig(
        filename=os.path.join(_meta, LOG_FILENAME),
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    DB_FILE  = jobs_db_path(BASE_DIR, EXP_ID)
    db = JobDatabase(DB_FILE)
    _init_pin_and_token()
    logging.info(f"[gunicorn] Dashboard initialised. DB: {DB_FILE}")
# ─────────────────────────────────────────────────────────────────────────


@app.before_request
def require_auth():
    if db is None:
        return
    path = request.path
    if path.startswith('/static') or path in _AUTH_EXEMPT:
        return
    token = request.cookies.get(SESSION_COOKIE, '')
    if db.validate_session(token):
        return
    # API calls: return JSON 401; page navigations: redirect
    if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
        return jsonify({'error': 'Not authenticated', 'redirect': '/auth'}), 401
    return redirect('/auth')


# ─────────────────────────────────────────────────────────────────────────


@app.after_request
def track_traffic(response):
    """Record request/response byte counts for the traffic dashboard."""
    if db is None:
        return response
    try:
        bytes_in  = request.content_length or 0
        bytes_out = response.content_length
        if bytes_out is None:
            bytes_out = len(response.get_data(as_text=False))
        db.add_traffic('dashboard', int(bytes_in), int(bytes_out))
    except Exception:
        pass
    return response


# --------------------- HELPER FUNCTIONS -----------------------


# --------------------- HELPER FUNCTIONS -----------------------


def load_jobs():
    """Load jobs from the SQLite database."""
    try:
        jobs = db.get_all_jobs()

        for job in jobs:
            wid = job_worker_id(job)
            if not wid:
                job["machine"] = "Unassigned"
            else:
                job["machine"] = worker_machine_label(wid)

        return jobs
    except Exception as e:
        logging.error(f"Error loading jobs from database: {e}")
        return []


def format_timestamp(timestamp):
    """Convert a Unix timestamp to human-readable format using client's local timezone."""
    if not timestamp:
        return "N/A"
    try:
        if timestamp < 0:
            return "N/A"
    except TypeError:
        return "N/A"
    # Return the raw timestamp for client-side formatting
    return timestamp


def format_time(seconds):
    """Convert minutes to hh:mm:ss format."""
    return str(timedelta(seconds=round(seconds)))


def calculate_machine_stats(jobs):
    """Per-host stats: instances, workers, jobs done, share of completed work."""
    machine_stats = defaultdict(
        lambda: {
            "count": 0,
            "total_time": 0,
            "instances": set(),
            "workers": set(),
        })
    total_completed = len(
        [job for job in jobs if job["status"] == STATUS_DONE])

    for job in jobs:
        if job["status"] != STATUS_DONE:
            continue
        wid = job_worker_id(job)
        if not wid:
            continue
        host = worker_machine_label(wid)
        machine_stats[host]["count"] += 1
        machine_stats[host]["total_time"] += job["required_time"]
        machine_stats[host]["workers"].add(wid)
        _, inst, _ = JobDatabase.parse_worker_id_parts(wid)
        if inst:
            machine_stats[host]["instances"].add(inst)

    for host, data in machine_stats.items():
        data["average_time"] = format_time(
            (data["total_time"] / data["count"]) if data["count"] else 0)
        data["percentage"] = (
            data["count"] / total_completed * 100) if total_completed else 0
        data["percentage"] = round(data["percentage"], 2)
        data["instance_count"] = len(data["instances"])
        data["worker_count"] = len(data["workers"])

    return dict(sorted(machine_stats.items(), key=lambda kv: kv[0]))


def _valid_completion_ts(job) -> Optional[float]:
    """Return completion timestamp when usable for chart bucketing."""
    ts = job.get("completion_timestamp")
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return ts


def _utc_day_start(ts: float) -> float:
    """UTC midnight for the calendar day containing ts."""
    dt = datetime.fromtimestamp(ts, tz=pytz.utc)
    midnight = datetime(dt.year, dt.month, dt.day, tzinfo=pytz.utc)
    return midnight.timestamp()

# ------------------------- JOB STATISTICS ---------------------


@app.route("/job_stats", methods=["GET"])
def job_stats():
    # Track API request
    db.track_api_request("Job Statistics", "GET")

    jobs = load_jobs()
    interval = request.args.get("interval", "hourly")
    machine = request.args.get("machine", "all")
    # Use UTC for server-side calculations, let client handle timezone conversion
    now = datetime.now(pytz.utc).timestamp()
    job_counts = defaultdict(int)
    total_jobs_completed = 0

    filtered_jobs = [job for job in jobs if job["status"] == STATUS_DONE and (
        machine == "all" or job["machine"] == machine)]

    if interval == "minutely":
        start_time = now - 1800
        # Return timestamps for client-side formatting
        x_labels = [start_time + i * 60 for i in range(30)]
        for job in filtered_jobs:
            if job["completion_timestamp"] >= start_time:
                minute = int((job["completion_timestamp"] - start_time) // 60)
                job_counts[minute] += 1
                total_jobs_completed += 1
    elif interval == "hourly":
        start_time = now - 86400
        # Return timestamps for client-side formatting
        x_labels = [start_time + i * 3600 for i in range(24)]
        for job in filtered_jobs:
            if job["completion_timestamp"] >= start_time:
                hour = int((job["completion_timestamp"] - start_time) // 3600)
                job_counts[hour] += 1
                total_jobs_completed += 1
    else:
        valid_jobs = [
            job for job in filtered_jobs
            if _valid_completion_ts(job) is not None
        ]
        if not valid_jobs:
            return jsonify({"labels": [], "values": [], "total_jobs": 0, "timestamps": True})

        completion_times = [_valid_completion_ts(job) for job in valid_jobs]
        first_day = _utc_day_start(min(completion_times))
        last_day = _utc_day_start(max(completion_times))
        days_elapsed = int((last_day - first_day) // 86400) + 1
        x_labels = [first_day + i * 86400 for i in range(days_elapsed)]
        for job in valid_jobs:
            ts = _valid_completion_ts(job)
            day = int((_utc_day_start(ts) - first_day) // 86400)
            if 0 <= day < days_elapsed:
                job_counts[day] += 1
                total_jobs_completed += 1

    y_values = [job_counts[i] for i in range(len(x_labels))]
    return jsonify({"labels": x_labels, "values": y_values, "total_jobs": total_jobs_completed, "timestamps": True})


@app.route("/api_stats", methods=["GET"])
def api_stats():
    """Return API statistics in JSON format."""
    # Track API request
    db.track_api_request("API Statistics", "GET")

    stats = db.get_api_stats()
    return jsonify({"api_stats": stats})


@app.route("/database_info", methods=["GET"])
def get_database_info():
    """Get database information including indexes and table sizes."""
    # Track API request
    db.track_api_request("Database Info", "GET")

    info = db.get_database_info()
    return jsonify(info)


@app.route("/change_job_status", methods=["POST"])
def change_job_status():
    """Change job status for DONE, ABORTED, or PENDING jobs."""
    # Track API request
    db.track_api_request("Change Job Status", "POST")

    try:
        data = request.get_json()
        job_id = data.get('job_id')
        new_status = data.get('new_status')
        reason = data.get('reason', '')

        if job_id is None or new_status is None:
            return jsonify({"success": False, "error": "Missing job_id or new_status"}), 400

        success = db.change_job_status(job_id, new_status, reason)

        if success:
            return jsonify({"success": True, "message": f"Job {job_id} status changed to {new_status}"})
        else:
            return jsonify({"success": False, "error": "Failed to change job status"}), 400

    except Exception as e:
        logging.error(f"Error changing job status: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/delete_job", methods=["POST"])
def delete_job():
    """Delete a PENDING job by setting its status to DELETED."""
    db.track_api_request("Delete Job", "POST")

    try:
        data = request.get_json()
        job_id = data.get('job_id')
        reason = data.get('reason', '')

        if job_id is None:
            return jsonify({"success": False, "error": "Missing job_id"}), 400

        job = db.get_job_by_id(job_id)
        if not job:
            return jsonify({"success": False, "error": "Job not found"}), 404

        if job['status'] != STATUS_PENDING:
            return jsonify({"success": False, "error": f"Only PENDING jobs can be deleted. Current status: {job['status']}"}), 400

        success = db.delete_job(job_id, reason)
        if success:
            return jsonify({"success": True, "message": f"Job {job_id} deleted successfully"})
        else:
            return jsonify({"success": False, "error": "Failed to delete job"}), 400

    except Exception as e:
        logging.error(f"Error deleting job: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/restore_job", methods=["POST"])
def restore_job():
    """Restore a DELETED job back to PENDING."""
    db.track_api_request("Restore Job", "POST")

    try:
        data = request.get_json()
        job_id = data.get('job_id')
        reason = data.get('reason', '')

        if job_id is None:
            return jsonify({"success": False, "error": "Missing job_id"}), 400

        job = db.get_job_by_id(job_id)
        if not job:
            return jsonify({"success": False, "error": "Job not found"}), 404

        if job['status'] != STATUS_DELETED:
            return jsonify({"success": False, "error": f"Only DELETED jobs can be restored. Current status: {job['status']}"}), 400

        success = db.restore_deleted_job(job_id, reason)
        if success:
            return jsonify({"success": True, "message": f"Job {job_id} restored to PENDING successfully"})
        else:
            return jsonify({"success": False, "error": "Failed to restore job"}), 400

    except Exception as e:
        logging.error(f"Error restoring job: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/update_job_parameters", methods=["POST"])
def update_job_parameters():
    """Update parameters of a PENDING job."""
    db.track_api_request("Update Job Parameters", "POST")

    try:
        data = request.get_json()
        job_id = data.get('job_id')
        updates = data.get('updates')
        reason = data.get('reason', '')

        if job_id is None:
            return jsonify({"success": False, "error": "Missing job_id"}), 400

        if not updates or not isinstance(updates, dict):
            return jsonify({"success": False, "error": "Missing or invalid 'updates' field. Must be a non-empty object."}), 400

        job = db.get_job_by_id(job_id)
        if not job:
            return jsonify({"success": False, "error": "Job not found"}), 404

        if job['status'] != STATUS_PENDING:
            return jsonify({"success": False, "error": f"Only PENDING jobs can have their parameters updated. Current status: {job['status']}"}), 400

        unknown_keys = [k for k in updates if k not in job['parameters']]
        if unknown_keys:
            return jsonify({"success": False, "error": f"Unknown parameter key(s): {', '.join(unknown_keys)}"}), 400

        success = db.update_job_parameters(job_id, updates, reason)
        if success:
            return jsonify({"success": True, "message": f"Job {job_id} parameters updated successfully"})
        else:
            return jsonify({"success": False, "error": "Failed to update parameters. Job may no longer be in PENDING state."}), 400

    except Exception as e:
        logging.error(f"Error updating job parameters: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/create_jobs", methods=["POST"])
def create_jobs():
    """Create all jobs from a parameter grid. Replaces all existing jobs."""
    db.track_api_request("Create Jobs", "POST")

    try:
        data = request.get_json()
        parameters = data.get('parameters')
        idle_timeout = data.get('idle_timeout', 600)
        aborted_job_reset_timeout = data.get('aborted_job_reset_timeout', 1200)
        replace = data.get('replace', False)

        if not parameters or not isinstance(parameters, dict):
            return jsonify({"success": False, "error": "Missing or invalid 'parameters'. Must be a non-empty object."}), 400

        for key, vals in parameters.items():
            if not isinstance(vals, list) or len(vals) == 0:
                return jsonify({"success": False, "error": f"Values for '{key}' must be a non-empty array."}), 400

        keys = list(parameters.keys())
        values = list(parameters.values())
        combos = list(itertools.product(*values))
        parameters_list = [json.dumps(dict(zip(keys, combo))) for combo in combos]

        if replace:
            total_jobs = db.create_jobs(parameters_list)
            action = "Created"
        else:
            total_jobs = db.append_jobs(parameters_list)
            action = "Appended"

        db.set_config_value("idle_timeout", str(int(idle_timeout)))
        db.set_config_value("aborted_job_reset_timeout", str(int(aborted_job_reset_timeout)))

        logging.info(f"{action} {total_jobs} jobs from {len(keys)} parameters (replace={replace}).")
        return jsonify({"success": True, "message": f"{action} {total_jobs} jobs", "total_jobs": total_jobs, "action": action})

    except Exception as e:
        logging.error(f"Error creating jobs: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/server_config", methods=["GET"])
def get_server_config():
    """Return current server configuration (idle_timeout, aborted_job_reset_timeout)."""
    try:
        config = db.get_all_config()
        return jsonify({
            "idle_timeout": int(config.get("idle_timeout", 600)),
            "aborted_job_reset_timeout": int(config.get("aborted_job_reset_timeout", 1200)),
        })
    except Exception as e:
        logging.error(f"Error reading server config: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/update_server_config", methods=["POST"])
def update_server_config():
    """Update idle_timeout and/or aborted_job_reset_timeout."""
    db.track_api_request("Update Server Config", "POST")

    try:
        data = request.get_json()
        updated = []

        for key in ("idle_timeout", "aborted_job_reset_timeout"):
            if key in data:
                val = int(data[key])
                if val <= 0:
                    return jsonify({"success": False, "error": f"'{key}' must be a positive integer."}), 400
                db.set_config_value(key, str(val))
                updated.append(key)

        if not updated:
            return jsonify({"success": False, "error": "No recognised config keys provided."}), 400

        return jsonify({"success": True, "message": f"Updated: {', '.join(updated)}"})

    except Exception as e:
        logging.error(f"Error updating server config: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/auth", methods=["GET"])
def auth_page():
    token = request.cookies.get(SESSION_COOKIE, '')
    if db and db.validate_session(token):
        return redirect('/')
    return render_template('pin_entry.html', expId=EXP_ID)


@app.route("/auth/login", methods=["POST"])
def auth_login():
    ip  = request.remote_addr or '0.0.0.0'
    now = time.time()

    rl  = _rate_limit.get(ip, {'attempts': 0, 'blocked_until': 0.0})

    if now < rl.get('blocked_until', 0.0):
        remaining = int(rl['blocked_until'] - now)
        return jsonify({
            'success': False,
            'error': f'Too many failed attempts. Try again in {remaining} seconds.',
            'blocked_seconds': remaining,
        }), 429

    data = request.get_json(silent=True) or {}
    pin  = str(data.get('pin', ''))

    if not pin.isdigit() or len(pin) != 6:
        return jsonify({'success': False, 'error': 'PIN must be exactly 6 digits.'}), 400

    if db.verify_pin(pin):
        _rate_limit.pop(ip, None)
        token = db.create_session()
        resp  = make_response(jsonify({'success': True}))
        resp.set_cookie(SESSION_COOKIE, token,
                        max_age=7 * 24 * 3600, httponly=True, samesite='Lax')
        return resp

    # Wrong PIN — increment counter
    rl['attempts'] = rl.get('attempts', 0) + 1
    if rl['attempts'] >= MAX_ATTEMPTS:
        rl['blocked_until'] = now + BLOCK_DURATION
        rl['attempts']      = 0
        _rate_limit[ip]     = rl
        return jsonify({
            'success': False,
            'error': f'Too many failed attempts. Blocked for {BLOCK_DURATION // 60} minutes.',
            'blocked_seconds': BLOCK_DURATION,
        }), 429

    _rate_limit[ip] = rl
    left = MAX_ATTEMPTS - rl['attempts']
    return jsonify({
        'success': False,
        'error': f'Incorrect PIN. {left} attempt{"s" if left != 1 else ""} remaining.',
    }), 401


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    token = request.cookies.get(SESSION_COOKIE, '')
    if token:
        db.delete_session(token)
    resp = make_response(jsonify({'success': True}))
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.route("/update_pin", methods=["POST"])
def update_pin():
    """Change PIN — requires valid session (enforced by before_request) + current PIN."""
    data        = request.get_json(silent=True) or {}
    current_pin = str(data.get('current_pin', ''))
    new_pin     = str(data.get('new_pin', ''))

    if not new_pin.isdigit() or len(new_pin) != 6:
        return jsonify({'success': False, 'error': 'New PIN must be exactly 6 digits.'}), 400
    if current_pin == new_pin:
        return jsonify({'success': False, 'error': 'New PIN must differ from the current PIN.'}), 400
    if not db.verify_pin(current_pin):
        return jsonify({'success': False, 'error': 'Current PIN is incorrect.'}), 400

    db.set_pin(new_pin)
    db.clear_all_sessions()
    logging.info("Dashboard PIN changed via Settings. All sessions invalidated.")
    resp = make_response(jsonify({
        'success': True,
        'message': 'PIN updated. All sessions have been signed out — log in again with your new PIN.',
        'reauth_required': True,
    }))
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.route("/admin/override_pin", methods=["POST"])
def admin_override_pin():
    """Override PIN without knowing the current one. Requires the admin token.
    This endpoint is intentionally excluded from session auth (before_request exempt).
    """
    data           = request.get_json(silent=True) or {}
    provided_token = (request.headers.get('X-Admin-Token') or data.get('admin_token', '')).strip()
    expected_token = db.get_config_value('admin_token', '')

    if not provided_token or provided_token != expected_token:
        return jsonify({'success': False, 'error': 'Invalid or missing admin token.'}), 403

    new_pin = str(data.get('new_pin', ''))
    if not new_pin.isdigit() or len(new_pin) != 6:
        return jsonify({'success': False, 'error': 'new_pin must be exactly 6 digits.'}), 400

    db.set_pin(new_pin)
    db.clear_all_sessions()   # force re-authentication for all active browsers
    logging.warning(f"PIN overridden via admin API. All sessions invalidated.")
    return jsonify({'success': True, 'message': 'PIN overridden and all sessions cleared.'})


@app.route("/traffic_stats", methods=["GET"])
def traffic_stats():
    """Return cumulative HTTP traffic byte counts for both services."""
    try:
        return jsonify(db.get_traffic_stats())
    except Exception as e:
        logging.error(f"Error fetching traffic stats: {e}")
        return jsonify({"error": str(e)}), 500


def _jobs_bulk_request(data: dict) -> dict:
    status = (data.get("status") or "").strip().upper() or None
    search = data.get("search") or data.get("search_job_id")
    if search is not None:
        search = str(search).strip() or None
    job_ids = data.get("job_ids")
    if job_ids is not None:
        if not isinstance(job_ids, list):
            raise ValueError("job_ids must be a list")
        job_ids = [int(x) for x in job_ids]
    target = data.get("target")
    if target is not None and data.get("scope") == "job":
        target = int(target)
    return {
        "status": status,
        "search_job_id": search,
        "job_ids": job_ids,
        "target": target,
    }


def _validate_jobs_bulk_scope(scope: str, target, job_ids) -> Optional[str]:
    if scope not in ("job", "jobs", "all"):
        return "scope must be job, jobs, or all"
    if scope == "job" and target is None:
        return "target job id is required for job scope"
    if scope == "jobs" and not job_ids:
        return "job_ids is required for jobs scope"
    return None


@app.route("/jobs/preview", methods=["POST"])
def jobs_preview():
    """Preview jobs affected by a bulk dashboard action."""
    db.track_api_request("Jobs Preview", "POST")
    data = request.json or {}
    action = (data.get("action") or "").strip().lower()
    scope = (data.get("scope") or "").strip().lower()
    if action not in ("delete", "restore", "to_pending", "to_done"):
        return jsonify({"error": "action must be delete, restore, to_pending, or to_done"}), 400
    err = _validate_jobs_bulk_scope(scope, data.get("target"), data.get("job_ids"))
    if err:
        return jsonify({"error": err}), 400
    try:
        filters = _jobs_bulk_request(data)
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    result = db.preview_jobs_bulk_action(
        action, scope, filters["target"], job_ids=filters["job_ids"],
        status=filters["status"], search_job_id=filters["search_job_id"],
    )
    return jsonify({"success": True, **result})


@app.route("/jobs/bulk_action", methods=["POST"])
def jobs_bulk_action():
    """Execute bulk job action with audit reason."""
    db.track_api_request("Jobs Bulk Action", "POST")
    data = request.json or {}
    action = (data.get("action") or "").strip().lower()
    scope = (data.get("scope") or "").strip().lower()
    reason = (data.get("reason") or "").strip()
    if action not in ("delete", "restore", "to_pending", "to_done"):
        return jsonify({"error": "action must be delete, restore, to_pending, or to_done"}), 400
    err = _validate_jobs_bulk_scope(scope, data.get("target"), data.get("job_ids"))
    if err:
        return jsonify({"error": err}), 400
    try:
        filters = _jobs_bulk_request(data)
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    result = db.execute_jobs_bulk_action(
        action, scope, reason,
        filters["target"], job_ids=filters["job_ids"],
        status=filters["status"], search_job_id=filters["search_job_id"],
    )
    logging.info(
        f"Jobs bulk: action={action} scope={scope} affected={result['affected']} "
        f"failed={len(result.get('failed') or [])}"
    )
    return jsonify({"success": True, **result})


@app.route("/jobs_paginated", methods=["GET"])
def get_jobs_paginated():
    """Get jobs with pagination support."""
    # Track API request
    db.track_api_request("Jobs Paginated", "GET")

    try:
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", 50))
        status = request.args.get("status", None)
        search_job_id = request.args.get("search_job_id", None)
        search = request.args.get("search", None) or search_job_id

        # Validate parameters
        if page < 1:
            page = 1
        if per_page < 1 or per_page > 1000:
            per_page = 50

        result = db.get_jobs_paginated(
            page=page, per_page=per_page, status=status, search=search)

        for job in result['jobs']:
            wid = job_worker_id(job)
            job["worker_id"] = wid if wid else "Unassigned"
            job["machine"] = (
                worker_machine_label(wid) if wid else "Unassigned"
            )

        return jsonify(result)

    except Exception as e:
        logging.error(f"Error getting paginated jobs: {e}")
        return jsonify({"error": str(e)}), 500


def _upload_rows_for_job(job_id: int) -> list:
    """List uploads from SQLite, backfilling from disk when the table is empty."""
    rows = db.list_uploads(job_id)
    if not rows:
        disk_rows = scan_uploads_from_disk(BASE_DIR, EXP_ID, str(job_id))
        if disk_rows:
            db.backfill_uploads(disk_rows)
            rows = db.list_uploads(job_id)
    enriched = []
    for row in rows:
        ext = os.path.splitext(row["filename"])[1]
        item = dict(row)
        item["format"] = file_format(ext)
        enriched.append(item)
    return enriched


@app.route("/job_uploads", methods=["GET"])
def list_job_uploads():
    """List result upload versions for a job (newest first)."""
    db.track_api_request("Job Uploads List", "GET")
    try:
        job_id = request.args.get("job_id")
        if not job_id:
            return jsonify({"error": "job_id is required"}), 400
        job_id = int(job_id)
        if db.get_job_by_id(job_id) is None:
            return jsonify({"error": "Job not found"}), 404

        uploads = _upload_rows_for_job(job_id)
        return jsonify(
            {
                "job_id": job_id,
                "uploads": uploads,
                "max_preview_bytes": MAX_PREVIEW_BYTES,
            }
        )
    except ValueError:
        return jsonify({"error": "Invalid job_id"}), 400
    except Exception as e:
        logging.error(f"Error listing job uploads: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/job_uploads/content", methods=["GET"])
def job_upload_content():
    """Return upload file content for in-dashboard preview (≤2 MB, known formats)."""
    db.track_api_request("Job Upload Content", "GET")
    try:
        job_id = request.args.get("job_id")
        filename = request.args.get("filename", "")
        if not job_id or not filename:
            return jsonify({"error": "job_id and filename are required"}), 400
        if not validate_result_filename(filename):
            return jsonify({"error": "Invalid filename"}), 400

        job_id = int(job_id)
        path = resolve_result_file(BASE_DIR, EXP_ID, str(job_id), filename)
        if not path:
            return jsonify({"error": "File not found"}), 404

        preview = read_upload_preview(path)
        preview["job_id"] = job_id
        preview["filename"] = filename
        return jsonify(preview)
    except ValueError:
        return jsonify({"error": "Invalid job_id"}), 400
    except Exception as e:
        logging.error(f"Error reading upload content: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/job_uploads/download", methods=["GET"])
def job_upload_download():
    """Download a result upload file (full size, up to upload limit)."""
    db.track_api_request("Job Upload Download", "GET")
    try:
        job_id = request.args.get("job_id")
        filename = request.args.get("filename", "")
        if not job_id or not filename:
            return jsonify({"error": "job_id and filename are required"}), 400
        if not validate_result_filename(filename):
            return jsonify({"error": "Invalid filename"}), 400

        job_id = int(job_id)
        path = resolve_result_file(BASE_DIR, EXP_ID, str(job_id), filename)
        if not path:
            return jsonify({"error": "File not found"}), 404

        return send_file(path, as_attachment=True, download_name=filename)
    except ValueError:
        return jsonify({"error": "Invalid job_id"}), 400
    except Exception as e:
        logging.error(f"Error downloading upload: {e}")
        return jsonify({"error": str(e)}), 500


# ------------------------ WORKER MANAGEMENT ---------------------
@app.route("/workers/summary", methods=["GET"])
def workers_summary():
    """Live idle/busy worker counts for the dashboard sidebar."""
    db.track_api_request("Worker Summary", "GET")
    return jsonify(db.get_worker_summary())


@app.route("/workers/filters", methods=["GET"])
def workers_filters():
    """Host / instance / slot options for worker filter dropdowns."""
    db.track_api_request("Worker Filters", "GET")
    lifecycle = request.args.get("lifecycle", "active").strip().lower()
    if lifecycle not in ("active", "disabled", "pending", "paused", "all"):
        lifecycle = "active"
    lc = None if lifecycle == "all" else lifecycle
    return jsonify(db.get_worker_filters(lifecycle=lc))


@app.route("/workers/list", methods=["GET"])
def workers_list():
    """Paginated worker list with lifecycle and host/instance/slot filters."""
    db.track_api_request("Worker List", "GET")
    lifecycle = request.args.get("lifecycle", "active").strip().lower()
    if lifecycle not in ("active", "disabled", "pending", "paused", "all"):
        lifecycle = "active"
    host = request.args.get("host", "").strip() or None
    instance = request.args.get("instance", "").strip() or None
    slot_raw = request.args.get("slot", "").strip()
    slot = int(slot_raw) if slot_raw.isdigit() else None
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(request.args.get("per_page", 50))
    except (TypeError, ValueError):
        per_page = 50
    per_page = max(1, min(per_page, 500))
    search = request.args.get("q", "").strip() or None
    lc = None if lifecycle == "all" else lifecycle
    result = db.list_workers_paginated(
        page=page,
        per_page=per_page,
        lifecycle=lc,
        host=host,
        instance=instance,
        slot=slot,
        search=search,
    )
    completed_counts = db.count_completed_jobs_by_workers(
        [w.get("worker_id") or "" for w in result["workers"]],
    )
    for w in result["workers"]:
        w["last_poll_at_fmt"] = format_timestamp(w.get("last_poll_at"))
        w["disabled_at_fmt"] = format_timestamp(w.get("disabled_at"))
        w["first_poll_at_fmt"] = format_timestamp(w.get("first_poll_at"))
        w["completed_jobs"] = completed_counts.get(w.get("worker_id") or "", 0)
    return jsonify(result)


@app.route("/workers/detail", methods=["GET"])
def workers_detail():
    """Worker record without full history (use /workers/history for pages)."""
    db.track_api_request("Worker Detail", "GET")
    worker_id = (request.args.get("worker_id") or "").strip()
    if not worker_id:
        return jsonify({"error": "worker_id is required"}), 400
    worker = db.get_worker(worker_id, include_history=False)
    if not worker:
        return jsonify({"error": "Worker not found"}), 404
    worker["last_poll_at_fmt"] = format_timestamp(worker.get("last_poll_at"))
    worker["disabled_at_fmt"] = format_timestamp(worker.get("disabled_at"))
    worker["first_poll_at_fmt"] = format_timestamp(worker.get("first_poll_at"))
    completed_counts = db.count_completed_jobs_by_workers([worker_id])
    worker["completed_jobs"] = completed_counts.get(worker_id, 0)
    return jsonify(worker)


@app.route("/workers/history", methods=["GET"])
def workers_history():
    """Paginated worker history (newest first)."""
    db.track_api_request("Worker History", "GET")
    worker_id = (request.args.get("worker_id") or "").strip()
    if not worker_id:
        return jsonify({"error": "worker_id is required"}), 400
    page_raw = (request.args.get("page") or "0").strip()
    page_size_raw = (request.args.get("page_size") or "10").strip()
    metrics_only = (request.args.get("metrics_only") or "").strip().lower() in (
        "1", "true", "yes",
    )
    try:
        page = max(0, int(page_raw))
    except ValueError:
        page = 0
    try:
        page_size = max(1, min(100, int(page_size_raw)))
    except ValueError:
        page_size = 10
    result = db.get_worker_history_page(
        worker_id,
        page=page,
        page_size=page_size,
        metrics_only=metrics_only,
    )
    if result is None:
        return jsonify({"error": "Worker not found"}), 404
    return jsonify(result)


def _workers_request_filters(data: dict) -> dict:
    """Parse lifecycle, sidebar filters, and search from a workers API payload."""
    lifecycle = (data.get("lifecycle") or "").strip().lower() or None
    if lifecycle not in ("active", "disabled", "pending", "paused", None):
        lifecycle = None
    host = (data.get("host") or "").strip() or None
    instance = (data.get("instance") or "").strip() or None
    slot_raw = data.get("slot")
    slot = None
    if slot_raw is not None and str(slot_raw).strip().isdigit():
        slot = int(str(slot_raw).strip())
    search = (data.get("q") or data.get("search") or "").strip() or None
    return {
        "lifecycle": lifecycle,
        "host": host,
        "instance": instance,
        "slot": slot,
        "search": search,
    }


def _workers_scope_validate(scope: str, target, worker_ids) -> Optional[str]:
    if scope not in ("worker", "host", "instance", "all", "workers"):
        return "scope must be worker, workers, host, instance, or all"
    if scope in ("worker", "host", "instance") and not target:
        return "target is required for worker/host/instance scope"
    if scope == "workers":
        ids = worker_ids if isinstance(worker_ids, list) else []
        if not ids:
            return "worker_ids is required for workers scope"
    return None


@app.route("/workers/preview", methods=["POST"])
def workers_preview():
    """Preview workers affected by a command or cancel."""
    db.track_api_request("Worker Preview", "POST")
    data = request.json or {}
    action = (data.get("action") or "").strip().lower()
    scope = (data.get("scope") or "").strip().lower()
    target = data.get("target")
    if target is not None:
        target = str(target).strip()
    worker_ids = data.get("worker_ids")
    if worker_ids is not None and not isinstance(worker_ids, list):
        return jsonify({"error": "worker_ids must be a list"}), 400

    err = _workers_scope_validate(scope, target, worker_ids)
    if err:
        return jsonify({"error": err}), 400
    if action not in ("run", "pause", "drain", "stop", "cancel"):
        return jsonify({"error": "action must be run, pause, drain, stop, or cancel"}), 400

    filters = _workers_request_filters(data)
    try:
        result = db.preview_workers_action(
            action,
            scope,
            target,
            worker_ids=worker_ids,
            **filters,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"success": True, **result})


@app.route("/workers/command", methods=["POST"])
def workers_command():
    """Queue run / pause / drain / stop for active workers (applied on next poll)."""
    db.track_api_request("Worker Command", "POST")
    data = request.json or {}
    action = (data.get("action") or "").strip().lower()
    scope = (data.get("scope") or "").strip().lower()
    target = data.get("target")
    if target is not None:
        target = str(target).strip()
    worker_ids = data.get("worker_ids")
    if worker_ids is not None and not isinstance(worker_ids, list):
        return jsonify({"error": "worker_ids must be a list"}), 400

    err = _workers_scope_validate(scope, target, worker_ids)
    if err:
        return jsonify({"error": err}), 400

    filters = _workers_request_filters(data)
    try:
        result = db.set_workers_command(
            action,
            scope,
            target,
            worker_ids=worker_ids,
            **filters,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    logging.info(
        f"Worker command: action={action} scope={scope} target={target} "
        f"affected={result['affected']}"
    )
    return jsonify({"success": True, **result})


@app.route("/workers/cancel", methods=["POST"])
def workers_cancel():
    """Cancel queued (not yet applied) worker commands on active workers."""
    db.track_api_request("Worker Cancel", "POST")
    data = request.json or {}
    scope = (data.get("scope") or "").strip().lower()
    target = data.get("target")
    if target is not None:
        target = str(target).strip()
    worker_ids = data.get("worker_ids")
    if worker_ids is not None and not isinstance(worker_ids, list):
        return jsonify({"error": "worker_ids must be a list"}), 400

    err = _workers_scope_validate(scope, target, worker_ids)
    if err:
        return jsonify({"error": err}), 400

    filters = _workers_request_filters(data)
    reverted = db.cancel_pending_worker_commands(
        scope,
        target,
        worker_ids=worker_ids,
        **filters,
    )
    logging.info(f"Worker cancel: scope={scope} target={target} reverted={reverted}")
    return jsonify({"success": True, "reverted": reverted})


# ------------------------ DASHBOARD ROUTE ---------------------
@app.route("/", methods=["GET"])
def dashboard():
    """Display job statistics and job details in an HTML page with column-based sorting icons."""
    # Track API request
    db.track_api_request("Dashboard", "GET")

    expId = EXP_ID

    # Use efficient data loading instead of loading all jobs
    job_counts = db.get_job_counts_by_status()
    total_jobs = sum(job_counts.values())
    total_jobs_served = job_counts.get(STATUS_SERVED, 0)
    total_jobs_completed = job_counts.get(STATUS_DONE, 0)
    total_jobs_aborted = job_counts.get(STATUS_ABORTED, 0)
    total_jobs_deleted = job_counts.get(STATUS_DELETED, 0)

    # Get machine names efficiently (only from completed jobs for stats)
    completed_jobs = db.get_jobs_by_status(STATUS_DONE)
    machine_names = sorted(set(
        worker_machine_label(job_worker_id(job))
        for job in completed_jobs
        if job_worker_id(job)
    ))

    # Calculate machine stats efficiently
    machine_stats = calculate_machine_stats(completed_jobs)
    # Calculate average completion time efficiently
    avg_completion_time = ""
    if total_jobs_completed > 0:
        total_time = sum(j["required_time"] for j in completed_jobs)
        avg_completion_time = format_time(total_time / total_jobs_completed)

    return render_template(
        'dashboard.html',
        expId=expId,
        hub_dashboard_url=_hub_dashboard_url(),
        total_jobs=total_jobs,
        total_jobs_served=total_jobs_served,
        total_jobs_completed=total_jobs_completed,
        total_jobs_aborted=total_jobs_aborted,
        total_jobs_deleted=total_jobs_deleted,
        job_counts=job_counts,
        avg_completion_time=avg_completion_time,
        format_timestamp=format_timestamp,
        format_time=format_time,
        machine_stats=machine_stats,
        machine_names=machine_names,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Start the Flask Dashboard server")
    parser.add_argument("--expId", type=str, required=True,
                        help="Unique experiment name")
    parser.add_argument("--workspacePath",
                        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."),
                        help="Directory where experiment data lives")
    parser.add_argument("--port", type=int, default=5050,
                        help="Dashboard port (default: 5050)")
    parser.add_argument("--serverPort", type=int, default=5000,
                        help="Job server port, used only for display (default: 5000)")
    args = parser.parse_args()

    BASE_DIR = args.workspacePath
    EXP_ID = args.expId

    ensure_exp_layout(args.workspacePath, args.expId)
    setup_log(args)

    DB_FILE = jobs_db_path(args.workspacePath, args.expId)
    logging.info(f"Starting Flask Dashboard on 0.0.0.0:{args.port}, DB: {DB_FILE}")

    db = JobDatabase(DB_FILE)
    _init_pin_and_token()

    app.run(host="0.0.0.0", port=args.port)

# python src/dashboard.py --expId=mnist_tune --workspacePath=/data/experiments --port=5050 --serverPort=5000
