"""SQLite registry for background jd_worker_cli processes on this machine."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
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
CREATE INDEX IF NOT EXISTS idx_workers_exp_pid ON workers(exp_id, pid);

CREATE TABLE IF NOT EXISTS experiment_meta (
    exp_id   TEXT PRIMARY KEY,
    drained  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS possible_instances_name (
    name  TEXT PRIMARY KEY,
    taken INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_instances_taken ON possible_instances_name(taken);
"""

_WORKER_COLUMNS = (
    "worker_id", "exp_id", "pid", "process_id", "runner_id", "entry_script",
    "machine_type", "log_path", "started_at", "status", "current_job_id",
    "last_ping_at", "config_json",
)


_JD_DATA_DIRNAME = "jd_data"
_DEFAULT_CACHE_ROOT = os.path.join(os.path.expanduser("~"), ".jd_cache")


def resolve_workspace_parent(explicit: Optional[str] = None) -> str:
    """Parent directory for ``jd_data/`` (job sandboxes, default logs).

    Uses ``JD_WORKSPACE_PATH`` when set; otherwise ``~`` so the default
    workspace root is ``~/jd_data`` (see :func:`resolve_workspace_path`).
    """
    if explicit is not None and str(explicit).strip():
        return os.path.abspath(os.path.expanduser(str(explicit)))
    env = os.environ.get("JD_WORKSPACE_PATH", "").strip()
    return os.path.abspath(os.path.expanduser(env or "~"))


def resolve_workspace_path(explicit_parent: Optional[str] = None) -> str:
    """Absolute ``jd_data`` root (default ``~/jd_data``)."""
    return os.path.join(resolve_workspace_parent(explicit_parent), _JD_DATA_DIRNAME)


def resolve_cache_parent(explicit: Optional[str] = None) -> str:
    """Root directory under which ``.cache/<expId>/`` is created.

    Uses ``JD_CACHE_PATH`` when set (recommended on Lustre/NFS for SQLite),
    otherwise ``~/.jd_cache``.
    """
    if explicit is not None and str(explicit).strip():
        return os.path.abspath(os.path.expanduser(str(explicit)))
    env = os.environ.get("JD_CACHE_PATH", "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.abspath(_DEFAULT_CACHE_ROOT)


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
    instance: Optional[str] = None,
) -> str:
    """Create a worker id using the experiment registry name pool.

    Format: ``{host}_{instance}_{slot}``  e.g. ``gpunode_egg_0``

    When *instance* is provided, no new name is allocated from the pool
    (used for additional slots on the same machine instance).
    """
    return WorkerRegistry(exp_id.strip().lower(), parent).new_worker_id(
        slot=slot, instance=instance,
    )


