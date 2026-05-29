"""
jd_worker_cli — Job Distributor Worker CLI
=======================================
Requests jobs from a jd server, runs the entry script with the job's
parameters as CLI flags, sends a unified heartbeat via POST /worker/heartbeat
(every 57 s while busy, every 3 min when idle), and reports DONE or ABORTED
when the script finishes.

Usage
-----
    # Start workers in the background (default — no tmux needed)
    jd_worker_cli expId=<id> entry_script=<script.py> [options]

    # List background workers for an experiment
    jd_worker_cli expId=<id> worker-list

    # List all experiments with running worker counts on this machine
    jd_worker_cli exp-list

    # Stop workers
    jd_worker_cli expId=<id> stop all
    jd_worker_cli expId=<id> stop <worker-id>

    # Management (see docs/jd-worker.md)
    jd_worker_cli version
    jd_worker_cli health [expId=<id>]
    jd_worker_cli expId=<id> exp-status
    jd_worker_cli expId=<id> worker-status <worker-id>
    jd_worker_cli expId=<id> worker-logs <worker-id> [lines=N] [follow=true]
    jd_worker_cli expId=<id> server-info
    jd_worker_cli expId=<id> where
    jd_worker_cli expId=<id> show-config <worker-id>
    jd_worker_cli expId=<id> restart all|<worker-id>
    jd_worker_cli expId=<id> scale num_workers=<N>
    jd_worker_cli expId=<id> drain
    jd_worker_cli expId=<id> stop job=<job-id>
    jd_worker_cli expId=<id> confirm-stop
    jd_worker_cli stop all-experiments
    jd_worker_cli prune

    # Interactive shell (mysql-style)
    jd_worker_cli
    jd_worker_cli interactive
    jd_worker_cli -i

    Interactive mode requires a valid Hub API key before the shell starts
    (from JD_API_KEY / api_key=, or prompted). The key is verified with the Hub.

Required (start)
----------------
    expId=<id>              Experiment identifier (must match the server).
    entry_script=<path>     Python script to run for each job.

Authentication
--------------
    api_key=<key>           Your API key from the Hub (env: JD_API_KEY).

    The worker connects to https://hub.jobdistributor.net by default.
    Just supply your API key — the server URL is discovered automatically:

        jd_worker_cli expId=mnist-v1 entry_script=train.py api_key=jd_xxxx

    Recommended: set the key once as an environment variable:
        export JD_API_KEY=jd_xxxx
        jd_worker_cli expId=mnist-v1 entry_script=train.py

    To use a self-hosted Hub, override with:
        hub=<url>           Hub base URL (env: JD_HUB_URL).
        jd_worker_cli expId=mnist-v1 entry_script=train.py \\
                  hub=https://my-hub.example.com api_key=jd_xxxx

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
    foreground=true         Run in the foreground (attached to terminal) instead
                            of the default background mode.
    once=true               Exit after completing a single job (or when no
                            job is available).  Without this flag the worker
                            keeps running and probes every 3 minutes when the
                            queue is empty or the server is unreachable.

    Local job data lives under ``<parent>/jd_data/<expId>/<job_id>/``.
    ``parent`` is ``JD_WORKSPACE_PATH`` if set, otherwise ``~``.

    Worker registry (SQLite) lives under ``<cache>/.cache/<expId>/workers.db``.
    ``cache`` is ``JD_CACHE_PATH`` if set, otherwise ``~/.jd_cache``.
    On HPC, set ``JD_CACHE_PATH`` to node-local scratch (e.g. ``/tmp/.jd_cache``)
    and keep ``JD_WORKSPACE_PATH`` on shared storage for large job I/O.

Install
-------
    pip install jd-worker
"""

from __future__ import annotations

import json
import logging
import os
import platform
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Optional, Tuple
from urllib.parse import urlparse, urlunparse

import psutil
import requests

from jd import __version__
from jd.auth import WorkerTokenManager
from jd.worker_registry import (
    WorkerRegistry,
    host_from_worker_id,
    new_worker_id as new_registry_worker_id,
    registry_db_path,
    resolve_cache_parent,
    resolve_workspace_parent,
    resolve_workspace_path,
)

IS_WINDOWS = platform.system() == "Windows"
BUSY_HEARTBEAT_INTERVAL = 57   # seconds while a job runs (also refreshes the job row)
IDLE_POLL_INTERVAL = 180         # seconds when idle — heartbeat + optional job assignment

