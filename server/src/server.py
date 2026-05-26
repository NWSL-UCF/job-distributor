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
from workspace_layout import (
    ensure_exp_layout,
    exp_meta_dir,
    job_worker_data_dir,
    jobs_db_path,
)

app = Flask(__name__)

# Hard limit: reject uploads larger than 100 MB at the WSGI layer
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100 MB

BASE_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DB_FILE    = ""
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
    DB_FILE = jobs_db_path(BASE_DIR, _jd_exp_id)
    EXP_ID  = _jd_exp_id
    db = JobDatabase(DB_FILE)
    logging.info(
        f"[gunicorn] Job server initialised. DB: {DB_FILE}  BASE_DIR: {BASE_DIR}"
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


@app.route("/request_job", methods=["POST"])
def request_job():
    """Assign the next available PENDING job to the requesting worker."""
    _, err = _require_worker_token()
    if err:
        return err
    db.track_api_request("Job Request", "POST")

    data = request.json or {}
    requested_by = data.get("requested_by")
    system_metrics = data.get("system_metrics", {})

    if not requested_by:
        logging.warning("Job request failed: No requester identification provided.")
        return jsonify({"error": "Requester identification is required"}), 400

    try:
        job = db.request_job(requested_by, system_metrics, None, 0.0, time.time())
    except Exception as e:
        logging.error(f"Error assigning job to {requested_by}: {e}")
        return jsonify({"error": "Internal server error"}), 500

    if not job:
        logging.info(f"No PENDING jobs available for {requested_by}.")
        return jsonify({"error": "No available jobs"}), 404

    logging.info(f"Assigned job {job['id']} to {requested_by}.")
    return jsonify({
        "job_id": job["id"],
        "parameters": job["parameters"],
        "status": STATUS_SERVED,
    }), 200


@app.route("/worker/poll", methods=["POST"])
def worker_poll():
    """Worker heartbeat + control channel. Idle workers may receive a job."""
    _, err = _require_worker_token()
    if err:
        return err
    db.track_api_request("Worker Poll", "POST")

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
        result = db.worker_poll(
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
        logging.error(f"Worker poll error for {worker_id}: {e}")
        return jsonify({"error": "Internal server error"}), 500

    if result.get("job"):
        logging.info(f"Poll assigned job {result['job']['job_id']} to {worker_id}.")
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


@app.route("/ping", methods=["POST"])
def ping_job():
    """Update last_ping_timestamp and system_metrics for a SERVED job."""
    _, err = _require_worker_token()
    if err:
        return err
    db.track_api_request("Job Ping", "POST")

    data = request.json or {}
    job_id = data.get("job_id", data.get("id"))
    system_metrics = data.get("system_metrics")

    if not isinstance(job_id, int):
        logging.warning(f"Invalid ping request: job_id={job_id}")
        return jsonify({"error": "Invalid job_id"}), 400

    if system_metrics is not None and not isinstance(system_metrics, dict):
        return jsonify({"error": "system_metrics must be an object"}), 400

    success = db.ping_job(job_id, system_metrics)
    if not success:
        job = db.get_job_by_id(job_id)
        if job:
            now = round(time.time())
            logging.debug(f"Ping received for job {job_id} (status: {job.get('status', 'UNKNOWN')}). Job not in SERVED state.")
            return jsonify({"message": f"Job {job_id} is not in SERVED state (current: {job.get('status', 'UNKNOWN')})",
                            "timestamp": now}), 200
        else:
            return jsonify({"error": "Job not found"}), 404

    now = round(time.time())
    logging.info(f"Ping received for job {job_id}. Updated last_ping_timestamp.")
    return jsonify({"message": f"Ping received for job {job_id}", "timestamp": now}), 200


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

    original_name = f.filename or "upload"
    _, ext = os.path.splitext(original_name)
    ext = ext.lower() or ".bin"

    job_directory = _job_dir(job_id)
    version       = _next_version(job_directory, "result_v")
    timestamp     = int(time.time())
    filename      = f"result_v{version}_{timestamp}{ext}"
    save_path     = os.path.join(job_directory, filename)

    with open(save_path, "wb") as out:
        out.write(data)

    db.record_upload(int(job_id), version, filename, len(data), float(timestamp))

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

    DB_FILE = jobs_db_path(args.workspacePath, args.expId)
    logging.info(f"Starting Flask job server on 0.0.0.0:{args.port}, DB: {DB_FILE}")

    db = JobDatabase(DB_FILE)
    _start_hub_threads()
    app.run(host="0.0.0.0", port=args.port, threaded=True)

# python src/server.py --expId=mnist_tune --workspacePath=/data/experiments --port=5000
