"""jd_worker_cli management subcommands."""

from __future__ import annotations

import json
import os
import platform
import signal
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import psutil
import requests

from jd import __version__
from jd.auth import WorkerTokenManager, fetch_worker_token
from jd.worker_registry import (
    WorkerRegistry,
    cache_root,
    exp_cache_dir,
    host_from_worker_id,
    list_all_experiments,
    new_worker_id,
    prune_all,
    registry_db_path,
    slot_from_worker_id,
)


def _cache_parent() -> Optional[str]:
    return os.environ.get("JD_WORKSPACE_PATH", "").strip() or None


def _resolve_exp_id(kv: dict) -> Optional[str]:
    exp_id = (kv.get("expId") or os.environ.get("JD_EXP_ID") or "").strip().lower()
    return exp_id or None


def _format_ts(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError):
        return str(ts)


def _cfg_for_storage(cfg: dict) -> str:
    return json.dumps({k: v for k, v in cfg.items() if not str(k).startswith("_")})


def _stop_worker_pid(
    worker_id: str,
    pid: int,
    registry: WorkerRegistry,
    *,
    exp_id: str,
    kv: dict,
    row: Optional[dict] = None,
    cli_action: str = "stop",
    skip_server_notify: bool = False,
) -> bool:
    if not skip_server_notify:
        from jd.worker_server_sync import notify_cli_worker_stop

        if row is None:
            row = registry.get(worker_id)
        job_id = row.get("current_job_id") if row else None
        notify_cli_worker_stop(exp_id, kv, worker_id, job_id, action=cli_action)

    registry.mark_stopping(worker_id)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        registry.unregister(worker_id)
        return False
    except PermissionError:
        print(f"Error: no permission to stop worker {worker_id} (pid {pid}).")
        return False

    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            registry.unregister(worker_id)
            return True
        time.sleep(0.5)

    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    registry.unregister(worker_id)
    return True


def _spawn_from_config(cfg: dict, worker_id: str, process_id: int) -> int:
    from jd.worker import _spawn_background_worker

    return _spawn_background_worker(cfg, worker_id, process_id)


def _launch_workers(cfg: dict, kv: dict) -> None:
    from jd.worker import _launch_workers

    _launch_workers(cfg, kv)


def _resolve_cfg(kv: dict) -> dict:
    from jd.worker import _resolve

    return _resolve(kv)


def _hub_server_url(exp_id: str, kv: dict) -> tuple[str, str]:
    """Return (hub_url, server_url) using env / kv / Hub token fetch."""
    cfg = _resolve_cfg({**kv, "expId": exp_id})
    hub = cfg.get("hub_url") or ""
    api_key = cfg.get("api_key") or ""
    server = cfg.get("base_url") or ""
    if hub and api_key:
        data = fetch_worker_token(hub, api_key, exp_id)
        if data and data.get("server_url"):
            server = data["server_url"]
    return hub, server


def _worker_token_headers(exp_id: str, kv: dict) -> dict:
    hub, _ = _hub_server_url(exp_id, kv)
    api_key = (kv.get("api_key") or os.environ.get("JD_API_KEY") or "").strip()
    if not hub or not api_key:
        return {}
    data = fetch_worker_token(hub, api_key, exp_id)
    if not data:
        return {}
    return {"Authorization": f"Bearer {data['token']}"}


def cmd_version() -> None:
    print(f"jd-worker {__version__}")
    print(f"Python    {sys.version.split()[0]} ({sys.executable})")
    print(f"Platform  {platform.platform()}")
    print(f"Cache     {os.path.join(cache_root(_cache_parent()), '.cache')}")


