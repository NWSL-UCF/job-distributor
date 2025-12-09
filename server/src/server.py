import argparse
import json
import logging
import os
import time
import threading
from collections import deque
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
# Job prediction percentage (for limiting predictions to avoid excessive computation)
job_prediction_percentage = 0.05  # Default value, will be overridden by config
# Prediction pool selection method: "sequential" or "random"
prediction_pool = "sequential"  # Default value, will be overridden by config

STATUS_PENDING = "PENDING"
STATUS_SERVED = "SERVED"
STATUS_DONE = "DONE"
STATUS_ABORTED = "ABORTED"

# Queue system for async job processing (like RabbitMQ)
# Dictionary to store ready jobs for workers: {worker_id: job_data}
ready_jobs = {}
ready_jobs_lock = threading.Lock()

# Queue for job request tasks: deque of (requested_by, system_metrics, initialization_timestamp)
job_request_queue = deque()
job_request_queue_lock = threading.Lock()

# Queue for status update tasks: deque of (job_id, status, message)
status_update_queue = deque()
status_update_queue_lock = threading.Lock()

# Queue for cleanup operations
# Dictionary to store cleanup operation results: {operation_id: result_data}
cleanup_results = {}
cleanup_results_lock = threading.Lock()

# Queue for cleanup tasks: deque of (operation_type, operation_id, params)
cleanup_queue = deque()
cleanup_queue_lock = threading.Lock()

# Worker thread control
worker_thread = None
worker_stop_event = threading.Event()


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


def process_job_request(requested_by: str, system_metrics: dict, initialization_timestamp: float):
    """Process a job request: select best job and store in ready_jobs."""
    global bandit, db, ready_jobs, job_prediction_percentage, prediction_pool
    
    try:
        # Get fresh list of pending jobs for this worker
        # This ensures each worker gets a unique job even when requests are processed concurrently
        pending_jobs = db.get_pending_jobs()
        
        if not pending_jobs:
            logging.info(f"No PENDING jobs available for {requested_by}.")
            with ready_jobs_lock:
                ready_jobs[requested_by] = {"error": "No available jobs", "status_code": 404}
            return
        
        # If bandit is available, predict runtime for pending jobs and select the one with lowest runtime
        # Optimization: Limit predictions to top N jobs to avoid excessive computation with many pending jobs
        selected_job_id = None
        selected_predicted_runtime = None
        job_predictions = []  # Initialize to empty list for fallback logic
        if bandit and system_metrics:
            try:
                # Load state once for all predictions (optimization for multiple pending jobs)
                if bandit.model_state_file and pending_jobs:
                    bandit.load_state(force_reload=False)
                
                # Optimization: For high concurrency, limit predictions to avoid excessive computation
                # Predict for configured percentage of total jobs or all pending jobs, whichever is minimum
                job_counts = db.get_job_counts_by_status()
                total_jobs = sum(job_counts.values())
                percentage_of_total = max(1, int(total_jobs * job_prediction_percentage))  # At least 1, round down
                num_to_predict = min(percentage_of_total, len(pending_jobs))
                
                # Select jobs based on prediction_pool setting
                if prediction_pool.lower() == "random":
                    import random
                    jobs_to_predict = random.sample(pending_jobs, min(num_to_predict, len(pending_jobs)))
                    logging.debug(f"Randomly selected {len(jobs_to_predict)} jobs from {len(pending_jobs)} pending jobs for prediction")
                else:  # sequential (default)
                    jobs_to_predict = pending_jobs[:num_to_predict]
                    logging.debug(f"Sequentially selected first {len(jobs_to_predict)} jobs from {len(pending_jobs)} pending jobs for prediction")
                
                # Predict runtime for selected jobs
                for job in jobs_to_predict:
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
                    logging.info(f"Selected job {selected_job_id} with predicted runtime: {selected_predicted_runtime:.2f} seconds for {requested_by}")
            except Exception as e:
                logging.error(f"Error in bandit prediction for {requested_by}: {e}, falling back to first available job")
                selected_job_id = None
                selected_predicted_runtime = None
                job_predictions = []  # Reset on error
        
        # Assign the selected job (or first available if no bandit/selection)
        # Since requests are processed sequentially by the background worker,
        # each worker gets a fresh view of pending jobs, so no race condition
        if selected_job_id is not None:
            job = db.request_job(requested_by, system_metrics, selected_job_id, selected_predicted_runtime, initialization_timestamp)
        else:
            # No bandit selection, just get first available job by ID (predicted_runtime = 0)
            # This happens when bandit_algorithm is "none" or bandit is not initialized
            job = db.request_job(requested_by, system_metrics, None, 0.0, initialization_timestamp)
            if job:
                logging.info(f"Assigned first available job {job['id']} to {requested_by} (no bandit prediction, predicted_runtime=0)")
        
        if not job:
            logging.info(f"No PENDING jobs available for {requested_by}.")
            with ready_jobs_lock:
                ready_jobs[requested_by] = {"error": "No available jobs", "status_code": 404}
            return
        
        # Job successfully assigned - store it in ready_jobs for this worker
        logging.info(f"Job {job['id']} prepared for {requested_by} and marked as SERVED.")
        with ready_jobs_lock:
            ready_jobs[requested_by] = {
                "job_id": job['id'],
                "parameters": job['parameters'],
                "status": STATUS_SERVED,
                "status_code": 200
            }
    except Exception as e:
        logging.error(f"Error processing job request for {requested_by}: {e}")
        with ready_jobs_lock:
            ready_jobs[requested_by] = {"error": f"Internal server error: {str(e)}", "status_code": 500}


