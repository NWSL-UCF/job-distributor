import time
import os
import sys
import logging
import argparse
import requests

from database import JobDatabase
from workspace_layout import ensure_exp_layout, exp_meta_dir, jobs_db_path

LOG_FILENAME = "job_cleaner.log"
POLLING_INTERVAL = 60  # seconds between each loop tick

DEFAULT_IDLE_TIMEOUT = 600
DEFAULT_ABORTED_RESET_TIMEOUT = 1200


def setup_log(workspace_path, exp_id):
    LOG_FILE = os.path.join(exp_meta_dir(workspace_path, exp_id), LOG_FILENAME)
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )


def reset_aborted_jobs(base_url):
    try:
        response = requests.post(
            f"{base_url}/cleanup/reset_aborted_jobs",
            json={},
            timeout=10
        )
        if response.status_code == 200:
            count = response.json().get("jobs_reset", 0)
            if count > 0:
                logging.info(f"Reset {count} ABORTED jobs to PENDING.")
            else:
                logging.info("No aborted jobs to reset.")
        else:
            logging.error(f"reset_aborted_jobs failed. Status: {response.status_code}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Error calling reset_aborted_jobs: {e}")


def reset_stale_served_jobs(base_url, idle_timeout):
    try:
        response = requests.post(
            f"{base_url}/cleanup/reset_stale_served_jobs",
            json={"idle_timeout": idle_timeout},
            timeout=10
        )
        if response.status_code == 200:
            count = response.json().get("jobs_reset", 0)
            if count > 0:
                logging.info(f"Reset {count} stale SERVED jobs (timeout: {idle_timeout}s).")
            else:
                logging.info("No stale SERVED jobs to reset.")
        else:
            logging.error(f"reset_stale_served_jobs failed. Status: {response.status_code}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Error calling reset_stale_served_jobs: {e}")


WORKER_LIFECYCLE_SYNC_INTERVAL = 60  # seconds between worker reconcile+stale passes


def cleanup_loop(server_url, server_port, db):
    # Give the job server a moment to finish binding its port before the
    # first cleanup request goes out.
    time.sleep(5)

    last_aborted_reset_time = 0
    last_idle_check_time = 0
    last_worker_sync_time = 0

    base_url = f"http://{server_url}:{server_port}"
    logging.info(f"Job cleaner connected to {base_url}")

    while True:
        now = time.time()

        # Re-read settings from DB on every tick so dashboard changes take effect immediately
        idle_timeout = int(db.get_config_value("idle_timeout", str(DEFAULT_IDLE_TIMEOUT)))
        aborted_reset_timeout = int(
            db.get_config_value("aborted_job_reset_timeout", str(DEFAULT_ABORTED_RESET_TIMEOUT))
        )

        # ── Worker lifecycle reconcile (replaces per-heartbeat full scan) ──────
        if now - last_worker_sync_time >= WORKER_LIFECYCLE_SYNC_INTERVAL:
            try:
                db.sync_worker_lifecycle()
                logging.debug("Worker lifecycle sync complete.")
            except Exception as exc:
                logging.error(f"Worker lifecycle sync failed: {exc}")
            last_worker_sync_time = now

        if now - last_aborted_reset_time >= aborted_reset_timeout:
            logging.info(f"Running aborted job cleanup (timeout={aborted_reset_timeout}s)...")
            reset_aborted_jobs(base_url)
            last_aborted_reset_time = now

        if now - last_idle_check_time >= idle_timeout:
            logging.info(f"Running stale SERVED job cleanup (idle_timeout={idle_timeout}s)...")
            reset_stale_served_jobs(base_url, idle_timeout)
            last_idle_check_time = now

        time.sleep(POLLING_INTERVAL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Start job_cleaner background service")
    parser.add_argument("--expId", type=str, required=True,
                        help="Unique experiment name")
    parser.add_argument("--workspacePath",
                        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."),
                        help="Directory where experiment data lives")
    parser.add_argument("--serverPort", type=int, default=5000,
                        help="Job server port (default: 5000)")
    parser.add_argument("--pollingInterval", type=int, default=60,
                        help="Seconds between loop ticks (default: 60)")
    args = parser.parse_args()

    POLLING_INTERVAL = args.pollingInterval

    ensure_exp_layout(args.workspacePath, args.expId)
    setup_log(args.workspacePath, args.expId)

    db_path = jobs_db_path(args.workspacePath, args.expId)
    db = JobDatabase(db_path)

    logging.info(f"Job cleaner started. Server: localhost:{args.serverPort}, DB: {db_path}")
    cleanup_loop("localhost", args.serverPort, db)