def cmd_health(kv: dict) -> None:
    exp_id = _resolve_exp_id(kv)
    hub = (kv.get("hub") or os.environ.get("JD_HUB_URL") or "https://hub.jobdistributor.net").strip()
    server = (kv.get("server") or os.environ.get("JD_SERVER") or "").strip()
    api_key = (kv.get("api_key") or os.environ.get("JD_API_KEY") or "").strip()

    if exp_id:
        cfg = _resolve_cfg(kv)
        hub = (cfg.get("hub_url") or hub).strip()
        server = (cfg.get("base_url") or server).strip()
        api_key = (cfg.get("api_key") or api_key).strip()
        if hub and api_key:
            _, server = _hub_server_url(exp_id, kv)

    print("Health check:")
    ok = True

    if hub:
        try:
            if exp_id and api_key:
                url = f"{hub.rstrip('/')}/api/experiments/{exp_id}/heartbeat"
                r = requests.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=15,
                )
                status = "OK" if r.status_code == 200 else f"HTTP {r.status_code}"
                if r.status_code != 200:
                    ok = False
            else:
                r = requests.get(hub.rstrip("/"), timeout=10)
                status = "OK" if r.status_code < 500 else f"HTTP {r.status_code}"
                if r.status_code >= 500:
                    ok = False
            print(f"  Hub ({hub}): {status}")
        except requests.RequestException as exc:
            print(f"  Hub ({hub}): FAIL — {exc}")
            ok = False
    else:
        print("  Hub: not configured")

    if server:
        try:
            headers = _worker_token_headers(exp_id, kv) if exp_id else {}
            r = requests.get(f"{server.rstrip('/')}/job_counts", timeout=15, headers=headers)
            if r.status_code == 404:
                r = requests.get(server.rstrip("/"), timeout=10)
            status = "OK" if r.status_code < 500 else f"HTTP {r.status_code}"
            if r.status_code >= 500:
                ok = False
            print(f"  Server ({server}): {status}")
        except requests.RequestException as exc:
            print(f"  Server ({server}): FAIL — {exc}")
            ok = False
    else:
        print("  Server: not configured (set expId + JD_API_KEY or server=)")

    sys.exit(0 if ok else 1)


def cmd_server_info(kv: dict) -> None:
    exp_id = _resolve_exp_id(kv)
    if not exp_id:
        print("Usage: jd_worker_cli expId=<id> server-info")
        sys.exit(1)

    _, server = _hub_server_url(exp_id, kv)
    if not server:
        print("Error: could not resolve server URL.")
        sys.exit(1)

    headers = _worker_token_headers(exp_id, kv)
    try:
        r = requests.get(f"{server.rstrip('/')}/job_counts", headers=headers, timeout=30)
    except requests.RequestException as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    if r.status_code == 401:
        print("Error: unauthorized — check JD_API_KEY / worker token.")
        sys.exit(1)
    if r.status_code != 200:
        print(f"Error: server returned HTTP {r.status_code}")
        sys.exit(1)

    counts = r.json()
    print(f"Job server: {server}")
    print(f"Experiment: {exp_id}")
    print(f"{'STATUS':<12} {'COUNT':>8}")
    print("-" * 22)
    total = 0
    for status in ("PENDING", "SERVED", "DONE", "ABORTED", "DELETED"):
        n = int(counts.get(status, 0))
        total += n
        print(f"{status:<12} {n:>8}")
    print("-" * 22)
    print(f"{'Total':<12} {total:>8}")


def cmd_where(kv: dict) -> None:
    exp_id = _resolve_exp_id(kv)
    if not exp_id:
        print("Usage: jd_worker_cli expId=<id> where")
        sys.exit(1)

    parent = _cache_parent()
    cfg = _resolve_cfg({**kv, "expId": exp_id})
    print(f"Experiment:     {exp_id}")
    print(f"Cache root:     {cache_root(parent)}")
    print(f"Registry DB:    {registry_db_path(exp_id, parent)}")
    print(f"Experiment dir: {exp_cache_dir(exp_id, parent)}")
    print(f"jd_data root:   {cfg['workspace_path']}")
    print(f"Job data:       {os.path.join(cfg['workspace_path'], exp_id)}")
    if cfg.get("log_dir_override"):
        print(f"Logs:           {os.path.join(cfg['log_dir_override'], exp_id)}")
    else:
        print(f"Logs:           {os.path.join(cfg['workspace_path'], exp_id, 'jd_worker_logs')}")


