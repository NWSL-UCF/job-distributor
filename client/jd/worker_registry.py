"""SQLite registry for background jd_worker_cli processes on this machine."""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import sqlite3
import string
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from jd.instance_names import (
    DEFAULT_INSTANCE_NAMES,
    INSTANCE_NAME_RE,
    is_valid_instance_name,
    normalize_instance_name,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    worker_id       TEXT PRIMARY KEY,
    exp_id          TEXT NOT NULL,
    pid             INTEGER NOT NULL,
    process_id      INTEGER NOT NULL,
    runner_id       TEXT NOT NULL DEFAULT '',
    entry_script    TEXT NOT NULL,
    machine_type    TEXT NOT NULL DEFAULT 'worker',
    log_path        TEXT NOT NULL DEFAULT '',
    started_at      REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'idle',
    current_job_id  INTEGER,
    last_ping_at    REAL,
    config_json     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_workers_exp ON workers(exp_id);
CREATE INDEX IF NOT EXISTS idx_workers_job ON workers(current_job_id);

CREATE TABLE IF NOT EXISTS experiment_meta (
    exp_id   TEXT PRIMARY KEY,
    drained  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS possible_instances_name (
    name  TEXT PRIMARY KEY,
    taken INTEGER NOT NULL DEFAULT 0
);
"""

_WORKER_COLUMNS = (
    "worker_id", "exp_id", "pid", "process_id", "runner_id", "entry_script",
    "machine_type", "log_path", "started_at", "status", "current_job_id",
    "last_ping_at", "config_json",
)


def resolve_workspace_parent(explicit: Optional[str] = None) -> str:
    """Parent directory for ``jd_data/`` (job sandboxes, default logs)."""
    if explicit is not None and str(explicit).strip():
        return os.path.abspath(os.path.expanduser(str(explicit)))
    env = os.environ.get("JD_WORKSPACE_PATH", "").strip()
    return os.path.abspath(os.path.expanduser(env or "~"))


def resolve_cache_parent(explicit: Optional[str] = None) -> str:
    """Root directory under which ``.cache/<expId>/`` is created.

    Uses ``JD_CACHE_PATH`` when set (recommended on Lustre/NFS for SQLite),
    otherwise the same parent as ``resolve_workspace_parent()``.
    """
    if explicit is not None and str(explicit).strip():
        return os.path.abspath(os.path.expanduser(str(explicit)))
    env = os.environ.get("JD_CACHE_PATH", "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return resolve_workspace_parent()


def cache_root(parent: Optional[str] = None) -> str:
    """Alias for :func:`resolve_cache_parent` (registry / ``.cache`` root)."""
    return resolve_cache_parent(parent)


def registry_db_path(exp_id: str, parent: Optional[str] = None) -> str:
    return os.path.join(cache_root(parent), ".cache", exp_id.strip().lower(), "workers.db")


def exp_cache_dir(exp_id: str, parent: Optional[str] = None) -> str:
    return os.path.join(cache_root(parent), ".cache", exp_id.strip().lower())


_INSTANCE_LEN = 6
_INSTANCE_ALPHABET = string.ascii_letters + string.digits
# Word names (1–6 letters) or legacy 6-char alphanumeric tokens.
_INSTANCE_RE = re.compile(r"^(?:[a-z]{1,6}|[0-9A-Za-z]{6})$")


def _legacy_random_instance() -> str:
    return "".join(secrets.choice(_INSTANCE_ALPHABET) for _ in range(_INSTANCE_LEN))


def _parse_host_instance_slot(worker_id: str) -> Optional[tuple[str, str, int]]:
    """Parse ``{host}_{instance}_{slot}`` if *worker_id* matches the current format."""
    parts = worker_id.strip().split("_")
    if len(parts) == 3 and parts[-1].isdigit() and _INSTANCE_RE.fullmatch(parts[1]):
        return parts[0], parts[1], int(parts[-1])
    return None


def _instance_from_worker_id(worker_id: str) -> Optional[str]:
    parsed = _parse_host_instance_slot(worker_id)
    return parsed[1] if parsed else None


def host_slug(max_len: int = 12) -> str:
    """Short filesystem-safe hostname prefix for worker IDs."""
    host = socket.gethostname().lower()
    slug = re.sub(r"[^a-z0-9]+", "", host)
    return (slug[:max_len] if slug else "host")


def new_worker_id(
    *,
    slot: int = 0,
    exp_id: str,
    parent: Optional[str] = None,
) -> str:
    """Create a unique worker id using the experiment registry name pool.

    Format: ``{host}_{instance}_{slot}``  e.g. ``gpunode_egg_0``
    """
    return WorkerRegistry(exp_id.strip().lower(), parent).new_worker_id(slot=slot)


def slot_from_worker_id(worker_id: str) -> int:
    """Return the launch slot from *worker_id*."""
    parsed = _parse_host_instance_slot(worker_id)
    if parsed:
        return parsed[2]
    rb = worker_id.strip()
    parts = rb.split("_")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        return int(parts[0])
    hparts = rb.split("-")
    if len(hparts) >= 3 and hparts[-1].isdigit():
        if re.fullmatch(r"[0-9a-fA-F]{4}", hparts[0]) or _INSTANCE_RE.fullmatch(hparts[0]):
            return int(hparts[-1])
        if hparts[1].isdigit():
            return int(hparts[1])
    return 0


def host_from_worker_id(worker_id: str) -> str:
    """Return hostname slug embedded in *worker_id*."""
    parsed = _parse_host_instance_slot(worker_id)
    if parsed:
        return parsed[0]
    rb = worker_id.strip()
    if "@" in rb:
        return rb.split("_")[0]
    parts = rb.split("_")
    hparts = rb.split("-")
    if len(hparts) >= 3 and hparts[-1].isdigit() and (
        re.fullmatch(r"[0-9a-fA-F]{4}", hparts[0]) or _INSTANCE_RE.fullmatch(hparts[0])
    ):
        return hparts[1]
    if hparts:
        return hparts[0]
    if parts:
        return parts[0]
    return worker_id


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class WorkerRegistry:
    def __init__(self, exp_id: str, parent: Optional[str] = None):
        self.exp_id = exp_id.strip().lower()
        self.parent = parent
        self.db_path = registry_db_path(self.exp_id, parent)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()
        self._ensure_db_mode()

    def _ensure_db_mode(self) -> None:
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            for col, typedef in (
                ("current_job_id", "INTEGER"),
                ("last_ping_at", "REAL"),
                ("config_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("worker_token", "TEXT NOT NULL DEFAULT ''"),
                ("token_updated_at", "REAL"),
            ):
                try:
                    conn.execute(f"ALTER TABLE workers ADD COLUMN {col} {typedef}")
                except sqlite3.OperationalError:
                    pass
            self._seed_instance_names(conn)
            conn.commit()

    def _seed_instance_names(self, conn: sqlite3.Connection) -> None:
        cur = conn.execute("SELECT COUNT(*) AS n FROM possible_instances_name")
        if int(cur.fetchone()[0]) > 0:
            return
        rows = [
            (n, 0) for n in DEFAULT_INSTANCE_NAMES if is_valid_instance_name(n)
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO possible_instances_name (name, taken) VALUES (?, ?)",
            rows,
        )

    def allocate_instance_name(self) -> str:
        """Pick a random unused name from ``possible_instances_name`` and mark it taken."""
        with self._lock:
            with self._connect() as conn:
                for _ in range(64):
                    row = conn.execute(
                        """
                        SELECT name FROM possible_instances_name
                        WHERE taken = 0
                        ORDER BY RANDOM()
                        LIMIT 1
                        """,
                    ).fetchone()
                    if not row:
                        break
                    name = normalize_instance_name(row["name"])
                    updated = conn.execute(
                        """
                        UPDATE possible_instances_name SET taken = 1
                        WHERE name = ? AND taken = 0
                        """,
                        (name,),
                    )
                    if updated.rowcount == 1:
                        conn.commit()
                        return name
                    conn.rollback()
                raise RuntimeError(
                    "No instance names available in the local registry pool. "
                    "Stop workers to release names, or clear the experiment cache."
                )

    def release_instance_name(self, instance: str) -> None:
        name = normalize_instance_name(instance)
        if not is_valid_instance_name(name):
            return
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE possible_instances_name SET taken = 0 WHERE name = ?",
                    (name,),
                )
                conn.commit()

    def new_worker_id(self, *, slot: int = 0) -> str:
        inst = self.allocate_instance_name()
        host = host_slug()
        return f"{host}_{inst}_{slot}"

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def register(
        self,
        worker_id: str,
        pid: int,
        process_id: int,
        runner_id: str,
        entry_script: str,
        machine_type: str,
        log_path: str,
        config_json: str = "{}",
    ) -> None:
        now = time.time()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO workers
                    (worker_id, exp_id, pid, process_id, runner_id, entry_script,
                     machine_type, log_path, started_at, status, current_job_id,
                     last_ping_at, config_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'idle', NULL, NULL, ?)
                    ON CONFLICT(worker_id) DO UPDATE SET
                        exp_id = excluded.exp_id,
                        pid = excluded.pid,
                        process_id = excluded.process_id,
                        runner_id = excluded.runner_id,
                        entry_script = excluded.entry_script,
                        machine_type = excluded.machine_type,
                        log_path = excluded.log_path,
                        started_at = excluded.started_at,
                        status = 'idle',
                        current_job_id = NULL,
                        config_json = excluded.config_json
                    """,
                    (
                        worker_id, self.exp_id, pid, process_id, runner_id,
                        entry_script, machine_type, log_path, now, config_json,
                    ),
                )
                conn.commit()
            self._ensure_db_mode()

    def set_worker_token(self, worker_id: str, token: str) -> None:
        """Persist the current Hub JWT for this worker (entry scripts read via registry)."""
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE workers SET worker_token = ?, token_updated_at = ?
                    WHERE worker_id = ? AND exp_id = ?
                    """,
                    (token.strip(), time.time(), worker_id, self.exp_id),
                )
                conn.commit()
            self._ensure_db_mode()

    def get_worker_token(self, worker_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT worker_token FROM workers WHERE worker_id = ? AND exp_id = ?",
                (worker_id, self.exp_id),
            ).fetchone()
        if not row:
            return ""
        return (row["worker_token"] or "").strip()

    def clear_worker_token(self, worker_id: str) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE workers SET worker_token = '', token_updated_at = NULL
                    WHERE worker_id = ? AND exp_id = ?
                    """,
                    (worker_id, self.exp_id),
                )
                conn.commit()

    def unregister(self, worker_id: str) -> None:
        inst = _instance_from_worker_id(worker_id)
        with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM workers WHERE worker_id = ?", (worker_id,))
                if inst:
                    conn.execute(
                        "UPDATE possible_instances_name SET taken = 0 WHERE name = ?",
                        (normalize_instance_name(inst),),
                    )
                conn.commit()

    def mark_stopping(self, worker_id: str) -> None:
        self.update_worker(worker_id, status="stopping")

    def update_worker(self, worker_id: str, **fields: Any) -> None:
        allowed = {
            "status", "current_job_id", "last_ping_at", "pid", "config_json",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        cols = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [worker_id]
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    f"UPDATE workers SET {cols} WHERE worker_id = ?",
                    vals,
                )
                conn.commit()

    def set_job(self, worker_id: str, job_id: Optional[int]) -> None:
        if job_id is None:
            self.update_worker(worker_id, current_job_id=None, status="idle")
        else:
            self.update_worker(worker_id, current_job_id=int(job_id), status="busy")

    def touch_ping(self, worker_id: str) -> None:
        self.update_worker(worker_id, last_ping_at=time.time())

    def list_workers(self, prune_dead: bool = True) -> List[Dict[str, Any]]:
        rows = self._fetch_all()
        if prune_dead:
            dead = [r for r in rows if not _pid_alive(r["pid"])]
            if dead:
                with self._lock:
                    with self._connect() as conn:
                        for row in dead:
                            inst = _instance_from_worker_id(row["worker_id"])
                            conn.execute(
                                "DELETE FROM workers WHERE worker_id = ?",
                                (row["worker_id"],),
                            )
                            if inst and is_valid_instance_name(inst):
                                conn.execute(
                                    "UPDATE possible_instances_name SET taken = 0 WHERE name = ?",
                                    (normalize_instance_name(inst),),
                                )
                        conn.commit()
                rows = [r for r in rows if _pid_alive(r["pid"])]
        return rows

    def get(self, worker_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ? AND exp_id = ?",
                (worker_id, self.exp_id),
            ).fetchone()
        return dict(row) if row else None

    def find_by_job_id(self, job_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM workers WHERE exp_id = ? AND current_job_id = ?",
                (self.exp_id, int(job_id)),
            ).fetchone()
        return dict(row) if row else None

    def get_config(self, worker_id: str) -> Optional[dict]:
        row = self.get(worker_id)
        if not row:
            return None
        try:
            return json.loads(row.get("config_json") or "{}")
        except json.JSONDecodeError:
            return {}

    def _fetch_all(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM workers WHERE exp_id = ? ORDER BY started_at",
                (self.exp_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def set_drained(self, drained: bool) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO experiment_meta (exp_id, drained)
                    VALUES (?, ?)
                    ON CONFLICT(exp_id) DO UPDATE SET drained = excluded.drained
                    """,
                    (self.exp_id, 1 if drained else 0),
                )
                conn.commit()

    def is_drained(self) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT drained FROM experiment_meta WHERE exp_id = ?",
                (self.exp_id,),
            ).fetchone()
        return bool(row and row["drained"])

    def prune_stale(self) -> int:
        """Remove dead workers; return count removed."""
        before = len(self._fetch_all())
        self.list_workers(prune_dead=True)
        return before - len(self._fetch_all())


def list_all_experiments(parent: Optional[str] = None) -> List[Dict[str, Any]]:
    cache_dir = os.path.join(cache_root(parent), ".cache")
    if not os.path.isdir(cache_dir):
        return []

    results: List[Dict[str, Any]] = []
    for name in sorted(os.listdir(cache_dir)):
        db_path = os.path.join(cache_dir, name, "workers.db")
        if not os.path.isfile(db_path):
            continue
        registry = WorkerRegistry(name, parent)
        workers = registry.list_workers(prune_dead=True)
        if workers:
            results.append({"exp_id": name, "worker_count": len(workers)})
    return results


def iter_experiment_registries(parent: Optional[str] = None) -> List[WorkerRegistry]:
    cache_dir = os.path.join(cache_root(parent), ".cache")
    if not os.path.isdir(cache_dir):
        return []
    registries = []
    for name in sorted(os.listdir(cache_dir)):
        db_path = os.path.join(cache_dir, name, "workers.db")
        if os.path.isfile(db_path):
            registries.append(WorkerRegistry(name, parent))
    return registries


def prune_all(parent: Optional[str] = None) -> Dict[str, int]:
    """Prune stale workers and legacy per-worker ``.token`` dirs. Returns summary counts."""
    removed_workers = 0
    removed_dirs = 0
    for registry in iter_experiment_registries(parent):
        removed_workers += registry.prune_stale()

    cache_dir = os.path.join(cache_root(parent), ".cache")
    if os.path.isdir(cache_dir):
        for exp_name in os.listdir(cache_dir):
            exp_path = os.path.join(cache_dir, exp_name)
            if not os.path.isdir(exp_path):
                continue
            for entry in os.listdir(exp_path):
                token_dir = os.path.join(exp_path, entry)
                if entry == "workers.db" or not os.path.isdir(token_dir):
                    continue
                token_file = os.path.join(token_dir, ".token")
                if os.path.isfile(token_file):
                    try:
                        os.remove(token_file)
                        os.rmdir(token_dir)
                        removed_dirs += 1
                    except OSError:
                        pass
            if exp_name != "workers.db":
                try:
                    if not os.listdir(exp_path):
                        os.rmdir(exp_path)
                except OSError:
                    pass

    return {"workers_removed": removed_workers, "token_dirs_removed": removed_dirs}


def clear_all_local_cache(parent: Optional[str] = None) -> Dict[str, Any]:
    """Delete all experiment directories under ``~/.cache`` (keeps REPL history file)."""
    import shutil

    cache_dir = os.path.join(cache_root(parent), ".cache")
    if not os.path.isdir(cache_dir):
        return {"experiments_cleared": 0, "experiments": []}

    removed: List[str] = []
    for name in sorted(os.listdir(cache_dir)):
        if name == "jd_worker_history":
            continue
        path = os.path.join(cache_dir, name)
        if not os.path.isdir(path):
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed.append(name)
    return {"experiments_cleared": len(removed), "experiments": removed}
