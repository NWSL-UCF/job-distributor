import argparse
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from database import JobDatabase
from flask import Flask, jsonify, request
from contextual_bandit import create_bandit


# Load .env if available (place .env in the server project root)
def _load_dotenv():
    try:
        from dotenv import load_dotenv

        # BASE_DIR here is parent of src, so .env in that folder
        dotenv_path = os.path.join(BASE_DIR, ".env")
        load_dotenv(dotenv_path)
        logging.info(f"Loaded .env from {dotenv_path}")
    except Exception:
        # dotenv is optional; ignore if not installed
        pass


def _parse_ngrok_yml_for_token(path: str) -> str | None:
    try:
        import yaml  # optional, but more robust
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            token = data.get("authtoken") or data.get("auth_token")
            if token and isinstance(token, str) and set(token) != {"*"}:
                return token.strip()
    except ImportError:
        # Fallback to simple line parsing if PyYAML isn't installed
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if "authtoken" in line:
                        parts = line.split(":", 1)
                        if len(parts) == 2:
                            token = parts[1].strip().strip(' "\'')
                            if token and set(token) != {"*"}:
                                return token
        except Exception:
            pass
    except Exception:
        pass
    return None


def _find_ngrok_token_from_yml() -> str | None:
    # Respect NGROK_CONFIG if set
    cfg_env = os.getenv("NGROK_CONFIG")
    if cfg_env and os.path.exists(cfg_env):
        token = _parse_ngrok_yml_for_token(cfg_env)
        if token:
            return token

    # Typical paths (v3 and legacy v2)
    candidates = []
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA")
        user_profile = os.environ.get(
            "USERPROFILE") or os.environ.get("HOMEPATH")
        if local_app:
            candidates.append(os.path.join(
                local_app, "ngrok", "ngrok.yml"))      # v3
        if user_profile:
            candidates.append(os.path.join(
                user_profile, ".ngrok2", "ngrok.yml"))  # v2
    else:
        home = os.path.expanduser("~")
        candidates.append(os.path.join(
            home, ".config", "ngrok", "ngrok.yml"))    # v3
        candidates.append(os.path.join(
            home, ".ngrok2", "ngrok.yml"))             # v2

    for p in candidates:
        if os.path.exists(p):
            token = _parse_ngrok_yml_for_token(p)
            if token:
                logging.info(f"ngrok token loaded from {p}")
                return token
    return None


def _get_ngrok_token() -> str | None:
    # 1) .env (if available), 2) environment variables, 3) YAML
    _load_dotenv()
    token = os.getenv("NGROK_AUTHTOKEN") or os.getenv("NGROK_TOKEN")
    if token:
        return token.strip()
    return _find_ngrok_token_from_yml()


app = Flask(__name__)

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DB_FILE = ""
LOG_FILENAME = "server.log"


def createExpBaseDirectory(args):
    os.makedirs(os.path.join(BASE_DIR, args.expId), exist_ok=True)


def setup_log(args):
    LOG_FILE = os.path.join(BASE_DIR, args.expId, LOG_FILENAME)
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )


# Initialize database connection
db = None
# Initialize contextual bandit
bandit = None