def cmd_show_config(kv: dict, worker_id: str) -> None:
    exp_id = _resolve_exp_id(kv)
    if not exp_id or not worker_id:
        print("Usage: jd_worker_cli expId=<id> show-config <worker-id>")
        sys.exit(1)

    registry = WorkerRegistry(exp_id, _cache_parent())
    row = registry.get(worker_id)
    if not row:
        print(f"Worker '{worker_id}' not found.")
        sys.exit(1)

    config = registry.get_config(worker_id) or {}
    print(f"Worker: {worker_id}  (pid {row['pid']}, status {row['status']})")
    for key in sorted(config):
        print(f"  {key}: {config[key]}")


def cmd_worker_status(kv: dict, worker_id: str) -> None:
    exp_id = _resolve_exp_id(kv)
    if not exp_id or not worker_id:
        print("Usage: jd_worker_cli expId=<id> worker-status <worker-id>")
        sys.exit(1)

    registry = WorkerRegistry(exp_id, _cache_parent())
    row = registry.get(worker_id)
    if not row:
        print(f"Worker '{worker_id}' not found.")
        sys.exit(1)

    uptime = time.time() - row["started_at"]
    job = row.get("current_job_id")
    print(f"worker_id:      {row['worker_id']}")
    print(f"host:           {host_from_worker_id(row['worker_id'])}")
    print(f"pid:            {row['pid']}")
    print(f"status:         {row['status']}")
    print(f"entry_script:   {row['entry_script']}")
    print(f"machine_type:   {row['machine_type']}")
    print(f"started:        {_format_ts(row['started_at'])}")
    print(f"uptime:         {int(uptime // 3600)}h {int((uptime % 3600) // 60)}m")
    print(f"current_job:    {job if job is not None else '—'}")
    print(f"last_ping:      {_format_ts(row.get('last_ping_at'))}")
    print(f"log:            {row['log_path']}")
    if registry.is_drained():
        print("experiment:     DRAINING (no new jobs after current finish)")


def cmd_worker_logs(kv: dict, worker_id: str) -> None:
    exp_id = _resolve_exp_id(kv)
    if not exp_id or not worker_id:
        print("Usage: jd_worker_cli expId=<id> worker-logs <worker-id> [lines=N] [follow=true]")
        sys.exit(1)

    lines = int(kv.get("lines", "50"))
    follow = kv.get("follow", "false").lower() == "true"

    registry = WorkerRegistry(exp_id, _cache_parent())
    row = registry.get(worker_id)
    if not row:
        print(f"Worker '{worker_id}' not found.")
        sys.exit(1)

    path = row["log_path"]
    if not os.path.isfile(path):
        print(f"Log file not found: {path}")
        sys.exit(1)

    if follow:
        try:
            subprocess = __import__("subprocess")
            subprocess.run(["tail", "-f", "-n", str(lines), path], check=False)
        except FileNotFoundError:
            print("tail not found — showing last lines without follow.")
            follow = False

    if not follow:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.readlines()
        for line in content[-lines:]:
            print(line, end="")


def cmd_exp_status(kv: dict) -> None:
    exp_id = _resolve_exp_id(kv)
    if not exp_id:
        print("Usage: jd_worker_cli expId=<id> exp-status")
        sys.exit(1)

    registry = WorkerRegistry(exp_id, _cache_parent())
    workers = registry.list_workers(prune_dead=True)
    drained = registry.is_drained()

    busy = sum(1 for w in workers if w.get("status") == "busy")
    idle = sum(1 for w in workers if w.get("status") != "busy")

    print(f"Experiment:  {exp_id}")
    print(f"Draining:    {'yes' if drained else 'no'}")
    print(f"Workers:     {len(workers)} total ({busy} busy, {idle} idle)")
    if workers:
        print(f"{'WORKER_ID':<14} {'STATUS':<10} {'JOB':<8} {'PID':<8}")
        print("-" * 44)
        for w in workers:
            job = w.get("current_job_id")
            job_s = str(job) if job is not None else "—"
            print(f"{w['worker_id']:<14} {w['status']:<10} {job_s:<8} {w['pid']:<8}")


