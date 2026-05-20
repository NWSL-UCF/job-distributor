import argparse
import itertools
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pytz
from database import JobDatabase
from flask import Flask, jsonify, make_response, redirect, render_template, request
from workspace_layout import ensure_exp_layout, exp_meta_dir, jobs_db_path

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

        # Add machine field for compatibility
        for job in jobs:
            if not job["requested_by"] or job["requested_by"].strip() == "":
                job["machine"] = "Unassigned"
            else:
                job["machine"] = job["requested_by"].split("_")[0]

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
    """Calculate statistics for each machine group."""
    machine_stats = defaultdict(
        lambda: {"count": 0, "total_time": 0, "instances": set()})
    total_completed = len(
        [job for job in jobs if job["status"] == STATUS_DONE])

    for job in jobs:
        if job["status"] == STATUS_DONE:
            machine_name = job["requested_by"].split(
                "_")[0]  # Extract machine prefix
            machine_stats[machine_name]["count"] += 1
            machine_stats[machine_name]["total_time"] += job["required_time"]
            machine_stats[machine_name]["instances"].add(job["requested_by"])

    for machine, data in machine_stats.items():
        data["average_time"] = format_time(
            (data["total_time"] / data["count"]) if data["count"] else 0)
        data["percentage"] = (
            data["count"] / total_completed * 100) if total_completed else 0
        data["percentage"] = round(data["percentage"], 2)
        data["instance_count"] = len(data["instances"])

    return machine_stats

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
        if not filtered_jobs:
            return jsonify({"labels": [], "values": [], "total_jobs": 0, "timestamps": True})
        first_day = min(job["completion_timestamp"] for job in filtered_jobs)
        days_elapsed = int((now - first_day) // 86400 + 1)
        # Return timestamps for client-side formatting
        x_labels = [first_day + i * 86400 for i in range(days_elapsed)]
        for job in filtered_jobs:
            day = int((job["completion_timestamp"] - first_day) // 86400)
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
    logging.info("Dashboard PIN changed via Settings.")
    return jsonify({'success': True, 'message': 'PIN updated successfully.'})


@app.route("/admin/override_pin", methods=["POST"])
def admin_override_pin():
    """Override PIN without knowing the current one. Requires the admin token.
    This endpoint is intentionally excluded from session auth (before_request exempt).
    The dashboard /update_pin route does NOT have this capability.
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

        # Validate parameters
        if page < 1:
            page = 1
        if per_page < 1 or per_page > 1000:
            per_page = 50

        result = db.get_jobs_paginated(
            page=page, per_page=per_page, status=status, search_job_id=search_job_id)

        # Add machine field for compatibility
        for job in result['jobs']:
            if not job["requested_by"] or job["requested_by"].strip() == "":
                job["machine"] = "Unassigned"
            else:
                job["machine"] = job["requested_by"].split("_")[0]

        return jsonify(result)

    except Exception as e:
        logging.error(f"Error getting paginated jobs: {e}")
        return jsonify({"error": str(e)}), 500


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
    machine_names = sorted(set(job["requested_by"].split(
        "_")[0] if job["requested_by"] else "Unassigned" for job in completed_jobs))

    # Calculate machine stats efficiently
    machine_stats = calculate_machine_stats(completed_jobs)
    api_stats = db.get_api_stats()

    # Calculate total API requests
    total_api_requests = sum(stat['request_count'] for stat in api_stats)

    # Calculate average completion time efficiently
    avg_completion_time = ""
    if total_jobs_completed > 0:
        total_time = sum(j["required_time"] for j in completed_jobs)
        avg_completion_time = format_time(total_time / total_jobs_completed)

    return render_template(
        'dashboard.html',
        expId=expId,
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
        api_stats=api_stats,
        total_api_requests=total_api_requests
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