def process_status_update(job_id: int, status: str, message: str):
    """Process a status update: update job status and bandit model."""
    global bandit, db
    
    try:
        # Get job info before updating (to retrieve system_metrics and parameters)
        job = db.get_job_by_id(job_id)
        if not job:
            logging.warning(f"Job {job_id} not found for status update")
            return
        
        success = db.update_job_status(job_id, status, message)
        if not success:
            logging.warning(f"Failed to update job {job_id} status: Job not found or not in SERVED status")
            return
        
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
            logging.info(f"Job {job_id} ABORTED. Reason: {message or 'No reason provided'}.")
    except Exception as e:
        logging.error(f"Error processing status update for job {job_id}: {e}")


def process_reset_aborted_jobs(operation_id: str):
    """Process reset aborted jobs: punish bandit and reset jobs."""
    global bandit, db, cleanup_results
    
    try:
        logging.info(f"Processing reset_aborted_jobs operation {operation_id}...")
        
        # Get aborted jobs before resetting (to get system_metrics and parameters)
        aborted_jobs = db.get_jobs_by_status(STATUS_ABORTED)
        
        # Punish the bandit model for failed jobs
        punished_count = 0
        if bandit and aborted_jobs:
            PUNISHMENT_RUNTIME = 1000000.0  # Large punishment value for failed jobs (in seconds)
            for job in aborted_jobs:
                try:
                    system_metrics = job.get('system_metrics', {})
                    job_parameters = job.get('parameters', {})
                    
                    if system_metrics and job_parameters:
                        # Update bandit with large punishment runtime
                        # Use update_with_persistence to prevent race conditions
                        if bandit.model_state_file:
                            bandit.update_with_persistence(system_metrics, job_parameters, PUNISHMENT_RUNTIME)
                        else:
                            bandit.update(system_metrics, job_parameters, PUNISHMENT_RUNTIME)
                        punished_count += 1
                        logging.info(f"Punished bandit model for failed job {job['id']} "
                                   f"(runtime={PUNISHMENT_RUNTIME:.0f}s)")
                except Exception as e:
                    logging.error(f"Error punishing bandit for job {job.get('id')}: {e}")
        
        if punished_count > 0:
            logging.info(f"Applied punishment to bandit model for {punished_count} failed jobs")
        
        # Reset aborted jobs to PENDING
        count = db.reset_aborted_jobs()
        
        if count > 0:
            logging.info(f"Reset {count} ABORTED jobs to PENDING via API.")
        
        # Store result
        with cleanup_results_lock:
            cleanup_results[operation_id] = {
                "status": "completed",
                "jobs_reset": count,
                "bandit_punishments": punished_count,
                "message": f"Reset {count} ABORTED jobs to PENDING"
            }
    except Exception as e:
        logging.error(f"Error processing reset_aborted_jobs operation {operation_id}: {e}")
        with cleanup_results_lock:
            cleanup_results[operation_id] = {
                "status": "error",
                "error": str(e),
                "jobs_reset": 0,
                "bandit_punishments": 0
            }


