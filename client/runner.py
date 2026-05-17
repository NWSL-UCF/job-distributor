import argparse
import json
import logging
import os
import platform
import random
import signal
import socket
import subprocess
import sys
import threading
import time

import requests
import psutil

# Check OS
IS_WINDOWS = platform.system() == "Windows"

# --------------- Read Config -------------------
CONFIG_PATH = "config.json"
with open(CONFIG_PATH, "r") as f:
    config = json.load(f)

expId = config["expId"]
job_server = config["job_server"]
port = config["port"]
run_command = config["run_command"]  # list, e.g., ["python", "main.py"]
machine_type = config["machine_type"]
heartBitInterval = config["heartBitInterval"] - 0.3
# seconds, added 300 ms to avoid exact timing issues which will tolerate network latency

# --------------- Argument Parser ----------------
parser = argparse.ArgumentParser()
parser.add_argument("--process_id", type=int, default=0,
                    help="Give a process id for log tracking")
args = parser.parse_args()

# --------------- Logger Setup ----------------
LOG_DIR = f"{expId}/logs"
os.makedirs(LOG_DIR, exist_ok=True)

username = os.getenv('USERNAME') or os.getenv('USER') or "user"
# Add random number to ensure uniqueness even if same machine/process_id
# Use current timestamp as seed for random number generation
random.seed(int(time.time() * 1000))  # Use milliseconds for better uniqueness
random_suffix = random.randint(10000, 99999)
runner_id = f"{username}@{socket.gethostname()}({machine_type})_{args.process_id}_{random_suffix}"
log_path = os.path.join(LOG_DIR, f"runner_{runner_id}.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_path),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --------------- Constants ----------------
# Use the job_server URL as-is if it already contains an explicit port,
# otherwise append the configured port.
from urllib.parse import urlparse as _urlparse
base_url = job_server if _urlparse(job_server).port else f"{job_server}:{port}"

REQUEST_JOB_URL = f"{base_url}/request_job"
UPDATE_JOB_URL = f"{base_url}/update_job_status"
PING_URL = f"{base_url}/ping"

# Track the current child process
current_proc = None

# --------------- System Metrics Collection ----------------

def get_system_metrics():
    try:
        # CPU metrics
        cpu_percent = psutil.cpu_percent(interval=0.1)
        cpu_count_physical = psutil.cpu_count(logical=False)
        cpu_count_logical = psutil.cpu_count(logical=True)
        
        # CPU frequency
        try:
            cpu_freq = psutil.cpu_freq()
            cpu_freq_mhz = cpu_freq.current if cpu_freq else 0
        except (AttributeError, RuntimeError):
            # Fallback for systems where CPU frequency is not available
            cpu_freq_mhz = 0
        
        # Memory metrics
        memory = psutil.virtual_memory()
        ram_total_gb = memory.total / (1024 ** 3)  # Convert bytes to GB
        ram_available_gb = memory.available / (1024 ** 3)
        ram_util_percent = memory.percent
        
        # System load averages (Unix/Linux only)
        if not IS_WINDOWS:
            try:
                load_avg = os.getloadavg()
                load_1min = load_avg[0]
                load_5min = load_avg[1]
                load_15min = load_avg[2]
                load_per_cpu = load_1min / cpu_count_logical if cpu_count_logical > 0 else 0
            except (AttributeError, OSError):
                # Windows doesn't support os.getloadavg()
                load_1min = 0
                load_5min = 0
                load_15min = 0
                load_per_cpu = 0
        else:
            # On Windows, approximate load using CPU usage
            # This is a rough approximation since Windows doesn't have load averages
            load_1min = cpu_percent / 100.0 * cpu_count_logical
            load_5min = load_1min  # Same approximation
            load_15min = load_1min  # Same approximation
            load_per_cpu = cpu_percent / 100.0
        
        # Calculate idle slots (available CPU capacity)
        # Idle slots = max(0, total_cores - current_load)
        idle_slots = max(0, cpu_count_logical - load_1min) if not IS_WINDOWS else max(0, cpu_count_logical * (1 - cpu_percent / 100.0))
        
        # Disk I/O utilization
        # Note: psutil doesn't directly provide disk I/O utilization percentage
        # We need to sample disk I/O counters over a short interval
        try:
            # First sample
            disk_io_start = psutil.disk_io_counters()
            time.sleep(0.1)  # Sample over 100ms
            # Second sample
            disk_io_end = psutil.disk_io_counters()
            
            if disk_io_start and disk_io_end:
                # Calculate I/O operations per second
                time_delta = 0.1
                read_ops = disk_io_end.read_count - disk_io_start.read_count
                write_ops = disk_io_end.write_count - disk_io_start.write_count
                total_ops = read_ops + write_ops
                ops_per_sec = total_ops / time_delta
                
                # Calculate I/O bytes per second
                read_bytes = disk_io_end.read_bytes - disk_io_start.read_bytes
                write_bytes = disk_io_end.write_bytes - disk_io_start.write_bytes
                total_bytes = read_bytes + write_bytes
                bytes_per_sec = total_bytes / time_delta
                
                # Estimate utilization based on I/O activity
                # This is a heuristic: normalize by a reasonable threshold
                # High-end SSDs can do ~100k IOPS, so we'll use that as a reference
                # For bytes, we'll use ~1GB/s as a reference
                max_iops = 100000  # Reference: high-end SSD
                max_throughput = 1e9  # 1 GB/s reference
                
                iops_util = min(100.0, (ops_per_sec / max_iops) * 100.0)
                throughput_util = min(100.0, (bytes_per_sec / max_throughput) * 100.0)
                
                # Take the maximum of the two as overall disk I/O utilization
                disk_io_util = max(iops_util, throughput_util)
            else:
                disk_io_util = 0.0
        except (AttributeError, RuntimeError, TypeError):
            disk_io_util = 0.0
        
        # Build the metrics dictionary
        metrics = {
            "cpu_util": round(cpu_percent, 1),
            "ram_util": round(ram_util_percent, 1),
            "ram_available": round(ram_available_gb, 15),  # Match precision from image
            "ram_total": round(ram_total_gb, 1),
            "worker_type": machine_type,
            "idle_slots": int(round(idle_slots)),
            "load_1min": round(load_1min, 10),
            "load_5min": round(load_5min, 10),
            "load_15min": round(load_15min, 10),
            "load_per_cpu": round(load_per_cpu, 13),
            "disk_io_util": round(disk_io_util, 2),
            "cpu_cores": cpu_count_physical if cpu_count_physical else cpu_count_logical,
            "cpu_threads": cpu_count_logical,
            "cpu_freq_mhz": int(round(cpu_freq_mhz)) if cpu_freq_mhz > 0 else 0
        }
        
        return metrics
        
    except Exception as e:
        logger.error(f"Error collecting system metrics: {type(e).__name__}: {e}")
        # Return default values on error
        return {
            "cpu_util": 0.0,
            "ram_util": 0.0,
            "ram_available": 0.0,
            "ram_total": 0.0,
            "worker_type": machine_type,
            "idle_slots": 0,
            "load_1min": 0.0,
            "load_5min": 0.0,
            "load_15min": 0.0,
            "load_per_cpu": 0.0,
            "disk_io_util": 0.0,
            "cpu_cores": 0,
            "cpu_threads": 0,
            "cpu_freq_mhz": 0
        }


def get_averaged_system_metrics():
    logger.info("Collecting system metrics (5 samples with 3-second intervals)...")
    
    # Collect five samples
    samples = []
    for i in range(5):
        logger.info(f"Collecting sample {i+1}/5...")
        metrics = get_system_metrics()
        samples.append(metrics)
        
        # Wait 3 seconds before next sample (except after the last one)
        if i < 4:
            time.sleep(3)
    
    # Calculate averages for numeric metrics
    # Metrics that should be averaged
    numeric_keys = [
        "cpu_util", "ram_util", "ram_available", "ram_total",
        "idle_slots", "load_1min", "load_5min", "load_15min",
        "load_per_cpu", "disk_io_util", "cpu_freq_mhz"
    ]
    
    # Metrics that should remain constant (take from first sample)
    constant_keys = ["worker_type", "cpu_cores", "cpu_threads"]
    
    # Initialize averaged metrics with first sample's constant values
    averaged_metrics = {}
    for key in constant_keys:
        if key in samples[0]:
            averaged_metrics[key] = samples[0][key]
    
    # Calculate averages for numeric metrics
    for key in numeric_keys:
        values = [sample.get(key, 0) for sample in samples if key in sample]
        if values:
            avg_value = sum(values) / len(values)
            # Round to match the precision of individual metrics
            if key in ["cpu_util", "ram_util", "ram_total"]:
                averaged_metrics[key] = round(avg_value, 1)
            elif key == "ram_available":
                averaged_metrics[key] = round(avg_value, 15)
            elif key in ["load_1min", "load_5min", "load_15min"]:
                averaged_metrics[key] = round(avg_value, 10)
            elif key == "load_per_cpu":
                averaged_metrics[key] = round(avg_value, 13)
            elif key == "disk_io_util":
                averaged_metrics[key] = round(avg_value, 2)
            elif key == "idle_slots":
                averaged_metrics[key] = int(round(avg_value))
            elif key == "cpu_freq_mhz":
                averaged_metrics[key] = int(round(avg_value))
            else:
                averaged_metrics[key] = avg_value
        else:
            averaged_metrics[key] = 0
    
    logger.info("System metrics collection completed.")
    return averaged_metrics

# --------------- Cleanup Handler ----------------


def cleanup(signum=None, frame=None):
    global current_proc
    if current_proc and current_proc.poll() is None:
        logger.info(f"Terminating subprocess with PID {current_proc.pid}")
        try:
            if IS_WINDOWS:
                current_proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(os.getpgid(current_proc.pid), signal.SIGTERM)
        except Exception as e:
            logger.warning(f"Could not kill subprocess group: {e}")
    logger.info("Runner shutting down.")
    sys.exit(0)


signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

# --------------- Heartbeat Pinger ----------------


def ping_job(job_id, stop_event):
    while not stop_event.is_set():
        try:
            res = requests.post(PING_URL, json={"id": job_id})
            if res.status_code == 200:
                logger.info(f"Ping sent for job {job_id}")
            else:
                logger.warning(
                    f"Ping failed for job {job_id}: HTTP {res.status_code} - {res.text}")
        except Exception as e:
            logger.warning(
                f"Ping exception for job {job_id}: {type(e).__name__}: {e}")
        time.sleep(heartBitInterval)

# --------------- Job Request ----------------

def request_and_get_job(system_metrics):
    """
    Request the next available job from the server.

    Returns:
        tuple: (success: bool, job_info: dict or None, error_message: str or None)
    """
    try:
        logger.info("Requesting a new job from server...")
        response = requests.post(REQUEST_JOB_URL, json={
            "requested_by": runner_id,
            "system_metrics": system_metrics
        }, timeout=30)

        if response.status_code == 404:
            logger.info("No more jobs available.")
            return (False, None, "No available jobs")

        if response.status_code == 200:
            job_info = response.json()
            logger.info(f"Job received: job_id={job_info.get('job_id')}")
            return (True, job_info, None)

        error_msg = f"Failed to request job. Status: {response.status_code}, Msg: {response.text}"
        logger.error(error_msg)
        return (False, None, error_msg)

    except requests.exceptions.RequestException as e:
        error_msg = f"Error requesting job: {type(e).__name__}: {e}"
        logger.error(error_msg)
        return (False, None, error_msg)
    except Exception as e:
        error_msg = f"Unexpected error in request_and_get_job: {type(e).__name__}: {e}"
        logger.error(error_msg)
        return (False, None, error_msg)

# --------------- Job Status Update ----------------


def update_status(job_id, status, message):
    try:
        res = requests.post(UPDATE_JOB_URL, json={
            "job_id": job_id,
            "status": status,
            "message": message
        })
        if res.status_code == 200:
            logger.info(f"Job {job_id} status successfully updated to {status} on {runner_id}")
        else:
            logger.warning(
                f"Failed to update status for job {job_id} on {runner_id}: HTTP {res.status_code} - {res.text}")
    except Exception as e:
        logger.error(
            f"Error while updating job {job_id} status on {runner_id}: {type(e).__name__}: {e}")

# --------------- Main Loop ----------------


def main():
    global current_proc
    logger.info(f"Runner started as {runner_id}")
    logger.info(f"Job Server URL: {job_server}:{port}")
    logger.info(f"Heart bit interval set to {heartBitInterval} seconds")

    while True:
        try:
            # Collect system metrics to send with the job request
            system_metrics = get_averaged_system_metrics()
            
            success, job_info, error_message = request_and_get_job(system_metrics)
            
            if not success:
                if error_message == "No available jobs":
                    logger.info("No more jobs available. Runner exiting.")
                    break
                else:
                    logger.error(f"Failed to get job: {error_message}")
                    # Wait a bit before retrying to avoid rapid retry loops
                    time.sleep(10)
                    continue
            
            # Parse job information
            job_id = job_info["job_id"]
            params = job_info["parameters"]

            logger.info(
                f"Job {job_id} assigned to {runner_id} with parameters: {params}")

            # Build the command
            cmd = list(run_command)
            for key, value in params.items():
                cmd.extend([f"--{key}", str(value)])

            base_path = os.path.join(os.path.expanduser(
                "~"), "data", "raw", expId, str(job_id))
            cmd.extend(["--base_path", base_path])
            logger.info(f"Executing command on {runner_id}: {' '.join(cmd)}")

            # Start heartbeat thread
            stop_event = threading.Event()
            pinger_thread = threading.Thread(
                target=ping_job, args=(job_id, stop_event))
            pinger_thread.start()

            # Run subprocess

            if IS_WINDOWS:
                current_proc = subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                                                stdout=subprocess.PIPE,
                                                stderr=subprocess.PIPE,
                                                text=True)
            else:
                current_proc = subprocess.Popen(cmd, preexec_fn=os.setsid,
                                                stdout=subprocess.PIPE,
                                                stderr=subprocess.PIPE,
                                                text=True)

            stdout, stderr = current_proc.communicate()

            # Stop heartbeat
            stop_event.set()
            pinger_thread.join()

            if current_proc.returncode == 0:
                logger.info(f"Job {job_id} completed successfully.")
                completion_message = f"Job execution completed successfully on {runner_id}."
                update_status(job_id, "DONE", completion_message)
            else:
                # Log the full output for debugging
                logger.error(
                    f"Job {job_id} failed with return code {current_proc.returncode}")
                logger.error(f"STDOUT:\n{stdout}")
                logger.error(f"STDERR:\n{stderr}")

                # Create a cleaner error message for status update
                error_message = f"Job execution failed on {runner_id}. Process exited with return code {current_proc.returncode}."

                # Add specific error handling based on return code
                if current_proc.returncode == -9:
                    error_message += " Process was killed (likely due to memory/time limits)."
                elif current_proc.returncode == -1:
                    error_message += " Process was terminated by signal."
                elif current_proc.returncode > 0:
                    error_message += f" Process exited with error code {current_proc.returncode}."

                # Only add stderr if it contains actual error messages (not just INFO logs)
                if stderr.strip() and any(keyword in stderr.lower() for keyword in ['error', 'exception', 'failed', 'fatal']):
                    error_message += f" Error details: {stderr}"
                elif stdout.strip() and any(keyword in stdout.lower() for keyword in ['error', 'exception', 'failed', 'fatal']):
                    error_message += f" Error details: {stdout}"
                else:
                    error_message += " Check logs for detailed output."

                update_status(job_id, "ABORTED", error_message)

            current_proc = None
            time.sleep(5)  # Wait before next job request

        except Exception as e:
            logger.exception(f"Unexpected error occurred: {str(e)}")
            # If we have a current job, update its status to ABORTED
            if 'job_id' in locals():
                exception_message = f"Unexpected exception occurred on {runner_id} while processing job. Exception: {str(e)}"
                update_status(job_id, "ABORTED", exception_message)
            break

        if machine_type == "htc":
            break


# --------------- Entry Point ----------------
if __name__ == "__main__":
    main()
