"""
jd_worker — Job Distributor Worker CLI
=======================================
Requests jobs from a jd server, runs the entry script with the job's
parameters as CLI flags, sends a heartbeat ping every 57 seconds, and
reports DONE or ABORTED when the script finishes.

Usage
-----
    jd_worker expId=<id> entry_script=<script.py> [options]

Required
--------
    expId=<id>              Experiment identifier (must match the server).
    entry_script=<path>     Python script to run for each job.

Optional
--------
    workspace_path=<path>   Worker-side directory root. Per job, local files
                            belong under
                            <workspace_path>/<expId>/<job_id>/ (passed as
                            --base_path and JD_WORKER_JOB_DIR).
                            (default: ./jd_workspace, env: JD_WORKER_WORKSPACE)
                            JD_WORKSPACE_PATH is used if JD_WORKER_WORKSPACE is unset.
                            Legacy: output_dir / JD_OUTPUT_DIR if none of the above.
    server=<url>            Job server base URL  (default: http://localhost,
                            env: JD_SERVER)
    port=<N>                Port if not included in server URL
                            (default: 5000, env: JD_PORT)
    log_dir=<path>          If set, logs go under <log_dir>/<expId>/; otherwise
                            under <workspace_path>/<expId>/jd_worker_logs/
                            (env: JD_LOG_DIR)
    output_dir=<path>       Deprecated alias for workspace_path when
                            workspace_path / JD_WORKER_WORKSPACE /
                            JD_WORKSPACE_PATH are unset (env: JD_OUTPUT_DIR)
    machine_type=<type>     Label for this machine in the dashboard
                            (default: worker, env: JD_MACHINE_TYPE)
    process_id=<N>          Numeric ID when running multiple workers on the
                            same machine  (default: 0)
    once=true               Exit after completing a single job instead of
                            looping until no jobs remain.

Examples
--------
    jd_worker expId=mnist_tune entry_script=train.py

    jd_worker expId=mnist_tune entry_script=train.py \\
              server=http://10.0.0.5 port=8000 \\
              workspace_path=/data/experiments \\
              machine_type=gpu_node

    # Run exactly one job:
    jd_worker expId=mnist_tune entry_script=train.py once=true

Install
-------
    pip install -e ./client     # from the repo root
    # then `jd_worker` is available in whatever env is active
"""

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
from urllib.parse import urlparse

import psutil
import requests

from jd import __version__

IS_WINDOWS = platform.system() == "Windows"
PING_INTERVAL = 57  # seconds — intentionally not 60 to avoid racing the idle timeout


# ── Argument parsing ─────────────────────────────────────────────────────────

def _parse_kv(argv: list) -> dict:
    """Parse key=value positional arguments into a plain dict."""
    cfg = {}
    for arg in argv:
        if '=' in arg:
            k, v = arg.split('=', 1)
            cfg[k.strip()] = v.strip()
        else:
            cfg[arg.strip()] = 'true'
    return cfg


def _resolve(cfg: dict) -> dict:
    """Merge CLI key=value pairs with environment variables and defaults."""

    def get(cli_key, env_key=None, default=None):
        if cli_key in cfg:
            return cfg[cli_key]
        if env_key:
            val = os.environ.get(env_key, '')
            if val:
                return val
        return default

    server_raw = get('server', 'JD_SERVER', 'http://localhost')
    port_raw   = get('port',   'JD_PORT',   '5000')

    parsed = urlparse(server_raw)
    base_url = server_raw.rstrip('/') if parsed.port else f"{server_raw.rstrip('/')}:{port_raw}"

    # Worker filesystem root → per-job dir: <workspace_path>/<expId>/<job_id>/
    ws = get('workspace_path', 'JD_WORKER_WORKSPACE', None)
    if not ws or not str(ws).strip():
        alt = os.environ.get('JD_WORKSPACE_PATH', '').strip()
        ws = alt if alt else None
    if not ws:
        ws = get('output_dir', 'JD_OUTPUT_DIR', './jd_workspace')
    workspace_path = os.path.abspath(os.path.expanduser(str(ws).strip()))

    log_override = None
    if 'log_dir' in cfg:
        log_override = os.path.expanduser(cfg['log_dir'].strip())
    elif os.environ.get('JD_LOG_DIR', '').strip():
        log_override = os.path.expanduser(os.environ['JD_LOG_DIR'].strip())

    return {
        'exp_id':           get('expId',        'JD_EXP_ID',       None),
        'entry_script':     get('entry_script', 'JD_ENTRY_SCRIPT', None),
        'base_url':         base_url,
        'workspace_path':   workspace_path,
        'log_dir_override': log_override,
        'machine_type':     get('machine_type', 'JD_MACHINE_TYPE', 'worker'),
        'process_id':       get('process_id',   None,              '0'),
        'once':             get('once',         'JD_ONCE',         'false').lower() == 'true',
    }