def process_reset_stale_served_jobs(operation_id: str, idle_timeout: int):
    """Process reset stale served jobs."""
    global db, cleanup_results
    
    try:
        logging.info(f"Processing reset_stale_served_jobs operation {operation_id} (timeout: {idle_timeout}s)...")
        
        count = db.reset_stale_served_jobs(int(idle_timeout))
        
        if count > 0:
            logging.info(f"Reset {count} SERVED jobs due to no ping via API.")
        
        # Store result
        with cleanup_results_lock:
            cleanup_results[operation_id] = {
                "status": "completed",
                "jobs_reset": count,
                "idle_timeout": idle_timeout,
                "message": f"Reset {count} stale SERVED jobs to PENDING"
            }
    except Exception as e:
        logging.error(f"Error processing reset_stale_served_jobs operation {operation_id}: {e}")
        with cleanup_results_lock:
            cleanup_results[operation_id] = {
                "status": "error",
                "error": str(e),
                "jobs_reset": 0,
                "idle_timeout": idle_timeout
            }


def background_worker():
    """Background worker thread that processes queued tasks sequentially."""
    global job_request_queue, status_update_queue, cleanup_queue, ready_jobs
    
    logging.info("Background worker thread started")
    
    while not worker_stop_event.is_set():
        try:
            # Process job request queue
            with job_request_queue_lock:
                if job_request_queue:
                    requested_by, system_metrics, initialization_timestamp = job_request_queue.popleft()
                else:
                    requested_by = None
                    initialization_timestamp = None
            
            if requested_by:
                logging.debug(f"Processing job request for {requested_by}")
                process_job_request(requested_by, system_metrics, initialization_timestamp)
            
            # Process status update queue
            with status_update_queue_lock:
                if status_update_queue:
                    job_id, status, message = status_update_queue.popleft()
                else:
                    job_id = None
            
            if job_id is not None:
                logging.debug(f"Processing status update for job {job_id}")
                process_status_update(job_id, status, message)
            
            # Process cleanup queue
            with cleanup_queue_lock:
                if cleanup_queue:
                    operation_type, operation_id, params = cleanup_queue.popleft()
                else:
                    operation_type = None
            
            if operation_type == "reset_aborted_jobs":
                logging.debug(f"Processing cleanup operation: {operation_type} ({operation_id})")
                process_reset_aborted_jobs(operation_id)
            elif operation_type == "reset_stale_served_jobs":
                logging.debug(f"Processing cleanup operation: {operation_type} ({operation_id})")
                idle_timeout = params.get("idle_timeout", 60)
                process_reset_stale_served_jobs(operation_id, idle_timeout)
            
            # Small sleep to prevent busy waiting
            if not requested_by and job_id is None and operation_type is None:
                time.sleep(0.1)
        except Exception as e:
            logging.error(f"Error in background worker: {e}")
            time.sleep(0.1)
    
    logging.info("Background worker thread stopped")


@app.route("/request_job", methods=["POST"])
def request_job():
    """Queue a job request for async processing. Returns immediately asking worker to wait."""
    global job_request_queue
    
    # Track API request
    db.track_api_request("Job Request", "POST")

    data = request.json or {}
    requested_by = data.get("requested_by")
    system_metrics = data.get("system_metrics", {})

    if not requested_by:
        logging.warning(
            "Job request failed: No requester identification provided.")
        return jsonify({"error": "Requester identification is required"}), 400

    # Check if there are any pending jobs (quick check before queuing)
    pending_jobs = db.get_pending_jobs()
    if not pending_jobs:
        logging.info("No PENDING jobs available.")
        return jsonify({"error": "No available jobs"}), 404

    # Queue the job request for background processing
    # Capture initialization timestamp when request arrives at server
    initialization_timestamp = time.time()
    with job_request_queue_lock:
        # Remove any existing request from this worker (only one pending request per worker)
        job_request_queue = deque([(r, m, t) for r, m, t in job_request_queue if r != requested_by])
        job_request_queue.append((requested_by, system_metrics, initialization_timestamp))
    
    # Clear any old ready job for this worker
    with ready_jobs_lock:
        if requested_by in ready_jobs:
            del ready_jobs[requested_by]
    
    logging.info(f"Job request queued for {requested_by}. Worker should poll /get_job endpoint.")
    return jsonify({
        "message": "Job request received. Please call /get_job endpoint to retrieve your job.",
        "requested_by": requested_by
    }), 202  # 202 Accepted - request accepted for processing


