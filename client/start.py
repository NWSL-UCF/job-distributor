import atexit
import json
import logging
import os
import platform
import signal
import socket
import subprocess
import sys

# Check OS
IS_WINDOWS = platform.system() == "Windows"

# ---------------- Load Config ----------------
CONFIG_PATH = "config.json"
with open(CONFIG_PATH, "r") as f:
    config = json.load(f)

exp_id = config["expId"]
job_server = config["job_server"]
port = config["port"]
# htc always prefers on process per machine
num_processes = config["number_of_parallel_process"] if config["machine_type"] != "htc" else 1


exp_dir = os.path.join(exp_id)
os.makedirs(exp_dir, exist_ok=True)

# ---------------- Logger Setup ----------------
user = os.getenv("USERNAME") if IS_WINDOWS else os.getenv(
    "USER") or "unknown_user"
host = socket.gethostname()
log_filename = f"start_{user}@{host}.log"
log_path = os.path.join(exp_dir, log_filename)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_path),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ---------------- Global Process List ----------------
processes = []

# ---------------- Cleanup Handler ----------------


def cleanup():
    logger.info("Cleaning up all child processes...")
    for proc in processes:
        try:
            if IS_WINDOWS:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception as e:
            logger.warning(f"Failed to kill process {proc.pid}: {e}")


atexit.register(cleanup)

# ---------------- Signal Handler ----------------


def signal_handler(sig, frame):
    logger.info(f"Received signal {sig}. Exiting...")
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ---------------- Launch Runners ----------------
logger.info(f"Starting {num_processes} runners for experiment '{exp_id}'")

for i in range(1, num_processes + 1):
    cmd = [
        "python", "runner.py",
        "--process_id", str(i)
    ]

    logger.info(f"Launching runner {i}: {' '.join(cmd)}")

    if IS_WINDOWS:
        proc = subprocess.Popen(
            cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        proc = subprocess.Popen(cmd, preexec_fn=os.setsid)

    processes.append(proc)

# ---------------- Wait for Completion ----------------
for i, proc in enumerate(processes, start=1):
    proc.wait()
    logger.info(f"Runner {i} completed.")