# ── Logging ───────────────────────────────────────────────────────────────────

def _setup_logger(log_dir: str, runner_id: str) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"jd_worker_{runner_id}.log")

    logger = logging.getLogger(f'jd_worker.{runner_id}')
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s")
    fh  = logging.FileHandler(log_path, encoding='utf-8')
    fh.setFormatter(fmt)
    sh  = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(sh)
    logger.propagate = False
    return logger


# ── System metrics (ported from runner.py, logger injected) ──────────────────

def _collect_metrics(machine_type: str, logger: logging.Logger) -> dict:
    """Collect a single snapshot of system metrics."""
    try:
        cpu_pct          = psutil.cpu_percent(interval=0.1)
        cpu_phys         = psutil.cpu_count(logical=False) or 1
        cpu_logi         = psutil.cpu_count(logical=True)  or 1

        try:
            freq         = psutil.cpu_freq()
            freq_mhz     = freq.current if freq else 0
        except (AttributeError, RuntimeError):
            freq_mhz     = 0

        mem              = psutil.virtual_memory()
        ram_total_gb     = mem.total     / (1024 ** 3)
        ram_avail_gb     = mem.available / (1024 ** 3)

        if not IS_WINDOWS:
            try:
                la           = os.getloadavg()
                load1, load5, load15 = la[0], la[1], la[2]
            except (AttributeError, OSError):
                load1 = load5 = load15 = 0.0
            load_per_cpu = load1 / cpu_logi if cpu_logi > 0 else 0.0
            idle_slots   = max(0, cpu_logi - load1)
        else:
            load1 = load5 = load15 = cpu_pct / 100.0 * cpu_logi
            load_per_cpu = cpu_pct / 100.0
            idle_slots   = max(0, cpu_logi * (1 - cpu_pct / 100.0))

        try:
            d0           = psutil.disk_io_counters()
            time.sleep(0.1)
            d1           = psutil.disk_io_counters()
            if d0 and d1:
                dt           = 0.1
                ops          = (d1.read_count  - d0.read_count  +
                                d1.write_count - d0.write_count) / dt
                bps          = (d1.read_bytes  - d0.read_bytes  +
                                d1.write_bytes - d0.write_bytes) / dt
                disk_io_util = max(min(100.0, (ops / 100_000) * 100),
                                   min(100.0, (bps / 1e9)     * 100))
            else:
                disk_io_util = 0.0
        except (AttributeError, RuntimeError, TypeError):
            disk_io_util = 0.0

        return {
            "cpu_util":     round(cpu_pct,        1),
            "ram_util":     round(mem.percent,     1),
            "ram_available": round(ram_avail_gb,  15),
            "ram_total":    round(ram_total_gb,    1),
            "worker_type":  machine_type,
            "idle_slots":   int(round(idle_slots)),
            "load_1min":    round(load1,          10),
            "load_5min":    round(load5,          10),
            "load_15min":   round(load15,         10),
            "load_per_cpu": round(load_per_cpu,   13),
            "disk_io_util": round(disk_io_util,    2),
            "cpu_cores":    cpu_phys,
            "cpu_threads":  cpu_logi,
            "cpu_freq_mhz": int(round(freq_mhz)) if freq_mhz > 0 else 0,
        }
    except Exception as exc:
        logger.error(f"Metrics collection error: {exc}")
        return {k: 0 for k in (
            "cpu_util", "ram_util", "ram_available", "ram_total",
            "idle_slots", "load_1min", "load_5min", "load_15min",
            "load_per_cpu", "disk_io_util", "cpu_cores", "cpu_threads",
            "cpu_freq_mhz",
        )} | {"worker_type": machine_type}


