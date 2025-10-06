import atexit
import json
import logging
import os
import shlex
import signal
import subprocess
import sys

CONFIG_FILE_PATH = os.path.join(os.path.dirname(
    os.path.abspath(__file__)), "config.json")
LOG_FILENAME = "__start__.log"
processes = {}  # moved to global so cleanup can access it


def quote_json_for_shell(payload: dict) -> str:
    """
    Return a shell-quoted JSON string based on OS.
    - Windows (cmd.exe): wrap in double quotes and escape inner quotes.
    - POSIX (Linux/macOS): wrap in single quotes (JSON uses double quotes inside).
    """
    s = json.dumps(payload)
    if os.name == "nt":
        # Windows: wrap in double quotes and escape inner double quotes
        return '"' + s.replace('"', '\\"') + '"'
    else:
        # POSIX shells: single quotes are safest for JSON
        return "'" + s + "'"


def run_command(cmd):
    """Run a command and stream its output."""
    process = subprocess.Popen(cmd, shell=True)
    process.wait()


def cleanup(*args):
    logging.info("Cleaning up child processes...")
    for name, proc in processes.items():
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"], check=False)
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            logging.info(f"Killed {name} (PID {proc.pid})")
        except Exception as e:
            logging.warning(f"Could not kill {name}: {e}")


def main():
    global processes  # so cleanup can access it

    with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    # Set up logging
    LOG_FILE = os.path.join(os.path.dirname(
        os.path.abspath(__file__)), LOG_FILENAME)
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    # Register cleanup on normal exit and termination signals
    atexit.register(cleanup)
    signal.signal(signal.SIGINT, lambda sig, frame: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda sig, frame: sys.exit(0))

    # Build commands from config
    params_arg = quote_json_for_shell(config["parameters"])

    create_cmd = (
        f"{sys.executable} src/create_job_db.py "
        f"--expId={config['expId']} "
        f"--jobDB={config['jobDB']} "
        f"--parameters={params_arg}"
    )

    enable_flag = "--enableNgrok" if str(config.get(
        "enable_ngork", False)).lower() in ("true", "1", "yes", "on") else ""

    server_cmd = (
        f"{sys.executable} src/server.py "
        f"--expId={config['expId']} "
        f"--jobDB={config['jobDB']} "
        f"{enable_flag} "
        f"--host={config['host']} "
        f"--port={config['server_port']}"
    )

    dashboard_cmd = (
        f"{sys.executable} src/dashboard.py "
        f"--expId={config['expId']} "
        f"--jobDB={config['jobDB']} "
        f"{enable_flag} "
        f"--host={config['host']} "
        f"--port={config['dashboard_port']}"
    )

    cleaner_cmd = (
        f"{sys.executable} src/job_cleaner.py "
        f"--expId={config['expId']} "
        f"--jobDB={config['jobDB']} "
        f"--abortedJobResetTimeout={config['abortedJobResetTimeout']} "
        f"--idleTimeout={config['idleTimeout']}"
    )

    if config["fresh_start"]:
        logging.info("Fresh start enabled. Creating new job database...")
        subprocess.run(create_cmd, shell=True, check=True)
        logging.info("Job DB created. Launching services in parallel...")
    else:
        logging.info("Fresh start disabled. Loading existing job database...")
        db_path = os.path.join(os.path.dirname(
            __file__), config["expId"], config["jobDB"])
        if os.path.exists(db_path):
            logging.info(f"Existing database found: {db_path}")
        else:
            logging.warning(f"No existing database found at: {db_path}")

    commands = {
        "server": server_cmd,
        "dashboard": dashboard_cmd,
        "job_cleaner": cleaner_cmd
    }

    def popen_cmd(cmd_str: str) -> subprocess.Popen:
        if os.name == "nt":
            # Windows
            return subprocess.Popen(shlex.split(cmd_str, posix=False), creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            # POSIX
            return subprocess.Popen(shlex.split(cmd_str), preexec_fn=os.setsid)

    for name, cmd in commands.items():
        logging.info(f"Launching {name}...")
        processes[name] = popen_cmd(cmd)
        logging.info(f"{name} started with PID {processes[name].pid}")

    # Save PIDs
    pid_dir = os.path.join(os.path.dirname(__file__), config["expId"])
    os.makedirs(pid_dir, exist_ok=True)
    pid_file = os.path.join(pid_dir, "pids.json")
    with open(pid_file, "w", encoding="utf-8") as f:
        json.dump({name: proc.pid for name, proc in processes.items()}, f)

    for name, proc in processes.items():
        proc.wait()
        logging.info(f"{name} process exited with code {proc.returncode}.")


if __name__ == "__main__":
    main()