def cmd_worker_list(kv: dict) -> None:
    exp_id = _resolve_exp_id(kv)
    if not exp_id:
        print("Usage: jd_worker_cli expId=<id> worker-list")
        sys.exit(1)

    registry = WorkerRegistry(exp_id, _cache_parent())
    workers = registry.list_workers(prune_dead=True)
    if not workers:
        print(f"No running workers for '{exp_id}'.")
        return

    print(f"Workers for '{exp_id}':")
    print(f"{'WORKER_ID':<14} {'PID':<8} {'STATUS':<10} {'JOB':<8} {'STARTED':<20} ENTRY_SCRIPT")
    print("-" * 80)
    for row in workers:
        job = row.get("current_job_id")
        job_s = str(job) if job is not None else "—"
        print(
            f"{row['worker_id']:<14} {row['pid']:<8} {row['status']:<10} {job_s:<8} "
            f"{_format_ts(row['started_at']):<20} {row['entry_script']}"
        )


def cmd_exp_list() -> None:
    experiments = list_all_experiments(_cache_parent())
    if not experiments:
        print("No running workers on this machine.")
        return
    total = sum(r["worker_count"] for r in experiments)
    print(f"{'EXPERIMENT':<24} {'WORKERS':>8}")
    print("-" * 34)
    for row in experiments:
        print(f"{row['exp_id']:<24} {row['worker_count']:>8}")
    print("-" * 34)
    print(f"{'Total':<24} {total:>8}")


def _kv_for_experiment(exp_id: str, kv: dict) -> dict:
    """Merge CLI/env kv with stored launch config for Hub auth."""
    merged = dict(kv)
    merged["expId"] = exp_id
    registry = WorkerRegistry(exp_id, _cache_parent())
    for w in registry.list_workers(prune_dead=False):
        cfg = registry.get_config(w["worker_id"])
        if not cfg:
            continue
        if cfg.get("hub_url"):
            merged.setdefault("hub", cfg["hub_url"])
        if cfg.get("api_key"):
            merged.setdefault("api_key", cfg["api_key"])
        if cfg.get("base_url"):
            merged.setdefault("base_url", cfg["base_url"])
        break
    return merged


def _confirm_clear_all(active_count: int, kv: dict) -> bool:
    if kv.get("confirm-clear-all", "false").lower() == "true":
        return True
    if active_count:
        print(
            f"WARNING: {active_count} active worker(s) will be killed and "
            "running jobs aborted on the server when reachable."
        )
    try:
        typed = input("Type 'clear_all' to wipe ALL experiment cache on this machine: ").strip()
    except EOFError:
        print("Cancelled.")
        return False
    if typed != "clear_all":
        print("Cancelled — confirmation did not match.")
        return False
    return True


def cmd_clear_all(kv: dict) -> None:
    """Remove all local experiment cache; notify server for active workers first."""
    from jd.worker_registry import clear_all_local_cache, iter_experiment_registries
    from jd.worker_server_sync import notify_cli_clear_all

    parent = _cache_parent()
    active: List[Dict[str, Any]] = []
    for registry in iter_experiment_registries(parent):
        for row in registry.list_workers(prune_dead=True):
            active.append({
                "worker_id": row["worker_id"],
                "exp_id": registry.exp_id,
                "job_id": row.get("current_job_id"),
                "pid": row["pid"],
                "registry": registry,
                "row": row,
            })

    if not _confirm_clear_all(len(active), kv):
        return

    server_ok = True
    by_exp: Dict[str, List[Dict[str, Any]]] = {}
    for item in active:
        by_exp.setdefault(item["exp_id"], []).append(item)

    for exp_id, items in sorted(by_exp.items()):
        exp_kv = _kv_for_experiment(exp_id, kv)
        payload = [
            {"worker_id": i["worker_id"], "job_id": i.get("job_id")}
            for i in items
        ]
        if payload and not notify_cli_clear_all(payload, exp_kv):
            server_ok = False

    for item in active:
        exp_kv = _kv_for_experiment(item["exp_id"], kv)
        _stop_worker_pid(
            item["worker_id"],
            item["pid"],
            item["registry"],
            exp_id=item["exp_id"],
            kv=exp_kv,
            row=item["row"],
            cli_action="clear_all",
            skip_server_notify=True,
        )

    cleared = clear_all_local_cache(parent)
    if not server_ok:
        print(
            "Warning: job server could not be reached for some experiments; "
            "local cache was still cleared."
        )
    exps = cleared.get("experiments") or []
    if exps:
        print(f"Cleared local cache for {cleared['experiments_cleared']} experiment(s): {', '.join(exps)}")
    else:
        print("No experiment cache directories to clear.")


