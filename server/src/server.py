import argparse
import io
import logging
import os
import re
import threading
import time
from datetime import datetime

import jwt
import requests as _requests
from database import JobDatabase
from flask import Flask, jsonify, request, send_file
from job_api_helpers import parse_create_jobs_payload, upload_rows_for_job
from job_files import (
    MAX_PREVIEW_BYTES,
    resolve_latest_result_file,
    resolve_upload_filename,
    sanitize_upload_basename,
    validate_upload_filename,
)
from workspace_layout import (
    ensure_exp_layout,
    exp_meta_dir,
    job_worker_data_dir,
)

app = Flask(__name__)

# Hard limit: reject uploads larger than 100 MB at the WSGI layer
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100 MB

BASE_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
EXP_ID     = ""
LOG_FILENAME = "server.log"

db = None

MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB (also checked in routes)
_VERSION_RE = re.compile(r'_v(\d+)_')

STATUS_PENDING = "PENDING"
STATUS_SERVED  = "SERVED"
STATUS_DONE    = "DONE"
STATUS_ABORTED = "ABORTED"

# ── Optional Hub integration ──────────────────────────────────────────────────
# Set these env vars (via Docker or start.py) to enable Hub mode:
#   JD_WORKER_SHARED_SECRET  — the secret used to verify worker JWTs
#   JD_HUB_URL               — Hub base URL (e.g. https://hub.jobdistributor.net)
#   JD_API_KEY               — Hub API key (used for heartbeats + revoked token poll)
#   JD_EXP_NAME              — experiment name (for Hub API calls)
_WORKER_SHARED_SECRET = os.environ.get("JD_WORKER_SHARED_SECRET", "").strip()
_HUB_URL              = os.environ.get("JD_HUB_URL", "").strip().rstrip("/")
_HUB_API_KEY          = os.environ.get("JD_API_KEY", "").strip()
_EXP_NAME             = os.environ.get("JD_EXP_NAME", "").strip()

# In-memory cache of revoked JTIs (refreshed every 5 minutes from Hub)
_revoked_jtis: set = set()
_revoked_lock = threading.Lock()


def _jwt_verify(token: str) -> dict | None:
    """Return the JWT payload if valid, None otherwise."""
    if not _WORKER_SHARED_SECRET:
        return {}   # Hub mode not enabled — accept all
    try:
        return jwt.decode(token, _WORKER_SHARED_SECRET, algorithms=["HS256"])
    except Exception:
        return None


def _require_worker_token():
    """
    Verify the worker JWT from Authorization header.
    Returns (payload, None) on success, (None, response) on failure.
    """
    if not _WORKER_SHARED_SECRET:
        return {}, None   # standalone mode — no token required
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None, (jsonify({"error": "Missing worker token"}), 401)
    raw = auth[7:].strip()
    payload = _jwt_verify(raw)
    if payload is None:
        return None, (jsonify({"error": "Invalid or expired worker token"}), 401)
    jti = payload.get("jti", "")
    with _revoked_lock:
        if jti in _revoked_jtis:
            return None, (jsonify({"error": "Worker token has been revoked"}), 401)
    return payload, None


def _refresh_revoked_jtis():
    """Poll Hub for revoked JTIs and update the in-memory set."""
    if not (_HUB_URL and _HUB_API_KEY and _EXP_NAME):
        return
    try:
        r = _requests.get(
            f"{_HUB_URL}/api/experiments/{_EXP_NAME}/revoked-tokens",
            headers={"Authorization": f"Bearer {_HUB_API_KEY}"},
            timeout=10,
        )
        if r.status_code == 200:
            jtis = r.json().get("revoked_jtis", [])
            with _revoked_lock:
                _revoked_jtis.clear()
                _revoked_jtis.update(jtis)
    except Exception as exc:
        logging.warning(f"Could not refresh revoked JTIs from Hub: {exc}")