@app.route("/get_job", methods=["POST"])
def get_job():
    """Retrieve a ready job for a worker. Returns job if ready, otherwise asks for more time."""
    global ready_jobs
    
    # Track API request
    db.track_api_request("Get Job", "POST")
    
    data = request.json or {}
    requested_by = data.get("requested_by")
    
    if not requested_by:
        logging.warning("Get job request failed: No requester identification provided.")
        return jsonify({"error": "Requester identification is required"}), 400
    
    # Check if job is ready for this worker
    with ready_jobs_lock:
        if requested_by in ready_jobs:
            job_data = ready_jobs.pop(requested_by)
            status_code = job_data.pop("status_code", 200)
            
            if "error" in job_data:
                logging.info(f"Returning error for {requested_by}: {job_data.get('error')}")
                return jsonify(job_data), status_code
            else:
                logging.info(f"Returning ready job to {requested_by}: job_id={job_data.get('job_id')}")
                return jsonify(job_data), status_code
    
    # Job not ready yet, ask worker to wait
    logging.debug(f"Job not ready yet for {requested_by}, asking for more time")
    return jsonify({
        "message": "Job selection in progress. Please try again in a moment.",
        "status": "processing"
    }), 202  # 202 Accepted - still processing


@app.route("/update_job_status", methods=["POST"])
def update_job_status():
    """Queue a job status update for async processing. Returns immediately confirming receipt."""
    global status_update_queue
    
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

    # Quick validation: check if job exists
    job = db.get_job_by_id(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    
    # Queue the status update for background processing
    with status_update_queue_lock:
        status_update_queue.append((job_id, status, message))
    
    logging.info(f"Status update queued for job {job_id} (status: {status}). Server will process it.")
    return jsonify({
        "message": f"Status update request received for job {job_id}. Server is processing it.",
        "job_id": job_id,
        "status": status
    }), 202  # 202 Accepted - request accepted for processing


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


@app.route("/cleanup/reset_aborted_jobs", methods=["POST"])
def reset_aborted_jobs():
    """Queue reset aborted jobs operation for async processing. Returns immediately."""
    global cleanup_queue, cleanup_results
    
    # Track API request
    db.track_api_request("Reset Aborted Jobs", "POST")
    
    # Generate unique operation ID
    operation_id = f"reset_aborted_{int(time.time() * 1000)}"
    
    # Check if there's already a pending operation of this type
    with cleanup_queue_lock:
        has_pending = any(op_type == "reset_aborted_jobs" for op_type, _, _ in cleanup_queue)
        if has_pending:
            # Check if there's a result for a recent operation (within last 5 minutes)
            with cleanup_results_lock:
                recent_ops = [op_id for op_id in cleanup_results.keys() 
                             if op_id.startswith("reset_aborted_")]
                if recent_ops:
                    # Return status of most recent operation
                    latest_op = sorted(recent_ops)[-1]
                    result = cleanup_results.get(latest_op, {})
                    if result.get("status") == "processing":
                        return jsonify({
                            "message": "Operation already in progress",
                            "operation_id": latest_op,
                            "status": "processing"
                        }), 202
    
    # Queue the operation
    with cleanup_queue_lock:
        cleanup_queue.append(("reset_aborted_jobs", operation_id, {}))
    
    # Mark as processing
    with cleanup_results_lock:
        cleanup_results[operation_id] = {"status": "processing"}
    
    logging.info(f"Queued reset_aborted_jobs operation {operation_id}")
    return jsonify({
        "message": "Reset aborted jobs operation queued. Check status with operation_id.",
        "operation_id": operation_id,
        "status": "queued"
    }), 202  # 202 Accepted - request accepted for processing


@app.route("/cleanup/reset_aborted_jobs/<operation_id>", methods=["GET"])
def get_reset_aborted_jobs_status(operation_id):
    """Get status of a reset aborted jobs operation."""
    global cleanup_results
    
    with cleanup_results_lock:
        result = cleanup_results.get(operation_id)
        if not result:
            return jsonify({"error": "Operation not found"}), 404
        
        if result.get("status") == "processing":
            return jsonify({
                "operation_id": operation_id,
                "status": "processing",
                "message": "Operation in progress"
            }), 202
        else:
            return jsonify(result), 200


@app.route("/cleanup/reset_stale_served_jobs", methods=["POST"])
def reset_stale_served_jobs():
    """Queue reset stale served jobs operation for async processing. Returns immediately."""
    global cleanup_queue, cleanup_results
    
    # Track API request
    db.track_api_request("Reset Stale Served Jobs", "POST")
    
    data = request.json or {}
    idle_timeout = data.get("idle_timeout", 60)  # Default 60 seconds
    
    if not isinstance(idle_timeout, (int, float)) or idle_timeout <= 0:
        return jsonify({"error": "Invalid idle_timeout. Must be a positive number."}), 400
    
    # Generate unique operation ID
    operation_id = f"reset_stale_{int(time.time() * 1000)}"
    
    # Check if there's already a pending operation of this type
    with cleanup_queue_lock:
        has_pending = any(op_type == "reset_stale_served_jobs" for op_type, _, _ in cleanup_queue)
        if has_pending:
            # Check if there's a result for a recent operation (within last 5 minutes)
            with cleanup_results_lock:
                recent_ops = [op_id for op_id in cleanup_results.keys() 
                             if op_id.startswith("reset_stale_")]
                if recent_ops:
                    # Return status of most recent operation
                    latest_op = sorted(recent_ops)[-1]
                    result = cleanup_results.get(latest_op, {})
                    if result.get("status") == "processing":
                        return jsonify({
                            "message": "Operation already in progress",
                            "operation_id": latest_op,
                            "status": "processing"
                        }), 202
    
    # Queue the operation
    with cleanup_queue_lock:
        cleanup_queue.append(("reset_stale_served_jobs", operation_id, {"idle_timeout": int(idle_timeout)}))
    
    # Mark as processing
    with cleanup_results_lock:
        cleanup_results[operation_id] = {"status": "processing"}
    
    logging.info(f"Queued reset_stale_served_jobs operation {operation_id} (timeout: {idle_timeout}s)")
    return jsonify({
        "message": "Reset stale served jobs operation queued. Check status with operation_id.",
        "operation_id": operation_id,
        "status": "queued",
        "idle_timeout": idle_timeout
    }), 202  # 202 Accepted - request accepted for processing


@app.route("/cleanup/reset_stale_served_jobs/<operation_id>", methods=["GET"])
def get_reset_stale_served_jobs_status(operation_id):
    """Get status of a reset stale served jobs operation."""
    global cleanup_results
    
    with cleanup_results_lock:
        result = cleanup_results.get(operation_id)
        if not result:
            return jsonify({"error": "Operation not found"}), 404
        
        if result.get("status") == "processing":
            return jsonify({
                "operation_id": operation_id,
                "status": "processing",
                "message": "Operation in progress"
            }), 202
        else:
            return jsonify(result), 200


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
    global job_prediction_percentage, prediction_pool
    try:
        config_path = os.path.join(BASE_DIR, "config.json")
        with open(config_path, 'r') as f:
            config = json.load(f)
        
        bandit_algorithm = config.get("bandit_algorithm", "linear")
        config_parameters = config.get("parameters", {})
        # Load job prediction percentage from config (default: 0.05 = 5%)
        job_prediction_percentage = config.get("job_prediction_percentage", 0.05)
        # Load prediction pool method from config (default: "sequential")
        prediction_pool = config.get("prediction_pool", "sequential")
        
        # If algorithm is "none", skip bandit initialization
        if bandit_algorithm.lower() == "none":
            logging.info("Bandit algorithm set to 'none'. Skipping bandit initialization. Jobs will be assigned by ID order.")
            bandit = None
        elif config_parameters:
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

    # Start background worker thread for processing queued tasks
    worker_stop_event.clear()
    worker_thread = threading.Thread(target=background_worker, daemon=True)
    worker_thread.start()
    logging.info("Background worker thread started for async job processing")

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