def _confirm_stop_all(exp_id: str, kv: dict, positionals: List[str]) -> bool:
    if kv.get("confirm-stop", "false").lower() != "true" and "confirm-stop" not in positionals:
        return True
    try:
        typed = input(f"Type experiment name '{exp_id}' to confirm stop all: ").strip().lower()
    except EOFError:
        print("Cancelled.")
        return False
    if typed != exp_id:
        print("Cancelled — name did not match.")
        return False
    return True


def cmd_stop(kv: dict, targets: List[str], positionals: List[str]) -> None:
    server_warned = False

    def _stop(row: dict, registry: WorkerRegistry, exp_id: str, action: str = "stop") -> bool:
        nonlocal server_warned
        exp_kv = _kv_for_experiment(exp_id, kv)
        from jd.worker_server_sync import notify_cli_worker_stop

        job_id = row.get("current_job_id")
        if not notify_cli_worker_stop(exp_id, exp_kv, row["worker_id"], job_id, action=action):
            if not server_warned:
                print(
                    "Note: job server unreachable or auth failed — "
                    "local registry will still be updated."
                )
                server_warned = True
        return _stop_worker_pid(
            row["worker_id"],
            row["pid"],
            registry,
            exp_id=exp_id,
            kv=exp_kv,
            row=row,
            cli_action=action,
            skip_server_notify=True,
        )

    if targets and targets[0].lower() == "all-experiments":
        stopped = 0
        for exp in list_all_experiments(_cache_parent()):
            sub_kv = dict(kv)
            sub_kv["expId"] = exp["exp_id"]
            cmd_stop(sub_kv, ["all"], positionals)
            stopped += exp["worker_count"]
        print(f"Stop all-experiments complete ({stopped} workers targeted).")
        return

    if kv.get("job") or (targets and targets[0].startswith("job=")):
        job_id = kv.get("job") or targets[0].split("=", 1)[1]
        exp_id = _resolve_exp_id(kv)
        if not exp_id:
            print("Usage: jd_worker_cli expId=<id> stop job=<job-id>")
            sys.exit(1)
        registry = WorkerRegistry(exp_id, _cache_parent())
        row = registry.find_by_job_id(int(job_id))
        if not row:
            print(f"No worker running job {job_id} for '{exp_id}'.")
            sys.exit(1)
        if _stop(row, registry, exp_id):
            print(f"Stopped worker {row['worker_id']} (job {job_id}, pid {row['pid']}).")
        return

    exp_id = _resolve_exp_id(kv)
    if not exp_id:
        print("Usage: jd_worker_cli expId=<id> stop all")
        print("       jd_worker_cli expId=<id> stop <worker-id>")
        print("       jd_worker_cli expId=<id> stop job=<job-id>")
        print("       jd_worker_cli stop all-experiments")
        sys.exit(1)

    if not targets:
        print("Usage: jd_worker_cli expId=<id> stop all| <worker-id>| job=<id>")
        sys.exit(1)

    target = targets[0].strip()
    registry = WorkerRegistry(exp_id, _cache_parent())

    if target.lower() == "all":
        if not _confirm_stop_all(exp_id, kv, positionals):
            return
        workers = registry.list_workers(prune_dead=True)
        if not workers:
            print(f"No running workers for '{exp_id}'.")
            return
        n = 0
        for row in workers:
            if _stop(row, registry, exp_id):
                print(f"Stopped {row['worker_id']} (pid {row['pid']}).")
                n += 1
        print(f"Stopped {n} worker(s).")
        return

    row = registry.get(target)
    if not row:
        print(f"Worker '{target}' not found.")
        sys.exit(1)
    if _stop(row, registry, exp_id):
        print(f"Stopped {target} (pid {row['pid']}).")