def new_worker_ids(
    *,
    count: int,
    exp_id: str,
    parent: Optional[str] = None,
) -> list[str]:
    """Allocate one instance name and return ``count`` worker ids (slots 0…count-1)."""
    if count < 1:
        return []
    return WorkerRegistry(exp_id.strip().lower(), parent).allocate_worker_ids(count)


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
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_workers_exp_pid "
                "ON workers(exp_id, pid)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_instances_taken "
                "ON possible_instances_name(taken)"
            )
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
        """Pick a random unused name from ``possible_instances_name`` and mark it taken.

        Uses BEGIN EXCLUSIVE to acquire an exclusive file-level lock before the
        SELECT, eliminating the SELECT→UPDATE race window that allowed two
        concurrent processes on the same node to claim the same name.
        """
        with self._lock:  # serialise within this process (threads)
            # isolation_level=None gives manual transaction control so we can
            # issue BEGIN EXCLUSIVE ourselves without Python's sqlite3 module
            # injecting its own implicit BEGIN first.
            conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                # EXCLUSIVE lock: no other process can read or write until COMMIT
                conn.execute("BEGIN EXCLUSIVE")
                row = conn.execute(
                    """
                    SELECT name FROM possible_instances_name
                    WHERE taken = 0
                    ORDER BY RANDOM()
                    LIMIT 1
                    """,
                ).fetchone()
                if not row:
                    conn.execute("ROLLBACK")
                    raise RuntimeError(
                        "No instance names available in the local registry pool. "
                        "Stop workers to release names, or clear the experiment cache."
                    )
                name = normalize_instance_name(row["name"])
                # No need for WHERE taken=0 or rowcount check — the exclusive
                # lock guarantees nobody else has modified this row since our SELECT.
                conn.execute(
                    "UPDATE possible_instances_name SET taken = 1 WHERE name = ?",
                    (name,),
                )
                conn.execute("COMMIT")
                return name
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

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

    def new_worker_id(self, *, slot: int = 0, instance: Optional[str] = None) -> str:
        inst = instance or self.allocate_instance_name()
        host = host_slug()
        return f"{host}_{inst}_{slot}"

    def allocate_worker_ids(self, count: int) -> list[str]:
        """One shared instance name; slots ``0 .. count-1`` on this host."""
        inst = self.allocate_instance_name()
        host = host_slug()
        return [f"{host}_{inst}_{i}" for i in range(count)]

    def next_worker_id(self, existing_worker_ids: list[str]) -> str:
        """Next slot on the same host/instance as *existing_worker_ids*, or a new instance."""
        host = host_slug()
        inst: Optional[str] = None
        used_slots: set[int] = set()
        for wid in existing_worker_ids:
            parsed = _parse_host_instance_slot(wid)
            if not parsed or parsed[0] != host:
                continue
            if inst is None:
                inst = parsed[1]
            if parsed[1] == inst:
                used_slots.add(parsed[2])
        if inst is None:
            inst = self.allocate_instance_name()
        slot = 0
        while slot in used_slots:
            slot += 1
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
                    norm = normalize_instance_name(inst)
                    still_used = conn.execute(
                        """
                        SELECT worker_id FROM workers
                        WHERE exp_id = ? AND worker_id != ?
                        """,
                        (self.exp_id, worker_id),
                    ).fetchall()
                    if not any(
                        _instance_from_worker_id(r["worker_id"]) == inst
                        for r in still_used
                    ):
                        conn.execute(
                            "UPDATE possible_instances_name SET taken = 0 WHERE name = ?",
                            (norm,),
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

    def count_workers(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM workers WHERE exp_id = ?",
                (self.exp_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def deep_prune(
        self,
        *,
        progress_tick: Optional[Any] = None,
    ) -> Dict[str, int]:
        """Remove dead workers, release unused instance names, drop empty experiment.

        *progress_tick* is an optional ``callable(n=1)`` invoked once per worker
        row scanned (for startup progress bar).
        """
        with self._connect() as conn:
            rows = list(conn.execute(
                "SELECT worker_id, pid FROM workers WHERE exp_id = ?",
                (self.exp_id,),
            ))

        dead_ids: List[str] = []
        active_instances: set[str] = set()
        for row in rows:
            wid = row["worker_id"]
            pid = int(row["pid"])
            if _pid_alive(pid):
                inst = _instance_from_worker_id(wid)
                if inst and is_valid_instance_name(inst):
                    active_instances.add(normalize_instance_name(inst))
            else:
                dead_ids.append(wid)
            if progress_tick is not None:
                progress_tick(1)

        workers_removed = 0
        instances_released = 0
        with self._lock:
            with self._connect() as conn:
                if dead_ids:
                    conn.executemany(
                        "DELETE FROM workers WHERE worker_id = ? AND exp_id = ?",
                        [(wid, self.exp_id) for wid in dead_ids],
                    )
                    workers_removed = len(dead_ids)

                cur = conn.execute(
                    "SELECT name FROM possible_instances_name WHERE taken = 1",
                )
                previously_taken = {r["name"] for r in cur.fetchall()}
                conn.execute(
                    "UPDATE possible_instances_name SET taken = 0 WHERE taken = 1",
                )
                if active_instances:
                    conn.executemany(
                        "UPDATE possible_instances_name SET taken = 1 WHERE name = ?",
                        [(n,) for n in sorted(active_instances)],
                    )
                instances_released = len(previously_taken - active_instances)

                remaining = int(conn.execute(
                    "SELECT COUNT(*) FROM workers WHERE exp_id = ?",
                    (self.exp_id,),
                ).fetchone()[0])
                conn.commit()

        experiment_removed = False
        if remaining == 0:
            experiment_removed = self._remove_experiment_cache()

        return {
            "workers_removed": workers_removed,
            "instances_released": instances_released,
            "experiment_removed": experiment_removed,
        }

    def _remove_experiment_cache(self) -> bool:
        path = exp_cache_dir(self.exp_id, self.parent)
        if not os.path.isdir(path):
            return False
        try:
            shutil.rmtree(path, ignore_errors=True)
            return True
        except OSError:
            return False

    def list_workers(self, prune_dead: bool = True) -> List[Dict[str, Any]]:
        if prune_dead:
            try:
                from jd.registry_prune import registry_recently_pruned
                if registry_recently_pruned():
                    prune_dead = False
            except ImportError:
                pass
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
        return self.deep_prune().get("workers_removed", 0)


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
    """Deep-clean all experiment registries (workers, instances, empty experiments)."""
    from jd.registry_prune import prune_all_registries

    summary = prune_all_registries(parent, show_progress=True)
    return {
        "workers_removed": summary.workers_removed,
        "instances_released": summary.instances_released,
        "experiments_removed": summary.experiments_removed,
        "token_dirs_removed": summary.token_dirs_removed,
    }


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
