"""
jd_worker_cli — Job Distributor Worker CLI
=======================================
Requests jobs from a jd server, runs the entry script with the job's
parameters as CLI flags, sends a heartbeat ping every 57 seconds, and
reports DONE or ABORTED when the script finishes.

Usage
-----
    jd_worker_cli expId=<id> entry_script=<script.py> [options]

Required
--------
    expId=<id>              Experiment identifier (must match the server).
    entry_script=<path>     Python script to run for each job.

Hub mode (recommended) — auto-discovers the server URL
-------------------------------------------------------
    api_key=<key>           Your API key from the Hub Profile page
                            (env: JD_API_KEY).

    The worker connects to https://hub.jobdistributor.net by default —
    no ``hub=`` or ``server=`` needed.  Just supply your API key:

        jd_worker_cli expId=mnist-v1 entry_script=train.py api_key=jd_xxxx

    Or set it once as an env var (recommended):
        export JD_API_KEY=jd_xxxx
        jd_worker_cli expId=mnist-v1 entry_script=train.py

    To use a self-hosted hub, override with:
        hub=<url>           Hub base URL (env: JD_HUB_URL).
        jd_worker_cli expId=mnist-v1 entry_script=train.py \\
                  hub=https://my-hub.example.com api_key=jd_xxxx

Standalone mode — connect directly to the job server
-----------------------------------------------------
    server=<url>            Job server base URL or host (default: http://localhost,
                            env: JD_SERVER). If you omit ``http://`` or ``https://``,
                            ``http://`` is assumed.
    port=<N>                Port if not included in server URL
                            (default: 5000, env: JD_PORT)

Other optional arguments
------------------------
    log_dir=<path>          If set, logs go under <log_dir>/<expId>/; otherwise
                            under <jd_data>/<expId>/jd_worker_logs/
                            (env: JD_LOG_DIR)
    machine_type=<type>     Label for this machine in the dashboard
                            (default: worker, env: JD_MACHINE_TYPE)
    process_id=<N>          Numeric ID when running multiple workers on the
                            same machine  (default: 0)
    num_workers=<N>         Spawn N parallel worker processes on this machine
                            (default: 1).  Each process gets an auto-assigned
                            process_id (0 … N-1).  Cannot be combined with
                            a manual process_id=.
    once=true               Exit after completing a single job instead of
                            looping until no jobs remain.

    Local job data lives under ``<parent>/jd_data/<expId>/<job_id>/``.
    ``parent`` is ``JD_WORKSPACE_PATH`` if set, otherwise ``~``.
    Inside your entry script use ``jd_job_dir()`` to get this path —
    no need to handle ``--base_path`` yourself.

Install
-------
    pip install jd-worker
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
from urllib.parse import urlparse, urlunparse

import psutil
import requests

from jd import __version__

IS_WINDOWS = platform.system() == "Windows"
PING_INTERVAL = 57  # seconds — intentionally not 60 to avoid racing the idle timeout

# Fixed subdirectory under JD_WORKSPACE_PATH (or home): …/jd_data/<expId>/<job_id>/
_WORKER_JD_DATA_DIRNAME = "jd_data"


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


def _normalize_server_base_url(server_raw: str, port_raw: str) -> str:
    """
    Build a base URL that requests/lib can open.

    Host-only values like ``localhost`` must become ``http://localhost:<port>``.
    Without a scheme, Python requests raises "No connection adapters were found".
    """
    s = (server_raw or "").strip().rstrip("/")
    if not s:
        s = "http://localhost"
    if not urlparse(s).scheme:
        s = "http://" + s.lstrip("/")
    parsed = urlparse(s)
    if parsed.port is not None:
        return s
    host = parsed.hostname
    if not host:
        host = "localhost"
    netloc = f"{host}:{port_raw}"
    return urlunparse(
        (parsed.scheme, netloc, parsed.path or "", "", "", "")
    ).rstrip("/")


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
    port_raw   = str(get('port', 'JD_PORT', '5000')).strip()

    base_url = _normalize_server_base_url(server_raw, port_raw)

    # …/<parent>/jd_data/<expId>/<job_id>/  — parent from env or ~
    parent = os.environ.get("JD_WORKSPACE_PATH", "").strip()
    if not parent:
        parent = os.path.expanduser("~")
    parent = os.path.abspath(os.path.expanduser(parent))
    workspace_path = os.path.join(parent, _WORKER_JD_DATA_DIRNAME)
    os.makedirs(workspace_path, exist_ok=True)

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
        'num_workers':      int(get('num_workers', 'JD_NUM_WORKERS', '1')),
        'once':             get('once',         'JD_ONCE',         'false').lower() == 'true',
        # Hub authentication — defaults to the public hub; override with hub= or JD_HUB_URL
        'hub_url':          (cfg.get('hub') or get('hub_url', 'JD_HUB_URL',
                             'https://hub.jobdistributor.net')).strip().rstrip('/'),
        'api_key':          get('api_key',      'JD_API_KEY',      '').strip(),
    }


# ── Hub authentication ────────────────────────────────────────────────────────

def _hub_get_worker_token(hub_url: str, api_key: str,
                           exp_id: str, logger: logging.Logger) -> tuple[str, str] | None:
    """
    Obtain a worker JWT and server URL from the Hub.

    Returns (worker_token, server_url) on success, None on failure.
    The returned server_url overrides any CLI-supplied server address.
    """
    endpoint = f"{hub_url}/api/worker/token"
    try:
        r = requests.post(
            endpoint,
            json={"experiment_name": exp_id},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        if r.status_code == 200:
            data = r.json()
            token      = data.get("worker_token", "")
            server_url = data.get("server_url", "")
            expires_at = data.get("expires_at", "unknown")
            logger.info(f"Worker token obtained from Hub (expires: {expires_at})")
            return token, server_url
        logger.error(f"Hub token request failed: HTTP {r.status_code} — {r.text[:300]}")
        return None
    except Exception as exc:
        logger.error(f"Hub connection error: {exc}")
        return None


# ── Logging ───────────────────────────────────────────────────────────────────

def _setup_logger(log_dir: str, runner_id: str) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"jd_worker_{runner_id}.log")

    logger = logging.getLogger(f'jd_worker_cli.{runner_id}')
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


# ── System metrics ────────────────────────────────────────────────────────────

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

def _auth_headers(worker_token: str) -> dict:
    """Return Authorization header dict if worker_token is set, else empty."""
    if worker_token:
        return {"Authorization": f"Bearer {worker_token}"}
    return {}


def _request_job(url: str, runner_id: str, metrics: dict,
                 logger: logging.Logger, worker_token: str = ""):
    """Returns (job_dict, None) on success, (None, reason) on failure."""
    try:
        r = requests.post(url, json={"requested_by": runner_id,
                                     "system_metrics": metrics},
                          headers=_auth_headers(worker_token),
                          timeout=30)
        if r.status_code == 404:
            return None, 'no_jobs'
        if r.status_code == 200:
            return r.json(), None
        return None, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as exc:
        return None, str(exc)


def _update_status(url: str, job_id: int, status: str,
                   message: str, logger: logging.Logger,
                   worker_token: str = "") -> None:
    try:
        r = requests.post(url, json={"job_id": job_id,
                                     "status":  status,
                                     "message": message},
                          headers=_auth_headers(worker_token),
                          timeout=30)
        if r.status_code == 200:
            logger.info(f"Job {job_id} → {status}")
        else:
            logger.warning(f"Status update failed: HTTP {r.status_code}")
    except Exception as exc:
        logger.error(f"Status update error: {exc}")


def _ping_loop(url: str, job_id: int,
               stop_event: threading.Event,
               logger: logging.Logger,
               worker_token: str = "") -> None:
    """Background thread: ping the server every PING_INTERVAL seconds."""
    while not stop_event.wait(PING_INTERVAL):
        try:
            r = requests.post(url, json={"id": job_id},
                              headers=_auth_headers(worker_token),
                              timeout=10)
            if r.status_code == 200:
                logger.info(f"Ping OK (job {job_id})")
            else:
                logger.warning(f"Ping HTTP {r.status_code} (job {job_id})")
        except Exception as exc:
            logger.warning(f"Ping error (job {job_id}): {exc}")


# ── Main entry point ──────────────────────────────────────────────────────────

def _run_worker(cfg: dict) -> None:
    """Run a single worker loop.  Called directly for num_workers=1, or
    launched in a subprocess (via _worker_subprocess_entry) for N > 1."""

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

    # ── Hub authentication (optional) ────────────────────────────────────────
    # When num_workers > 1, the parent process already authenticated and
    # stored the token in cfg['_worker_token'] — reuse it here directly.
    worker_token = cfg.get('_worker_token', '')
    if not worker_token:
        if cfg['hub_url'] and cfg['api_key']:
            logger.info(f"Hub mode: authenticating via {cfg['hub_url']}")
            result = _hub_get_worker_token(
                cfg['hub_url'], cfg['api_key'], cfg['exp_id'], logger
            )
            if result is None:
                logger.error("Failed to obtain worker token from Hub. Exiting.")
                sys.exit(1)
            worker_token, hub_server_url = result
            if hub_server_url:
                cfg['base_url'] = hub_server_url
                logger.info(f"Server URL from Hub: {hub_server_url}")
        elif cfg['hub_url'] or cfg['api_key']:
            logger.warning(
                "Both hub_url (JD_HUB_URL) and api_key (JD_API_KEY) must be set "
                "for Hub authentication. Running in standalone mode."
            )

    urls = {
        'request': f"{cfg['base_url']}/request_job",
        'update':  f"{cfg['base_url']}/update_job_status",
        'ping':    f"{cfg['base_url']}/ping",
    }

    logger.info(f"jd_worker_cli v{__version__}  |  runner: {runner_id}")
    logger.info(f"Server:        {cfg['base_url']}")
    logger.info(f"Entry script:   {cfg['entry_script']}")
    logger.info(f"Hub mode:       {'enabled' if worker_token else 'disabled'}")
    logger.info(f"Local jd_data root: {cfg['workspace_path']} "
                f"(each job: …/jd_data/<expId>/<job_id>/)")
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
        logger.info("jd_worker_cli shut down.")
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # ── Main job loop ────────────────────────────────────────────────────────
    while True:
        job_id = None
        try:
            metrics      = _averaged_metrics(cfg['machine_type'], logger)
            job, reason  = _request_job(urls['request'], runner_id, metrics, logger,
                                        worker_token=worker_token)

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
            logger.info(f"Job workspace: {job_root}")
            logger.info(f"Command: {' '.join(cmd)}")

            # Start heartbeat ping thread
            stop_ping = threading.Event()
            pinger    = threading.Thread(
                target=_ping_loop,
                args=(urls['ping'], job_id, stop_ping, logger, worker_token),
                daemon=True,
            )
            pinger.start()

            # Build child environment: inherit everything + inject JD_ context
            # so jd_upload / jd_update_checkpoint / jd_get_last_checkpoint work
            # inside the entry script without requiring explicit arguments.
            child_env = os.environ.copy()
            child_env["JD_JOB_ID"]                  = str(job_id)
            child_env["JD_SERVER"]                  = cfg["base_url"]
            child_env["JD_EXP_ID"]                  = cfg["exp_id"]
            child_env["JD_WORKER_JOB_DIR"]          = job_root
            child_env["JD_WORKER_WORKSPACE_ROOT"]   = cfg["workspace_path"]
            if worker_token:
                child_env["JD_WORKER_TOKEN"]        = worker_token

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
                    logger, worker_token=worker_token,
                )
            else:
                logger.error(f"Job {job_id} failed — exit code {rc}")
                if stderr.strip():
                    logger.error(f"STDERR (last 1000 chars):\n{stderr.strip()[-1000:]}")

                abort_msg = (
                    f"Job failed on {runner_id}. "
                    f"Exit code {rc}."
                )
                snippet = (stderr.strip() or stdout.strip())[-500:]
                if snippet and any(kw in snippet.lower()
                                   for kw in ('error', 'exception', 'traceback', 'failed')):
                    abort_msg += f" Last output: {snippet}"
                else:
                    abort_msg += " Check worker logs for details."

                if rc == -9:
                    abort_msg += " (Process killed — possible OOM or time limit.)"

                _update_status(urls['update'], job_id, 'ABORTED', abort_msg, logger,
                               worker_token=worker_token)

            _proc[0] = None

        except Exception as exc:
            logger.exception(f"Unexpected error: {exc}")
            if job_id is not None:
                _update_status(
                    urls['update'], job_id, 'ABORTED',
                    f"Unexpected exception on {runner_id}: {exc}",
                    logger, worker_token=worker_token,
                )
            break

        if cfg['once']:
            logger.info("once=true — exiting after one job.")
            break

        time.sleep(3)   # brief pause before requesting the next job


def _worker_subprocess_entry() -> None:
    """Entry point used by each child process when num_workers > 1.

    The parent serialises the resolved config via the JD_WORKER_CFG_JSON
    environment variable so the child skips re-parsing argv and re-running
    Hub authentication (the parent already obtained the token).
    """
    import json as _json
    raw = os.environ.get("JD_WORKER_CFG_JSON", "")
    if not raw:
        sys.exit("JD_WORKER_CFG_JSON not set — internal error")
    cfg = _json.loads(raw)
    _run_worker(cfg)


def main() -> None:
    argv = sys.argv[1:]

    # Show help
    if not argv or any(a in argv for a in ('help', '-h', '--help')):
        print(__doc__)
        sys.exit(0)

    kv  = _parse_kv(argv)
    cfg = _resolve(kv)

    # ── Validate num_workers ─────────────────────────────────────────────────
    num_workers = cfg.get('num_workers', 1)
    if not isinstance(num_workers, int) or num_workers < 1:
        print("Error: num_workers must be a positive integer.")
        sys.exit(1)

    # If the user explicitly set process_id alongside num_workers, warn them.
    if num_workers > 1 and kv.get('process_id') is not None:
        print("Warning: process_id is ignored when num_workers > 1. "
              "IDs are assigned automatically (0 … N-1).")

    # ── Validate required arguments ──────────────────────────────────────────
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
        print("Run `jd_worker_cli help` for usage.")
        sys.exit(1)

    if num_workers == 1:
        # ── Single worker — run directly in this process ─────────────────────
        _run_worker(cfg)
    else:
        # ── Multiple workers — spawn N child processes ────────────────────────
        import json as _json

        # Hub authentication once in the parent so all children share the same
        # token.  Children inherit it via JD_WORKER_CFG_JSON.
        if cfg['hub_url'] and cfg['api_key']:
            # Perform auth now so children don't all hit the Hub simultaneously.
            dummy_logger = logging.getLogger("jd_worker_cli.launcher")
            dummy_logger.addHandler(logging.StreamHandler())
            dummy_logger.setLevel(logging.INFO)
            dummy_logger.info(
                f"Hub mode: authenticating once for {num_workers} workers …"
            )
            result = _hub_get_worker_token(
                cfg['hub_url'], cfg['api_key'], cfg['exp_id'], dummy_logger
            )
            if result is None:
                dummy_logger.error("Failed to obtain worker token from Hub. Exiting.")
                sys.exit(1)
            worker_token, hub_server_url = result
            cfg['_worker_token']   = worker_token
            if hub_server_url:
                cfg['base_url'] = hub_server_url
            dummy_logger.info(
                f"Spawning {num_workers} workers (process IDs 0–{num_workers-1}) …"
            )

        procs = []
        for i in range(num_workers):
            child_cfg = dict(cfg)
            child_cfg['process_id']  = str(i)
            child_cfg['num_workers'] = 1   # children run as single workers

            env = os.environ.copy()
            env["JD_WORKER_CFG_JSON"] = _json.dumps(child_cfg)

            # Re-invoke the same Python executable with the internal entry point
            p = subprocess.Popen(
                [sys.executable, "-c",
                 "from jd.worker import _worker_subprocess_entry; "
                 "_worker_subprocess_entry()"],
                env=env,
            )
            procs.append(p)

        # Wait for all children; propagate Ctrl+C cleanly
        def _kill_all(signum=None, frame=None):
            for p in procs:
                try:
                    p.terminate()
                except Exception:
                    pass
            sys.exit(0)

        signal.signal(signal.SIGINT,  _kill_all)
        signal.signal(signal.SIGTERM, _kill_all)

        for p in procs:
            p.wait()


if __name__ == '__main__':
    main()