# Default local layout: ~/jd_data/<expId>/<job_id>/ and ~/.jd_cache/.cache/<expId>/workers.db


# ── Argument parsing ─────────────────────────────────────────────────────────

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

    parent = resolve_workspace_parent()
    cache_parent = resolve_cache_parent()
    workspace_path = resolve_workspace_path()
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
        'workspace_parent': parent,
        'cache_parent':     cache_parent,
        'log_dir_override': log_override,
        'machine_type':     get('machine_type', 'JD_MACHINE_TYPE', 'worker'),
        'process_id':       get('process_id',   None,              '0'),
        'num_workers':      int(get('num_workers', 'JD_NUM_WORKERS', '1')),
        'once':             get('once',         'JD_ONCE',         'false').lower() == 'true',
        'foreground':       get('foreground',   'JD_FOREGROUND',   'false').lower() == 'true',
        # Hub authentication — defaults to the public hub; override with hub= or JD_HUB_URL
        'hub_url':          (cfg.get('hub') or get('hub_url', 'JD_HUB_URL',
                             'https://hub.jobdistributor.net')).strip().rstrip('/'),
        'api_key':          get('api_key',      'JD_API_KEY',      '').strip(),
    }


# ── Logging ───────────────────────────────────────────────────────────────────

def _setup_logger(log_dir: str, worker_id: str, *, to_stdout: bool = True) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"jd_worker_{worker_id}.log")

    logger = logging.getLogger(f'jd_worker_cli.{worker_id}')
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s")
    fh  = logging.FileHandler(log_path, encoding='utf-8')
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if to_stdout:
        sh  = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    logger.propagate = False
    return logger, log_path


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


# ── Server communication ──────────────────────────────────────────────────────

def _worker_heartbeat(
    url: str,
    worker_id: str,
    host: str,
    machine_type: str,
    reported_status: str,
    current_job_id: Optional[int],
    applied_version: int,
    metrics: dict,
    logger: logging.Logger,
    token_mgr: Optional[WorkerTokenManager] = None,
):
    """POST /worker/heartbeat — liveness, control channel, optional job assignment."""
    try:
        headers = token_mgr.auth_headers() if token_mgr else {}
        payload = {
            "worker_id": worker_id,
            "host": host,
            "machine_type": machine_type,
            "reported_status": reported_status,
            "current_job_id": current_job_id,
            "applied_version": applied_version,
            "system_metrics": metrics,
            "jd_worker_version": __version__,
        }
        r = requests.post(url, json=payload, headers=headers, timeout=30)
        if r.status_code == 200:
            return r.json(), None
        return None, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as exc:
        return None, str(exc)


def _apply_server_control(
    heartbeat_resp: dict,
    applied_version: int,
    registry: Optional[WorkerRegistry],
    logger: logging.Logger,
) -> Tuple[int, bool, bool]:
    """Apply desired_state from heartbeat response.

    Returns (new_applied_version, exit_immediately, stop_after_current_job).
    """
    desired_state = heartbeat_resp.get("desired_state", "run")
    desired_version = int(heartbeat_resp.get("desired_version") or 0)
    if desired_version <= applied_version:
        return applied_version, False, False

    if desired_state == "run":
        if registry:
            registry.set_drained(False)
        logger.info(f"Server control: resume (v{desired_version})")
        return desired_version, False, False

    if desired_state == "pause":
        if registry:
            registry.set_drained(False)
        logger.info(
            f"Server control: pause — finish current job, stay idle without new jobs "
            f"(v{desired_version})"
        )
        return desired_version, False, False

    if desired_state == "drain":
        if registry:
            registry.set_drained(True)
        logger.info(f"Server control: drain — finish current job, no new jobs (v{desired_version})")
        return desired_version, False, False

    if desired_state == "stop":
        if registry:
            registry.set_drained(True)
        logger.info(f"Server control: stop (v{desired_version})")
        return desired_version, True, False

    return desired_version, False, False


def _kill_worker_proc(proc_ref: list, logger: logging.Logger) -> None:
    """Terminate the running job subprocess (used when stop arrives mid-job)."""
    p = proc_ref[0] if proc_ref else None
    if not p or p.poll() is not None:
        return
    logger.info(f"Stop command — terminating job subprocess PID {p.pid}…")
    try:
        if IS_WINDOWS:
            p.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
    except Exception as exc:
        logger.warning(f"Failed to kill job subprocess: {exc}")