def _hub_heartbeat():
    """Send a periodic heartbeat to Hub to keep experiment ACTIVE."""
    if not (_HUB_URL and _HUB_API_KEY and _EXP_NAME):
        return
    try:
        _requests.post(
            f"{_HUB_URL}/api/experiments/{_EXP_NAME}/heartbeat",
            headers={"Authorization": f"Bearer {_HUB_API_KEY}"},
            timeout=10,
        )
    except Exception as exc:
        logging.debug(f"Hub heartbeat failed: {exc}")


def _start_hub_threads():
    """Start background threads for Hub integration (revoked JTI cache + heartbeat)."""
    if not (_HUB_URL and _HUB_API_KEY):
        return

    def _loop_revoked():
        while True:
            time.sleep(300)   # every 5 minutes
            _refresh_revoked_jtis()

    def _loop_heartbeat():
        while True:
            time.sleep(270)   # every 4.5 minutes
            _hub_heartbeat()

    t1 = threading.Thread(target=_loop_revoked,   daemon=True, name="hub-revoked-poll")
    t2 = threading.Thread(target=_loop_heartbeat, daemon=True, name="hub-heartbeat")
    t1.start()
    t2.start()
    logging.info(f"Hub integration enabled: {_HUB_URL}  exp={_EXP_NAME}")


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
        db.add_traffic('server', int(bytes_in), int(bytes_out))
    except Exception:
        pass
    return response


# ── Gunicorn worker init ──────────────────────────────────────────────────
# When gunicorn imports this module it runs the block below.
# start.py sets JD_WORKSPACE_PATH and JD_EXP_ID in the subprocess environment
# so every worker process can reach the database independently.
_jd_workspace = os.environ.get("JD_WORKSPACE_PATH", "")
_jd_exp_id    = os.environ.get("JD_EXP_ID", "")
if _jd_workspace and _jd_exp_id:
    BASE_DIR = os.path.abspath(_jd_workspace)
    ensure_exp_layout(BASE_DIR, _jd_exp_id)
    _meta = exp_meta_dir(BASE_DIR, _jd_exp_id)
    logging.basicConfig(
        filename=os.path.join(_meta, LOG_FILENAME),
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    EXP_ID  = _jd_exp_id
    db = JobDatabase(os.environ["DATABASE_URL"])
    logging.info(
        f"[gunicorn] Job server initialised. DATABASE_URL set.  BASE_DIR: {BASE_DIR}"
    )
    _start_hub_threads()
# ─────────────────────────────────────────────────────────────────────────


def setup_log(args):
    LOG_FILE = os.path.join(exp_meta_dir(args.workspacePath, args.expId), LOG_FILENAME)
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )


def format_timestamp(timestamp):
    """Convert timestamp to human-readable format."""
    if not timestamp:
        return "N/A"
    try:
        if timestamp < 0:
            return "N/A"
    except TypeError:
        return "N/A"
    return datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')


@app.route("/worker/heartbeat", methods=["POST"])
def worker_heartbeat():
    """Worker heartbeat + control channel. Idle workers may receive a job."""
    _, err = _require_worker_token()
    if err:
        return err
    db.track_api_request("Worker Heartbeat", "POST")

    data = request.json or {}
    worker_id = (data.get("worker_id") or data.get("requested_by") or "").strip()
    if not worker_id:
        return jsonify({"error": "worker_id is required"}), 400

    host = (data.get("host") or "").strip()
    machine_type = (data.get("machine_type") or "worker").strip()
    reported_status = (data.get("reported_status") or "idle").strip()
    current_job_id = data.get("current_job_id")
    if current_job_id is not None and not isinstance(current_job_id, int):
        try:
            current_job_id = int(current_job_id)
        except (TypeError, ValueError):
            return jsonify({"error": "current_job_id must be an integer"}), 400

    applied_version = data.get("applied_version", 0)
    try:
        applied_version = int(applied_version)
    except (TypeError, ValueError):
        return jsonify({"error": "applied_version must be an integer"}), 400

    system_metrics = data.get("system_metrics", {})
    if not isinstance(system_metrics, dict):
        return jsonify({"error": "system_metrics must be an object"}), 400

    jd_worker_version = (data.get("jd_worker_version") or "").strip()

    try:
        result = db.worker_heartbeat(
            worker_id=worker_id,
            host=host,
            machine_type=machine_type,
            reported_status=reported_status,
            current_job_id=current_job_id,
            applied_version=applied_version,
            system_metrics=system_metrics,
            jd_worker_version=jd_worker_version,
        )
    except Exception as e:
        logging.error(f"Worker heartbeat error for {worker_id}: {e}")
        return jsonify({"error": "Internal server error"}), 500

    if result.get("job"):
        logging.info(f"Heartbeat assigned job {result['job']['job_id']} to {worker_id}.")
    return jsonify(result), 200


