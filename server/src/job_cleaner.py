import time
import os
import logging
import argparse
import requests

# ---------------- Constants ----------------
BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
LOG_FILENAME = "job_cleaner.log"

ABORTED_JOB_RESET_TIMEOUT = 30 * 60 # ideal time out for aborted jobs
IDLE_TIMEOUT = 60
POLLING_INTERVAL = 60  # Default polling interval

# ---------------- Setup ----------------

def createExpBaseDirectory(args):
    os.makedirs(os.path.join(BASE_DIR, args.expId), exist_ok=True)

def setup_log(args):
    LOG_FILE = os.path.join(BASE_DIR, args.expId, LOG_FILENAME)
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

# ---------------- Cleanup Logic ----------------
# All database and model operations are now handled via API calls to the server

# ---------------- Main Cleanup Loop ----------------

def cleanup_loop(server_url, server_port):
    last_aborted_reset_time = 0
    last_idle_check_time = 0
    
    # Track pending operations to prevent duplicate requests
    pending_aborted_operation_id = None
    pending_stale_operation_id = None
    
    base_url = f"http://{server_url}:{server_port}"

    while True:
        now = time.time()
        jobs_updated = False

        # Check if it's time to reset aborted jobs
        if now - last_aborted_reset_time >= ABORTED_JOB_RESET_TIMEOUT:
            # Check if we have a pending operation
            if pending_aborted_operation_id:
                # Check status of pending operation
                try:
                    response = requests.get(
                        f"{base_url}/cleanup/reset_aborted_jobs/{pending_aborted_operation_id}",
                        timeout=10
                    )
                    
                    if response.status_code == 200:
                        result = response.json()
                        if result.get("status") == "completed":
                            count = result.get("jobs_reset", 0)
                            punished_count = result.get("bandit_punishments", 0)
                            if count > 0:
                                logging.info(f"Reset {count} ABORTED jobs to PENDING via API (punished {punished_count} in bandit).")
                                jobs_updated = True
                            else:
                                logging.info("No aborted jobs to reset.")
                            pending_aborted_operation_id = None
                            last_aborted_reset_time = now
                        elif result.get("status") == "processing":
                            logging.debug(f"Reset aborted jobs operation {pending_aborted_operation_id} still processing...")
                        elif result.get("status") == "error":
                            logging.error(f"Reset aborted jobs operation failed: {result.get('error')}")
                            pending_aborted_operation_id = None
                            last_aborted_reset_time = now
                    elif response.status_code == 202:
                        # Still processing
                        logging.debug(f"Reset aborted jobs operation {pending_aborted_operation_id} still processing...")
                    else:
                        logging.warning(f"Unexpected status checking aborted jobs operation: {response.status_code}")
                        pending_aborted_operation_id = None
                except requests.exceptions.RequestException as e:
                    logging.error(f"Error checking reset_aborted_jobs status: {e}")
            else:
                # No pending operation, make a new request
                logging.info("Requesting aborted job cleanup via API...")
                try:
                    response = requests.post(
                        f"{base_url}/cleanup/reset_aborted_jobs",
                        json={},
                        timeout=10
                    )
                    
                    if response.status_code == 202:
                        result = response.json()
                        operation_id = result.get("operation_id")
                        if operation_id:
                            pending_aborted_operation_id = operation_id
                            logging.info(f"Queued reset_aborted_jobs operation {operation_id}")
                        else:
                            # Server says operation already in progress
                            logging.info("Reset aborted jobs operation already in progress on server")
                    else:
                        logging.error(f"Failed to queue reset aborted jobs. Status: {response.status_code}, Response: {response.text}")
                except requests.exceptions.RequestException as e:
                    logging.error(f"Error calling reset_aborted_jobs API: {e}")

        # Check if it's time to reset stale served jobs
        if now - last_idle_check_time >= IDLE_TIMEOUT:
            # Check if we have a pending operation
            if pending_stale_operation_id:
                # Check status of pending operation
                try:
                    response = requests.get(
                        f"{base_url}/cleanup/reset_stale_served_jobs/{pending_stale_operation_id}",
                        timeout=10
                    )
                    
                    if response.status_code == 200:
                        result = response.json()
                        if result.get("status") == "completed":
                            count = result.get("jobs_reset", 0)
                            if count > 0:
                                logging.info(f"Reset {count} SERVED jobs due to no ping via API.")
                                jobs_updated = True
                            else:
                                logging.info("No stale served jobs to reset.")
                            pending_stale_operation_id = None
                            last_idle_check_time = now
                        elif result.get("status") == "processing":
                            logging.debug(f"Reset stale served jobs operation {pending_stale_operation_id} still processing...")
                        elif result.get("status") == "error":
                            logging.error(f"Reset stale served jobs operation failed: {result.get('error')}")
                            pending_stale_operation_id = None
                            last_idle_check_time = now
                    elif response.status_code == 202:
                        # Still processing
                        logging.debug(f"Reset stale served jobs operation {pending_stale_operation_id} still processing...")
                    else:
                        logging.warning(f"Unexpected status checking stale served jobs operation: {response.status_code}")
                        pending_stale_operation_id = None
                except requests.exceptions.RequestException as e:
                    logging.error(f"Error checking reset_stale_served_jobs status: {e}")
            else:
                # No pending operation, make a new request
                logging.info("Requesting stale SERVED job cleanup via API...")
                try:
                    response = requests.post(
                        f"{base_url}/cleanup/reset_stale_served_jobs",
                        json={"idle_timeout": IDLE_TIMEOUT},
                        timeout=10
                    )
                    
                    if response.status_code == 202:
                        result = response.json()
                        operation_id = result.get("operation_id")
                        if operation_id:
                            pending_stale_operation_id = operation_id
                            logging.info(f"Queued reset_stale_served_jobs operation {operation_id}")
                        else:
                            # Server says operation already in progress
                            logging.info("Reset stale served jobs operation already in progress on server")
                    else:
                        logging.error(f"Failed to queue reset stale served jobs. Status: {response.status_code}, Response: {response.text}")
                except requests.exceptions.RequestException as e:
                    logging.error(f"Error calling reset_stale_served_jobs API: {e}")

        if not jobs_updated and not pending_aborted_operation_id and not pending_stale_operation_id:
            logging.info("No updates made in this cycle.")

        time.sleep(POLLING_INTERVAL)