def _averaged_metrics(machine_type: str, logger: logging.Logger,
                      samples: int = 5, interval: float = 3.0) -> dict:
    """Collect `samples` snapshots and return their numeric averages."""
    logger.info(f"Collecting system metrics ({samples} samples × {interval}s)…")
    snapshots = []
    for i in range(samples):
        logger.info(f"  Sample {i+1}/{samples}")
        snapshots.append(_collect_metrics(machine_type, logger))
        if i < samples - 1:
            time.sleep(interval)

    numeric = [
        "cpu_util", "ram_util", "ram_available", "ram_total",
        "idle_slots", "load_1min", "load_5min", "load_15min",
        "load_per_cpu", "disk_io_util", "cpu_freq_mhz",
    ]
    result = {"worker_type": machine_type,
              "cpu_cores":   snapshots[0]["cpu_cores"],
              "cpu_threads": snapshots[0]["cpu_threads"]}
    for key in numeric:
        vals = [s[key] for s in snapshots]
        avg  = sum(vals) / len(vals)
        if key in ("idle_slots", "cpu_freq_mhz"):
            result[key] = int(round(avg))
        elif key in ("cpu_util", "ram_util", "ram_total"):
            result[key] = round(avg, 1)
        elif key == "ram_available":
            result[key] = round(avg, 15)
        elif key in ("load_1min", "load_5min", "load_15min"):
            result[key] = round(avg, 10)
        elif key == "load_per_cpu":
            result[key] = round(avg, 13)
        else:
            result[key] = round(avg, 2)
    logger.info("System metrics ready.")
    return result


# ── Server communication ──────────────────────────────────────────────────────

def _request_job(url: str, runner_id: str, metrics: dict,
                 logger: logging.Logger):
    """Returns (job_dict, None) on success, (None, reason) on failure."""
    try:
        r = requests.post(url, json={"requested_by": runner_id,
                                     "system_metrics": metrics},
                          timeout=30)
        if r.status_code == 404:
            return None, 'no_jobs'
        if r.status_code == 200:
            return r.json(), None
        return None, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as exc:
        return None, str(exc)


def _update_status(url: str, job_id: int, status: str,
                   message: str, logger: logging.Logger) -> None:
    try:
        r = requests.post(url, json={"job_id": job_id,
                                     "status":  status,
                                     "message": message},
                          timeout=30)
        if r.status_code == 200:
            logger.info(f"Job {job_id} → {status}")
        else:
            logger.warning(f"Status update failed: HTTP {r.status_code}")
    except Exception as exc:
        logger.error(f"Status update error: {exc}")


def _ping_loop(url: str, job_id: int,
               stop_event: threading.Event,
               logger: logging.Logger) -> None:
    """Background thread: ping the server every PING_INTERVAL seconds."""
    while not stop_event.wait(PING_INTERVAL):
        try:
            r = requests.post(url, json={"id": job_id}, timeout=10)
            if r.status_code == 200:
                logger.info(f"Ping OK (job {job_id})")
            else:
                logger.warning(f"Ping HTTP {r.status_code} (job {job_id})")
        except Exception as exc:
            logger.warning(f"Ping error (job {job_id}): {exc}")


# ── Main entry point ──────────────────────────────────────────────────────────