@app.route("/workers/cli/stop", methods=["POST"])
def workers_cli_stop():
    """Record worker stop initiated from jd_worker_cli (dashboard history + job abort)."""
    _, err = _require_worker_token()
    if err:
        return err
    db.track_api_request("Worker CLI Stop", "POST")

    data = request.json or {}
    worker_id = (data.get("worker_id") or "").strip()
    if not worker_id:
        return jsonify({"error": "worker_id is required"}), 400

    job_id = data.get("job_id")
    if job_id is not None and not isinstance(job_id, int):
        try:
            job_id = int(job_id)
        except (TypeError, ValueError):
            return jsonify({"error": "job_id must be an integer"}), 400

    source = (data.get("source") or data.get("host") or "").strip()
    action = (data.get("action") or "stop").strip().lower()

    try:
        result = db.handle_cli_worker_stop(
            worker_id=worker_id,
            source=source,
            action=action,
            job_id=job_id,
        )
    except Exception as e:
        logging.error(f"CLI worker stop error for {worker_id}: {e}")
        return jsonify({"error": "Internal server error"}), 500

    logging.info(
        f"CLI stop recorded for {worker_id} (job_aborted={result.get('job_aborted')})."
    )
    return jsonify({"success": True, **result}), 200


@app.route("/workers/cli/clear_all", methods=["POST"])
def workers_cli_clear_all():
    """Record jd_worker_cli clear_all — batch worker stops and job aborts."""
    _, err = _require_worker_token()
    if err:
        return err
    db.track_api_request("Worker CLI Clear All", "POST")

    data = request.json or {}
    workers = data.get("workers")
    if not isinstance(workers, list):
        return jsonify({"error": "workers must be a list"}), 400

    source = (data.get("source") or data.get("host") or "").strip()
    try:
        result = db.handle_cli_clear_all(workers, source=source)
    except Exception as e:
        logging.error(f"CLI clear_all error: {e}")
        return jsonify({"error": "Internal server error"}), 500

    logging.info(f"CLI clear_all recorded for {result.get('processed', 0)} worker(s).")
    return jsonify({"success": True, **result}), 200