def _heartbeat_loop(
    heartbeat_url: str,
    worker_id: str,
    host: str,
    machine_type: str,
    job_id: int,
    stop_event: threading.Event,
    control: dict,
    logger: logging.Logger,
    proc_ref: Optional[list] = None,
    token_mgr: Optional[WorkerTokenManager] = None,
    registry: Optional[WorkerRegistry] = None,
) -> None:
    """Background heartbeat via POST /worker/heartbeat while a job runs."""
    while not stop_event.wait(BUSY_HEARTBEAT_INTERVAL):
        try:
            if token_mgr:
                token_mgr.ensure_fresh()
            metrics = _collect_metrics(machine_type, logger)
            resp, err = _worker_heartbeat(
                heartbeat_url, worker_id, host, machine_type,
                "busy", job_id, control["applied_version"], metrics,
                logger, token_mgr=token_mgr,
            )
            if resp is None:
                logger.warning(f"Heartbeat failed (job {job_id}): {err}")
                continue
            av, exit_now, stop_after = _apply_server_control(
                resp, control["applied_version"], registry, logger,
            )
            control["applied_version"] = av
            if stop_after:
                control["stop_after_job"] = True
            if exit_now:
                control["exit_now"] = True
                if proc_ref is not None:
                    _kill_worker_proc(proc_ref, logger)
        except Exception as exc:
            logger.warning(f"Heartbeat error (job {job_id}): {exc}")


def _update_status(url: str, job_id: int, status: str,
                   message: str, logger: logging.Logger,
                   token_mgr: Optional[WorkerTokenManager] = None) -> None:
    try:
        headers = token_mgr.auth_headers() if token_mgr else {}
        r = requests.post(url, json={"job_id": job_id,
                                     "status":  status,
                                     "message": message},
                          headers=headers,
                          timeout=30)
        if r.status_code == 200:
            logger.info(f"Job {job_id} → {status}")
        else:
            logger.warning(f"Status update failed: HTTP {r.status_code}")
    except Exception as exc:
        logger.error(f"Status update error: {exc}")


def _cfg_for_storage(cfg: dict) -> str:
    return json.dumps({k: v for k, v in cfg.items() if not str(k).startswith('_')})


# ── Main entry point ──────────────────────────────────────────────────────────