STATUS_PENDING = "PENDING"
STATUS_SERVED = "SERVED"
STATUS_DONE = "DONE"
STATUS_ABORTED = "ABORTED"


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
    """Assign a PENDING job to a requester and mark it as SERVED."""
    global bandit, db
    # Track API request
    db.track_api_request("Job Request", "POST")

    data = request.json or {}
    requested_by = data.get("requested_by")
    system_metrics = data.get("system_metrics", {})

    if not requested_by:
        logging.warning(
            "Job request failed: No requester identification provided.")
        return jsonify({"error": "Requester identification is required"}), 400

    # Get all pending jobs
    pending_jobs = db.get_pending_jobs()
    
    if not pending_jobs:
        logging.info("No PENDING jobs available.")
        return jsonify({"error": "No available jobs"}), 404

    # If bandit is available, predict runtime for all pending jobs and select the one with lowest runtime
    selected_job_id = None
    selected_predicted_runtime = None
    if bandit and system_metrics:
        try:
            # Load state once for all predictions (optimization for multiple pending jobs)
            if bandit.model_state_file and pending_jobs:
                bandit.load_state(force_reload=False)
            
            # Predict runtime for each pending job
            job_predictions = []
            for job in pending_jobs:
                try:
                    # Skip state load for subsequent predictions (already loaded once)
                    predicted_runtime = bandit.predict(system_metrics, job['parameters'], skip_state_load=True)
                    job_predictions.append((job['id'], predicted_runtime))
                    logging.debug(f"Job {job['id']} predicted runtime: {predicted_runtime:.2f} seconds")
                except Exception as e:
                    logging.warning(f"Error predicting runtime for job {job['id']}: {e}")
                    # If prediction fails, use a large default value
                    job_predictions.append((job['id'], float('inf')))
            
            # Select job with lowest predicted runtime
            if job_predictions:
                job_predictions.sort(key=lambda x: x[1])
                selected_job_id = job_predictions[0][0]
                selected_predicted_runtime = job_predictions[0][1]
                logging.info(f"Selected job {selected_job_id} with predicted runtime: {selected_predicted_runtime:.2f} seconds")
        except Exception as e:
            logging.error(f"Error in bandit prediction: {e}, falling back to first available job")
            selected_job_id = None
            selected_predicted_runtime = None

    # Assign the selected job (or first available if no bandit/selection)
    job = db.request_job(requested_by, system_metrics, selected_job_id, selected_predicted_runtime)
    
    if not job:
        logging.info("No PENDING jobs available.")
        return jsonify({"error": "No available jobs"}), 404

    logging.info(
        f"Job {job['id']} assigned to {requested_by} and marked as SERVED.")
    return jsonify({"job_id": job['id'], "parameters": job['parameters'], "status": STATUS_SERVED}), 200