def cmd_drain(kv: dict) -> None:
    exp_id = _resolve_exp_id(kv)
    if not exp_id:
        print("Usage: jd_worker_cli expId=<id> drain")
        sys.exit(1)
    registry = WorkerRegistry(exp_id, _cache_parent())
    registry.set_drained(True)
    workers = registry.list_workers(prune_dead=True)
    print(f"Experiment '{exp_id}' marked draining.")
    print(f"{len(workers)} worker(s) will exit after their current job (if any).")


def cmd_prune() -> None:
    summary = prune_all(_cache_parent())
    print(f"Pruned {summary['workers_removed']} stale worker row(s).")
    print(f"Removed {summary['token_dirs_removed']} orphaned token dir(s).")


def _sample_launch_config(registry: WorkerRegistry) -> Optional[dict]:
    workers = registry.list_workers(prune_dead=False)
    for w in workers:
        cfg = registry.get_config(w["worker_id"])
        if cfg and cfg.get("entry_script"):
            return cfg
    return None


def cmd_restart(kv: dict, targets: List[str]) -> None:
    exp_id = _resolve_exp_id(kv)
    if not exp_id:
        print("Usage: jd_worker_cli expId=<id> restart all|<worker-id>")
        sys.exit(1)
    if not targets:
        print("Usage: jd_worker_cli expId=<id> restart all|<worker-id>")
        sys.exit(1)

    registry = WorkerRegistry(exp_id, _cache_parent())
    target = targets[0].strip().lower()

    if target == "all":
        workers = registry.list_workers(prune_dead=True)
    else:
        row = registry.get(target)
        workers = [row] if row else []

    if not workers:
        print("No workers to restart.")
        sys.exit(1)

    sample = _sample_launch_config(registry)
    if not sample:
        print("Error: no stored launch config — restart requires at least one worker row.")
        sys.exit(1)

    restarted = 0
    exp_kv = _kv_for_experiment(exp_id, kv)
    for row in workers:
        cfg = registry.get_config(row["worker_id"]) or sample
        wid = row["worker_id"]
        pid = row["pid"]
        process_id = int(row.get("process_id", 0))
        _stop_worker_pid(
            wid, pid, registry,
            exp_id=exp_id, kv=exp_kv, row=row, cli_action="restart",
        )
        time.sleep(0.5)
        slot = slot_from_worker_id(wid)
        new_cfg = dict(cfg)
        new_cfg["exp_id"] = exp_id
        new_cfg["worker_id"] = new_worker_id(slot=slot)
        new_cfg["process_id"] = str(process_id)
        new_cfg["num_workers"] = 1
        new_pid = _spawn_from_config(new_cfg, new_cfg["worker_id"], process_id)
        print(f"Restarted {wid} → {new_cfg['worker_id']} (pid {new_pid})")
        restarted += 1
    print(f"Restarted {restarted} worker(s).")


