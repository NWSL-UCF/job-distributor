import argparse
import atexit
import json
import logging
import os
import signal
import subprocess
import sys

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)
from workspace_layout import ensure_exp_layout, exp_meta_dir  # noqa: E402

LOG_FILENAME = "__start__.log"
processes = {}


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


def popen_cmd(cmd: list, env: dict) -> subprocess.Popen:
    """Launch a command list as a new process group."""
    if os.name == "nt":
        return subprocess.Popen(
            cmd, env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        return subprocess.Popen(cmd, env=env, preexec_fn=os.setsid)


def main():
    global processes

    parser = argparse.ArgumentParser(description="Start the job-distributor server stack")
    parser.add_argument("--expId", required=True,
                        help="Unique experiment name (used as the data subdirectory)")
    parser.add_argument("--server_port", type=int, default=5000,
                        help="Port for the job server (default: 5000)")
    parser.add_argument("--dashboard_port", type=int, default=5050,
                        help="Port for the dashboard (default: 5050)")
    parser.add_argument("--workspace_path",
                        default=os.path.dirname(os.path.abspath(__file__)),
                        help="Directory where experiment data (DB, logs) will be stored")
    parser.add_argument("--workers", type=int, default=8,
                        help="Gunicorn worker processes per server (default: 8)")
    parser.add_argument("--threads", type=int, default=16,
                        help="Threads per gunicorn worker (default: 16)")
    args = parser.parse_args()

    exp_dir = os.path.join(args.workspace_path, args.expId)
    os.makedirs(exp_dir, exist_ok=True)
    ensure_exp_layout(args.workspace_path, args.expId)
    meta_dir = exp_meta_dir(args.workspace_path, args.expId)

    LOG_FILE = os.path.join(meta_dir, LOG_FILENAME)
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    atexit.register(cleanup)
    signal.signal(signal.SIGINT,  lambda sig, frame: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda sig, frame: sys.exit(0))

    src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")

    # Build subprocess environment: inherit current env and inject JD_ vars
    # so gunicorn worker processes can initialise the DB at import time.
    env = os.environ.copy()
    env["JD_WORKSPACE_PATH"] = args.workspace_path
    env["JD_EXP_ID"]         = args.expId
    # DATABASE_URL is inherited from the environment (set by docker-compose or caller).
    # Fail early and loudly if it is missing rather than getting a cryptic error later.
    if "DATABASE_URL" not in env:
        sys.exit("ERROR: DATABASE_URL environment variable is not set.")

    # ── Job server ────────────────────────────────────────────────────────
    server_cmd = [
        sys.executable, "-m", "gunicorn",
        "server:app",
        "--bind",          f"0.0.0.0:{args.server_port}",
        "--worker-class",  "gthread",
        "--workers",       str(args.workers),
        "--threads",       str(args.threads),
        "--timeout",       "120",
        "--chdir",         src_dir,
        "--access-logfile", os.path.join(meta_dir, "server_access.log"),
        "--error-logfile",  os.path.join(meta_dir, "server.stderr.log"),
    ]

    # ── Dashboard ─────────────────────────────────────────────────────────
    dashboard_cmd = [
        sys.executable, "-m", "gunicorn",
        "dashboard:app",
        "--bind",          f"0.0.0.0:{args.dashboard_port}",
        "--worker-class",  "gthread",
        "--workers",       str(args.workers),
        "--threads",       str(args.threads),
        "--timeout",       "120",
        "--chdir",         src_dir,
        "--access-logfile", os.path.join(meta_dir, "dashboard_access.log"),
        "--error-logfile",  os.path.join(meta_dir, "dashboard.stderr.log"),
    ]

    # ── Job cleaner ───────────────────────────────────────────────────────
    cleaner_cmd = [
        sys.executable,
        os.path.join(src_dir, "job_cleaner.py"),
        f"--expId={args.expId}",
        f"--workspacePath={args.workspace_path}",
        f"--serverPort={args.server_port}",
    ]

    commands = {
        "server":      server_cmd,
        "dashboard":   dashboard_cmd,
        "job_cleaner": cleaner_cmd,
    }

    for name, cmd in commands.items():
        logging.info(f"Launching {name}: {' '.join(cmd)}")
        processes[name] = popen_cmd(cmd, env=env)
        logging.info(f"{name} started with PID {processes[name].pid}")

    pid_file = os.path.join(meta_dir, "pids.json")
    with open(pid_file, "w", encoding="utf-8") as f:
        json.dump({name: proc.pid for name, proc in processes.items()}, f, indent=2)
    logging.info(f"PIDs written to {pid_file}")

    for name, proc in processes.items():
        proc.wait()
        logging.info(f"{name} exited with code {proc.returncode}.")


if __name__ == "__main__":
    main()

# Example:
# python start.py --expId mnist_tune --server_port 5000 --dashboard_port 5050 \
#                 --workspace_path /data/experiments