@app.route("/update_job_status", methods=["POST"])
def update_job_status():
    """Update job status as DONE or ABORTED."""
    global bandit, db
    # Track API request
    db.track_api_request("Job Status Update", "POST")

    data = request.json or {}
    job_id = data.get("job_id")
    status = data.get("status")
    message = data.get("message", "")

    if not isinstance(job_id, int) or status not in [STATUS_DONE, STATUS_ABORTED]:
        logging.warning(
            f"Invalid job status update request: job_id={job_id}, status={status}")
        return jsonify({"error": "Invalid job_id or status"}), 400

    # Get job info before updating (to retrieve system_metrics and parameters)
    job = db.get_job_by_id(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    
    success = db.update_job_status(job_id, status, message)
    if not success:
        return jsonify({"error": "Job not found or not in SERVED status"}), 404

    # Update bandit model with observed runtime
    if bandit and job.get('system_metrics') and job.get('parameters'):
        try:
            system_metrics = job.get('system_metrics', {})
            job_parameters = job.get('parameters', {})
            
            if status == STATUS_DONE:
                # Get actual runtime and predicted runtime from updated job
                updated_job = db.get_job_by_id(job_id)
                if updated_job and updated_job.get('required_time', 0) > 0:
                    observed_runtime = updated_job['required_time']
                    saved_predicted_runtime = updated_job.get('predicted_runtime', 0)
                    
                    # Pass the saved predicted runtime to update method for consistent logging
                    # Use update_with_persistence to prevent race conditions
                    if bandit.model_state_file:
                        bandit.update_with_persistence(system_metrics, job_parameters, observed_runtime, 
                                                      saved_predicted_runtime=saved_predicted_runtime)
                    else:
                        bandit.update(system_metrics, job_parameters, observed_runtime,
                                    saved_predicted_runtime=saved_predicted_runtime)
                    logging.info(f"Updated bandit model with job {job_id} completion: "
                               f"runtime={observed_runtime:.2f}s")
            # Note: ABORTED jobs are handled by job_cleaner.py with punishment
        except Exception as e:
            logging.error(f"Error updating bandit model for job {job_id}: {e}")

    if status == STATUS_DONE:
        logging.info(f"Job {job_id} marked as DONE.")
    else:
        logging.info(
            f"Job {job_id} ABORTED. Reason: {message or 'No reason provided'}.")

    return jsonify({"message": f"Job {job_id} updated to {status}", "job_id": job_id}), 200


@app.route("/ping", methods=["POST"])
def ping_job():
    """Update last_ping_timestamp for a SERVED job."""
    # Track API request
    db.track_api_request("Job Ping", "POST")

    data = request.json or {}
    # Accept both keys for compatibility
    job_id = data.get("job_id", data.get("id"))

    if not isinstance(job_id, int):
        logging.warning(f"Invalid ping request: job_id={job_id}")
        return jsonify({"error": "Invalid job_id"}), 400

    success = db.ping_job(job_id)
    if not success:
        # Check if job exists but is not in SERVED state (likely DONE or ABORTED)
        job = db.get_job_by_id(job_id)
        if job:
            # Job exists but is not in SERVED state - this is expected when job completes
            # Return 200 to avoid unnecessary warnings, but don't update ping timestamp
            now = round(time.time())
            logging.debug(
                f"Ping received for job {job_id} (status: {job.get('status', 'UNKNOWN')}). Job not in SERVED state.")
            return jsonify({"message": f"Job {job_id} is not in SERVED state (current: {job.get('status', 'UNKNOWN')})", 
                          "timestamp": now}), 200
        else:
            # Job doesn't exist
            return jsonify({"error": "Job not found"}), 404

    now = round(time.time())
    logging.info(
        f"Ping received for job {job_id}. Updated last_ping_timestamp.")
    return jsonify({"message": f"Ping received for job {job_id}", "timestamp": now}), 200


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Start the Flask server")
    parser.add_argument("--host", default="0.0.0.0",
                        help="IP address to bind to")
    parser.add_argument("--jobDB", default="jobs.db",
                        help="SQLite database file (<filename>.db) placed in the same directory as server.py")
    parser.add_argument("--enableNgrok", action="store_true",
                        help="Enable ngrok for external access")
    parser.add_argument("--port", type=int, default=5000,
                        help="Port number to listen on")
    parser.add_argument("--expId", type=str, default="sim1",
                        help="Give an unique name")
    args = parser.parse_args()
    createExpBaseDirectory(args)
    setup_log(args)
    logging.info(f"Starting Flask server on {args.host}:{args.port}...")
    DB_FILE = os.path.join(BASE_DIR, args.expId, args.jobDB)

    # Initialize database connection
    db = JobDatabase(DB_FILE)

    # Initialize contextual bandit from config
    try:
        config_path = os.path.join(BASE_DIR, "config.json")
        with open(config_path, 'r') as f:
            config = json.load(f)
        
        bandit_algorithm = config.get("bandit_algorithm", "linear")
        config_parameters = config.get("parameters", {})
        
        if config_parameters:
            # Use shared state file for multi-process safety
            state_file_path = os.path.join(BASE_DIR, args.expId, "bandit_model_state.pkl")
            bandit = create_bandit(bandit_algorithm, config_parameters, state_file_path=state_file_path)
            logging.info(f"Initialized contextual bandit: {bandit_algorithm} with state file: {state_file_path}")
        else:
            logging.warning("No parameters found in config, bandit not initialized")
            bandit = None
    except Exception as e:
        logging.error(f"Failed to initialize contextual bandit: {e}")
        bandit = None

    # Start ngrok only if requested and authtoken is set
    if args.enableNgrok:
        # token = os.getenv("NGROK_AUTHTOKEN") or os.getenv("NGROK_TOKEN")
        token = _get_ngrok_token()

        if not token:
            logging.warning(
                "enableNgrok=True but NGROK_AUTHTOKEN is not set. Skipping ngrok.")
        else:
            try:
                from pyngrok import ngrok
                ngrok.set_auth_token(token)
                public_url = ngrok.connect(args.port).public_url
                print(f" >> job_server : {public_url}")
                logging.info(f"ngrok tunnel established at {public_url}")
            except Exception as e:
                logging.error(f"Failed to start ngrok: {e}")

    app.run(host=args.host, port=args.port)

# python server.py --expId=sim1 --jobDB=jobs.db --host=0.0.0.0 --port=5000