@app.route("/update_job_status", methods=["POST"])
def update_job_status():
    """Update the status of a job (DONE or ABORTED)."""
    _, err = _require_worker_token()
    if err:
        return err
    db.track_api_request("Job Status Update", "POST")

    data = request.json or {}
    job_id = data.get("job_id")
    status = data.get("status")
    message = data.get("message", "")

    if not isinstance(job_id, int) or status not in [STATUS_DONE, STATUS_ABORTED]:
        logging.warning(f"Invalid job status update: job_id={job_id}, status={status}")
        return jsonify({"error": "Invalid job_id or status"}), 400

    job = db.get_job_by_id(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    success = db.update_job_status(job_id, status, message)
    if not success:
        logging.warning(f"Failed to update job {job_id} status: not found or not in SERVED state")
        return jsonify({"error": "Job not found or not in SERVED state"}), 400

    if status == STATUS_DONE:
        logging.info(f"Job {job_id} marked as DONE.")
    else:
        logging.info(f"Job {job_id} ABORTED. Reason: {message or 'No reason provided'}.")

    return jsonify({"message": f"Job {job_id} status updated to {status}", "job_id": job_id, "status": status}), 200


@app.route("/job_counts", methods=["GET"])
def job_counts():
    """Return job counts grouped by status (worker JWT auth)."""
    _, err = _require_worker_token()
    if err:
        return err
    db.track_api_request("Job Counts", "GET")
    counts = db.get_job_counts_by_status()
    return jsonify(counts), 200


@app.route("/cleanup/reset_aborted_jobs", methods=["POST"])
def reset_aborted_jobs():
    """Reset all ABORTED jobs back to PENDING."""
    db.track_api_request("Reset Aborted Jobs", "POST")

    try:
        count = db.reset_aborted_jobs()
        logging.info(f"Reset {count} ABORTED jobs to PENDING.")
        return jsonify({"message": f"Reset {count} ABORTED jobs to PENDING", "jobs_reset": count}), 200
    except Exception as e:
        logging.error(f"Error resetting aborted jobs: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/cleanup/reset_stale_served_jobs", methods=["POST"])
def reset_stale_served_jobs():
    """Reset SERVED jobs that have not pinged within idle_timeout seconds back to PENDING."""
    db.track_api_request("Reset Stale Served Jobs", "POST")

    data = request.json or {}
    idle_timeout = data.get("idle_timeout", 60)

    if not isinstance(idle_timeout, (int, float)) or idle_timeout <= 0:
        return jsonify({"error": "Invalid idle_timeout. Must be a positive number."}), 400

    try:
        count = db.reset_stale_served_jobs(int(idle_timeout))
        logging.info(f"Reset {count} stale SERVED jobs to PENDING (timeout: {idle_timeout}s).")
        return jsonify({"message": f"Reset {count} stale SERVED jobs to PENDING",
                        "jobs_reset": count, "idle_timeout": idle_timeout}), 200
    except Exception as e:
        logging.error(f"Error resetting stale served jobs: {e}")
        return jsonify({"error": str(e)}), 500


def _job_dir(job_id: str) -> str:
    """Return (and create) the per-job directory for worker uploads/checkpoints."""
    path = job_worker_data_dir(BASE_DIR, EXP_ID, str(job_id))
    os.makedirs(path, exist_ok=True)
    return path


def _next_version(directory: str, prefix: str) -> int:
    """Scan *directory* for files named <prefix>_v{N}_* and return N+1."""
    versions = []
    for fname in os.listdir(directory):
        if fname.startswith(prefix):
            m = _VERSION_RE.search(fname)
            if m:
                versions.append(int(m.group(1)))
    return (max(versions) + 1) if versions else 0


@app.route("/upload", methods=["POST"])
def upload_file():
    """Accept a result file (≤100 MB) from a worker and store it versioned."""
    _, err = _require_worker_token()
    if err:
        return err
    db.track_api_request("Upload File", "POST")

    job_id = request.form.get("job_id")
    if not job_id:
        return jsonify({"error": "job_id is required"}), 400

    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file attached (field name: 'file')"}), 400

    # Enforce size limit in case WSGI layer is bypassed
    data = f.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return jsonify({"error": "File exceeds the 100 MB limit"}), 413

    original_name = sanitize_upload_basename(f.filename or "upload")
    job_directory = _job_dir(job_id)
    filename, _file_version = resolve_upload_filename(job_directory, original_name)
    save_path = os.path.join(job_directory, filename)
    version = db.next_upload_version(int(job_id))
    timestamp = time.time()

    with open(save_path, "wb") as out:
        out.write(data)

    db.record_upload(int(job_id), version, filename, len(data), timestamp)

    logging.info(f"Upload saved: job={job_id}  file={filename}  bytes={len(data)}")
    return jsonify({"success": True, "filename": filename, "version": version,
                    "size_bytes": len(data)})


@app.route("/checkpoint", methods=["POST"])
def save_checkpoint():
    """Accept a serialised checkpoint (≤100 MB) and store it versioned."""
    _, err = _require_worker_token()
    if err:
        return err
    db.track_api_request("Save Checkpoint", "POST")

    job_id = request.form.get("job_id")
    if not job_id:
        return jsonify({"error": "job_id is required"}), 400

    ckpt_file = request.files.get("checkpoint")
    if not ckpt_file:
        return jsonify({"error": "No checkpoint attached (field name: 'checkpoint')"}), 400

    data = ckpt_file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return jsonify({"error": "Checkpoint exceeds the 100 MB limit"}), 413

    job_directory = _job_dir(job_id)
    version       = _next_version(job_directory, "checkpoint_v")
    timestamp     = int(time.time())
    filename      = f"checkpoint_v{version}_{timestamp}.pt"
    save_path     = os.path.join(job_directory, filename)

    with open(save_path, "wb") as out:
        out.write(data)

    logging.info(f"Checkpoint saved: job={job_id}  file={filename}  bytes={len(data)}")
    return jsonify({"success": True, "filename": filename, "version": version,
                    "size_bytes": len(data)})


@app.route("/checkpoint/latest", methods=["GET"])
def get_latest_checkpoint():
    """Return the highest-versioned checkpoint for a job as raw bytes."""
    _, err = _require_worker_token()
    if err:
        return err
    db.track_api_request("Get Latest Checkpoint", "GET")

    job_id = request.args.get("job_id")
    if not job_id:
        return jsonify({"error": "job_id is required"}), 400

    job_directory = job_worker_data_dir(BASE_DIR, EXP_ID, str(job_id))
    if not os.path.isdir(job_directory):
        return jsonify({"error": f"No data found for job {job_id}"}), 404

    candidates = [
        f for f in os.listdir(job_directory)
        if f.startswith("checkpoint_v") and f.endswith(".pt")
    ]
    if not candidates:
        return jsonify({"error": f"No checkpoints found for job {job_id}"}), 404

    def _version(fname):
        m = _VERSION_RE.search(fname)
        return int(m.group(1)) if m else -1

    latest    = max(candidates, key=_version)
    ckpt_path = os.path.join(job_directory, latest)

    logging.info(f"Checkpoint served: job={job_id}  file={latest}")
    return send_file(ckpt_path, mimetype="application/octet-stream",
                     as_attachment=True, download_name=latest)


@app.route("/api/jobs/create", methods=["POST"])
def api_create_jobs():
    """Create or append jobs (JWT auth — for jd job-management library)."""
    _, err = _require_worker_token()
    if err:
        return err
    db.track_api_request("API Create Jobs", "POST")

    try:
        data = request.get_json() or {}
        try:
            parameters, jobs, parameters_list = parse_create_jobs_payload(data)
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

        replace = bool(data.get("replace", False))
        if replace:
            total_jobs = db.create_jobs(parameters_list)
            action = "Created"
        else:
            total_jobs = db.append_jobs(parameters_list)
            action = "Appended"

        idle_timeout = data.get("idle_timeout")
        aborted_job_reset_timeout = data.get("aborted_job_reset_timeout")
        if idle_timeout is not None:
            db.set_config_value("idle_timeout", str(int(idle_timeout)))
        if aborted_job_reset_timeout is not None:
            db.set_config_value(
                "aborted_job_reset_timeout", str(int(aborted_job_reset_timeout))
            )

        if jobs is not None:
            source = f"{len(jobs)} explicit jobs"
        else:
            source = f"{len(parameters or {})} parameters"
        logging.info(f"{action} {total_jobs} jobs from {source} (replace={replace}).")
        return jsonify(
            {
                "success": True,
                "message": f"{action} {total_jobs} jobs",
                "total_jobs": total_jobs,
                "action": action,
            }
        )
    except Exception as exc:
        logging.error(f"Error creating jobs via API: {exc}")
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/jobs", methods=["GET"])
def api_list_jobs():
    """List jobs with pagination (JWT auth)."""
    _, err = _require_worker_token()
    if err:
        return err
    db.track_api_request("API List Jobs", "GET")

    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = int(request.args.get("per_page", 50))
        if per_page < 1 or per_page > 1000:
            per_page = 50
        status = request.args.get("status") or None
        search = request.args.get("search") or request.args.get("search_job_id") or None
        result = db.get_jobs_paginated(
            page=page, per_page=per_page, status=status, search=search
        )
        return jsonify(result)
    except Exception as exc:
        logging.error(f"Error listing jobs via API: {exc}")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/jobs/<int:job_id>/uploads", methods=["GET"])
def api_list_job_uploads(job_id: int):
    """List result uploads for a job (JWT auth)."""
    _, err = _require_worker_token()
    if err:
        return err
    db.track_api_request("API Job Uploads List", "GET")

    try:
        if db.get_job_by_id(job_id) is None:
            return jsonify({"error": "Job not found"}), 404
        uploads = upload_rows_for_job(db, BASE_DIR, EXP_ID, job_id)
        return jsonify(
            {
                "job_id": job_id,
                "uploads": uploads,
                "max_preview_bytes": MAX_PREVIEW_BYTES,
            }
        )
    except Exception as exc:
        logging.error(f"Error listing uploads via API: {exc}")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/jobs/<int:job_id>/uploads/download", methods=["GET"])
def api_download_job_upload(job_id: int):
    """Download a result file; resolves latest version for a logical basename (JWT auth)."""
    _, err = _require_worker_token()
    if err:
        return err
    db.track_api_request("API Job Upload Download", "GET")

    try:
        logical_name = (
            request.args.get("filename")
            or request.args.get("name")
            or ""
        ).strip()
        if not logical_name:
            return jsonify({"error": "filename is required"}), 400
        if not validate_upload_filename(sanitize_upload_basename(logical_name)):
            return jsonify({"error": "Invalid filename"}), 400
        if db.get_job_by_id(job_id) is None:
            return jsonify({"error": "Job not found"}), 404

        path, on_disk = resolve_latest_result_file(
            BASE_DIR, EXP_ID, str(job_id), logical_name
        )
        if not path or not on_disk:
            return jsonify({"error": "File not found"}), 404

        return send_file(path, as_attachment=True, download_name=on_disk)
    except Exception as exc:
        logging.error(f"Error downloading upload via API: {exc}")
        return jsonify({"error": str(exc)}), 500


@app.route("/admin/token", methods=["GET"])
def admin_get_token():
    """Return the admin token. Only accessible from localhost (used by hub scripts)."""
    remote = request.remote_addr or ""
    if remote not in ("127.0.0.1", "::1", "localhost"):
        return jsonify({"error": "Forbidden"}), 403
    token = db.get_or_create_admin_token()
    return jsonify({"admin_token": token})


@app.route("/admin/shutdown", methods=["POST"])
def admin_shutdown():
    """Stop all active workers. Called by Hub before experiment deletion."""
    provided_token = (request.headers.get("X-Admin-Token") or "").strip()
    expected_token = db.get_config_value("admin_token", "")

    if not provided_token or not expected_token or provided_token != expected_token:
        return jsonify({"success": False, "error": "Invalid or missing admin token."}), 403

    result = db.shutdown_stop_all_workers()
    logging.warning(
        "Shutdown requested via admin API — stop queued for %d worker(s).",
        result.get("workers_stopped", 0),
    )
    return jsonify({"success": True, **result})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Start the Flask job server")
    parser.add_argument("--expId", type=str, required=True,
                        help="Unique experiment name")
    parser.add_argument("--workspacePath",
                        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."),
                        help="Directory where experiment data lives (default: parent of src/)")
    parser.add_argument("--port", type=int, default=5000,
                        help="Port number to listen on (default: 5000)")
    args = parser.parse_args()

    BASE_DIR = args.workspacePath
    EXP_ID   = args.expId
    ensure_exp_layout(args.workspacePath, args.expId)
    setup_log(args)

    logging.info(f"Starting Flask job server on 0.0.0.0:{args.port}")

    db = JobDatabase(os.environ["DATABASE_URL"])
    _start_hub_threads()
    app.run(host="0.0.0.0", port=args.port, threaded=True)

# python src/server.py --expId=mnist_tune --workspacePath=/data/experiments --port=5000
