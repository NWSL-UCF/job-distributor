import argparse
import logging
import os
import time
from datetime import datetime

from database import JobDatabase
from flask import Flask, jsonify, request

app = Flask(__name__)

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DB_FILE = ""
LOG_FILENAME = "server.log"

db = None

STATUS_PENDING = "PENDING"
STATUS_SERVED = "SERVED"
STATUS_DONE = "DONE"
STATUS_ABORTED = "ABORTED"

# ── Gunicorn worker init ──────────────────────────────────────────────────
# When gunicorn imports this module it runs the block below.
# start.py sets JD_WORKSPACE_PATH and JD_EXP_ID in the subprocess environment
# so every worker process can reach the database independently.
_jd_workspace = os.environ.get("JD_WORKSPACE_PATH", "")
_jd_exp_id    = os.environ.get("JD_EXP_ID", "")
if _jd_workspace and _jd_exp_id:
    _exp_dir = os.path.join(_jd_workspace, _jd_exp_id)
    os.makedirs(_exp_dir, exist_ok=True)
    logging.basicConfig(
        filename=os.path.join(_exp_dir, LOG_FILENAME),
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    DB_FILE = os.path.join(_jd_workspace, _jd_exp_id, "jobs.db")
    db = JobDatabase(DB_FILE)
    logging.info(f"[gunicorn] Job server initialised. DB: {DB_FILE}")
# ─────────────────────────────────────────────────────────────────────────


def createExpBaseDirectory(args):
    os.makedirs(os.path.join(BASE_DIR, args.expId), exist_ok=True)


def setup_log(args):
    LOG_FILE = os.path.join(BASE_DIR, args.expId, LOG_FILENAME)
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


@app.route("/update_job_status", methods=["POST"])
def update_job_status():
    """Update the status of a job (DONE or ABORTED)."""
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
    """Update last_ping_timestamp for a SERVED job."""
    db.track_api_request("Job Ping", "POST")

    data = request.json or {}
    job_id = data.get("job_id", data.get("id"))

    if not isinstance(job_id, int):
        logging.warning(f"Invalid ping request: job_id={job_id}")
        return jsonify({"error": "Invalid job_id"}), 400

    success = db.ping_job(job_id)
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

    # Override BASE_DIR with the workspace path so logs go to the right place
    BASE_DIR = args.workspacePath
    createExpBaseDirectory(args)
    setup_log(args)

    DB_FILE = os.path.join(args.workspacePath, args.expId, "jobs.db")
    logging.info(f"Starting Flask job server on 0.0.0.0:{args.port}, DB: {DB_FILE}")

    db = JobDatabase(DB_FILE)
    app.run(host="0.0.0.0", port=args.port, threaded=True)

# python src/server.py --expId=mnist_tune --workspacePath=/data/experiments --port=5000
