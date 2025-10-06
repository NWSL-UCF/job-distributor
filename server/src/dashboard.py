import argparse
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pytz
from database import JobDatabase
from flask import Flask, jsonify, render_template_string, request

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

# -------------------------- CONFIG --------------------------
BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DB_FILE = ""
LOG_FILENAME = "dashboard.log"
EXP_ID = "sim100"


def createExpBaseDirectory(args):
    os.makedirs(os.path.join(BASE_DIR, args.expId), exist_ok=True)


def setup_log(args):
    LOG_FILE = os.path.join(BASE_DIR, args.expId, LOG_FILENAME)
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )


STATUS_PENDING = "PENDING"
STATUS_SERVED = "SERVED"
STATUS_DONE = "DONE"
STATUS_ABORTED = "ABORTED"

# Initialize database connection
db = None

# Load configuration


def load_config():
    """Load configuration from config.json"""
    config_path = os.path.join(BASE_DIR, "config.json")
    try:
        with open(config_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Error loading config: {e}")
        return {"status_change_pin": "1234"}  # Default fallback


config = load_config()

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
        pin = data.get('pin', '')

        if job_id is None or new_status is None:
            return jsonify({"success": False, "error": "Missing job_id or new_status"}), 400

        # Validate PIN
        config_pin = config.get('status_change_pin',
                                '1234')  # Default fallback
        if pin != config_pin:
            return jsonify({"success": False, "error": "Invalid PIN"}), 401

        success = db.change_job_status(job_id, new_status, reason)

        if success:
            return jsonify({"success": True, "message": f"Job {job_id} status changed to {new_status}"})
        else:
            return jsonify({"success": False, "error": "Failed to change job status"}), 400

    except Exception as e:
        logging.error(f"Error changing job status: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


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

    # ---------------------- HTML TEMPLATE ----------------------

    html_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Job Dashboard</title>
        <script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
        <link rel="stylesheet" href="https://cdn.datatables.net/2.3.1/css/dataTables.dataTables.min.css">
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <script src="https://cdn.datatables.net/2.3.1/js/dataTables.min.js"></script>
        <style>
            * {
                box-sizing: border-box;
            }
            
            body { 
                font-family: Arial, sans-serif;
                margin: 0;
                padding: 0;
                background: #f8f9fa;
                min-height: 100vh;
            }
            
            .container {
                max-width: 1920px;
                width: 100%;
                height: 100vh;
                margin: 0 auto;
                background: white;
                display: flex;
                flex-direction: column;
                overflow: hidden;
            }
            
            .header {
                background: #f2f2f2;
                color: #333;
                padding: 20px 30px;
                text-align: center;
                flex-shrink: 0;
                border-bottom: 1px solid #ddd;
            }
            
            .header h1 {
                margin: 0;
                font-size: 2.2rem;
                font-weight: bold;
            }
            
            .header h1 i {
                margin-right: 10px;
                font-size: 2rem;
            }
            

            
            .main-content {
                flex: 1;
                display: flex;
                overflow: hidden;
            }
            
            .dashboard-grid {
                display: flex;
                width: 100%;
                height: 100%;
                gap: 0;
            }
            
            .sidebar {
                width: 408px;
                background: white;
                border-right: 1px solid #ddd;
                padding: 20px;
                overflow-y: auto;
                flex-shrink: 0;
                display: flex;
                flex-direction: column;
                gap: 20px;
                direction: rtl;
            }
            
            .sidebar > * {
                direction: ltr;
            }
            }
            
            .stats-card {
                background: white;
                border-radius: 12px;
                padding: 20px 25px;
                box-shadow: 0px 1px 15px 8px rgba(0, 0, 0, 0.08);
                border: none;
            }
            
            .stats-card h3 {
                margin: 0 0 20px 0;
                font-size: 1.4rem;
                font-weight: bold;
                color: #333;
                border-bottom: 2px solid #f0f0f0;
                padding-bottom: 10px;
            }
            
            .experiment-info {
                background: #f9f9f9;
                padding: 15px;
                border-radius: 8px;
                margin-bottom: 20px;
                border-left: 4px solid #2196F3;
            }
            
            .experiment-info p {
                margin: 10px 0;
                font-size: 16px;
                color: #555;
            }
            
            .experiment-info strong {
                color: #333;
                font-weight: bold;
            }
            
            .stats-grid {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 15px;
                margin: 20px 0;
            }
            
            .stat-item {
                text-align: center;
                padding: 15px 10px;
                background: white;
                border-radius: 8px;
                border: 1px solid #e0e0e0;
                box-shadow: 0 2px 4px rgba(0,0,0,0.05);
                transition: transform 0.2s ease;
            }
            
            .stat-item:hover {
                transform: translateY(-2px);
                box-shadow: 0 4px 8px rgba(0,0,0,0.1);
            }
            
            .stat-icon {
                font-size: 1.8rem;
                margin-bottom: 10px;
                display: block;
            }
            
            .stat-label {
                font-size: 14px;
                color: #666;
                margin-bottom: 8px;
                font-weight: 500;
            }
            
            .stat-value {
                font-weight: bold;
                color: #333;
                font-size: 1.5rem;
            }
            
            .chart-container {
                flex: 1;
                background: white;
                padding: 20px;
                display: flex;
                flex-direction: column;
                overflow: hidden;
            }
            
            .chart-wrapper {
                flex: 1;
                position: relative;
                min-height: 0;
            }
            
            #jobChart {
                width: 100% !important;
                height: 100% !important;
            }
            
            .tabs {
                display: flex;
                gap: 10px;
                margin: 20px 0;
                padding: 0;
                justify-content: center;
                flex-shrink: 0;
            }
            
            .tab-button {
                background: #f8f9fa;
                color: #495057;
                border: 1px solid #dee2e6;
                padding: 10px 18px;
                cursor: pointer;
                font-size: 14px;
                font-weight: 600;
                border-radius: 6px;
                transition: all 0.2s ease;
                position: relative;
                min-width: 120px;
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 6px;
                flex: 1;
                max-width: 150px;
            }
            
            .tab-button:hover {
                background: #e9ecef;
                border-color: #adb5bd;
                color: #212529;
                transform: translateY(-1px);
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }
            
            .tab-button.active {
                background: #495057;
                color: white;
                border-color: #495057;
                transform: translateY(-1px);
                box-shadow: 0 2px 4px rgba(0,0,0,0.15);
            }
            
            .tab-button.active::before {
                content: '';
                position: absolute;
                bottom: -6px;
                left: 50%;
                transform: translateX(-50%);
                width: 0;
                height: 0;
                border-left: 6px solid transparent;
                border-right: 6px solid transparent;
                border-top: 6px solid #495057;
            }
            
            .tab-button i {
                font-size: 16px;
            }
            
            .tab-button .tab-count {
                background: #e9ecef;
                color: #495057;
                padding: 2px 6px;
                border-radius: 4px;
                font-size: 11px;
                font-weight: bold;
                margin-left: 4px;
            }
            
            .tab-button.active .tab-count {
                background: rgba(255,255,255,0.2);
                color: white;
            }
            
            /* Notification System */
            .notification {
                position: fixed;
                top: 20px;
                right: 20px;
                z-index: 10000;
                max-width: 400px;
                border-radius: 8px;
                box-shadow: 0 4px 12px rgba(0,0,0,0.15);
                animation: slideInRight 0.3s ease-out;
                font-family: Arial, sans-serif;
            }
            
            @keyframes slideInRight {
                from {
                    transform: translateX(100%);
                    opacity: 0;
                }
                to {
                    transform: translateX(0);
                    opacity: 1;
                }
            }
            
            .notification-content {
                display: flex;
                align-items: center;
                justify-content: space-between;
                padding: 16px 20px;
                border-radius: 8px;
            }
            
            .notification-message {
                flex: 1;
                margin-right: 12px;
                font-size: 14px;
                font-weight: 500;
            }
            
            .notification-close {
                background: none;
                border: none;
                font-size: 18px;
                cursor: pointer;
                padding: 0;
                width: 20px;
                height: 20px;
                display: flex;
                align-items: center;
                justify-content: center;
                border-radius: 50%;
                transition: background-color 0.2s;
            }
            
            .notification-close:hover {
                background-color: rgba(0,0,0,0.1);
            }
            
            .notification-success {
                background: linear-gradient(135deg, #d4edda 0%, #c3e6cb 100%);
                border: 1px solid #c3e6cb;
                color: #155724;
            }
            
            .notification-error {
                background: linear-gradient(135deg, #f8d7da 0%, #f5c6cb 100%);
                border: 1px solid #f5c6cb;
                color: #721c24;
            }
            
            .notification-warning {
                background: linear-gradient(135deg, #fff3cd 0%, #ffeaa7 100%);
                border: 1px solid #ffeaa7;
                color: #856404;
            }
            
            .notification-info {
                background: linear-gradient(135deg, #d1ecf1 0%, #bee5eb 100%);
                border: 1px solid #bee5eb;
                color: #0c5460;
            }
            
            .tabcontent {
                display: none;
                padding: 20px 0;
            }
            
            .tabcontent.active {
                display: block;
            }
            
            .form-group {
                margin: 16px 0;
            }
            
            .form-label {
                display: block;
                margin-bottom: 8px;
                font-size: 16px;
                font-weight: 500;
                color: #374151;
            }
            
            .form-select {
                width: 100%;
                padding: 12px 16px;
                border: 1px solid #d1d5db;
                border-radius: 8px;
                background: white;
                font-size: 16px;
                color: #374151;
                cursor: pointer;
                transition: border-color 0.2s ease;
            }
            
            .form-select:focus {
                outline: none;
                border-color: #667eea;
                box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
            }
            
            .btn {
                background-color: #f2f2f2;
                color: #000;
                border: none;
                padding: 16px 20px;
                border-radius: 6px;
                font-weight: normal;
                cursor: pointer;
                width: 100%;
                margin-bottom: 12px;
                box-shadow: none;
                font-size: 16px;
                min-height: 50px;
            }
            
            .btn:hover {
                background-color: #e6e6e6;
            }
            
            .api-stats {
                max-height: 300px;
                overflow-y: auto;
            }
            
            .api-stat-item {
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 12px 0;
                border-bottom: 1px solid #f1f5f9;
            }
            
            .api-stat-item:last-child {
                border-bottom: none;
            }
            
            .api-endpoint {
                font-weight: 500;
                color: #1e293b;
                font-size: 0.875rem;
            }
            
            .api-method {
                font-size: 0.75rem;
                color: #64748b;
                margin-top: 2px;
            }
            
            .api-count {
                background: #3498db;
                color: white;
                padding: 4px 8px;
                border-radius: 4px;
                font-size: 0.75rem;
                font-weight: 500;
            }
            
            .tabcontent {
                flex: 1;
                overflow-y: auto;
                padding: 0;
            }
            
            .table-wrapper {
                flex: 1;
                overflow: auto;
                position: relative;
            }
            
            .table-watermark {
                position: absolute;
                top: 50%;
                left: 50%;
                transform: translate(-50%, -50%);
                text-align: center;
                z-index: 10;
                pointer-events: none;
                opacity: 0.4;
            }
            
            .table-watermark i {
                font-size: 4rem;
                color: #ccc;
                margin-bottom: 10px;
                display: block;
            }
            
            .table-watermark .watermark-text {
                font-size: 1.2rem;
                color: #999;
                font-weight: 500;
            }
            
            .status-badge {
                display: inline-block;
                padding: 4px 8px;
                border-radius: 12px;
                font-size: 0.75rem;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }
            
            .status-not-started {
                background-color: #f8f9fa;
                color: #6c757d;
                border: 1px solid #dee2e6;
            }
            
            .status-served {
                background-color: #fff3cd;
                color: #856404;
                border: 1px solid #ffeaa7;
            }
            
            .status-done {
                background-color: #d4edda;
                color: #155724;
                border: 1px solid #c3e6cb;
            }
            
            .status-aborted {
                background-color: #f8d7da;
                color: #721c24;
                border: 1px solid #f5c6cb;
            }
            
            .myTable {
                width: 100%;
                border-collapse: collapse;
                table-layout: fixed;
                margin: 0;
                font-size: 0.875rem;
                background: white;
            }
            
            .myTable th {
                background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%);
                color: #495057;
                font-weight: 600;
                padding: 12px 8px;
                text-align: left;
                border-bottom: 2px solid #dee2e6;
                border-right: 1px solid #dee2e6;
                position: sticky;
                top: 0;
                z-index: 10;
                font-size: 0.8rem;
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }
            
            .myTable th:last-child {
                border-right: none;
            }
            
            .myTable th:hover {
                background: linear-gradient(135deg, #e9ecef 0%, #dee2e6 100%);
            }
            
            /* Table Container and Header Styles */
            .table-container {
                display: flex;
                flex-direction: column;
                height: 100%;
                background: white;
                border-radius: 8px;
                border: 1px solid #dee2e6;
                overflow: hidden;
            }
            
            .table-header {
                background: #f8f9fa;
                padding: 15px 20px;
                border-bottom: 1px solid #dee2e6;
                display: flex;
                justify-content: space-between;
                align-items: center;
                flex-wrap: wrap;
                gap: 15px;
            }
            
            .table-title {
                display: flex;
                align-items: center;
                gap: 15px;
            }
            
            .table-title h3 {
                margin: 0;
                font-size: 1.1rem;
                font-weight: 600;
                color: #333;
            }
            
            .search-section {
                display: flex;
                align-items: center;
            }
            
            .search-input-group {
                display: flex;
                align-items: center;
                background: white;
                border: 1px solid #ced4da;
                border-radius: 6px;
                overflow: hidden;
                box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            }
            
            .search-input {
                padding: 8px 12px;
                border: none;
                font-size: 0.875rem;
                width: 200px;
                outline: none;
                background: transparent;
            }
            
            .search-input:focus {
                background: #f8f9fa;
            }
            
            .search-btn, .clear-btn {
                padding: 8px 12px;
                border: none;
                background: transparent;
                color: #6c757d;
                cursor: pointer;
                display: flex;
                align-items: center;
                justify-content: center;
                transition: all 0.2s;
                border-left: 1px solid #e9ecef;
            }
            
            .search-btn:hover {
                background: #e3f2fd;
                color: #1976d2;
            }
            
            .clear-btn:hover {
                background: #ffebee;
                color: #d32f2f;
            }
            
            .pagination-info {
                font-size: 0.875rem;
                color: #6c757d;
                font-weight: 500;
                background: #e9ecef;
                padding: 4px 8px;
                border-radius: 4px;
            }
            
            .pagination-controls {
                display: flex;
                justify-content: center;
                align-items: center;
                gap: 15px;
                padding: 15px;
                background: #f8f9fa;
                border-top: 1px solid #dee2e6;
            }
            
            .pagination-btn {
                padding: 8px 12px;
                border: 1px solid #ced4da;
                background: white;
                border-radius: 4px;
                cursor: pointer;
                display: flex;
                align-items: center;
                gap: 5px;
                font-size: 0.875rem;
                transition: all 0.2s;
            }
            
            .pagination-btn:hover:not(:disabled) {
                background: #e9ecef;
                border-color: #adb5bd;
            }
            
            .pagination-btn:disabled {
                opacity: 0.5;
                cursor: not-allowed;
            }
            
            .page-info {
                font-size: 0.875rem;
                color: #6c757d;
                font-weight: 500;
                min-width: 80px;
                text-align: center;
            }
            
            .loading-message {
                text-align: center;
                padding: 40px;
                color: #6c757d;
                font-style: italic;
            }
            
            .loading-message i {
                margin-right: 8px;
            }
            
            .empty-state {
                text-align: center;
                padding: 60px 20px;
                color: #6c757d;
                background: #f8f9fa;
                border: 2px dashed #dee2e6;
                border-radius: 8px;
                margin: 20px;
            }
            
            .empty-icon {
                margin-bottom: 15px;
            }
            
            .empty-icon i {
                font-size: 3rem;
                color: #adb5bd;
                opacity: 0.6;
            }
            
            .empty-text {
                font-size: 1.1rem;
                font-weight: 600;
                color: #495057;
                margin-bottom: 5px;
            }
            
            .empty-subtext {
                font-size: 0.9rem;
                color: #6c757d;
                font-style: italic;
            }
            
            .error-state {
                background: #fff5f5;
                border-color: #feb2b2;
            }
            
            .error-state .empty-icon i {
                color: #e53e3e;
            }
            
            .error-state .empty-text {
                color: #c53030;
            }
            }
            
            .myTable td {
                padding: 10px 8px;
                border-bottom: 1px solid #e9ecef;
                border-right: 1px solid #e9ecef;
                vertical-align: middle;
                word-wrap: break-word;
                overflow-wrap: break-word;
                transition: all 0.2s ease;
            }
            
            .myTable td:last-child {
                border-right: none;
            }
            
            .myTable tr:nth-child(even) {
                background-color: #f8f9fa;
            }
            
            .myTable tr:nth-child(odd) {
                background-color: white;
            }
            
            .myTable tr:hover {
                background-color: #e3f2fd;
                transform: translateY(-1px);
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }
            
            /* Fixed column widths */
            .myTable th:nth-child(1), .myTable td:nth-child(1) { width: 80px; } /* ID */
            .myTable th:nth-child(2), .myTable td:nth-child(2) { width: 140px; } /* Machine */
            .myTable th:nth-child(3), .myTable td:nth-child(3) { width: 160px; } /* Requested At */
            .myTable th:nth-child(4), .myTable td:nth-child(4) { width: 160px; } /* Started At */
            .myTable th:nth-child(5), .myTable td:nth-child(5) { width: 160px; } /* Completed At */
            .myTable th:nth-child(6), .myTable td:nth-child(6) { width: 120px; } /* Duration */
            .myTable th:nth-child(7), .myTable td:nth-child(7) { width: 140px; } /* Job History */
            
            .myTable td.clickable {
                cursor: pointer;
                color: #3498db;
            }
            
            .myTable td.clickable:hover {
                background-color: #e3f2fd;
                color: #2980b9;
            }
            
            .view-details-btn {
                background: #f8f9fa;
                color: #495057;
                padding: 8px;
                border: 1px solid #dee2e6;
                border-radius: 6px;
                cursor: pointer;
                font-size: 0.9rem;
                transition: all 0.2s ease;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                width: 32px;
                height: 32px;
            }
            
            .view-details-btn:hover {
                background: #e9ecef;
                border-color: #adb5bd;
                color: #212529;
                transform: translateY(-1px);
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }
            
            .sort-icon {
                cursor: pointer;
                margin-left: 0.3em;
                font-size: 0.9em;
                color: #333;
                font-weight: bold;
            }

            /* Modal Styling */
            .modal {
                display: none;
                position: fixed;
                z-index: 1000;
                left: 0;
                top: 0;
                width: 100%;
                height: 100%;
                background-color: rgba(0, 0, 0, 0.4);
            }
            .modal-content {
                background-color: white;
                margin: 5% auto;
                padding: 20px;
                border: 1px solid #888;
                width: 50%;
                text-align: left;
                border-radius: 10px;
                box-shadow: 0px 4px 8px rgba(0, 0, 0, 0.2);
                font-family: Arial, sans-serif;
                overflow-y: auto;
                max-height: calc(100% - 20%);
            }
            .close {
                float: right;
                font-size: 28px;
                font-weight: bold;
                cursor: pointer;
            }

            /* Timeline Styling */
            .timeline {
                position: relative;
            }
            .timeline-item {
                position: relative;
                padding: 10px 0;
                border:1px solid #007bff;
                border-left:3px solid #007bff;
                padding-left:20px;
                margin-bottom:5px;
                border-radius:6px;
            }
            .timeline-item::before {
                content: "●";
                color: #007bff;
                position: absolute;
                left: 6px;
                top: 10px;
                font-size: 14px;
            }
            .timestamp {
                color: #666;
                font-size: 12px;
                font-style: italic;
            }
            .message {
                font-size: 16px;
                font-weight: bold;
            }
            .stats-box {
                background-color: white;
                border-radius: 12px;
                padding: 20px 25px;
                box-shadow: 0px 1px 15px 8px rgba(0, 0, 0, 0.08);
                display: flex;
                flex-direction: column;
                gap: 6px;
                font-family: Arial, sans-serif;
                width: 280px;
            }
            .stats-box p {
                margin: 5px 0;
                font-size: 14px;
                color: #333;
            }
            .stats-box select {
                width: 100%;
                padding: 5px;
                border-radius: 5px;
                border: 1px solid #ccc;
                margin-top: 0px;
            }
            
            .hidden {
                display: none;
            }

            #parametersTable table {
                width: 100%;
                border-collapse: collapse;
                margin-top: 10px;
            }

            #parametersTable th, #parametersTable td {
                border: 1px solid #ccc;
                padding: 8px;
                text-align: left;
            }

            #parametersTable th {
                background-color: #f5f5f5;
            }
                padding: 8px;
                text-align: left;
            }

            #parametersTable th {
                background-color: #f5f5f5;
            }


        </style>
        <style>
            body { font-family: Arial, sans-serif; }
            .modal-2 {
                display: none;
                position: fixed;
                z-index: 1000;
                left: 0;
                top: 0;
                width: 100%;
                height: 100%;
                background-color: rgba(0, 0, 0, 0.4);
            }
            .modal-content-2 {
                background-color: white;
                margin: 5% auto;
                overflow-y: auto;
                max-height: calc(100% - 20%);
                padding: 20px;
                border-radius: 10px;
                width: 60%;
                text-align: center;
                box-shadow: 0px 4px 8px rgba(0, 0, 0, 0.2);
            }
            table.stats-table td:first-child, table.stats-table th:first-child {
                position: sticky;
                left: 0;
            }
            .close {
                float: right;
                font-size: 28px;
                font-weight: bold;
                cursor: pointer;
            }
            .stats-table {
                width: 100%;
                border-collapse: collapse;
                font-size: 16px;
            }
            .stats-table th, .stats-table td {
                padding: 10px;
                text-align: center;
            }
            .stats-table th {
                background-color: #f2f2f2;
                color: black;
                font-weight: bold;
            }
            .modal-button {
                padding: 12px 24px;
                font-size: 16px;
                cursor: pointer;
                background-color: #f2f2f2;
                color: black;
                border: none;
                border-radius: 5px;
                width: 260px;
            }
            #chart-container {
                height: 420px;
                flex: auto;
                justify-content: center;
                align-items: center;
                background-color: white;
                border-radius: 12px;
                padding: 15px;
                box-shadow: 0px 1px 15px 8px rgba(0, 0, 0, 0.08);
            }
            canvas {
                max-width: 100%;
                max-height: 100%;
            }

            .total-jobs-box {
                position: relative;
                bottom: 10px;
                left: 10px;
                background-color: white;
                border-radius: 6px;
                padding: 8px 12px;
                font-size: 14px;
                font-weight: bold;
                color: #333;
                box-shadow: 2px 2px 5px rgba(0, 0, 0, 0.1);
                display: inline-block;
            }
            
            
            
            #jobChart {
                max-width: 100%;
                max-height: 100%;
                width: auto !important;
                height: auto !important;
            }

            /* Progress Bar Styles */
            .progress-container {
                position: relative;
                margin: 20px 0;
                padding: 15px;
                background: #f9f9f9;
                border-radius: 8px;
                border: 1px solid #e0e0e0;
            }
            
            .progress-container h4 {
                margin: 0 0 12px 0;
                font-size: 16px;
                color: #333;
                font-weight: bold;
            }
            
            .progress-bar {
                background-color: #e0e0e0;
                border-radius: 10px;
                overflow: hidden;
                height: 20px;
                box-shadow: inset 0 2px 4px rgba(0,0,0,0.1);
            }
            
            .progress-fill {
                height: 100%;
                border-radius: 10px;
                transition: width 0.3s ease;
                background: linear-gradient(90deg, #4CAF50 0%, #45a049 100%);
                position: relative;
                overflow: hidden;
            }
            
            .progress-fill::after {
                content: '';
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                bottom: 0;
                background: linear-gradient(90deg, 
                    transparent 0%, 
                    rgba(255,255,255,0.3) 50%, 
                    transparent 100%);
                animation: shine 2s infinite;
            }
            
            @keyframes shine {
                0% { transform: translateX(-100%); }
                100% { transform: translateX(100%); }
            }
            
            .progress-text {
                display: flex;
                justify-content: space-between;
                font-size: 14px;
                margin-top: 10px;
                color: #555;
            }

        </style>
        <script>
            function openModal() {
                document.getElementById("statsModal").style.display = "block";
            }
            function closeModal() {
                document.getElementById("statsModal").style.display = "none";
            }
        </script>
        <script>


            // Pagination and Search Variables
            let currentPages = {
                'SERVED': 1,
                'DONE': 1,
                'ABORTED': 1,
                'PENDING': 1
            };
            let currentSearchJobId = null;
            let currentStatus = 'SERVED';

            // Enhanced encoding with validation - Define these functions first
            function encodeForHtmlAttribute(text) {
                try {
                    console.log('encodeForHtmlAttribute input:', text);
                    console.log('encodeForHtmlAttribute input type:', typeof text);
                    
                    if (typeof text !== 'string') {
                        text = safeStringify(text);
                        console.log('After safeStringify:', text);
                    }
                    
                    if (!text || text === 'null' || text === 'undefined') {
                        console.log('Empty/null/undefined, returning empty string');
                        return '';
                    }
                    
                    // Handle essential characters for onclick + preserve newlines
                    const result = text
                        .replace(/"/g, '&quot;')          // Double quote
                        .replace(/'/g, '&#39;')           // Single quote
                        .replace(/&/g, '&amp;')           // Ampersand
                        .replace(/</g, '&lt;')            // Less than
                        .replace(/>/g, '&gt;')            // Greater than
                        .replace(/\\n/g, '&#10;')          // Newline
                        .replace(/\\r/g, '&#13;');         // Carriage return
                    
                    console.log('encodeForHtmlAttribute result:', result);
                    return result;
                } catch (error) {
                    console.error('Error in encodeForHtmlAttribute:', error);
                    return '';
                }
            }

            function decodeFromHtmlAttribute(text) {
                console.log('decodeFromHtmlAttribute input:', text);
                console.log('decodeFromHtmlAttribute input type:', typeof text);
                
                if (typeof text !== 'string') {
                    console.log('Not a string, returning as-is');
                    return text;
                }
                
                const result = text
                    .replace(/&quot;/g, '"')          // Double quote
                    .replace(/&#39;/g, "'")            // Single quote
                    .replace(/&amp;/g, '&')           // Ampersand
                    .replace(/&lt;/g, '<')            // Less than
                    .replace(/&gt;/g, '>')            // Greater than
                    .replace(/&#10;/g, '\\n')          // Newline
                    .replace(/&#13;/g, '\\r');         // Carriage return
                
                console.log('decodeFromHtmlAttribute result:', result);
                return result;
            }

            function safeStringify(obj, fallback = '{}') {
                try {
                    return JSON.stringify(obj);
                } catch (error) {
                    console.error('Error stringifying object:', error);
                    return fallback;
                }
            }

            function safeJsonParse(jsonString, fallback = null) {
                try {
                    console.log('safeJsonParse input:', jsonString);
                    console.log('safeJsonParse input type:', typeof jsonString);
                    
                    // First decode HTML entities
                    const decoded = decodeFromHtmlAttribute(jsonString);
                    console.log('After decodeFromHtmlAttribute:', decoded);
                    
                    // Clean up control characters that break JSON parsing
                    const cleaned = decoded
                        .replace(/\\n/g, '\\\\n')      // Escape newlines for JSON
                        .replace(/\\r/g, '\\\\r')      // Escape carriage returns for JSON
                        .replace(/\\t/g, '\\\\t');     // Escape tabs for JSON
                    
                    console.log('After cleaning control characters:', cleaned);
                    
                    // Then parse JSON
                    const result = JSON.parse(cleaned);
                    console.log('After JSON.parse:', result);
                    return result;
                } catch (error) {
                    console.error('JSON parsing error:', error);
                    console.error('Original string:', jsonString);
                    return fallback;
                }
            }

            function formatMessageForDisplay(message) {
                if (typeof message === 'string') {
                    // Convert newlines to HTML line breaks
                    return message.replace(/\\n/g, '<br>');
                }
                return String(message);
            }

            // Load jobs for a specific status and page
            function loadJobs(status, page = 1, searchJobId = null) {
                const tbody = document.getElementById(`tbody-${status}`);
                const loadingRow = `<tr><td colspan="7" class="loading-message"><i class="fas fa-spinner fa-spin"></i> Loading jobs...</td></tr>`;
                tbody.innerHTML = loadingRow;

                const params = new URLSearchParams({
                    page: page,
                    per_page: 50,
                    status: status
                });

                if (searchJobId) {
                    params.append('search_job_id', searchJobId);
                }

                fetch(`/jobs_paginated?${params}`)
                    .then(response => response.json())
                    .then(data => {
                        if (data.error) {
                            tbody.innerHTML = `
                                <tr>
                                    <td colspan="7" class="empty-state error-state">
                                        <div class="empty-icon">
                                            <i class="fas fa-exclamation-triangle"></i>
                                        </div>
                                        <div class="empty-text">Error Loading Jobs</div>
                                        <div class="empty-subtext">${data.error}</div>
                                    </td>
                                </tr>
                            `;
                            return;
                        }

                        // Update pagination info
                        document.getElementById(`page-info-${status}`).textContent = `Page ${data.current_page} of ${data.total_pages}`;
                        document.getElementById(`prev-${status}`).disabled = data.current_page <= 1;
                        document.getElementById(`next-${status}`).disabled = data.current_page >= data.total_pages;

                        // Update pagination info for this status
                        document.getElementById(`paginationInfo-${status}`).textContent = 
                            `Showing ${((data.current_page - 1) * data.per_page) + 1}-${Math.min(data.current_page * data.per_page, data.total_count)} of ${data.total_count} jobs`;

                        // Render jobs
                        if (data.jobs.length === 0) {
                            tbody.innerHTML = `
                                <tr>
                                    <td colspan="7" class="empty-state">
                                        <div class="empty-icon">
                                            <i class="fas fa-database"></i>
                                        </div>
                                        <div class="empty-text">No jobs found</div>
                                        <div class="empty-subtext">No ${status.toLowerCase()} jobs available</div>
                                    </td>
                                </tr>
                            `;
                            return;
                        }

                        let html = '';
                        data.jobs.forEach(job => {
                            const machine = job.machine || '';
                            const requestTime = job.request_timestamp ? new Date(job.request_timestamp * 1000).toLocaleString() : '';
                            const completionTime = job.completion_timestamp ? new Date(job.completion_timestamp * 1000).toLocaleString() : '';
                            const duration = job.required_time ? formatTime(job.required_time) : '';

                            // Use comprehensive encoding for all special characters
                            const messageJson = encodeForHtmlAttribute(job.message);
                            const parametersJson = encodeForHtmlAttribute(job.parameters);

                            html += `
                                <tr>
                                    <td style="font-weight: bold;">${job.id}</td>
                                    <td>${machine}</td>
                                    <td data-timestamp="${job.request_timestamp || ''}">${requestTime}</td>
                                    <td data-timestamp="${job.request_timestamp || ''}">${requestTime}</td>
                                    <td data-timestamp="${job.completion_timestamp || ''}">${completionTime}</td>
                                    <td>${duration}</td>
                                    <td>
                                        <button class="view-details-btn" onclick="showMessageModalWithRecovery(${job.id}, '${messageJson}', '${parametersJson}', '${job.status}')" title="View Details">
                                            <i class="fas fa-eye"></i>
                                        </button>
                                    </td>
                                </tr>
                            `;
                        });
                        tbody.innerHTML = html;
                    })
                    .catch(error => {
                        console.error('Error loading jobs:', error);
                        tbody.innerHTML = `
                            <tr>
                                <td colspan="7" class="empty-state error-state">
                                    <div class="empty-icon">
                                        <i class="fas fa-exclamation-triangle"></i>
                                    </div>
                                    <div class="empty-text">Error Loading Jobs</div>
                                    <div class="empty-subtext">Failed to load jobs. Please try again.</div>
                                </td>
                            </tr>
                        `;
                    });
            }

            // Change page for a specific status
            function changePage(status, direction) {
                const newPage = currentPages[status] + direction;
                if (newPage >= 1) {
                    currentPages[status] = newPage;
                    // Get current search value for this status
                    const searchInput = document.getElementById(`jobSearch-${status}`);
                    const searchJobId = searchInput.value.trim() || null;
                    loadJobs(status, newPage, searchJobId);
                }
            }

            // Search jobs by ID for a specific tab
            function searchJobs(status) {
                const searchInput = document.getElementById(`jobSearch-${status}`);
                const jobId = searchInput.value.trim();
                
                if (jobId === '') {
                    clearSearch(status);
                    return;
                }

                // Reset page to 1 for this status
                currentPages[status] = 1;
                
                // Load jobs for this specific tab
                loadJobs(status, 1, jobId);
            }

            // Clear search for a specific tab
            function clearSearch(status) {
                const searchInput = document.getElementById(`jobSearch-${status}`);
                searchInput.value = '';
                
                // Reset page to 1 for this status
                currentPages[status] = 1;
                
                // Load jobs for this specific tab
                loadJobs(status, 1);
            }

            // Helper function to format time
            function formatTime(seconds) {
                const hours = Math.floor(seconds / 3600);
                const minutes = Math.floor((seconds % 3600) / 60);
                const secs = Math.floor(seconds % 60);
                return `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
            }

            // Initialize jobs loading when tab is clicked
            function openTab(event, tabName) {
                var i, tabcontent, tablinks;
                tabcontent = document.getElementsByClassName("tabcontent");
                for (i = 0; i < tabcontent.length; i++) {
                    tabcontent[i].classList.remove("active");
                }
                tablinks = document.getElementsByClassName("tab-button");
                for (i = 0; i < tablinks.length; i++) {
                    tablinks[i].classList.remove("active");
                }
                document.getElementById(tabName).classList.add("active");
                event.currentTarget.classList.add("active");
                
                // Load jobs for the selected tab
                currentStatus = tabName;
                // Get current search value for this status
                const searchInput = document.getElementById(`jobSearch-${tabName}`);
                const searchJobId = searchInput ? searchInput.value.trim() || null : null;
                loadJobs(tabName, currentPages[tabName], searchJobId);
            }

            // Load initial data when page loads
            document.addEventListener('DOMContentLoaded', function() {
                // Load jobs for all tabs initially
                const statuses = ['SERVED', 'DONE', 'ABORTED', 'PENDING'];
                statuses.forEach(status => {
                    loadJobs(status, 1);
                    
                    // Add enter key support for each search input
                    const searchInput = document.getElementById(`jobSearch-${status}`);
                    if (searchInput) {
                        searchInput.addEventListener('keypress', function(e) {
                            if (e.key === 'Enter') {
                                searchJobs(status);
                            }
                        });
                    }
                });
            });

            function showMessageModal(jobId, message, parameters, currentStatus) {
                const modal = document.getElementById("messageModal");
                const jobIdSpan = document.getElementById("jobId");
                const messageTimeline = document.getElementById("messageTimeline");
                const parametersTable = document.getElementById("parametersTable");
                const toggleBtn = document.getElementById("toggleParamsBtn");
                const statusChangeSection = document.getElementById("statusChangeSection");
                const statusChangeTitle = document.getElementById("statusChangeTitle");

                // Set Job ID
                jobIdSpan.textContent = jobId;

                // Only show status change for DONE, ABORTED, or PENDING jobs
                if (currentStatus === 'DONE' || currentStatus === 'ABORTED' || currentStatus === 'PENDING') {
                    statusChangeSection.style.display = 'block';
                    
                    // Set title based on current status
                    if (currentStatus === 'DONE') {
                        statusChangeTitle.textContent = 'Change Job Status to PENDING';
                    } else if (currentStatus === 'ABORTED') {
                        statusChangeTitle.textContent = 'Change Job Status to PENDING';
                    } else if (currentStatus === 'PENDING') {
                        statusChangeTitle.textContent = 'Change Job Status to DONE';
                    }
                } else {
                    statusChangeSection.style.display = 'none';
                }

                // Clear old contents
                messageTimeline.innerHTML = "";
                parametersTable.innerHTML = "";
                parametersTable.classList.add("hidden");
                toggleBtn.innerHTML = "&#9654; View Parameters"; // ➤ icon

                try {
                    // Debug: Log the raw data
                    console.log('Raw message:', message);
                    console.log('Raw parameters:', parameters);
                    
                    // Parse parameters using safe JSON parsing
                    let parsedParameters = safeJsonParse(parameters, {});
                    console.log('Parsed parameters:', parsedParameters);

                    // Show parameters table
                    if (parsedParameters && typeof parsedParameters === "object") {
                        const table = document.createElement("table");
                        table.innerHTML = `<tr><th>Key</th><th>Value</th></tr>`;
                        for (let key in parsedParameters) {
                            const row = document.createElement("tr");
                            row.innerHTML = `<td>${key}</td><td>${parsedParameters[key]}</td>`;
                            table.appendChild(row);
                        }
                        parametersTable.appendChild(table);
                    }

                    // Parse message using safe JSON parsing
                    let parsedMessage = safeJsonParse(message, []);
                    console.log('Parsed message:', parsedMessage);

                    // Show job history (newest first)
                    const reversedMessage = JSON.parse(JSON.stringify(parsedMessage)).reverse();

                    reversedMessage.forEach(entry => {
                        const item = document.createElement("div");
                        item.classList.add("timeline-item");

                        const timestamp = new Date(entry.timestamp * 1000).toLocaleString("en-US", {
                            weekday: "long",
                            year: "numeric",
                            month: "short",
                            day: "numeric",
                            hour: "2-digit",
                            minute: "2-digit",
                            second: "2-digit",
                            hour12: true
                        });

                        item.innerHTML = `
                            <div class="message">${formatMessageForDisplay(entry.reason)}</div>
                            <div class="timestamp">${timestamp}</div>
                        `;
                        messageTimeline.appendChild(item);
                    });

                } catch (e) {
                    console.error('Error parsing message/parameters:', e);
                    messageTimeline.innerHTML = "<p>Invalid message format</p>";
                }

                modal.style.display = "block";
            }

            function toggleParams() {
                const parametersTable = document.getElementById("parametersTable");
                const toggleBtn = document.getElementById("toggleParamsBtn");

                if (parametersTable.classList.contains("hidden")) {
                    parametersTable.classList.remove("hidden");
                    toggleBtn.innerHTML = "&#9660; Hide Parameters"; // ▼
                } else {
                    parametersTable.classList.add("hidden");
                    toggleBtn.innerHTML = "&#9654; View Parameters"; // ➤
                }
            }

            function closeMessageModal() {
                document.getElementById("messageModal").style.display = "none";
            }

            function changeJobStatus() {
                const jobId = document.getElementById("jobId").textContent;
                const reason = document.getElementById("statusChangeReason").value;
                const pin = document.getElementById("statusChangePin").value;
                const title = document.getElementById("statusChangeTitle").textContent;
                
                // Determine new status from title
                let newStatus = "";
                if (title.includes("to PENDING")) {
                    newStatus = "PENDING";
                } else if (title.includes("to DONE")) {
                    newStatus = "DONE";
                }
                
                if (!newStatus) {
                    showNotification("Unable to determine target status", "error");
                    return;
                }
                
                if (!pin) {
                    showNotification("Please enter the 4-digit PIN", "warning");
                    return;
                }
                
                if (pin.length !== 4 || !/^'\'d{4}$/.test(pin)) {
                    showNotification("PIN must be exactly 4 digits", "warning");
                    return;
                }
                
                // Show loading state
                const changeBtn = document.getElementById("changeStatusBtn");
                const originalText = changeBtn.textContent;
                changeBtn.textContent = "Changing...";
                changeBtn.disabled = true;
                
                fetch('/change_job_status', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        job_id: parseInt(jobId),
                        new_status: newStatus,
                        reason: reason,
                        pin: pin
                    })
                })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        showNotification("Status changed successfully!", "success");
                        // Reset form
                        document.getElementById("statusChangeReason").value = "";
                        document.getElementById("statusChangePin").value = "";
                        // Close modal after a short delay
                        setTimeout(() => {
                            closeMessageModal();
                            location.reload();
                        }, 1500);
                    } else {
                        showNotification("Error: " + (data.error || "Failed to change status"), "error");
                    }
                })
                .catch(error => {
                    console.error('Error:', error);
                    showNotification("Error changing status: " + error.message, "error");
                })
                .finally(() => {
                    // Reset button state
                    changeBtn.textContent = originalText;
                    changeBtn.disabled = false;
                });
            }

            function showNotification(message, type = "info") {
                // Remove existing notifications
                const existingNotifications = document.querySelectorAll('.notification');
                existingNotifications.forEach(notification => notification.remove());
                
                // Create notification element
                const notification = document.createElement('div');
                notification.className = `notification notification-${type}`;
                notification.innerHTML = `
                    <div class="notification-content">
                        <span class="notification-message">${message}</span>
                        <button class="notification-close" onclick="this.parentElement.parentElement.remove()">×</button>
                    </div>
                `;
                
                // Add to page
                document.body.appendChild(notification);
                
                // Auto-remove after 5 seconds for success/info, 8 seconds for warnings/errors
                const autoRemoveTime = (type === "success" || type === "info") ? 5000 : 8000;
                setTimeout(() => {
                    if (notification.parentElement) {
                        notification.remove();
                    }
                }, autoRemoveTime);
            }

            window.onclick = function(event) {
                const modal = document.getElementById("messageModal");
                if (event.target === modal) {
                    modal.style.display = "none";
                }
            };

            /**
            * Sort an HTML table (tbody rows) by a specific column index.
            * tableId: the ID of the <table> element
            * colIndex: the zero-based index of the column to sort
            * dataType: "string" | "number" for how values are compared
            * iconRef: The <span> element that was clicked (to track asc/desc)
            */
            function sortTable(tableId, colIndex, dataType, iconRef) {
                const table = document.getElementById(tableId);
                const tbody = table.querySelector("tbody");
                const rows = Array.from(tbody.querySelectorAll("tr"));

                // Check or toggle ascending/descending state on the icon
                let ascending = iconRef.getAttribute("data-ascending") === "true" ? false : true;
                iconRef.setAttribute("data-ascending", ascending ? "true" : "false");

                rows.sort((rowA, rowB) => {
                    const cellA = rowA.children[colIndex].innerText.trim();
                    const cellB = rowB.children[colIndex].innerText.trim();

                    // For numeric columns
                    if (dataType === "number") {
                        const valA = parseFloat(cellA) || 0;
                        const valB = parseFloat(cellB) || 0;
                        return ascending ? valA - valB : valB - valA;
                    } 
                    // Otherwise string compare
                    if (ascending) {
                        return cellA.localeCompare(cellB);
                    } else {
                        return cellB.localeCompare(cellA);
                    }
                });

                // Re-insert sorted rows into the table
                rows.forEach(row => tbody.appendChild(row));
            }

            // Enhanced error handling and validation
            function validateJobData(job) {
                if (!job || typeof job !== 'object') {
                    console.error('Invalid job data:', job);
                    return false;
                }
                
                if (!job.id || !job.status) {
                    console.error('Job missing required fields:', job);
                    return false;
                }
                
                return true;
            }



            // Comprehensive error recovery for message modal
            function showMessageModalWithRecovery(jobId, message, parameters, currentStatus) {
                try {
                    showMessageModal(jobId, message, parameters, currentStatus);
                } catch (error) {
                    console.error('Error in showMessageModal, attempting recovery:', error);
                    
                    // Fallback: show basic information without parsing
                    const modal = document.getElementById("messageModal");
                    const jobIdSpan = document.getElementById("jobId");
                    const messageTimeline = document.getElementById("messageTimeline");
                    
                    if (modal && jobIdSpan && messageTimeline) {
                        jobIdSpan.textContent = jobId;
                        messageTimeline.innerHTML = `
                            <div class="timeline-item">
                                <div class="message">Job ID: ${jobId}</div>
                                <div class="timestamp">Status: ${currentStatus}</div>
                            </div>
                            <div class="timeline-item">
                                <div class="message">⚠️ Error displaying job details</div>
                                <div class="timestamp">Please check console for details</div>
                            </div>
                        `;
                        modal.style.display = "block";
                    } else {
                        alert(`Error displaying job ${jobId}. Please check console for details.`);
                    }
                }
            }
        </script>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1><i class="fas fa-chart-line"></i> Job Distribution Dashboard</h1>
            </div>
            
            <div class="main-content">
        
        <div id="statsModal" class="modal-2">
            <div class="modal-content-2">
                <span class="close" onclick="closeModal()">&times;</span>
                <h2>Machine Specific Stats</h2>
                <div style="text-align: center; margin-bottom: 20px;">
                    <div style="display: inline-block; position: relative; width: 80px; height: 80px;">
                        <svg width="80" height="80" style="transform: rotate(-90deg);">
                            <circle cx="40" cy="40" r="35" stroke="#e0e0e0" stroke-width="6" fill="none"></circle>
                            <circle cx="40" cy="40" r="35" stroke="#4CAF50" stroke-width="6" fill="none" 
                                stroke-dasharray="{{ 2 * 3.14159 * 35 }}" 
                                stroke-dashoffset="{{ 2 * 3.14159 * 35 * (1 - (total_jobs_completed / total_jobs if total_jobs > 0 else 0)) }}"
                                style="transition: stroke-dashoffset 0.5s ease;"></circle>
                        </svg>
                        <div style="position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); font-weight: bold; font-size: 12px;">
                            {{ "%.0f"|format((total_jobs_completed / total_jobs * 100) if total_jobs > 0 else 0) }}%
                        </div>
                    </div>
                    <p style="margin: 10px 0 0 0; font-size: 14px; color: #666;">Overall Progress</p>
                </div>
                <table class="stats-table">
                    <tr style="position:sticky; top:-17px; z-index:2;">
                        <th>Name</th>
                        <th>Instances</th>
                        <th>Jobs Done</th>
                        <th>Avg Completion Time</th>
                        <th>Percentage of Share</th>
                    </tr>
                    {% for machine, data in machine_stats.items() %}
                    <tr>
                        <td>{{ machine }}</td>
                        <td>{{ data.instance_count }}</td>
                        <td>{{ data.count }}</td>
                        <td>{{ data.average_time }}</td>
                        <td>{{ data.percentage }}%</td>
                    </tr>
                    {% endfor %}
                </table>
            </div>
        </div>
                <div class="dashboard-grid">
                    <div class="sidebar">
                        <button class="btn" onclick="openModal()">
                            <i class="fas fa-chart-bar"></i> Show Machine Statistics
                        </button>
                        
                        <div class="stats-card">
                            <h3><i class="fas fa-info-circle"></i> Experiment Overview</h3>
                            <div class="experiment-info">
                                <p><strong>ID:</strong> {{ expId }}</p>
                                <p><strong>Total Jobs:</strong> {{ total_jobs }}</p>
                                <p><strong>Avg Time:</strong> {{ avg_completion_time }}</p>
                            </div>
                            
                            <div class="stats-grid">
                                <div class="stat-item">
                                    <div class="stat-icon" style="color: #27ae60;"><i class="fas fa-check-circle"></i></div>
                                    <div class="stat-label">Completed</div>
                                    <div class="stat-value">{{ total_jobs_completed }}</div>
                                </div>
                                <div class="stat-item">
                                    <div class="stat-icon" style="color: #f39c12;"><i class="fas fa-play-circle"></i></div>
                                    <div class="stat-label">Running</div>
                                    <div class="stat-value">{{ total_jobs_served }}</div>
                                </div>
                                <div class="stat-item">
                                    <div class="stat-icon" style="color: #e74c3c;"><i class="fas fa-times-circle"></i></div>
                                    <div class="stat-label">Aborted</div>
                                    <div class="stat-value">{{ total_jobs_aborted }}</div>
                                </div>
                                <div class="stat-item">
                                    <div class="stat-icon" style="color: #95a5a6;"><i class="fas fa-pause-circle"></i></div>
                                    <div class="stat-label">Pending</div>
                                    <div class="stat-value">{{ total_jobs - total_jobs_completed - total_jobs_served - total_jobs_aborted }}</div>
                                </div>
                            </div>
                            
                            <div class="progress-container">
                                <h4>Job Completion Progress</h4>
                                <div class="progress-bar">
                                    <div class="progress-fill" style="width: {{ (total_jobs_completed / total_jobs * 100) if total_jobs > 0 else 0 }}%;"></div>
                                </div>
                                <div class="progress-text">
                                    <span>{{ "%.1f"|format((total_jobs_completed / total_jobs * 100) if total_jobs > 0 else 0) }}% Complete</span>
                                    <span>{{ total_jobs - total_jobs_completed }} Remaining</span>
                                </div>
                            </div>
                            
                            <div style="border-top: 1px solid #e0e0e0; padding-top: 16px; margin-top: 16px; background: #f9f9f9; padding: 12px; border-radius: 6px;">
                                <strong style="color: #333;">Interval Total:</strong> <span id="totalJobs" style="color: #2196F3; font-weight: bold;">0</span>
                            </div>
                            
                            <div class="form-group">
                                <label class="form-label" for="machineFilter">Select Machine:</label>
                                <select class="form-select" id="machineFilter" onchange="updateChart()">
                                    <option value="all">All Machines</option>
                                    {% for machine in machine_names %}
                                        <option value="{{ machine }}">{{ machine }}</option>
                                    {% endfor %}
                                </select>
                            </div>
                            
                            <div class="form-group">
                                <label class="form-label" for="timeInterval">Select Interval:</label>
                                <select class="form-select" id="timeInterval" onchange="updateChart()">
                                    <option value="hourly">Hourly</option>
                                    <option value="minutely">Minutely</option>
                                    <option value="daily">Daily</option>
                                </select>
                            </div>
                        </div>
                        
                        <div class="stats-card">
                            <h3><i class="fas fa-server"></i> API Request Statistics</h3>
                            <div class="api-stats">
                                {% for stat in api_stats %}
                                <div class="api-stat-item">
                                    <div>
                                        <div class="api-endpoint">{{ stat.endpoint }}</div>
                                        <div class="api-method">{{ stat.method }}</div>
                                    </div>
                                    <div class="api-count">{{ stat.request_count }}</div>
                                </div>
                                {% endfor %}
                                {% if not api_stats %}
                                <div style="text-align: center; color: #64748b; font-size: 0.875rem; padding: 20px;">
                                    No API requests tracked yet
                                </div>
                                {% endif %}
                                {% if api_stats %}
                                <div class="api-stat-item" style="border-top: 2px solid #e2e8f0; margin-top: 10px; padding-top: 10px;">
                                    <div>
                                        <div class="api-endpoint" style="font-weight: bold; color: #1e293b;">Total Requests</div>
                                        <div class="api-method" style="color: #64748b;">All Endpoints</div>
                                    </div>
                                    <div class="api-count" style="background: #3b82f6; color: white; font-weight: bold;">{{ total_api_requests }}</div>
                                </div>
                                {% endif %}
                            </div>
                        </div>
                    </div>
                    
                    <div class="chart-container">
                        <div class="chart-wrapper">
                            <canvas id="jobChart"></canvas>
                        </div>
                        
                        <!-- Tab Buttons -->
                        <div class="tabs">
                            <button class="tab-button active" onclick="openTab(event, 'SERVED')">
                                <i class="fas fa-play-circle"></i>
                                SERVED
                                <span class="tab-count">{{ total_jobs_served }}</span>
                            </button>
                            <button class="tab-button" onclick="openTab(event, 'DONE')">
                                <i class="fas fa-check-circle"></i>
                                DONE
                                <span class="tab-count">{{ total_jobs_completed }}</span>
                            </button>
                            <button class="tab-button" onclick="openTab(event, 'ABORTED')">
                                <i class="fas fa-times-circle"></i>
                                ABORTED
                                <span class="tab-count">{{ total_jobs_aborted }}</span>
                            </button>
                            <button class="tab-button" onclick="openTab(event, 'PENDING')">
                                <i class="fas fa-pause-circle"></i>
                                PENDING
                                <span class="tab-count">{{ job_counts.get('PENDING', 0) }}</span>
                            </button>
                        </div>
                        
                        <!-- Tab Contents -->
                        {% for status in ['SERVED', 'DONE', 'ABORTED', 'PENDING'] %}
                            <div id="{{ status }}" class="tabcontent {% if status == 'SERVED' %}active{% endif %}">
                                <div class="table-container">
                                    <!-- Table Header with Search -->
                                    <div class="table-header">
                                        <div class="table-title">
                                            <h3>{{ status }} Jobs</h3>
                                            <span id="paginationInfo-{{ status }}" class="pagination-info">Loading...</span>
                                        </div>
                                        <div class="search-section">
                                            <div class="search-input-group">
                                                <input type="text" id="jobSearch-{{ status }}" placeholder="Search by Job ID..." class="search-input">
                                                <button onclick="searchJobs('{{ status }}')" class="search-btn" title="Search">
                                                    <i class="fas fa-search"></i>
                                                </button>
                                                <button onclick="clearSearch('{{ status }}')" class="clear-btn" title="Clear">
                                                    <i class="fas fa-times"></i>
                                                </button>
                                            </div>
                                        </div>
                                    </div>
                                    
                                    <div class="table-wrapper">
                                        <table class="myTable" id="table-{{ status }}">
                                            <thead>
                                                <tr>
                                                    <th>ID <span class="sort-icon" onclick="sortTable(this, 0, 'number')">↕</span></th>
                                                    <th>Machine <span class="sort-icon" onclick="sortTable(this, 1, 'string')">↕</span></th>
                                                    <th>Requested At <span class="sort-icon" onclick="sortTable(this, 2, 'string')">↕</span></th>
                                                    <th>Started At <span class="sort-icon" onclick="sortTable(this, 3, 'string')">↕</span></th>
                                                    <th>Completed At <span class="sort-icon" onclick="sortTable(this, 4, 'string')">↕</span></th>
                                                    <th>Duration <span class="sort-icon" onclick="sortTable(this, 5, 'string')">↕</span></th>
                                                    <th>Job History <span class="sort-icon" onclick="sortTable(this, 6, 'string')">↕</span></th>
                                                </tr>
                                            </thead>
                                            <tbody id="tbody-{{ status }}">
                                                <tr>
                                                    <td colspan="7" class="loading-message">
                                                        <i class="fas fa-spinner fa-spin"></i> Loading jobs...
                                                    </td>
                                                </tr>
                                            </tbody>
                                        </table>
                                    </div>
                                    
                                    <!-- Pagination Controls -->
                                    <div class="pagination-controls">
                                        <button id="prev-{{ status }}" onclick="changePage('{{ status }}', -1)" class="pagination-btn" disabled>
                                            <i class="fas fa-chevron-left"></i> Previous
                                        </button>
                                        <span id="page-info-{{ status }}" class="page-info">Page 1</span>
                                        <button id="next-{{ status }}" onclick="changePage('{{ status }}', 1)" class="pagination-btn">
                                            <i class="fas fa-chevron-right"></i> Next
                                        </button>
                                    </div>
                                </div>
                            </div>
                        {% endfor %}
                        

                    </div>
                </div>
        
        <script>
            let chart;
            function updateChart() {
                let interval = document.getElementById("timeInterval").value;
                let machine = document.getElementById("machineFilter").value;
                fetch(`/job_stats?interval=` + interval + `&machine=` + machine)
                    .then(response => response.json())
                    .then(data => {
                        if (chart) { chart.destroy(); }
                        
                        // Convert timestamps to local time for chart labels
                        let formattedLabels = data.labels;
                        if (data.timestamps) {
                            formattedLabels = data.labels.map(timestamp => {
                                if (typeof timestamp === 'number') {
                                    const date = new Date(timestamp * 1000);
                                    if (interval === 'minutely') {
                                        return date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
                                    } else if (interval === 'hourly') {
                                        return date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
                                    } else {
                                        return date.toLocaleDateString([], {month: 'short', day: 'numeric'});
                                    }
                                }
                                return timestamp;
                            });
                        }
                        
                        let ctx = document.getElementById("jobChart").getContext("2d");
                        chart = new Chart(ctx, {
                            type: "bar",
                            data: {
                                labels: formattedLabels,
                                datasets: [{
                                    label: "Jobs Completed",
                                    data: data.values,
                                    backgroundColor: "#3498db",
                                    borderColor: "#2980b9",
                                    borderWidth: 1
                                }]
                            },
                            options: {
                                responsive: true,
                                maintainAspectRatio: false,
                                scales: { 
                                    y: { beginAtZero: true } 
                                },
                                plugins: {
                                    legend: {
                                        display: true,
                                        position: 'top'
                                    }
                                }
                            }
                        });
                        // Update the total jobs count in the HTML element with id "totalJobs"
                        document.getElementById("totalJobs").innerText = data.total_jobs;
                    });
            }
            updateChart();
        </script>
        
        <!-- Modal -->
        <div id="messageModal" class="modal">
            <div class="modal-content">
                <span class="close" onclick="closeMessageModal()">&times;</span>
                <h2>Job History</h2>

                <div style="display: flex; align-items: center; justify-content: space-between;">
                    <p><strong>Job ID:</strong> <span id="jobId"></span></p>
                    <button id="toggleParamsBtn" onclick="toggleParams()" style="cursor: pointer; border: none; background: none; font-weight: bold;">&#9654; View Parameters</button>
                </div>

                <!-- Status Change Section -->
                <div id="statusChangeSection" style="margin: 15px 0; padding: 15px; background: #f8f9fa; border-radius: 8px; border: 1px solid #dee2e6;">
                    <h4 id="statusChangeTitle" style="margin: 0 0 10px 0; color: #495057;">Change Job Status</h4>
                    <div style="display: flex; gap: 10px; align-items: center;">
                        <input type="text" id="statusChangeReason" placeholder="Reason (optional)" style="flex: 1; padding: 8px; border: 1px solid #ced4da; border-radius: 4px;">
                        <input type="password" id="statusChangePin" placeholder="PIN" style="padding: 8px; border: 1px solid #ced4da; border-radius: 4px; width: 80px;" maxlength="4" pattern="[0-9]{4}">
                        <button id="changeStatusBtn" onclick="changeJobStatus()" style="padding: 8px 16px; background: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; white-space: nowrap;">Change Status</button>
                    </div>
                </div>

                <div id="parametersTable" class="hidden" style="margin-bottom: 15px;"></div>

                <div class="timeline" id="messageTimeline"></div>
            </div>
        </div>



            </div> <!-- End main-content -->
        </div> <!-- End container -->
        
        <script>
            // Function to convert Unix timestamp to local time
            function formatTimestamp(timestamp) {
                if (!timestamp || timestamp === 'N/A') return 'N/A';
                const date = new Date(timestamp * 1000); // Convert Unix timestamp to milliseconds
                return date.toLocaleString(); // Use browser's local timezone
            }
            
            // Function to convert all timestamps in the table to local time
            function convertTimestampsToLocal() {
                const timestampCells = document.querySelectorAll('[data-timestamp]');
                timestampCells.forEach(cell => {
                    const timestamp = cell.getAttribute('data-timestamp');
                    if (timestamp && timestamp !== 'N/A') {
                        cell.textContent = formatTimestamp(parseFloat(timestamp));
                    }
                });
            }
            
            // Function to handle watermark visibility
            function handleTableWatermarks() {
                const tables = document.querySelectorAll('.myTable');
                tables.forEach(table => {
                    const tableWrapper = table.closest('.table-wrapper');
                    const tbody = table.querySelector('tbody');
                    
                    if (tbody) {
                        const visibleRows = tbody.querySelectorAll('tr:not(.dtr-hidden)');
                        
                        // Check if watermark exists, if not create it
                        let watermark = tableWrapper.querySelector('.table-watermark');
                        if (!watermark) {
                            watermark = document.createElement('div');
                            watermark.className = 'table-watermark';
                            watermark.innerHTML = '<i class="fas fa-database"></i><div class="watermark-text">No Data Available</div>';
                            tableWrapper.appendChild(watermark);
                        }
                        
                        if (visibleRows.length === 0) {
                            watermark.style.display = 'block';
                        } else {
                            watermark.style.display = 'none';
                        }
                    }
                });
            }
            
           $(document).ready(function () {
                // Convert timestamps to local time
                convertTimestampsToLocal();
                
                // Handle watermark visibility
                handleTableWatermarks();
            });
        </script>
    </body>
    </html>
    """

    return render_template_string(
        html_template,
        expId=expId,
        total_jobs=total_jobs,
        total_jobs_served=total_jobs_served,
        total_jobs_completed=total_jobs_completed,
        total_jobs_aborted=total_jobs_aborted,
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
    parser = argparse.ArgumentParser(
        description="Start the Flask Dashboard server")
    parser.add_argument("--host", default="0.0.0.0",
                        help="IP address to bind to")
    parser.add_argument("--jobDB", default="jobs.db",
                        help="SQLite database file (<filename>.db) placed in the same directory as server.py")
    parser.add_argument("--enableNgrok", action="store_true",
                        help="Enable ngrok for external access")
    parser.add_argument("--port", type=int, default=5050,
                        help="Port number to listen on")
    parser.add_argument("--expId", type=str, default="sim1",
                        help="Give an unique name")
    args = parser.parse_args()
    createExpBaseDirectory(args)
    setup_log(args)
    logging.info(
        f"Starting Flask Dashboard server on {args.host}:{args.port}...")
    DB_FILE = os.path.join(BASE_DIR, args.expId, args.jobDB)
    EXP_ID = args.expId

    # Initialize database connection
    db = JobDatabase(DB_FILE)

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
                print(f" >> dashboard : {public_url}")
                logging.info(f"ngrok tunnel established at {public_url}")
            except Exception as e:
                logging.error(f"Failed to start ngrok: {e}")

    # Start the Flask app
    app.run(host=args.host, port=args.port)

# python dashboard.py --expId=sim1 --jobDB=jobs.db --host=0.0.0.0 --port=5050