def main() -> None:
    argv = sys.argv[1:]

    # Show help
    if not argv or any(a in argv for a in ('help', '-h', '--help')):
        print(__doc__)
        sys.exit(0)

    kv  = _parse_kv(argv)
    cfg = _resolve(kv)

    # Validate required arguments
    errors = []
    if not cfg['exp_id']:
        errors.append("expId is required")
    if not cfg['entry_script']:
        errors.append("entry_script is required")
    elif not os.path.isfile(cfg['entry_script']):
        errors.append(f"entry_script '{cfg['entry_script']}' not found")
    if errors:
        for e in errors:
            print(f"Error: {e}")
        print("Run `jd_worker help` for usage.")
        sys.exit(1)

    # Build a unique runner ID visible in the dashboard
    username  = os.getenv('USER') or os.getenv('USERNAME') or 'user'
    random.seed(int(time.time() * 1000))
    suffix    = random.randint(10000, 99999)
    runner_id = (f"{username}@{socket.gethostname()}"
                 f"({cfg['machine_type']})_{cfg['process_id']}_{suffix}")

    if cfg['log_dir_override'] is not None:
        log_dir = os.path.join(cfg['log_dir_override'], cfg['exp_id'])
    else:
        log_dir = os.path.join(cfg['workspace_path'], cfg['exp_id'], 'jd_worker_logs')
    logger  = _setup_logger(log_dir, runner_id)

    urls = {
        'request': f"{cfg['base_url']}/request_job",
        'update':  f"{cfg['base_url']}/update_job_status",
        'ping':    f"{cfg['base_url']}/ping",
    }

    logger.info(f"jd_worker v{__version__}  |  runner: {runner_id}")
    logger.info(f"Server:        {cfg['base_url']}")
    logger.info(f"Entry script:   {cfg['entry_script']}")
    logger.info(f"Workspace path: {cfg['workspace_path']} "
                f"(per-job: <workspace>/<expId>/<job_id>/)")
    logger.info(f"Ping interval: {PING_INTERVAL}s")
    if cfg['once']:
        logger.info("Mode: single job (once=true)")

    # Track the current child process so SIGINT/SIGTERM can clean it up
    _proc: list = [None]

    def _shutdown(signum=None, frame=None):
        p = _proc[0]
        if p and p.poll() is None:
            logger.info(f"Killing subprocess PID {p.pid}…")
            try:
                if IS_WINDOWS:
                    p.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:
                pass
        logger.info("jd_worker shut down.")
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # ── Main job loop ────────────────────────────────────────────────────────
    while True:
        job_id = None
        try:
            metrics      = _averaged_metrics(cfg['machine_type'], logger)
            job, reason  = _request_job(urls['request'], runner_id, metrics, logger)

            if job is None:
                if reason == 'no_jobs':
                    logger.info("No more jobs available. Exiting.")
                    break
                logger.error(f"Job request failed: {reason}. Retrying in 10 s…")
                time.sleep(10)
                continue

            job_id = job['job_id']
            params = job['parameters']
            logger.info(f"Job {job_id} received  |  params: {params}")

            # Local sandbox for this job — keep worker-side I/O under this directory
            job_root = os.path.abspath(os.path.join(
                cfg['workspace_path'], cfg['exp_id'], str(job_id)))
            os.makedirs(job_root, exist_ok=True)

            # Build subprocess command
            # Uses the *current* Python interpreter so venv/conda is respected
            cmd = [sys.executable, cfg['entry_script']]
            for k, v in params.items():
                cmd.extend([f"--{k}", str(v)])
            cmd.extend(['--base_path', job_root])
            logger.info(f"Job workspace: {job_root}")
            logger.info(f"Command: {' '.join(cmd)}")

            # Start heartbeat ping thread
            stop_ping = threading.Event()
            pinger    = threading.Thread(
                target=_ping_loop,
                args=(urls['ping'], job_id, stop_ping, logger),
                daemon=True,
            )
            pinger.start()

            # Build child environment: inherit everything + inject JD_ context
            # so jd_upload / jd_update_checkpoint / jd_get_last_checkpoint work
            # inside the entry script without requiring explicit arguments.
            child_env = os.environ.copy()
            child_env["JD_JOB_ID"]         = str(job_id)
            child_env["JD_SERVER"]         = cfg["base_url"]
            child_env["JD_EXP_ID"]         = cfg["exp_id"]
            child_env["JD_WORKER_JOB_DIR"] = job_root

            # Launch the entry script
            popen_kw = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=child_env)
            if IS_WINDOWS:
                _proc[0] = subprocess.Popen(
                    cmd,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                    **popen_kw,
                )
            else:
                _proc[0] = subprocess.Popen(
                    cmd,
                    preexec_fn=os.setsid,
                    **popen_kw,
                )

            stdout, stderr = _proc[0].communicate()
            rc             = _proc[0].returncode

            # Stop heartbeat
            stop_ping.set()
            pinger.join(timeout=5)

            if rc == 0:
                logger.info(f"Job {job_id} finished successfully.")
                _update_status(
                    urls['update'], job_id, 'DONE',
                    f"Completed successfully on {runner_id}.",
                    logger,
                )
            else:
                logger.error(f"Job {job_id} failed — exit code {rc}")
                if stderr.strip():
                    logger.error(f"STDERR (last 1000 chars):\n{stderr.strip()[-1000:]}")

                abort_msg = (
                    f"Job failed on {runner_id}. "
                    f"Exit code {rc}."
                )
                # Add the most relevant error snippet
                snippet = (stderr.strip() or stdout.strip())[-500:]
                if snippet and any(kw in snippet.lower()
                                   for kw in ('error', 'exception', 'traceback', 'failed')):
                    abort_msg += f" Last output: {snippet}"
                else:
                    abort_msg += " Check worker logs for details."

                if rc == -9:
                    abort_msg += " (Process killed — possible OOM or time limit.)"

                _update_status(urls['update'], job_id, 'ABORTED', abort_msg, logger)

            _proc[0] = None

        except Exception as exc:
            logger.exception(f"Unexpected error: {exc}")
            if job_id is not None:
                _update_status(
                    urls['update'], job_id, 'ABORTED',
                    f"Unexpected exception on {runner_id}: {exc}",
                    logger,
                )
            break

        if cfg['once']:
            logger.info("once=true — exiting after one job.")
            break

        time.sleep(3)   # brief pause before requesting the next job


if __name__ == '__main__':
    main()
