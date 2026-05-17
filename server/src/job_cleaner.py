import time
import os
import logging
import argparse
import requests

# ---------------- Constants ----------------
BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
LOG_FILENAME = "job_cleaner.log"

ABORTED_JOB_RESET_TIMEOUT = 30 * 60  # seconds between aborted-job resets
IDLE_TIMEOUT = 60                     # seconds of silence before a SERVED job is considered stale
POLLING_INTERVAL = 60                 # how often the cleaner loop runs

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

def reset_aborted_jobs(base_url):
    try:
        response = requests.post(
            f"{base_url}/cleanup/reset_aborted_jobs",
            json={},
            timeout=10
        )
        if response.status_code == 200:
            result = response.json()
            count = result.get("jobs_reset", 0)
            if count > 0:
                logging.info(f"Reset {count} ABORTED jobs to PENDING.")
            else:
                logging.info("No aborted jobs to reset.")
        else:
            logging.error(f"Failed to reset aborted jobs. Status: {response.status_code}, Response: {response.text}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Error calling reset_aborted_jobs API: {e}")


def reset_stale_served_jobs(base_url, idle_timeout):
    try:
        response = requests.post(
            f"{base_url}/cleanup/reset_stale_served_jobs",
            json={"idle_timeout": idle_timeout},
            timeout=10
        )
        if response.status_code == 200:
            result = response.json()
            count = result.get("jobs_reset", 0)
            if count > 0:
                logging.info(f"Reset {count} stale SERVED jobs to PENDING (timeout: {idle_timeout}s).")
            else:
                logging.info("No stale SERVED jobs to reset.")
        else:
            logging.error(f"Failed to reset stale served jobs. Status: {response.status_code}, Response: {response.text}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Error calling reset_stale_served_jobs API: {e}")

# ---------------- Main Cleanup Loop ----------------

def cleanup_loop(server_url, server_port):
    last_aborted_reset_time = 0
    last_idle_check_time = 0

    base_url = f"http://{server_url}:{server_port}"

    while True:
        now = time.time()

        if now - last_aborted_reset_time >= ABORTED_JOB_RESET_TIMEOUT:
            logging.info("Running aborted job cleanup...")
            reset_aborted_jobs(base_url)
            last_aborted_reset_time = now

        if now - last_idle_check_time >= IDLE_TIMEOUT:
            logging.info("Running stale SERVED job cleanup...")
            reset_stale_served_jobs(base_url, IDLE_TIMEOUT)
            last_idle_check_time = now

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
