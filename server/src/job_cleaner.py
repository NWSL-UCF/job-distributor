import time
import os
import json
import logging
import argparse
from database import JobDatabase
from contextual_bandit import create_bandit

# ---------------- Constants ----------------
BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DB_FILE = ""
LOG_FILENAME = "job_cleaner.log"

STATUS_PENDING = "PENDING"
STATUS_SERVED = "SERVED"
STATUS_DONE = "DONE"
STATUS_ABORTED = "ABORTED"

ABORTED_JOB_RESET_TIMEOUT = 30 * 60 # ideal time out for aborted jobs
IDLE_TIMEOUT = 60
POLLING_INTERVAL = 60  # Default polling interval
PUNISHMENT_RUNTIME = 1000000.0  # Large punishment value for failed jobs (in seconds)

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
# Database operations are now handled by the JobDatabase class

# ---------------- Main Cleanup Loop ----------------

def cleanup_loop(db, bandit=None):
    last_aborted_reset_time = 0
    last_idle_check_time = 0

    while True:
        now = time.time()
        jobs_updated = False

        if now - last_aborted_reset_time >= ABORTED_JOB_RESET_TIMEOUT:
            logging.info("Running aborted job cleanup...")
            
            # Get aborted jobs before resetting (to get system_metrics and parameters)
            aborted_jobs = db.get_jobs_by_status(STATUS_ABORTED)
            
            # Punish the bandit model for failed jobs
            if bandit and aborted_jobs:
                punished_count = 0
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
            
            count = db.reset_aborted_jobs()
            if count > 0:
                logging.info(f"Reset {count} ABORTED jobs to PENDING.")
                jobs_updated = True
            last_aborted_reset_time = now

        if now - last_idle_check_time >= IDLE_TIMEOUT:
            logging.info("Running stale SERVED job timeout...")
            count = db.reset_stale_served_jobs(IDLE_TIMEOUT)
            if count > 0:
                logging.info(f"Reset {count} SERVED jobs due to no ping.")
                jobs_updated = True
            last_idle_check_time = now

        if not jobs_updated:
            logging.info("No updates made in this cycle.")

        time.sleep(POLLING_INTERVAL)

# ---------------- Entry Point ----------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Start job_cleaner background service")
    parser.add_argument("--jobDB", default="jobs.db", help="SQLite database file (<filename>.db) placed in the same directory as server.py")
    parser.add_argument("--expId", type=str, default="sim1", help="Give a unique name of your experiment")
    parser.add_argument("--abortedJobResetTimeout", type=int, default=1800, help="How often to reset aborted jobs (in seconds)")
    parser.add_argument("--idleTimeout", type=int, default=60, help="Max silence period for SERVED jobs (in seconds)")
    parser.add_argument("--pollingInterval", type=int, default=60, help="How often to poll for cleanup (in seconds)")
    args = parser.parse_args()

    createExpBaseDirectory(args)
    setup_log(args)

    DB_FILE = os.path.join(BASE_DIR, args.expId, args.jobDB)
    ABORTED_JOB_RESET_TIMEOUT = args.abortedJobResetTimeout
    IDLE_TIMEOUT = args.idleTimeout
    POLLING_INTERVAL = args.pollingInterval

    # Initialize database connection
    db = JobDatabase(DB_FILE)

    # Initialize contextual bandit from config (for punishment mechanism)
    bandit = None
    try:
        config_path = os.path.join(BASE_DIR, "config.json")
        with open(config_path, 'r') as f:
            config = json.load(f)
        
        bandit_algorithm = config.get("bandit_algorithm", "linear")
        config_parameters = config.get("parameters", {})
        
        if config_parameters:
            # Use same shared state file as server.py for multi-process safety
            state_file_path = os.path.join(BASE_DIR, args.expId, "bandit_model_state.pkl")
            bandit = create_bandit(bandit_algorithm, config_parameters, state_file_path=state_file_path)
            logging.info(f"Initialized contextual bandit for punishment: {bandit_algorithm} with state file: {state_file_path}")
        else:
            logging.warning("No parameters found in config, bandit not initialized for punishment")
    except Exception as e:
        logging.error(f"Failed to initialize contextual bandit for punishment: {e}")

    logging.info("Job cleaner started.")
    cleanup_loop(db, bandit)