# ---------------- Entry Point ----------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Start job_cleaner background service")
    parser.add_argument("--expId", type=str, default="sim1", help="Give a unique name of your experiment")
    parser.add_argument("--serverUrl", type=str, default="localhost", help="Server URL or hostname")
    parser.add_argument("--serverPort", type=int, default=5000, help="Server port number")
    parser.add_argument("--abortedJobResetTimeout", type=int, default=1800, help="How often to reset aborted jobs (in seconds)")
    parser.add_argument("--idleTimeout", type=int, default=60, help="Max silence period for SERVED jobs (in seconds)")
    parser.add_argument("--pollingInterval", type=int, default=60, help="How often to poll for cleanup (in seconds)")
    args = parser.parse_args()

    createExpBaseDirectory(args)
    setup_log(args)

    ABORTED_JOB_RESET_TIMEOUT = args.abortedJobResetTimeout
    IDLE_TIMEOUT = args.idleTimeout
    POLLING_INTERVAL = args.pollingInterval

    logging.info(f"Job cleaner started. Connecting to server at {args.serverUrl}:{args.serverPort}")
    logging.info(f"Configuration: abortedJobResetTimeout={ABORTED_JOB_RESET_TIMEOUT}s, "
                 f"idleTimeout={IDLE_TIMEOUT}s, pollingInterval={POLLING_INTERVAL}s")
    
    cleanup_loop(args.serverUrl, args.serverPort)