def _run_worker(cfg: dict) -> None:
    """Run a single worker loop.  Called directly for foreground mode, or
    launched as a background/internal subprocess."""

    worker_id = (cfg.get('worker_id') or '').strip()
    if not worker_id:
        worker_id = new_registry_worker_id(
            slot=0, exp_id=cfg['exp_id'], parent=cfg.get('cache_parent'),
        )

    daemon_mode = cfg.get('_daemon', False)
    if cfg['log_dir_override'] is not None:
        log_dir = os.path.join(cfg['log_dir_override'], cfg['exp_id'])
    else:
        log_dir = os.path.join(cfg['workspace_path'], cfg['exp_id'], 'jd_worker_logs')
    logger, log_path = _setup_logger(log_dir, worker_id, to_stdout=not daemon_mode)

    registry: Optional[WorkerRegistry] = None
    config_json = _cfg_for_storage(cfg)
    if cfg.get('_register'):
        registry = WorkerRegistry(cfg['exp_id'], cfg['cache_parent'])
        registry.register(
            worker_id=worker_id,
            pid=os.getpid(),
            process_id=int(cfg.get('process_id', 0)),
            runner_id=worker_id,
            entry_script=cfg['entry_script'],
            machine_type=cfg['machine_type'],
            log_path=log_path,
            config_json=config_json,
        )

    # ── Hub authentication (optional) ────────────────────────────────────────
    # WorkerTokenManager refreshes the JWT and persists it in workers.db (worker_token).
    token_mgr: Optional[WorkerTokenManager] = None
    if cfg['hub_url'] and cfg['api_key']:
        logger.info(f"Hub mode: authenticating via {cfg['hub_url']}")
        token_mgr = WorkerTokenManager(
            cfg['hub_url'],
            cfg['api_key'],
            cfg['exp_id'],
            logger,
            initial_token=cfg.get('_worker_token', ''),
        )
        if cfg.get('_worker_token'):
            token_mgr.ensure_fresh()
        elif not token_mgr.refresh_now():
            logger.error("Failed to obtain worker token from Hub. Exiting.")
            sys.exit(1)
        if token_mgr.last_server_url:
            cfg['base_url'] = token_mgr.last_server_url
            logger.info(f"Server URL from Hub: {token_mgr.last_server_url}")
        if registry:
            token_mgr.set_token_registry(registry, worker_id)
    elif cfg['hub_url'] or cfg['api_key']:
        logger.warning(
            "Both hub_url (JD_HUB_URL) and api_key (JD_API_KEY) must be set "
            "for Hub authentication. Running in standalone mode."
        )

    urls = {
        'heartbeat': f"{cfg['base_url']}/worker/heartbeat",
        'update':    f"{cfg['base_url']}/update_job_status",
    }

    host = host_from_worker_id(worker_id)
    applied_version = 0

    logger.info(f"jd_worker_cli v{__version__}  |  worker_id: {worker_id}")
    logger.info(f"Server:        {cfg['base_url']}")
    logger.info(f"Entry script:   {cfg['entry_script']}")
    logger.info(f"Hub mode:       {'enabled' if token_mgr else 'disabled'}")
    logger.info(f"Local jd_data root: {cfg['workspace_path']} "
                f"(each job: …/jd_data/<expId>/<job_id>/)")
    logger.info(f"Registry DB:     {registry_db_path(cfg['exp_id'], cfg['cache_parent'])}")
    if cfg['once']:
        logger.info("Mode: single job (once=true)")
    else:
        logger.info(
            f"Heartbeat: {IDLE_POLL_INTERVAL}s idle / "
            f"{BUSY_HEARTBEAT_INTERVAL}s while running a job (POST /worker/heartbeat)"
        )

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
        if token_mgr:
            token_mgr.clear_token_store()
        if registry:
            registry.unregister(worker_id)
        logger.info("jd_worker_cli shut down.")
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # ── Main job loop ────────────────────────────────────────────────────────
    while True:
        if registry and registry.is_drained():
            logger.info("Experiment is draining — exiting worker.")
            break

        job_id = None
        stop_heartbeat = threading.Event()
        heartbeat = None
        control = {"applied_version": applied_version, "stop_after_job": False}
        try:
            if token_mgr:
                token_mgr.ensure_fresh()

            metrics = _collect_metrics(cfg['machine_type'], logger)
            job = None
            reason = "no_jobs"
            poll_interval = IDLE_POLL_INTERVAL

            hb_resp, reason = _worker_heartbeat(
                urls['heartbeat'], worker_id, host, cfg['machine_type'],
                "idle", None, applied_version, metrics, logger,
                token_mgr=token_mgr,
            )
            if hb_resp is None:
                logger.error(f"Idle heartbeat failed: {reason}.")
                time.sleep(IDLE_POLL_INTERVAL)
                continue

            poll_interval = int(
                hb_resp.get("heartbeat_interval")
                or hb_resp.get("poll_interval")
                or IDLE_POLL_INTERVAL
            )
            applied_version, exit_now, _ = _apply_server_control(
                hb_resp, applied_version, registry, logger,
            )
            control["applied_version"] = applied_version
            if exit_now:
                logger.info("Stop command applied — acknowledging and exiting.")
                _worker_heartbeat(
                    urls['heartbeat'], worker_id, host, cfg['machine_type'],
                    "idle", None, applied_version, metrics, logger,
                    token_mgr=token_mgr,
                )
                break
            if hb_resp.get("job"):
                job = hb_resp["job"]
            else:
                reason = "no_jobs"

            if job is None:
                if reason == "no_jobs" and cfg["once"]:
                    logger.info("No jobs available (once=true). Exiting.")
                    break
                if hb_resp.get("desired_state") == "pause":
                    logger.info(
                        f"Worker paused — no new jobs until resume. "
                        f"Next heartbeat in {poll_interval}s…"
                    )
                elif reason == "no_jobs":
                    logger.info(
                        f"No jobs available. Next idle heartbeat in {poll_interval}s…"
                    )
                time.sleep(poll_interval)
                continue

            job_id = job['job_id']
            params = job['parameters']
            logger.info(f"Job {job_id} received  |  params: {params}")
            if registry:
                registry.set_job(worker_id, job_id)

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

            # Background heartbeat: poll (busy) updates worker + job on the server
            heartbeat = threading.Thread(
                target=_heartbeat_loop,
                args=(
                    urls["heartbeat"], worker_id, host, cfg["machine_type"],
                    job_id, stop_heartbeat, control, logger, _proc,
                ),
                kwargs={"token_mgr": token_mgr, "registry": registry},
                daemon=True,
            )
            heartbeat.start()

            # Build child environment: inherit everything + inject JD_ context
            # so jd_upload / jd_update_checkpoint / jd_get_last_checkpoint work
            # inside the entry script without requiring explicit arguments.
            child_env = os.environ.copy()
            child_env["JD_JOB_ID"]                  = str(job_id)
            child_env["JD_SERVER"]                  = cfg["base_url"]
            child_env["JD_EXP_ID"]                  = cfg["exp_id"]
            child_env["JD_WORKER_JOB_DIR"]          = job_root
            child_env["JD_WORKER_WORKSPACE_ROOT"]   = cfg["workspace_path"]
            child_env["JD_WORKSPACE_PATH"]        = cfg["workspace_parent"]
            if cfg["cache_parent"] != cfg["workspace_parent"]:
                child_env["JD_CACHE_PATH"] = cfg["cache_parent"]
            if token_mgr:
                child_env["JD_WORKER_ID"]         = worker_id
                child_env["JD_WORKER_TOKEN"]      = token_mgr.get_token()

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

            if control.get("exit_now") and job_id is not None:
                logger.info(f"Job {job_id} aborted — dashboard stop command.")
                _update_status(
                    urls['update'], job_id, 'ABORTED',
                    f"Job aborted on {worker_id}: dashboard stop command.",
                    logger, token_mgr=token_mgr,
                )
                _worker_heartbeat(
                    urls['heartbeat'], worker_id, host, cfg['machine_type'],
                    "idle", None, control["applied_version"], metrics, logger,
                    token_mgr=token_mgr,
                )
                _proc[0] = None
                if registry:
                    registry.set_job(worker_id, None)
                break

            if rc == 0:
                logger.info(f"Job {job_id} finished successfully.")
                _update_status(
                    urls['update'], job_id, 'DONE',
                    f"Completed successfully on {worker_id}.",
                    logger, token_mgr=token_mgr,
                )
            else:
                logger.error(f"Job {job_id} failed — exit code {rc}")
                if stderr.strip():
                    logger.error(f"STDERR (last 1000 chars):\n{stderr.strip()[-1000:]}")

                abort_msg = (
                    f"Job failed on {worker_id}. "
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
                               token_mgr=token_mgr)

            _proc[0] = None
            if registry:
                registry.set_job(worker_id, None)

            applied_version = control["applied_version"]
            if control["stop_after_job"]:
                logger.info("Stop/drain command received during job — exiting.")
                break

        except Exception as exc:
            logger.exception(f"Unexpected error: {exc}")
            if job_id is not None:
                _update_status(
                    urls['update'], job_id, 'ABORTED',
                    f"Unexpected exception on {worker_id}: {exc}",
                    logger, token_mgr=token_mgr,
                )
            break
        finally:
            stop_heartbeat.set()
            if heartbeat is not None:
                heartbeat.join(timeout=5)

        if cfg['once']:
            logger.info("once=true — exiting after one job.")
            break

        time.sleep(3)   # brief pause before requesting the next job

    if token_mgr:
        token_mgr.clear_token_store()
    if registry:
        registry.unregister(worker_id)