def cmd_scale(kv: dict) -> None:
    exp_id = _resolve_exp_id(kv)
    target = int(kv.get("num_workers", "0"))
    if not exp_id or target < 1:
        print("Usage: jd_worker_cli expId=<id> scale num_workers=<N>")
        sys.exit(1)

    registry = WorkerRegistry(exp_id, _cache_parent())
    workers = registry.list_workers(prune_dead=True)
    current = len(workers)

    if target == current:
        print(f"Already running {current} worker(s).")
        return

    sample = _sample_launch_config(registry)
    if not sample and target > current:
        print("Error: no existing worker config — start workers first.")
        sys.exit(1)

    if target > current:
        to_add = target - current
        used_ids = {int(w.get("process_id", 0)) for w in workers}
        next_id = 0
        for _ in range(to_add):
            while next_id in used_ids:
                next_id += 1
            cfg = dict(sample)
            cfg["exp_id"] = exp_id
            wid = new_worker_id(slot=next_id)
            pid = _spawn_from_config(cfg, wid, next_id)
            used_ids.add(next_id)
            print(f"Scaled up: worker_id={wid} pid={pid}")
            next_id += 1
        print(f"Scaled {exp_id}: {current} → {target} workers.")
        return

    to_remove = current - target
    victims = sorted(workers, key=lambda w: w["started_at"], reverse=True)[:to_remove]
    exp_kv = _kv_for_experiment(exp_id, kv)
    for row in victims:
        _stop_worker_pid(
            row["worker_id"], row["pid"], registry,
            exp_id=exp_id, kv=exp_kv, row=row, cli_action="scale",
        )
        print(f"Scaled down: stopped {row['worker_id']}")
    print(f"Scaled {exp_id}: {current} → {target} workers.")


def dispatch(argv: List[str]) -> None:
    kv = {}
    for arg in argv:
        if "=" in arg:
            k, v = arg.split("=", 1)
            kv[k.strip()] = v.strip()
        else:
            kv[arg.strip()] = "true"

    positionals = [a for a in argv if "=" not in a]

    if "version" in positionals:
        cmd_version()
        return
    if "health" in positionals:
        cmd_health(kv)
        return
    if "exp-list" in positionals:
        cmd_exp_list()
        return
    if "prune" in positionals:
        cmd_prune()
        return
    if "clear_all" in positionals or "clear-all" in positionals:
        cmd_clear_all(kv)
        return
    if "confirm-stop" in positionals and "stop" not in positionals:
        kv["confirm-stop"] = "true"
        cmd_stop(kv, ["all"], positionals)
        return
    if "worker-list" in positionals:
        cmd_worker_list(kv)
        return
    if "exp-status" in positionals:
        cmd_exp_status(kv)
        return
    if "server-info" in positionals:
        cmd_server_info(kv)
        return
    if "where" in positionals:
        cmd_where(kv)
        return
    if "drain" in positionals:
        cmd_drain(kv)
        return
    if "scale" in positionals:
        cmd_scale(kv)
        return

    if "worker-status" in positionals:
        idx = positionals.index("worker-status")
        wid = positionals[idx + 1] if idx + 1 < len(positionals) else ""
        cmd_worker_status(kv, wid)
        return
    if "worker-logs" in positionals:
        idx = positionals.index("worker-logs")
        wid = positionals[idx + 1] if idx + 1 < len(positionals) else ""
        cmd_worker_logs(kv, wid)
        return
    if "show-config" in positionals:
        idx = positionals.index("show-config")
        wid = positionals[idx + 1] if idx + 1 < len(positionals) else ""
        cmd_show_config(kv, wid)
        return
    if "restart" in positionals:
        idx = positionals.index("restart")
        targets = positionals[idx + 1 : idx + 2]
        cmd_restart(kv, targets)
        return
    if "stop" in positionals:
        idx = positionals.index("stop")
        cmd_stop(kv, positionals[idx + 1 :], positionals)
        return

    # Default: start workers
    cfg = _resolve_cfg(kv)
    errors = []
    if not cfg["exp_id"]:
        errors.append("expId is required")
    if not cfg["entry_script"]:
        errors.append("entry_script is required")
    elif not os.path.isfile(cfg["entry_script"]):
        errors.append(f"entry_script '{cfg['entry_script']}' not found")
    if errors:
        for e in errors:
            print(f"Error: {e}")
        sys.exit(1)
    if cfg.get("num_workers", 1) < 1:
        print("Error: num_workers must be positive.")
        sys.exit(1)
    _launch_workers(cfg, kv)