def _spawn_background_worker(cfg: dict, worker_id: str, process_id: int) -> int:
    """Start one detached worker process; return its PID."""
    child_cfg = dict(cfg)
    child_cfg['worker_id'] = worker_id
    child_cfg['process_id'] = str(process_id)
    child_cfg['num_workers'] = 1
    child_cfg['_daemon'] = True
    child_cfg['_register'] = True
    child_cfg.pop('_launch_slot', None)

    env = os.environ.copy()
    env['JD_WORKER_CFG_JSON'] = json.dumps(child_cfg)
    env['JD_WORKER_INTERNAL'] = '1'

    popen_kw = dict(
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if IS_WINDOWS:
        popen_kw['creationflags'] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        popen_kw['start_new_session'] = True

    proc = subprocess.Popen(
        [sys.executable, "-c",
         "from jd.worker import _internal_worker_entry; _internal_worker_entry()"],
        **popen_kw,
    )
    return proc.pid


def _launch_workers(cfg: dict, kv: dict) -> None:
    """Start one or more workers (background by default)."""
    num_workers = cfg.get('num_workers', 1)
    foreground = cfg.get('foreground', False)

    if num_workers > 1 and kv.get('process_id') is not None:
        print("Warning: process_id is ignored when num_workers > 1. "
              "IDs are assigned automatically (0 … N-1).")

    # Hub authentication once before spawning so children inherit the token.
    if cfg['hub_url'] and cfg['api_key']:
        dummy_logger = logging.getLogger("jd_worker_cli.launcher")
        dummy_logger.handlers.clear()
        dummy_logger.addHandler(logging.StreamHandler())
        dummy_logger.setLevel(logging.INFO)
        if num_workers > 1 or not foreground:
            dummy_logger.info(
                f"Hub mode: authenticating once for {num_workers} worker(s) …"
            )
        launcher_mgr = WorkerTokenManager(
            cfg['hub_url'], cfg['api_key'], cfg['exp_id'], dummy_logger,
        )
        if not launcher_mgr.refresh_now():
            print("Error: failed to obtain worker token from Hub.")
            sys.exit(1)
        cfg['_worker_token'] = launcher_mgr.get_token()
        if launcher_mgr.last_server_url:
            cfg['base_url'] = launcher_mgr.last_server_url

    if foreground:
        if num_workers == 1:
            cfg['_register'] = True
            cfg['worker_id'] = cfg.get('worker_id') or new_registry_worker_id(
                slot=0, exp_id=cfg['exp_id'], parent=cfg.get('cache_parent'),
            )
            _run_worker(cfg)
            return

        procs = []
        for i in range(num_workers):
            child_cfg = dict(cfg)
            child_cfg['process_id'] = str(i)
            child_cfg['num_workers'] = 1
            child_cfg['worker_id'] = new_registry_worker_id(
                slot=i, exp_id=cfg['exp_id'], parent=cfg.get('cache_parent'),
            )
            child_cfg['_register'] = True
            env = os.environ.copy()
            env['JD_WORKER_CFG_JSON'] = json.dumps(child_cfg)
            env['JD_WORKER_INTERNAL'] = '1'
            p = subprocess.Popen(
                [sys.executable, "-c",
                 "from jd.worker import _internal_worker_entry; _internal_worker_entry()"],
                env=env,
            )
            procs.append(p)

        def _kill_all(signum=None, frame=None):
            for p in procs:
                try:
                    p.terminate()
                except Exception:
                    pass
            sys.exit(0)

        signal.signal(signal.SIGINT, _kill_all)
        signal.signal(signal.SIGTERM, _kill_all)
        for p in procs:
            p.wait()
        return

    # Default: detach all workers and exit the launcher immediately.
    started = []
    base_process_id = int(cfg.get('process_id', 0))
    for i in range(num_workers):
        process_id = i if num_workers > 1 else base_process_id
        wid = new_registry_worker_id(
            slot=i if num_workers > 1 else 0,
            exp_id=cfg['exp_id'],
            parent=cfg.get('cache_parent'),
        )
        pid = _spawn_background_worker(cfg, wid, process_id)
        started.append((wid, pid))

    print(f"Started {len(started)} background worker(s) for experiment '{cfg['exp_id']}':")
    for wid, pid in started:
        print(f"  worker_id={wid}  pid={pid}")
    print(f"Logs: …/jd_data/{cfg['exp_id']}/jd_worker_logs/")
    print(f"List: jd_worker_cli expId={cfg['exp_id']} worker-list")
    print(f"All:  jd_worker_cli exp-list")
    print(f"Stop: jd_worker_cli expId={cfg['exp_id']} stop all")


def _internal_worker_entry() -> None:
    """Entry point for background / multi-worker child processes."""
    raw = os.environ.get("JD_WORKER_CFG_JSON", "")
    if not raw:
        sys.exit("JD_WORKER_CFG_JSON not set — internal error")
    cfg = json.loads(raw)
    _run_worker(cfg)


def _worker_subprocess_entry() -> None:
    """Legacy entry — delegates to _internal_worker_entry."""
    _internal_worker_entry()


def main() -> None:
    if os.environ.get('JD_WORKER_INTERNAL') == '1':
        _internal_worker_entry()
        return

    argv = sys.argv[1:]

    skip_prune = (
        os.environ.get("JD_SKIP_REGISTRY_PRUNE") == "1"
        or any(a in argv for a in ("help", "-h", "--help"))
    )
    if not skip_prune:
        from jd.registry_prune import ensure_registry_pruned
        ensure_registry_pruned()

    if not argv:
        from jd.worker_repl import run_repl
        run_repl()
        return

    if argv[0] in ('interactive', '-i', '--interactive'):
        from jd.worker_repl import run_repl
        run_repl(argv[1:])
        return

    if any(a in argv for a in ('help', '-h', '--help')):
        print(__doc__)
        sys.exit(0)

    from jd.worker_commands import dispatch
    dispatch(argv)


if __name__ == '__main__':
    main()
