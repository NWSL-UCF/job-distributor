import hashlib
import os
import re
import secrets
import json
import logging
import time
from contextlib import contextmanager
from typing import Any, List, Dict, Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool
import psycopg2.errors

# Constants for job statuses
STATUS_PENDING = "PENDING"
STATUS_SERVED = "SERVED"
STATUS_DONE = "DONE"
STATUS_ABORTED = "ABORTED"
STATUS_DELETED = "DELETED"

WORKER_STATE_RUN = "run"
WORKER_STATE_PAUSE = "pause"
WORKER_STATE_DRAIN = "drain"
WORKER_STATE_STOP = "stop"
WORKER_REPORTED_IDLE = "idle"
WORKER_REPORTED_BUSY = "busy"
WORKER_STALE_SECONDS = 600             # 5× busy interval before a worker is declared dead
WORKER_POLL_INTERVAL_BUSY      = 120   # seconds between heartbeats while a job is running
                                       #   5k busy workers → 42 req/s (comfortable default)
WORKER_POLL_INTERVAL_IDLE      = 180   # backward-compat default / fallback
WORKER_POLL_INTERVAL_IDLE_JOBS = 30    # idle worker, pending jobs exist  → grab them fast
WORKER_POLL_INTERVAL_IDLE_NONE = 300   # idle worker, queue empty          → back off
                                       #   5k idle workers → 17 req/s
WORKER_POLL_INTERVAL = WORKER_POLL_INTERVAL_IDLE  # backward-compatible alias
WORKER_LIFECYCLE_ACTIVE = "active"
WORKER_LIFECYCLE_DISABLED = "disabled"
WORKER_LIST_PENDING = "pending"  # dashboard list filter (queued commands)
WORKER_LIST_PAUSED = "paused"  # dashboard list filter (applied pause)
WORKER_STOP_SLA_SECONDS = 300  # finalize stop after ~2 missed busy heartbeats (2 × 120s)
_WORKER_INSTANCE_RE = re.compile(r"^(?:[a-z]{1,6}|[0-9A-Za-z]{6,12})$")


def job_worker_id(job: Dict[str, Any]) -> str:
    """Canonical worker assignee: ``worker_id`` column, else legacy ``requested_by``."""
    return (job.get("worker_id") or job.get("requested_by") or "").strip()


def enrich_job_record(job: Dict[str, Any]) -> Dict[str, Any]:
    """Expose a single ``worker_id`` field on job dicts returned from the DB."""
    job["worker_id"] = job_worker_id(job)
    return job


def _parse_job_message(raw: Any) -> Any:
    """Parse job message JSON; keep raw text when the column is not valid JSON."""
    if raw is None:
        return []
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        text = raw.strip()
        return text if text else []


def _jobs_assignee_sql() -> str:
    """SQL expression for the worker that ran or was assigned a job."""
    return "COALESCE(NULLIF(worker_id, ''), requested_by)"


def jobs_search_sql(search: Optional[str]) -> tuple:
    """
    Build a WHERE fragment and params for job list search.

    Numeric query → exact job id.
    Otherwise → host, host_instance, or full worker id (prefix match).
    """
    q = (search or "").strip()
    if not q:
        return "", []
    if q.isdigit():
        return "id = %s", [int(q)]
    assignee = _jobs_assignee_sql()
    ql = q.lower()
    return (
        f"(LOWER({assignee}) = %s OR LOWER({assignee}) LIKE %s)",
        [ql, ql + "_%"],
    )


# Columns fetched on hot paths (heartbeat, list pages, command preview/set).
# Excludes the legacy 'history' TEXT blob which is now in worker_history table.
_WORKER_HOT_COLS = (
    "worker_id, host, instance, slot, machine_type, reported_status, "
    "current_job_id, jd_worker_version, last_poll_at, applied_version, "
    "desired_state, desired_version, previous_desired_state, pending_batch_id, "
    "lifecycle_status, disabled_at, first_poll_at, system_metrics"
)


class JobDatabase:
    """PostgreSQL database handler for job distribution system."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=15,   # 8 server + 4 dashboard workers × 15 = 180 total, fits PG limit of 250
            dsn=dsn,
        )
        self._init_database()

    @contextmanager
    def get_connection(self):
        """Get a database connection from the pool with proper error handling."""
        conn = None
        try:
            conn = self._pool.getconn()
            # Use RealDictCursor so rows behave like dicts everywhere
            conn.cursor_factory = psycopg2.extras.RealDictCursor
            yield conn
        except Exception as e:
            if conn and not conn.closed:
                conn.rollback()
            logging.error(f"Database error: {e}")
            raise
        finally:
            if conn and not conn.closed:
                # rollback is a no-op after commit; closes any implicit read transaction
                try:
                    conn.rollback()
                except Exception:
                    pass
            if conn:
                self._pool.putconn(conn)

    def _init_database(self):
        """Initialize the database schema.

        Uses a transaction-level PostgreSQL advisory lock so that when multiple
        Gunicorn workers start simultaneously only one runs the DDL at a time.
        The lock is released automatically on commit or rollback.
        """
        with self.get_connection() as conn:
            cur = conn.cursor()
            # Acquire an exclusive transaction-level advisory lock before touching DDL.
            # This prevents the race where concurrent workers all race to CREATE TABLE
            # and collide on the implicit pg_type entry, raising UniqueViolation.
            # Transaction-level locks auto-release on commit or rollback — no explicit
            # unlock needed, so the error path is safe too.
            cur.execute("SELECT pg_advisory_xact_lock(5263425)")  # 0x504D_0001 — "JD init"

            cur.execute('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id                     INTEGER PRIMARY KEY,
                    requested_by           TEXT NOT NULL DEFAULT '',
                    request_timestamp      DOUBLE PRECISION NOT NULL DEFAULT 0,
                    completion_timestamp   DOUBLE PRECISION NOT NULL DEFAULT 0,
                    required_time          DOUBLE PRECISION NOT NULL DEFAULT 0,
                    predicted_runtime      DOUBLE PRECISION NOT NULL DEFAULT 0,
                    last_ping_timestamp    DOUBLE PRECISION NOT NULL DEFAULT 0,
                    initialization_timestamp DOUBLE PRECISION NOT NULL DEFAULT 0,
                    status                 TEXT NOT NULL DEFAULT 'PENDING',
                    message                TEXT NOT NULL DEFAULT '[]',
                    parameters             TEXT NOT NULL,
                    system_metrics         TEXT NOT NULL DEFAULT '{}',
                    worker_id              TEXT NOT NULL DEFAULT ''
                )
            ''')

            cur.execute('''
                CREATE TABLE IF NOT EXISTS api_stats (
                    id            BIGSERIAL PRIMARY KEY,
                    endpoint      TEXT NOT NULL,
                    method        TEXT NOT NULL,
                    request_count INTEGER NOT NULL DEFAULT 0,
                    last_updated  DOUBLE PRECISION NOT NULL DEFAULT 0,
                    UNIQUE(endpoint, method)
                )
            ''')

            cur.execute('''
                CREATE TABLE IF NOT EXISTS server_config (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            ''')

            # Seed defaults — skip rows that already exist
            cur.execute('''
                INSERT INTO server_config (key, value)
                VALUES
                    ('idle_timeout',              '600'),
                    ('aborted_job_reset_timeout', '1200'),
                    ('hold_workers',              '1'),
                    ('traffic_server_in',    '0'),
                    ('traffic_server_out',   '0'),
                    ('traffic_dashboard_in', '0'),
                    ('traffic_dashboard_out','0'),
                    ('worker_history_migrated', '1'),
                    -- Worker / heartbeat performance settings (all sent back in heartbeat response)
                    -- Defaults target 5 000 parallel workers comfortably.
                    -- Tune upward via the dashboard Settings → Performance tab for more workers.
                    ('heartbeat_busy',          '120'),
                    ('heartbeat_idle_jobs',     '30'),
                    ('heartbeat_idle_none',     '300'),
                    ('heartbeat_control_chunk', '60'),
                    ('worker_stale_seconds',    '600'),
                    ('status_retry_count',      '8'),
                    ('status_retry_base_delay', '5'),
                    ('status_retry_max_delay',  '120')
                ON CONFLICT (key) DO NOTHING
            ''')

            cur.execute('''
                CREATE TABLE IF NOT EXISTS sessions (
                    token      TEXT PRIMARY KEY,
                    created_at DOUBLE PRECISION NOT NULL,
                    expires_at DOUBLE PRECISION NOT NULL
                )
            ''')

            cur.execute('''
                CREATE TABLE IF NOT EXISTS uploads (
                    id          BIGSERIAL PRIMARY KEY,
                    job_id      INTEGER NOT NULL,
                    version     INTEGER NOT NULL,
                    filename    TEXT NOT NULL,
                    size_bytes  INTEGER NOT NULL,
                    uploaded_at DOUBLE PRECISION NOT NULL,
                    UNIQUE(job_id, version)
                )
            ''')

            cur.execute('''
                CREATE TABLE IF NOT EXISTS workers (
                    worker_id              TEXT PRIMARY KEY,
                    host                   TEXT NOT NULL DEFAULT '',
                    machine_type           TEXT NOT NULL DEFAULT 'worker',
                    reported_status        TEXT NOT NULL DEFAULT 'idle',
                    current_job_id         INTEGER,
                    jd_worker_version      TEXT NOT NULL DEFAULT '',
                    last_poll_at           DOUBLE PRECISION NOT NULL DEFAULT 0,
                    applied_version        INTEGER NOT NULL DEFAULT 0,
                    desired_state          TEXT NOT NULL DEFAULT 'run',
                    desired_version        INTEGER NOT NULL DEFAULT 0,
                    previous_desired_state TEXT NOT NULL DEFAULT 'run',
                    pending_batch_id       TEXT,
                    system_metrics         TEXT NOT NULL DEFAULT '{}',
                    lifecycle_status       TEXT NOT NULL DEFAULT 'active',
                    instance               TEXT NOT NULL DEFAULT '',
                    slot                   INTEGER NOT NULL DEFAULT 0,
                    disabled_at            DOUBLE PRECISION NOT NULL DEFAULT 0,
                    first_poll_at          DOUBLE PRECISION NOT NULL DEFAULT 0
                )
            ''')

            cur.execute('''
                CREATE TABLE IF NOT EXISTS worker_history (
                    id        BIGSERIAL PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    timestamp DOUBLE PRECISION NOT NULL DEFAULT 0,
                    event     TEXT NOT NULL DEFAULT '',
                    reason    TEXT NOT NULL DEFAULT '',
                    metrics   TEXT
                )
            ''')

            # Indexes
            cur.execute('CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_jobs_status_id ON jobs(status, id)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_jobs_last_ping ON jobs(last_ping_timestamp)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_jobs_status_ping ON jobs(status, last_ping_timestamp)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_jobs_requested_by ON jobs(requested_by)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_jobs_request_timestamp ON jobs(request_timestamp)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_jobs_completion_timestamp ON jobs(completion_timestamp)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_jobs_worker_id ON jobs(worker_id)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_uploads_job_id ON uploads(job_id)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_workers_host ON workers(host)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_workers_last_poll ON workers(last_poll_at)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_workers_lifecycle ON workers(lifecycle_status)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_workers_lifecycle_poll ON workers(lifecycle_status, last_poll_at)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_worker_history_worker ON worker_history(worker_id)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_worker_history_worker_ts ON worker_history(worker_id, timestamp DESC)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_worker_history_metrics ON worker_history(worker_id, metrics)')

            conn.commit()  # lock auto-released here
            logging.info("PostgreSQL database initialized.")

    def create_jobs(self, parameters_list: List[str], clear_api_stats: bool = True) -> int:
        """Replace ALL existing jobs with a fresh set. Also clears API stats by default."""
        with self.get_connection() as conn:
            cur = conn.cursor()

            cur.execute("DELETE FROM jobs")

            if clear_api_stats:
                cur.execute("DELETE FROM api_stats")
                logging.info("API stats cleared for fresh start")

            jobs_data = [
                (
                    i,
                    '',
                    0, 0, 0, 0, 0, 0,
                    STATUS_PENDING,
                    '[]',
                    params,
                    '{}',
                    '',
                )
                for i, params in enumerate(parameters_list)
            ]

            psycopg2.extras.execute_values(
                cur,
                '''
                INSERT INTO jobs
                (id, requested_by, request_timestamp, completion_timestamp,
                 required_time, predicted_runtime, last_ping_timestamp,
                 initialization_timestamp, status, message, parameters,
                 system_metrics, worker_id)
                VALUES %s
                ''',
                jobs_data,
            )

            conn.commit()
            total_jobs = len(parameters_list)
            logging.info(f"Created {total_jobs} jobs in database")
            return total_jobs

    def append_jobs(self, parameters_list: List[str]) -> int:
        """Append new PENDING jobs without touching existing ones. IDs continue from the current maximum."""
        with self.get_connection() as conn:
            cur = conn.cursor()

            cur.execute("SELECT COALESCE(MAX(id), -1) AS max_id FROM jobs")
            start_id = cur.fetchone()['max_id'] + 1

            jobs_data = [
                (
                    start_id + i,
                    '', 0, 0, 0, 0, 0, 0,
                    STATUS_PENDING,
                    '[]',
                    params,
                    '{}',
                    '',
                )
                for i, params in enumerate(parameters_list)
            ]

            psycopg2.extras.execute_values(
                cur,
                '''
                INSERT INTO jobs
                (id, requested_by, request_timestamp, completion_timestamp,
                 required_time, predicted_runtime, last_ping_timestamp,
                 initialization_timestamp, status, message, parameters,
                 system_metrics, worker_id)
                VALUES %s
                ''',
                jobs_data,
            )

            conn.commit()
            total_new = len(parameters_list)
            logging.info(f"Appended {total_new} jobs (starting at id={start_id})")
            return total_new

    def get_all_jobs(self) -> List[Dict[str, Any]]:
        """Get all jobs from the database."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM jobs ORDER BY id")
            rows = cur.fetchall()

            jobs = []
            for row in rows:
                job = dict(row)
                job['message'] = _parse_job_message(job['message'])
                try:
                    job['parameters'] = json.loads(job['parameters'])
                except (json.JSONDecodeError, TypeError):
                    job['parameters'] = {}
                try:
                    job['system_metrics'] = json.loads(job.get('system_metrics', '{}') or '{}')
                except (json.JSONDecodeError, KeyError):
                    job['system_metrics'] = {}
                enrich_job_record(job)
                jobs.append(job)

            return jobs

    def get_config_value(self, key: str, default: str = "") -> str:
        """Read a value from server_config. Returns default if key is absent."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM server_config WHERE key = %s", (key,))
            row = cur.fetchone()
            return row['value'] if row else default

    def set_config_value(self, key: str, value: str) -> None:
        """Insert or update a key in server_config."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO server_config (key, value) VALUES (%s, %s) "
                "ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value",
                (key, value)
            )
            conn.commit()

    def get_all_config(self) -> Dict[str, str]:
        """Return the full server_config table as a dict."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM server_config")
            return {row['key']: row['value'] for row in cur.fetchall()}

    def get_performance_config(self) -> Dict[str, int]:
        """Return all worker/heartbeat performance settings as typed ints."""
        cfg = self.get_all_config()
        return {
            "heartbeat_busy":          int(cfg.get("heartbeat_busy",          WORKER_POLL_INTERVAL_BUSY)),
            "heartbeat_idle_jobs":     int(cfg.get("heartbeat_idle_jobs",     WORKER_POLL_INTERVAL_IDLE_JOBS)),
            "heartbeat_idle_none":     int(cfg.get("heartbeat_idle_none",     WORKER_POLL_INTERVAL_IDLE_NONE)),
            "heartbeat_control_chunk": int(cfg.get("heartbeat_control_chunk", 60)),
            "worker_stale_seconds":    int(cfg.get("worker_stale_seconds",    WORKER_STALE_SECONDS)),
            "status_retry_count":      int(cfg.get("status_retry_count",      8)),
            "status_retry_base_delay": int(cfg.get("status_retry_base_delay", 5)),
            "status_retry_max_delay":  int(cfg.get("status_retry_max_delay",  120)),
        }

    # ── PIN ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _hash_pin(pin: str, salt: bytes = None) -> str:
        """Return '<salt_hex>:<hash_hex>' using PBKDF2-SHA256."""
        if salt is None:
            salt = os.urandom(16)
        elif isinstance(salt, str):
            salt = bytes.fromhex(salt)
        h = hashlib.pbkdf2_hmac('sha256', pin.encode(), salt, 100_000)
        return f"{salt.hex()}:{h.hex()}"

    def set_pin(self, pin: str) -> None:
        """Hash and store a new dashboard PIN."""
        self.set_config_value('dashboard_pin', self._hash_pin(pin))

    def verify_pin(self, pin: str) -> bool:
        """Return True if pin matches the stored hash."""
        stored = self.get_config_value('dashboard_pin', '')
        if ':' not in stored:
            return False
        salt_hex, _ = stored.split(':', 1)
        return self._hash_pin(pin, salt_hex) == stored

    def pin_is_set(self) -> bool:
        return ':' in self.get_config_value('dashboard_pin', '')

    # ── Admin token ───────────────────────────────────────────────────────────

    def get_or_create_admin_token(self) -> str:
        """Return the admin override token, creating it on first call."""
        token = self.get_config_value('admin_token', '')
        if not token:
            token = secrets.token_urlsafe(32)
            self.set_config_value('admin_token', token)
        return token

    # ── Sessions ─────────────────────────────────────────────────────────────

    SESSION_DURATION = 7 * 24 * 3600  # 7 days

    def create_session(self) -> str:
        """Create a new session token and persist it. Returns the token."""
        token = secrets.token_hex(32)
        now = time.time()
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                'INSERT INTO sessions (token, created_at, expires_at) VALUES (%s, %s, %s)',
                (token, now, now + self.SESSION_DURATION)
            )
            conn.commit()
        return token

    def validate_session(self, token: str) -> bool:
        """Return True if the token exists and has not expired."""
        if not token:
            return False
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute('SELECT expires_at FROM sessions WHERE token = %s', (token,))
            row = cur.fetchone()
        return bool(row and time.time() < row['expires_at'])

    def delete_session(self, token: str) -> None:
        """Delete a single session (logout)."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute('DELETE FROM sessions WHERE token = %s', (token,))
            conn.commit()

    def clear_all_sessions(self) -> None:
        """Invalidate every active session (used after PIN override)."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute('DELETE FROM sessions')
            conn.commit()

    def cleanup_expired_sessions(self) -> None:
        """Prune expired sessions from the table."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute('DELETE FROM sessions WHERE expires_at < %s', (time.time(),))
            conn.commit()

    # ─────────────────────────────────────────────────────────────────────────

    def add_traffic(self, source: str, bytes_in: int, bytes_out: int) -> None:
        """Atomically add bytes_in / bytes_out to the named source counters."""
        if bytes_in <= 0 and bytes_out <= 0:
            return
        with self.get_connection() as conn:
            cur = conn.cursor()
            for key, delta in [
                (f"traffic_{source}_in",  bytes_in),
                (f"traffic_{source}_out", bytes_out),
            ]:
                if delta > 0:
                    cur.execute(
                        "INSERT INTO server_config (key, value) VALUES (%s, %s) "
                        "ON CONFLICT(key) DO UPDATE SET value = CAST(server_config.value AS BIGINT) + %s",
                        (key, str(delta), delta)
                    )
            conn.commit()

    def get_traffic_stats(self) -> Dict[str, int]:
        """Return cumulative byte counters for both services."""
        cfg = self.get_all_config()
        return {
            'server_in':     int(cfg.get('traffic_server_in',    '0')),
            'server_out':    int(cfg.get('traffic_server_out',   '0')),
            'dashboard_in':  int(cfg.get('traffic_dashboard_in', '0')),
            'dashboard_out': int(cfg.get('traffic_dashboard_out','0')),
        }

    def track_api_request(self, endpoint: str, method: str):
        """Track an API request by incrementing the counter."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            now = time.time()
            cur.execute('''
                INSERT INTO api_stats (endpoint, method, request_count, last_updated)
                VALUES (%s, %s, 1, %s)
                ON CONFLICT(endpoint, method)
                DO UPDATE SET
                    request_count = api_stats.request_count + 1,
                    last_updated = %s
            ''', (endpoint, method, now, now))
            conn.commit()

    def get_api_stats(self) -> List[Dict[str, Any]]:
        """Get API request statistics."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute('''
                SELECT endpoint, method, request_count, last_updated
                FROM api_stats
                ORDER BY request_count DESC
            ''')
            return [dict(row) for row in cur.fetchall()]

    def clear_api_stats(self) -> bool:
        """Clear all API request statistics."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM api_stats")
            conn.commit()
            logging.info("API stats cleared")
            return True

    def get_database_info(self) -> Dict[str, Any]:
        """Get database information including indexes and table sizes."""
        with self.get_connection() as conn:
            cur = conn.cursor()

            cur.execute("SELECT COUNT(*) AS count FROM jobs")
            jobs_count = cur.fetchone()['count']

            cur.execute("SELECT COUNT(*) AS count FROM api_stats")
            api_stats_count = cur.fetchone()['count']

            cur.execute("""
                SELECT indexname AS name, indexdef AS sql
                FROM pg_indexes
                WHERE tablename = 'jobs'
                ORDER BY indexname
            """)
            indexes = [{'name': row['name'], 'sql': row['sql']} for row in cur.fetchall()]

            cur.execute("""
                SELECT column_name AS name, data_type AS type
                FROM information_schema.columns
                WHERE table_name = 'jobs'
                ORDER BY ordinal_position
            """)
            schema = [{'name': row['name'], 'type': row['type']} for row in cur.fetchall()]

            return {
                'jobs_count': jobs_count,
                'api_stats_count': api_stats_count,
                'indexes': indexes,
                'schema': schema,
            }

    def get_job_by_id(self, job_id: int) -> Optional[Dict[str, Any]]:
        """Get a specific job by ID."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()

            if row:
                job = dict(row)
                job['message'] = _parse_job_message(job['message'])
                try:
                    job['parameters'] = json.loads(job['parameters'])
                except (json.JSONDecodeError, TypeError):
                    job['parameters'] = {}
                try:
                    job['system_metrics'] = json.loads(job.get('system_metrics', '{}') or '{}')
                except (json.JSONDecodeError, KeyError):
                    job['system_metrics'] = {}
                return enrich_job_record(job)
            return None

    def get_jobs_paginated(
        self,
        page: int = 1,
        per_page: int = 50,
        status: str = None,
        search_job_id: str = None,
        search: str = None,
    ) -> Dict[str, Any]:
        """Get jobs with pagination support."""
        query = (search or search_job_id or "").strip() or None
        with self.get_connection() as conn:
            cur = conn.cursor()

            where_conditions = []
            params = []

            if status:
                where_conditions.append("status = %s")
                params.append(status)

            search_clause, search_params = jobs_search_sql(query)
            if search_clause:
                where_conditions.append(search_clause)
                params.extend(search_params)

            where_clause = " WHERE " + " AND ".join(where_conditions) if where_conditions else ""

            cur.execute(f"SELECT COUNT(*) AS count FROM jobs{where_clause}", params)
            total_count = cur.fetchone()['count']

            total_pages = (total_count + per_page - 1) // per_page
            offset = (page - 1) * per_page

            cur.execute(
                f"SELECT * FROM jobs{where_clause} ORDER BY id LIMIT %s OFFSET %s",
                params + [per_page, offset],
            )
            rows = cur.fetchall()

            jobs = []
            for row in rows:
                job = dict(row)
                job['message'] = _parse_job_message(job['message'])
                try:
                    job['parameters'] = json.loads(job['parameters'])
                except (json.JSONDecodeError, TypeError):
                    job['parameters'] = {}
                try:
                    job['system_metrics'] = json.loads(job.get('system_metrics', '{}') or '{}')
                except (json.JSONDecodeError, KeyError):
                    job['system_metrics'] = {}
                enrich_job_record(job)
                jobs.append(job)

            return {
                'jobs': jobs,
                'total_count': total_count,
                'total_pages': total_pages,
                'current_page': page,
                'per_page': per_page,
            }

    def _claim_job(
        self,
        cur: Any,
        worker_id: str,
        system_metrics: Optional[Dict[str, Any]] = None,
        job_id: Optional[int] = None,
        predicted_runtime: Optional[float] = None,
        initialization_timestamp: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Claim a PENDING job within an existing transaction using FOR UPDATE SKIP LOCKED.

        This is the atomic job-claiming primitive. It must be called within an open
        transaction so that the row lock is held until the caller commits. No other
        concurrent transaction can claim the same row — PostgreSQL skips locked rows
        automatically, so parallel callers each get a distinct job.
        """
        if job_id is not None:
            cur.execute(
                "SELECT * FROM jobs WHERE id = %s AND status = %s FOR UPDATE SKIP LOCKED",
                (job_id, STATUS_PENDING),
            )
        else:
            cur.execute(
                "SELECT * FROM jobs WHERE status = %s ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED",
                (STATUS_PENDING,),
            )

        row = cur.fetchone()
        if not row:
            return None

        job = dict(row)
        timestamp = time.time()

        try:
            messages = json.loads(job['message'])
        except (json.JSONDecodeError, TypeError):
            messages = []

        wid = (worker_id or "").strip()
        messages.append({
            "reason": f"{wid} requests this job for execution",
            "timestamp": timestamp,
        })

        system_metrics_json = json.dumps(system_metrics) if system_metrics else '{}'
        predicted_runtime_value = predicted_runtime if predicted_runtime is not None else 0.0
        init_timestamp = initialization_timestamp if initialization_timestamp is not None else timestamp

        cur.execute('''
            UPDATE jobs
            SET worker_id = %s, requested_by = %s, status = %s, request_timestamp = %s,
                message = %s, system_metrics = %s, predicted_runtime = %s,
                initialization_timestamp = %s, last_ping_timestamp = %s
            WHERE id = %s
        ''', (
            wid, wid, STATUS_SERVED, timestamp,
            json.dumps(messages), system_metrics_json, predicted_runtime_value,
            init_timestamp, timestamp,  # set last_ping_timestamp immediately to avoid false-stale
            job['id'],
        ))

        job['worker_id'] = wid
        job['requested_by'] = wid
        job['status'] = STATUS_SERVED
        job['request_timestamp'] = timestamp
        job['initialization_timestamp'] = init_timestamp
        job['last_ping_timestamp'] = timestamp
        job['message'] = messages
        job['system_metrics'] = system_metrics if system_metrics else {}
        try:
            job['parameters'] = json.loads(job['parameters'])
        except (json.JSONDecodeError, TypeError):
            job['parameters'] = {}

        return job

    def request_job(
        self,
        worker_id: str,
        system_metrics: Optional[Dict[str, Any]] = None,
        job_id: Optional[int] = None,
        predicted_runtime: Optional[float] = None,
        initialization_timestamp: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Assign a PENDING job to a worker and mark it SERVED.

        Uses FOR UPDATE SKIP LOCKED — safe for any number of concurrent callers.
        """
        with self.get_connection() as conn:
            cur = conn.cursor()
            job = self._claim_job(
                cur, worker_id, system_metrics, job_id, predicted_runtime, initialization_timestamp,
            )
            conn.commit()
            return job

    def get_pending_jobs(self) -> List[Dict[str, Any]]:
        """Get all PENDING jobs."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM jobs WHERE status = %s ORDER BY id", (STATUS_PENDING,))
            rows = cur.fetchall()

            jobs = []
            for row in rows:
                job = dict(row)
                try:
                    job['parameters'] = json.loads(job['parameters'])
                except (json.JSONDecodeError, TypeError):
                    job['parameters'] = {}
                try:
                    job['system_metrics'] = json.loads(job.get('system_metrics', '{}') or '{}')
                except (json.JSONDecodeError, KeyError):
                    job['system_metrics'] = {}
                jobs.append(job)

            return jobs

    def update_job_status(self, job_id: int, status: str, message: str = "") -> bool:
        """Update job status to DONE or ABORTED. Atomically updates the worker row too."""
        if status not in [STATUS_DONE, STATUS_ABORTED]:
            return False

        with self.get_connection() as conn:
            cur = conn.cursor()

            cur.execute(
                "SELECT * FROM jobs WHERE id = %s AND status = %s FOR UPDATE",
                (job_id, STATUS_SERVED),
            )
            row = cur.fetchone()

            if not row:
                return False

            job = dict(row)
            now = time.time()
            required_time = now - job['request_timestamp']

            try:
                messages = json.loads(job['message'])
            except (json.JSONDecodeError, TypeError):
                messages = []

            messages.append({
                "reason": message if message else "No reason provided",
                "timestamp": now,
            })

            cur.execute('''
                UPDATE jobs
                SET status = %s, completion_timestamp = %s, required_time = %s, message = %s
                WHERE id = %s
            ''', (status, now, required_time, json.dumps(messages), job_id))

            wid = job_worker_id(job)
            if wid:
                self._touch_worker_presence_locked(
                    conn, wid,
                    current_job_id=None,
                    reported_status=WORKER_REPORTED_IDLE,
                )

            if status == STATUS_DONE:
                self._maybe_stop_workers_when_all_jobs_complete_locked(conn)

            conn.commit()
            return True

    def change_job_status(self, job_id: int, new_status: str, reason: str = "") -> bool:
        """Change job status for DONE, ABORTED, or PENDING jobs."""
        if new_status not in [STATUS_DONE, STATUS_ABORTED, STATUS_PENDING]:
            return False

        with self.get_connection() as conn:
            cur = conn.cursor()

            cur.execute(
                "SELECT * FROM jobs WHERE id = %s AND status = ANY(%s) FOR UPDATE",
                (job_id, [STATUS_DONE, STATUS_ABORTED, STATUS_PENDING]),
            )
            row = cur.fetchone()

            if not row:
                return False

            job = dict(row)
            now = time.time()
            old_status = job['status']

            try:
                messages = json.loads(job['message'])
            except (json.JSONDecodeError, TypeError):
                messages = []

            status_change_message = f"Manual Status Change: {old_status} → {new_status}"
            if reason:
                status_change_message += f" | Reason: {reason}"
            else:
                status_change_message += " | No reason provided"

            messages.append({"reason": status_change_message, "timestamp": now})

            if new_status == STATUS_PENDING:
                cur.execute('''
                    UPDATE jobs
                    SET status = %s, message = %s, request_timestamp = 0,
                        completion_timestamp = 0, required_time = 0,
                        last_ping_timestamp = 0, initialization_timestamp = 0,
                        worker_id = '', requested_by = ''
                    WHERE id = %s
                ''', (new_status, json.dumps(messages), job_id))
            else:
                cur.execute('''
                    UPDATE jobs SET status = %s, message = %s WHERE id = %s
                ''', (new_status, json.dumps(messages), job_id))

            if new_status == STATUS_DONE:
                self._maybe_stop_workers_when_all_jobs_complete_locked(conn)

            conn.commit()
            return True

    def reset_aborted_jobs(self) -> int:
        """Reset all ABORTED jobs to PENDING."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            current_time = time.time()

            cur.execute("SELECT * FROM jobs WHERE status = %s FOR UPDATE", (STATUS_ABORTED,))
            aborted_jobs = cur.fetchall()

            count = 0
            for row in aborted_jobs:
                job = dict(row)
                prev_requester = job_worker_id(job)

                try:
                    messages = json.loads(job['message'])
                except (json.JSONDecodeError, TypeError):
                    messages = []

                messages.append({
                    "reason": (
                        f"Job Cleaner: Reset job to PENDING status. "
                        f"Previous worker '{prev_requester}'. Job is now available for reassignment."
                    ),
                    "timestamp": current_time,
                })

                cur.execute('''
                    UPDATE jobs
                    SET status = %s, worker_id = '', requested_by = '',
                        request_timestamp = 0, completion_timestamp = 0, required_time = 0,
                        last_ping_timestamp = 0, initialization_timestamp = 0, message = %s
                    WHERE id = %s
                ''', (STATUS_PENDING, json.dumps(messages), job['id']))
                count += 1

            conn.commit()
            return count

    def reset_stale_served_jobs(self, idle_timeout: int) -> int:
        """Reset SERVED jobs that haven't pinged within the timeout."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            current_time = time.time()
            cutoff_time = current_time - idle_timeout

            cur.execute(
                "SELECT * FROM jobs WHERE status = %s AND last_ping_timestamp < %s FOR UPDATE",
                (STATUS_SERVED, cutoff_time),
            )
            stale_jobs = cur.fetchall()

            count = 0
            for row in stale_jobs:
                job = dict(row)
                prev_requester = job_worker_id(job)
                last_ping = job['last_ping_timestamp']
                minutes_silent = round((current_time - last_ping) / 60)

                try:
                    messages = json.loads(job['message'])
                except (json.JSONDecodeError, TypeError):
                    messages = []

                messages.append({
                    "reason": (
                        f"Job Cleaner: Reset job to PENDING status. "
                        f"Worker '{prev_requester}' stopped responding "
                        f"({minutes_silent} minutes of inactivity). "
                        f"Job is now available for reassignment."
                    ),
                    "timestamp": current_time,
                })

                cur.execute('''
                    UPDATE jobs
                    SET status = %s, worker_id = '', requested_by = '',
                        request_timestamp = 0, completion_timestamp = 0, required_time = 0,
                        last_ping_timestamp = 0, initialization_timestamp = 0, message = %s
                    WHERE id = %s
                ''', (STATUS_PENDING, json.dumps(messages), job['id']))
                count += 1

            conn.commit()
            return count

    def delete_job(self, job_id: int, reason: str = "") -> bool:
        """Delete a PENDING job by setting its status to DELETED. Only works on PENDING jobs."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM jobs WHERE id = %s AND status = %s FOR UPDATE",
                (job_id, STATUS_PENDING),
            )
            row = cur.fetchone()
            if not row:
                return False

            job = dict(row)
            now = time.time()
            try:
                messages = json.loads(job['message'])
            except (json.JSONDecodeError, TypeError):
                messages = []

            messages.append({
                "reason": f"Job Deleted: {reason if reason else 'No reason provided'}. Job will not be assigned to any worker.",
                "timestamp": now,
            })

            cur.execute(
                "UPDATE jobs SET status = %s, message = %s WHERE id = %s",
                (STATUS_DELETED, json.dumps(messages), job_id),
            )
            self._maybe_stop_workers_when_all_jobs_complete_locked(conn)
            conn.commit()
            return True

    def restore_deleted_job(self, job_id: int, reason: str = "") -> bool:
        """Restore a DELETED job back to PENDING. Only works on DELETED jobs."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM jobs WHERE id = %s AND status = %s FOR UPDATE",
                (job_id, STATUS_DELETED),
            )
            row = cur.fetchone()
            if not row:
                return False

            job = dict(row)
            now = time.time()
            try:
                messages = json.loads(job['message'])
            except (json.JSONDecodeError, TypeError):
                messages = []

            messages.append({
                "reason": f"Job Restored to PENDING: {reason if reason else 'No reason provided'}. Job is now available for assignment.",
                "timestamp": now,
            })

            cur.execute('''
                UPDATE jobs
                SET status = %s, message = %s, worker_id = '', requested_by = '',
                    request_timestamp = 0, completion_timestamp = 0, required_time = 0,
                    last_ping_timestamp = 0, initialization_timestamp = 0
                WHERE id = %s
            ''', (STATUS_PENDING, json.dumps(messages), job_id))
            conn.commit()
            return True

    @staticmethod
    def _job_eligible_for_bulk_action(job: Dict[str, Any], action: str) -> bool:
        status = job.get("status") or ""
        if action == "delete":
            return status == STATUS_PENDING
        if action == "restore":
            return status == STATUS_DELETED
        if action == "to_pending":
            return status in (STATUS_DONE, STATUS_ABORTED)
        if action == "to_done":
            return status == STATUS_PENDING
        return False

    def _job_matches_scope(
        self,
        job: Dict[str, Any],
        scope: str,
        target: Optional[Any],
        job_ids: Optional[List[int]] = None,
    ) -> bool:
        jid = int(job["id"])
        if scope == "jobs":
            ids = {int(x) for x in (job_ids or [])}
            return jid in ids
        if scope == "job":
            try:
                return jid == int(target)
            except (TypeError, ValueError):
                return False
        if scope == "all":
            return True
        return False

    def _jobs_for_bulk_action(
        self,
        action: str,
        scope: str,
        target: Optional[Any] = None,
        job_ids: Optional[List[int]] = None,
        *,
        status: Optional[str] = None,
        search_job_id: Optional[str] = None,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Jobs that would be affected by a bulk dashboard action."""
        query = (search or search_job_id or "").strip() or None
        with self.get_connection() as conn:
            cur = conn.cursor()
            where_conditions = []
            params = []
            if status:
                where_conditions.append("status = %s")
                params.append(status)
            search_clause, search_params = jobs_search_sql(query)
            if search_clause:
                where_conditions.append(search_clause)
                params.extend(search_params)
            where = (" WHERE " + " AND ".join(where_conditions)) if where_conditions else ""
            cur.execute(f"SELECT * FROM jobs{where}", params)
            rows = [dict(r) for r in cur.fetchall()]

        out: List[Dict[str, Any]] = []
        for job in rows:
            if not self._job_matches_scope(job, scope, target, job_ids):
                continue
            if not self._job_eligible_for_bulk_action(job, action):
                continue
            job = enrich_job_record(job)
            out.append({
                "id": job["id"],
                "status": job.get("status"),
                "worker_id": job_worker_id(job) or "Unassigned",
                "request_timestamp": job.get("request_timestamp"),
                "completion_timestamp": job.get("completion_timestamp"),
            })
        out.sort(key=lambda j: int(j["id"]))
        return out

    def preview_jobs_bulk_action(
        self,
        action: str,
        scope: str,
        target: Optional[Any] = None,
        job_ids: Optional[List[int]] = None,
        *,
        status: Optional[str] = None,
        search_job_id: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        jobs = self._jobs_for_bulk_action(
            action, scope, target, job_ids,
            status=status, search_job_id=search_job_id, search=search,
        )
        return {"jobs": jobs, "count": len(jobs)}

    def execute_jobs_bulk_action(
        self,
        action: str,
        scope: str,
        reason: str = "",
        target: Optional[Any] = None,
        job_ids: Optional[List[int]] = None,
        *,
        status: Optional[str] = None,
        search_job_id: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Apply delete / restore / to_pending / to_done to matching jobs."""
        jobs = self._jobs_for_bulk_action(
            action, scope, target, job_ids,
            status=status, search_job_id=search_job_id, search=search,
        )
        affected = 0
        failed: List[Dict[str, Any]] = []
        for job in jobs:
            jid = int(job["id"])
            ok = False
            try:
                if action == "delete":
                    ok = self.delete_job(jid, reason)
                elif action == "restore":
                    ok = self.restore_deleted_job(jid, reason)
                elif action == "to_pending":
                    ok = self.change_job_status(jid, STATUS_PENDING, reason)
                elif action == "to_done":
                    ok = self.change_job_status(jid, STATUS_DONE, reason)
                else:
                    failed.append({"id": jid, "error": f"Unknown action: {action}"})
                    continue
            except Exception as exc:
                failed.append({"id": jid, "error": str(exc)})
                continue
            if ok:
                affected += 1
            else:
                failed.append({"id": jid, "error": "Action rejected by server"})
        return {"affected": affected, "failed": failed, "total": len(jobs)}

    def update_job_parameters(self, job_id: int, updates: Dict[str, Any], reason: str = "") -> bool:
        """Update parameters of a PENDING job. Only works on PENDING jobs."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM jobs WHERE id = %s AND status = %s FOR UPDATE",
                (job_id, STATUS_PENDING),
            )
            row = cur.fetchone()
            if not row:
                return False

            job = dict(row)
            now = time.time()

            try:
                current_params = json.loads(job['parameters'])
            except (json.JSONDecodeError, TypeError):
                current_params = {}

            try:
                messages = json.loads(job['message'])
            except (json.JSONDecodeError, TypeError):
                messages = []

            changed_parts = []
            for key, new_value in updates.items():
                old_value = current_params.get(key, "<not set>")
                changed_parts.append(
                    f"{key}: {json.dumps(old_value)} \u2192 {json.dumps(new_value)}"
                )
                current_params[key] = new_value

            if not changed_parts:
                return True

            audit_reason = "Parameters Updated: " + ", ".join(changed_parts)
            if reason:
                audit_reason += f" | Reason: {reason}"

            messages.append({"reason": audit_reason, "timestamp": now})

            cur.execute(
                "UPDATE jobs SET parameters = %s, message = %s WHERE id = %s",
                (json.dumps(current_params), json.dumps(messages), job_id),
            )
            conn.commit()
            return True

    def get_job_counts_by_status(self) -> Dict[str, int]:
        """Get job counts by status efficiently."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status")
            rows = cur.fetchall()

            counts = {
                STATUS_PENDING: 0, STATUS_SERVED: 0, STATUS_DONE: 0,
                STATUS_ABORTED: 0, STATUS_DELETED: 0,
            }
            for row in rows:
                counts[row['status']] = row['count']

            return counts

    def get_first_job_assignment_timestamp(self) -> Optional[float]:
        """Return the earliest request_timestamp across all assigned jobs (>0)."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT MIN(request_timestamp) AS first_ts FROM jobs "
                "WHERE request_timestamp > 0"
            )
            row = cur.fetchone()
            if row and row['first_ts']:
                return float(row['first_ts'])
            return None

    def get_last_completion_timestamp(self) -> Optional[float]:
        """Return the latest completion_timestamp across all DONE jobs."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT MAX(completion_timestamp) AS last_ts FROM jobs "
                "WHERE status = %s AND completion_timestamp > 0",
                (STATUS_DONE,),
            )
            row = cur.fetchone()
            if row and row['last_ts']:
                return float(row['last_ts'])
            return None

    def next_upload_version(self, job_id: int) -> int:
        """Return the next monotonic upload version number for a job."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT MAX(version) AS mx FROM uploads WHERE job_id = %s",
                (job_id,),
            )
            row = cur.fetchone()
            if row and row["mx"] is not None:
                return int(row["mx"]) + 1
            return 0

    def record_upload(
        self,
        job_id: int,
        version: int,
        filename: str,
        size_bytes: int,
        uploaded_at: float,
    ) -> None:
        """Persist metadata for a worker result upload."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                '''
                INSERT INTO uploads (job_id, version, filename, size_bytes, uploaded_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT(job_id, version) DO UPDATE SET
                    filename = EXCLUDED.filename,
                    size_bytes = EXCLUDED.size_bytes,
                    uploaded_at = EXCLUDED.uploaded_at
                ''',
                (job_id, version, filename, size_bytes, uploaded_at),
            )
            conn.commit()

    def list_uploads(self, job_id: int) -> List[Dict[str, Any]]:
        """Return upload rows for a job, newest version first."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                '''
                SELECT job_id, version, filename, size_bytes, uploaded_at
                FROM uploads
                WHERE job_id = %s
                ORDER BY version DESC
                ''',
                (job_id,),
            )
            return [dict(row) for row in cur.fetchall()]

    def backfill_uploads(self, rows: List[Dict[str, Any]]) -> None:
        """Insert disk-scanned uploads that are not yet in the database."""
        if not rows:
            return
        with self.get_connection() as conn:
            cur = conn.cursor()
            for row in rows:
                cur.execute(
                    '''
                    INSERT INTO uploads (job_id, version, filename, size_bytes, uploaded_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT(job_id, version) DO NOTHING
                    ''',
                    (
                        row["job_id"],
                        row["version"],
                        row["filename"],
                        row["size_bytes"],
                        row["uploaded_at"],
                    ),
                )
            conn.commit()

    def get_percentile_runtime(self, percentile: float) -> float:
        """Get the percentile runtime from completed jobs."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT required_time
                FROM jobs
                WHERE status = %s AND required_time > 0
                ORDER BY required_time
            """, (STATUS_DONE,))

            runtimes = [row['required_time'] for row in cur.fetchall()]

            if not runtimes:
                return 0.0

            n = len(runtimes)
            index = int(percentile * (n - 1))
            index = max(0, min(index, n - 1))

            return runtimes[index]

    def get_jobs_by_status(self, status: str) -> List[Dict[str, Any]]:
        """Get all jobs with a specific status."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM jobs WHERE status = %s ORDER BY id", (status,))
            rows = cur.fetchall()

            jobs = []
            for row in rows:
                job = dict(row)
                job['message'] = _parse_job_message(job['message'])
                try:
                    job['parameters'] = json.loads(job['parameters'])
                except (json.JSONDecodeError, TypeError):
                    job['parameters'] = {}
                try:
                    job['system_metrics'] = json.loads(job.get('system_metrics', '{}') or '{}')
                except (json.JSONDecodeError, KeyError):
                    job['system_metrics'] = {}
                enrich_job_record(job)
                jobs.append(job)

            return jobs

    # ── Worker registry (poll-based heartbeat + dashboard control) ─────────

    @staticmethod
    def parse_worker_id_parts(worker_id: str) -> tuple:
        """Return (host, instance, slot) from ``{host}_{instance}_{slot}``."""
        parts = (worker_id or "").strip().split("_")
        if len(parts) == 3 and parts[-1].isdigit() and _WORKER_INSTANCE_RE.fullmatch(parts[1]):
            return parts[0], parts[1], int(parts[-1])
        if parts:
            return parts[0], "", 0
        return worker_id or "unknown", "", 0

    @staticmethod
    def _parse_worker_history(raw: Any) -> List[Dict[str, Any]]:
        if raw is None:
            return []
        if isinstance(raw, list):
            return raw
        if not isinstance(raw, str):
            return []
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return [{"reason": text, "timestamp": None, "event": "legacy"}]

    def _append_worker_history(
        self,
        conn: Any,
        worker_id: str,
        reason: str,
        *,
        event: str = "",
        metrics: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append one event to the worker_history table within an existing transaction."""
        metrics_json = json.dumps(metrics) if metrics else None
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO worker_history (worker_id, timestamp, event, reason, metrics) "
            "VALUES (%s, %s, %s, %s, %s)",
            (worker_id, time.time(), event or "info", reason, metrics_json),
        )

    def _touch_worker_presence_locked(
        self,
        conn: Any,
        worker_id: str,
        *,
        system_metrics: Optional[Dict[str, Any]] = None,
        current_job_id: Optional[int] = None,
        reported_status: str = WORKER_REPORTED_IDLE,
        machine_type: Optional[str] = None,
    ) -> None:
        """Refresh worker row from job lifecycle events within an existing transaction."""
        worker_id = (worker_id or "").strip()
        if not worker_id:
            return
        status = reported_status if reported_status in (
            WORKER_REPORTED_IDLE, WORKER_REPORTED_BUSY,
        ) else WORKER_REPORTED_IDLE
        now = time.time()
        host, instance, slot = self.parse_worker_id_parts(worker_id)
        metrics = system_metrics or {}
        mtype = (machine_type or metrics.get("worker_type") or "worker").strip()
        metrics_json = json.dumps(metrics)
        cur = conn.cursor()
        cur.execute("SELECT worker_id FROM workers WHERE worker_id = %s", (worker_id,))
        if cur.fetchone():
            cur.execute(
                """
                UPDATE workers SET
                    host = %s, instance = %s, slot = %s, machine_type = %s,
                    reported_status = %s, current_job_id = %s,
                    last_poll_at = %s, system_metrics = %s,
                    lifecycle_status = %s, disabled_at = 0
                WHERE worker_id = %s
                """,
                (
                    host, instance, slot, mtype, status, current_job_id,
                    now, metrics_json, WORKER_LIFECYCLE_ACTIVE, worker_id,
                ),
            )
        else:
            cur.execute(
                """
                INSERT INTO workers
                (worker_id, host, instance, slot, machine_type, reported_status,
                 current_job_id, last_poll_at, applied_version, desired_state,
                 desired_version, previous_desired_state, system_metrics,
                 lifecycle_status, disabled_at, first_poll_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, 'run', 0, 'run', %s, %s, 0, %s)
                """,
                (
                    worker_id, host, instance, slot, mtype, status, current_job_id,
                    now, metrics_json, WORKER_LIFECYCLE_ACTIVE, now,
                ),
            )

    def touch_worker_presence(
        self,
        worker_id: str,
        *,
        system_metrics: Optional[Dict[str, Any]] = None,
        current_job_id: Optional[int] = None,
        reported_status: str = WORKER_REPORTED_IDLE,
        machine_type: Optional[str] = None,
    ) -> None:
        with self.get_connection() as conn:
            self._backfill_worker_identity_columns(conn)
            self._touch_worker_presence_locked(
                conn, worker_id,
                system_metrics=system_metrics,
                current_job_id=current_job_id,
                reported_status=reported_status,
                machine_type=machine_type,
            )
            conn.commit()

    def _abort_served_job_locked(
        self,
        conn: Any,
        job_id: int,
        message: str,
        worker_id: str = "",
    ) -> bool:
        """Abort a SERVED job within an existing transaction."""
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM jobs WHERE id = %s AND status = %s FOR UPDATE",
            (int(job_id), STATUS_SERVED),
        )
        row = cur.fetchone()
        if not row:
            return False
        job = dict(row)
        now = time.time()
        required_time = now - job["request_timestamp"]
        try:
            messages = json.loads(job["message"])
        except (json.JSONDecodeError, TypeError):
            messages = []
        messages.append({
            "reason": message if message else "No reason provided",
            "timestamp": now,
        })
        cur.execute(
            """
            UPDATE jobs
            SET status = %s, completion_timestamp = %s, required_time = %s, message = %s
            WHERE id = %s
            """,
            (STATUS_ABORTED, now, required_time, json.dumps(messages), int(job_id)),
        )
        wid = worker_id or job_worker_id(job)
        if wid:
            self._touch_worker_presence_locked(
                conn, wid,
                current_job_id=None,
                reported_status=WORKER_REPORTED_IDLE,
            )
        return True

    def _finalize_worker_stop_locked(
        self,
        conn: Any,
        row: Dict[str, Any],
        reason: str,
    ) -> None:
        """Apply pending stop server-side: clear pending, disable, abort active job."""
        wid = row["worker_id"]
        desired_v = int(row.get("desired_version") or 0)
        now = time.time()
        job_id = row.get("current_job_id")
        if job_id is not None:
            self._abort_served_job_locked(
                conn,
                int(job_id),
                f"Job aborted: {reason} (worker {wid}).",
                wid,
            )
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE workers SET
                applied_version = %s,
                desired_state = %s,
                lifecycle_status = %s,
                disabled_at = %s,
                reported_status = %s,
                current_job_id = NULL
            WHERE worker_id = %s
            """,
            (
                desired_v,
                WORKER_STATE_STOP,
                WORKER_LIFECYCLE_DISABLED,
                now,
                WORKER_REPORTED_IDLE,
                wid,
            ),
        )
        self._append_worker_history(conn, wid, reason, event="stop_finalized")

    def _cancel_pending_locked(
        self,
        conn: Any,
        row: Dict[str, Any],
        reason: str,
    ) -> None:
        """Revert a queued command without requiring a recent poll."""
        applied = int(row.get("applied_version") or 0)
        desired_v = int(row.get("desired_version") or 0)
        if desired_v <= applied:
            return
        wid = row["worker_id"]
        prev = row.get("previous_desired_state") or WORKER_STATE_RUN
        cancelled = row.get("desired_state") or WORKER_STATE_RUN
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE workers SET
                desired_state = %s,
                desired_version = desired_version + 1,
                previous_desired_state = %s
            WHERE worker_id = %s
            """,
            (prev, cancelled, wid),
        )
        self._append_worker_history(conn, wid, reason, event="command_cancelled")

    def _reconcile_worker_commands(self, conn: Any) -> None:
        """Finalize overdue stop commands and clear orphan pending on stopped workers."""
        now = time.time()
        cur = conn.cursor()
        cur.execute(
            "SELECT worker_id, current_job_id, applied_version, desired_version, "
            "desired_state, previous_desired_state, lifecycle_status, last_poll_at "
            "FROM workers WHERE desired_version > applied_version"
        )
        for raw in cur.fetchall():
            row = dict(raw)
            applied = int(row.get("applied_version") or 0)
            desired_v = int(row.get("desired_version") or 0)
            if desired_v <= applied:
                continue
            desired_state = row.get("desired_state") or WORKER_STATE_RUN
            lifecycle = row.get("lifecycle_status") or WORKER_LIFECYCLE_ACTIVE
            last_poll = float(row.get("last_poll_at") or 0)
            poll_age = (now - last_poll) if last_poll > 0 else float("inf")

            if desired_state == WORKER_STATE_STOP:
                if lifecycle == WORKER_LIFECYCLE_DISABLED:
                    self._finalize_worker_stop_locked(
                        conn, row, "Stop command finalized — worker already stopped.",
                    )
                elif poll_age > WORKER_STOP_SLA_SECONDS:
                    self._finalize_worker_stop_locked(
                        conn, row,
                        "Stop command finalized — no worker poll within ~3 minutes after dashboard stop.",
                    )
            elif lifecycle == WORKER_LIFECYCLE_DISABLED:
                self._cancel_pending_locked(
                    conn, row, "Pending command cleared — worker already stopped.",
                )

    def _all_jobs_terminal_locked(self, conn: Any) -> bool:
        """True when at least one job exists and every job is DONE or DELETED."""
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM jobs")
        row = cur.fetchone()
        total = int(row['n']) if row else 0
        if total == 0:
            return False
        cur.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status NOT IN (%s, %s)",
            (STATUS_DONE, STATUS_DELETED),
        )
        row = cur.fetchone()
        return int(row['n'] or 0) == 0

    def _queue_stop_all_active_workers_locked(
        self,
        conn: Any,
        reason: str,
    ) -> int:
        """Queue dashboard stop for all active workers that would accept it."""
        cur = conn.cursor()
        cur.execute(
            f"SELECT {_WORKER_HOT_COLS} FROM workers WHERE lifecycle_status = %s",
            (WORKER_LIFECYCLE_ACTIVE,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        targets = self._command_target_rows(
            rows,
            WORKER_STATE_STOP,
            "all",
            None,
            None,
            lifecycle=WORKER_LIFECYCLE_ACTIVE,
        )
        affected = 0
        for row in targets:
            wid = row["worker_id"]
            current = row.get("desired_state") or WORKER_STATE_RUN
            applied = int(row.get("applied_version") or 0)
            desired_v = int(row.get("desired_version") or 0)
            pending = desired_v > applied
            prev = (
                current if pending
                else row.get("previous_desired_state") or WORKER_STATE_RUN
            )
            new_version = desired_v + 1
            cur.execute(
                """
                UPDATE workers SET
                    previous_desired_state = %s,
                    desired_state = %s,
                    desired_version = %s
                WHERE worker_id = %s
                """,
                (prev, WORKER_STATE_STOP, new_version, wid),
            )
            self._append_worker_history(conn, wid, reason, event="auto_stop_all_jobs_complete")
            cur.execute(
                f"SELECT {_WORKER_HOT_COLS} FROM workers WHERE worker_id = %s", (wid,),
            )
            updated = dict(cur.fetchone())
            job_id = updated.get("current_job_id")
            if job_id is None and (
                updated.get("reported_status") or WORKER_REPORTED_IDLE
            ) == WORKER_REPORTED_IDLE:
                self._finalize_worker_stop_locked(conn, updated, reason)
            affected += 1
        return affected

    def _hold_workers_enabled_locked(self, conn: Any) -> bool:
        """When True (default), workers stay alive after all jobs reach DONE/DELETED."""
        cur = conn.cursor()
        cur.execute("SELECT value FROM server_config WHERE key = 'hold_workers'")
        row = cur.fetchone()
        if row is None:
            return True
        return str(row['value']).lower() not in ("0", "false", "no", "off")

    def _maybe_stop_workers_when_all_jobs_complete_locked(
        self,
        conn: Any,
    ) -> Dict[str, Any]:
        """If every job is DONE or DELETED, queue stop for active workers."""
        if self._hold_workers_enabled_locked(conn):
            return {"triggered": False, "workers_stopped": 0, "held": True}
        if not self._all_jobs_terminal_locked(conn):
            return {"triggered": False, "workers_stopped": 0}
        reason = "All jobs are DONE or DELETED — stop queued for active workers."
        n = self._queue_stop_all_active_workers_locked(conn, reason)
        if n:
            logging.info(
                "All jobs terminal (DONE/DELETED); queued stop for %d worker(s).", n,
            )
        return {"triggered": bool(n), "workers_stopped": n}

    def maybe_stop_workers_when_all_jobs_complete(self) -> Dict[str, Any]:
        """Public entry: stop all active workers when the job queue is fully terminal."""
        with self.get_connection() as conn:
            result = self._maybe_stop_workers_when_all_jobs_complete_locked(conn)
            conn.commit()
        return result

    def shutdown_stop_all_workers(self) -> Dict[str, Any]:
        """Queue stop for every active worker (experiment shutdown / Hub deletion)."""
        reason = "Experiment shutdown — stop queued for active workers."
        with self.get_connection() as conn:
            n = self._queue_stop_all_active_workers_locked(conn, reason)
            conn.commit()
        if n:
            logging.info("Shutdown: queued stop for %d active worker(s).", n)
        return {"workers_stopped": n}

    def _sync_worker_lifecycle(self, conn: Any) -> None:
        """Reconcile commands, then disable active workers with no recent poll."""
        self._reconcile_worker_commands(conn)
        self._disable_stale_workers(conn)

    def sync_worker_lifecycle(self) -> None:
        """Public entry point for the background timer (job_cleaner)."""
        with self.get_connection() as conn:
            self._backfill_worker_identity_columns(conn)
            self._sync_worker_lifecycle(conn)
            self._maybe_stop_workers_when_all_jobs_complete_locked(conn)
            conn.commit()
        logging.debug("sync_worker_lifecycle: reconcile + stale-disable pass complete.")

    def _disable_stale_workers(self, conn: Any) -> None:
        """Move active workers with no recent poll to disabled lifecycle."""
        now = time.time()
        stale_threshold = now - WORKER_STALE_SECONDS
        cur = conn.cursor()
        cur.execute(
            "SELECT worker_id, current_job_id, last_poll_at "
            "FROM workers "
            "WHERE lifecycle_status = %s "
            "  AND last_poll_at > 0 "
            "  AND last_poll_at < %s "
            "  AND NOT (desired_version > applied_version AND desired_state = %s)",
            (WORKER_LIFECYCLE_ACTIVE, stale_threshold, WORKER_STATE_STOP),
        )
        for row in cur.fetchall():
            row = dict(row)
            last_poll = float(row.get("last_poll_at") or 0)
            wid = row["worker_id"]
            mins = max(1, round((now - last_poll) / 60))
            job_id = row.get("current_job_id")
            if job_id is not None:
                self._abort_served_job_locked(
                    conn,
                    int(job_id),
                    f"Job aborted: worker disabled after {mins} minutes with no poll (worker {wid}).",
                    wid,
                )
            self._append_worker_history(
                conn, wid,
                f"Worker disabled — no poll for {mins} minutes "
                f"(machine shutdown, process stopped, or network loss).",
                event="disabled",
            )
            cur.execute(
                """
                UPDATE workers SET lifecycle_status = %s, disabled_at = %s,
                    reported_status = %s, current_job_id = NULL
                WHERE worker_id = %s
                """,
                (WORKER_LIFECYCLE_DISABLED, now, WORKER_REPORTED_IDLE, wid),
            )

    def _worker_row_to_dict(
        self, row: Any, *, include_history: bool = False,
    ) -> Dict[str, Any]:
        """Convert a worker DB row to a dict."""
        d = dict(row)
        now = time.time()
        d["stale"] = (now - float(d.get("last_poll_at") or 0)) > WORKER_STALE_SECONDS
        d["pending"] = int(d.get("desired_version") or 0) > int(d.get("applied_version") or 0)
        d["lifecycle_status"] = d.get("lifecycle_status") or WORKER_LIFECYCLE_ACTIVE
        try:
            d["system_metrics"] = json.loads(d.get("system_metrics") or "{}")
        except json.JSONDecodeError:
            d["system_metrics"] = {}
        d.pop("history", None)
        d["history_total"] = 0
        h, inst, slot = self.parse_worker_id_parts(d.get("worker_id", ""))
        if not d.get("host"):
            d["host"] = h
        if not d.get("instance"):
            d["instance"] = inst
        if d.get("slot") in (None, ""):
            d["slot"] = slot
        return d

    @staticmethod
    def _count_worker_history(conn: Any, worker_id: str) -> int:
        """Return the total number of history entries for one worker."""
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM worker_history WHERE worker_id = %s", (worker_id,)
        )
        row = cur.fetchone()
        return int(row['cnt']) if row else 0

    def _backfill_worker_identity_columns(self, conn: Any) -> None:
        cur = conn.cursor()
        cur.execute(
            "SELECT worker_id, host, instance, slot FROM workers "
            "WHERE instance = '' OR host = ''"
        )
        for row in cur.fetchall():
            h, inst, slot = self.parse_worker_id_parts(row["worker_id"])
            cur.execute(
                "UPDATE workers SET host = %s, instance = %s, slot = %s WHERE worker_id = %s",
                (h, inst, slot, row["worker_id"]),
            )

    def _list_worker_rows(
        self,
        lifecycle: Optional[str] = None,
        host: Optional[str] = None,
        instance: Optional[str] = None,
        slot: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT {_WORKER_HOT_COLS} FROM workers "
                "ORDER BY host, instance, slot, worker_id"
            )
            rows = [
                self._worker_row_to_dict(r, include_history=False)
                for r in cur.fetchall()
            ]
        if lifecycle == WORKER_LIST_PENDING:
            rows = [
                r for r in rows
                if r.get("pending")
                and (r.get("desired_state") or WORKER_STATE_RUN) != WORKER_STATE_STOP
            ]
        elif lifecycle == WORKER_LIST_PAUSED:
            rows = [
                r for r in rows
                if r.get("lifecycle_status") == WORKER_LIFECYCLE_ACTIVE
                and not r.get("pending")
                and (r.get("desired_state") or WORKER_STATE_RUN) == WORKER_STATE_PAUSE
            ]
        elif lifecycle == WORKER_LIFECYCLE_ACTIVE:
            rows = [
                r for r in rows
                if r.get("lifecycle_status") == WORKER_LIFECYCLE_ACTIVE
                and not r.get("pending")
                and (r.get("desired_state") or WORKER_STATE_RUN) in (
                    WORKER_STATE_RUN,
                    WORKER_STATE_DRAIN,
                )
            ]
        elif lifecycle == WORKER_LIFECYCLE_DISABLED:
            rows = [
                r for r in rows
                if (r.get("lifecycle_status") or WORKER_LIFECYCLE_ACTIVE)
                == WORKER_LIFECYCLE_DISABLED
                or (r.get("desired_state") or WORKER_STATE_RUN) == WORKER_STATE_STOP
            ]
        elif lifecycle:
            rows = [r for r in rows if r.get("lifecycle_status") == lifecycle]
        if host:
            rows = [r for r in rows if (r.get("host") or "") == host]
        if instance:
            rows = [r for r in rows if (r.get("instance") or "") == instance]
        if slot is not None:
            rows = [r for r in rows if int(r.get("slot") or 0) == int(slot)]
        return rows

    def get_worker_summary(self) -> Dict[str, int]:
        """Single-pass worker summary — one query, no per-request lifecycle sync."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT lifecycle_status, reported_status, desired_state, "
                "       desired_version, applied_version "
                "FROM workers"
            )
            all_rows = [dict(r) for r in cur.fetchall()]

        total = busy = idle = pending_cmds = paused = disabled = 0
        for r in all_rows:
            lc = r.get("lifecycle_status") or WORKER_LIFECYCLE_ACTIVE
            ds = r.get("desired_state") or WORKER_STATE_RUN
            has_pending = int(r.get("desired_version") or 0) > int(r.get("applied_version") or 0)
            rs = r.get("reported_status") or WORKER_REPORTED_IDLE

            if lc == WORKER_LIFECYCLE_DISABLED or ds == WORKER_STATE_STOP:
                disabled += 1
                continue
            total += 1
            if has_pending and ds != WORKER_STATE_STOP:
                pending_cmds += 1
            elif ds == WORKER_STATE_PAUSE:
                paused += 1
            if rs == WORKER_REPORTED_BUSY:
                busy += 1
            elif rs == WORKER_REPORTED_IDLE:
                idle += 1

        return {
            "total": total,
            "busy": busy,
            "idle": idle,
            "paused": paused,
            "pending_commands": pending_cmds,
            "disabled": disabled,
        }

    def get_worker_filters(self, lifecycle: Optional[str] = None) -> Dict[str, Any]:
        rows = self._list_worker_rows(lifecycle=lifecycle)
        hosts = sorted({r.get("host") or "unknown" for r in rows})
        instances_by_host: Dict[str, List[str]] = {}
        slots_by_host_instance: Dict[str, List[int]] = {}
        for r in rows:
            h = r.get("host") or "unknown"
            inst = r.get("instance") or ""
            sl = int(r.get("slot") or 0)
            if inst:
                instances_by_host.setdefault(h, set()).add(inst)
                key = f"{h}|{inst}"
                slots_by_host_instance.setdefault(key, set()).add(sl)
        return {
            "hosts": hosts,
            "instances_by_host": {k: sorted(v) for k, v in instances_by_host.items()},
            "slots_by_host_instance": {
                k: sorted(v) for k, v in slots_by_host_instance.items()
            },
        }

    def list_workers(
        self,
        lifecycle: Optional[str] = None,
        host: Optional[str] = None,
        instance: Optional[str] = None,
        slot: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        return self._list_worker_rows(
            lifecycle=lifecycle, host=host, instance=instance, slot=slot,
        )

    @staticmethod
    def _worker_list_filters_sql(
        lifecycle: Optional[str] = None,
        host: Optional[str] = None,
        instance: Optional[str] = None,
        slot: Optional[int] = None,
        search: Optional[str] = None,
    ) -> tuple:
        clauses: List[str] = []
        params: List[Any] = []
        if lifecycle == WORKER_LIST_PENDING:
            clauses.append("desired_version > applied_version")
            clauses.append("desired_state != %s")
            params.append(WORKER_STATE_STOP)
        elif lifecycle == WORKER_LIST_PAUSED:
            clauses.append("lifecycle_status = %s")
            params.append(WORKER_LIFECYCLE_ACTIVE)
            clauses.append("desired_version <= applied_version")
            clauses.append("desired_state = %s")
            params.append(WORKER_STATE_PAUSE)
        elif lifecycle == WORKER_LIFECYCLE_ACTIVE:
            clauses.append("lifecycle_status = %s")
            params.append(WORKER_LIFECYCLE_ACTIVE)
            clauses.append("desired_version <= applied_version")
            clauses.append("desired_state = ANY(%s)")
            params.append([WORKER_STATE_RUN, WORKER_STATE_DRAIN])
        elif lifecycle == WORKER_LIFECYCLE_DISABLED:
            clauses.append("(lifecycle_status = %s OR desired_state = %s)")
            params.extend([WORKER_LIFECYCLE_DISABLED, WORKER_STATE_STOP])
        elif lifecycle:
            clauses.append("lifecycle_status = %s")
            params.append(lifecycle)
        if host:
            clauses.append("host = %s")
            params.append(host)
        if instance:
            clauses.append("instance = %s")
            params.append(instance)
        if slot is not None:
            clauses.append("slot = %s")
            params.append(int(slot))
        q = (search or "").strip().lower()
        if q:
            clauses.append("(LOWER(worker_id) = %s OR LOWER(worker_id) LIKE %s)")
            params.extend([q, q + "_%"])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def list_workers_paginated(
        self,
        page: int = 1,
        per_page: int = 50,
        lifecycle: Optional[str] = None,
        host: Optional[str] = None,
        instance: Optional[str] = None,
        slot: Optional[int] = None,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        page = max(1, int(page))
        per_page = max(1, min(int(per_page), 500))
        where, params = self._worker_list_filters_sql(
            lifecycle, host, instance, slot, search,
        )
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) AS cnt FROM workers{where}", params)
            total_count = int(cur.fetchone()['cnt'])
            total_pages = (
                (total_count + per_page - 1) // per_page if total_count else 0
            )
            if total_count == 0:
                return {
                    "workers": [],
                    "total_count": 0,
                    "current_page": 1,
                    "total_pages": 0,
                    "per_page": per_page,
                }
            page = min(page, total_pages)
            offset = (page - 1) * per_page
            cur.execute(
                f"SELECT {_WORKER_HOT_COLS} FROM workers{where} "
                "ORDER BY host, instance, slot, worker_id "
                "LIMIT %s OFFSET %s",
                params + [per_page, offset],
            )
            workers = [
                self._worker_row_to_dict(r, include_history=False)
                for r in cur.fetchall()
            ]
        return {
            "workers": workers,
            "total_count": total_count,
            "current_page": page,
            "total_pages": total_pages,
            "per_page": per_page,
        }

    def count_completed_jobs_by_workers(
        self, worker_ids: Optional[List[str]] = None,
    ) -> Dict[str, int]:
        """Count DONE jobs per worker id."""
        assignee = "COALESCE(NULLIF(worker_id, ''), requested_by)"
        with self.get_connection() as conn:
            cur = conn.cursor()
            if worker_ids:
                ids = [w for w in worker_ids if w]
                if not ids:
                    return {}
                cur.execute(
                    f"""
                    SELECT {assignee} AS wid, COUNT(*) AS n
                    FROM jobs
                    WHERE status = %s AND {assignee} = ANY(%s)
                    GROUP BY wid
                    """,
                    [STATUS_DONE, ids],
                )
            else:
                cur.execute(
                    f"""
                    SELECT {assignee} AS wid, COUNT(*) AS n
                    FROM jobs
                    WHERE status = %s AND {assignee} != ''
                    GROUP BY wid
                    """,
                    (STATUS_DONE,),
                )
            return {row['wid']: int(row['n']) for row in cur.fetchall()}

    def get_worker(
        self, worker_id: str, *, include_history: bool = False,
    ) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT {_WORKER_HOT_COLS} FROM workers WHERE worker_id = %s",
                (worker_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            history_total = self._count_worker_history(conn, worker_id)
        d = self._worker_row_to_dict(row, include_history=False)
        d["history_total"] = history_total
        return d

    def get_worker_history_page(
        self,
        worker_id: str,
        page: int = 0,
        page_size: int = 10,
        metrics_only: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Paginated worker history (newest first)."""
        page = max(0, int(page))
        page_size = max(1, min(int(page_size), 100))
        offset = page * page_size

        metrics_filter = "AND metrics IS NOT NULL" if metrics_only else ""

        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM workers WHERE worker_id = %s", (worker_id,))
            if not cur.fetchone():
                return None

            cur.execute(
                f"SELECT COUNT(*) AS cnt FROM worker_history WHERE worker_id = %s {metrics_filter}",
                (worker_id,),
            )
            total = int(cur.fetchone()['cnt'])

            if metrics_only:
                cur.execute(
                    "SELECT reason, timestamp, event, metrics "
                    "FROM worker_history "
                    "WHERE worker_id = %s AND metrics IS NOT NULL "
                    "ORDER BY timestamp DESC LIMIT %s OFFSET %s",
                    (worker_id, page_size, offset),
                )
            else:
                cur.execute(
                    "SELECT reason, timestamp, event "
                    "FROM worker_history "
                    "WHERE worker_id = %s "
                    "ORDER BY timestamp DESC LIMIT %s OFFSET %s",
                    (worker_id, page_size, offset),
                )
            rows = cur.fetchall()

        out_entries = []
        for r in rows:
            entry: Dict[str, Any] = {
                "reason": r["reason"],
                "timestamp": r["timestamp"],
                "event": r["event"],
            }
            if metrics_only:
                try:
                    entry["metrics"] = json.loads(r["metrics"]) if r["metrics"] else {}
                except (json.JSONDecodeError, TypeError):
                    entry["metrics"] = {}
            out_entries.append(entry)

        total_pages = max(1, (total + page_size - 1) // page_size) if total else 0
        return {
            "entries": out_entries,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }

    def list_workers_by_host(self) -> Dict[str, List[Dict[str, Any]]]:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for row in self.list_workers():
            host = row.get("host") or "unknown"
            grouped.setdefault(host, []).append(row)
        return grouped

    def worker_heartbeat(
        self,
        worker_id: str,
        host: str,
        machine_type: str,
        reported_status: str,
        current_job_id: Optional[int],
        applied_version: int,
        system_metrics: Optional[Dict[str, Any]],
        jd_worker_version: str = "",
    ) -> Dict[str, Any]:
        """
        Register worker heartbeat; return desired state and optional job.

        The entire heartbeat — including job claiming and worker status updates —
        runs in a single database transaction. Job claiming uses FOR UPDATE SKIP LOCKED
        so concurrent heartbeats from different workers always claim distinct jobs.
        """
        now = time.time()
        metrics_json = json.dumps(system_metrics or {})
        status = reported_status if reported_status in (
            WORKER_REPORTED_IDLE, WORKER_REPORTED_BUSY
        ) else WORKER_REPORTED_IDLE
        parsed_host, parsed_inst, parsed_slot = self.parse_worker_id_parts(worker_id)
        if not host:
            host = parsed_host
        instance = parsed_inst
        slot = parsed_slot

        job_payload = None

        with self.get_connection() as conn:
            cur = conn.cursor()

            # ── Step 1: read current worker row ─────────────────────────────
            cur.execute(
                f"SELECT {_WORKER_HOT_COLS} FROM workers WHERE worker_id = %s",
                (worker_id,),
            )
            row = cur.fetchone()
            prev = dict(row) if row else None
            prev_applied = int(prev.get("applied_version") or 0) if prev else 0
            prev_status = prev.get("reported_status") if prev else None
            prev_job = prev.get("current_job_id") if prev else None
            was_disabled = (
                prev
                and (prev.get("lifecycle_status") or WORKER_LIFECYCLE_ACTIVE)
                == WORKER_LIFECYCLE_DISABLED
            )

            # ── Step 2: upsert worker row ────────────────────────────────────
            if row:
                cur.execute(
                    """
                    UPDATE workers SET
                        host = %s, instance = %s, slot = %s, machine_type = %s,
                        reported_status = %s, current_job_id = %s,
                        jd_worker_version = %s, last_poll_at = %s,
                        applied_version = GREATEST(applied_version, %s),
                        system_metrics = %s, lifecycle_status = %s,
                        disabled_at = 0
                    WHERE worker_id = %s
                    """,
                    (
                        host, instance, slot, machine_type, status, current_job_id,
                        jd_worker_version, now, int(applied_version),
                        metrics_json, WORKER_LIFECYCLE_ACTIVE, worker_id,
                    ),
                )
                if was_disabled:
                    self._append_worker_history(
                        conn, worker_id,
                        "Worker reconnected after being disabled.",
                        event="reconnected",
                        metrics=system_metrics,
                    )
                elif int(applied_version) > prev_applied:
                    desired = prev.get("desired_state") or WORKER_STATE_RUN
                    self._append_worker_history(
                        conn, worker_id,
                        f"Worker applied dashboard command: {desired} (version {applied_version}).",
                        event="command_applied",
                        metrics=system_metrics,
                    )
                if prev_status == WORKER_REPORTED_BUSY and status == WORKER_REPORTED_IDLE:
                    self._append_worker_history(
                        conn, worker_id,
                        f"Job #{prev_job or current_job_id or '?'} finished — worker idle.",
                        event="job_finished",
                        metrics=system_metrics,
                    )
                    # If the job is still SERVED (e.g. the client's explicit
                    # _update_status POST timed out and was never received), close
                    # it here so it doesn't stay stuck in running state forever.
                    if prev_job is not None:
                        assignee = _jobs_assignee_sql()
                        cur.execute(
                            f"""
                            UPDATE jobs
                            SET    status = %s,
                                   completion_timestamp = %s
                            WHERE  id     = %s
                              AND  status = %s
                              AND  {assignee} = %s
                            """,
                            (STATUS_DONE, now, int(prev_job), STATUS_SERVED, worker_id),
                        )
                        if cur.rowcount:
                            logging.info(
                                f"[heartbeat] Closed job {prev_job} → DONE via "
                                f"busy→idle transition for worker {worker_id} "
                                f"(client status update was not received)."
                            )
            else:
                cur.execute(
                    """
                    INSERT INTO workers
                    (worker_id, host, instance, slot, machine_type, reported_status,
                     current_job_id, jd_worker_version, last_poll_at, applied_version,
                     desired_state, desired_version, previous_desired_state,
                     system_metrics, lifecycle_status, first_poll_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 'run', %s, %s, %s)
                    """,
                    (
                        worker_id, host, instance, slot, machine_type, status,
                        current_job_id, jd_worker_version, now, int(applied_version),
                        WORKER_STATE_RUN, metrics_json, WORKER_LIFECYCLE_ACTIVE, now,
                    ),
                )
                self._append_worker_history(
                    conn, worker_id,
                    f"Worker registered (first poll) — id {worker_id}.",
                    event="registered",
                    metrics=system_metrics,
                )

            # ── Step 3: update job ping timestamp for busy workers ───────────
            if status == WORKER_REPORTED_BUSY and current_job_id is not None:
                cur.execute(
                    """
                    UPDATE jobs SET last_ping_timestamp = %s, system_metrics = %s
                    WHERE id = %s AND status = %s
                    """,
                    (now, metrics_json, int(current_job_id), STATUS_SERVED),
                )

            conn.commit()

            # ── Step 4: re-read fresh worker state ───────────────────────────
            cur.execute(
                f"SELECT {_WORKER_HOT_COLS} FROM workers WHERE worker_id = %s",
                (worker_id,),
            )
            worker = self._worker_row_to_dict(cur.fetchone())

            # ── Step 5: finalize stop if acknowledged ────────────────────────
            if (
                worker.get("desired_state") == WORKER_STATE_STOP
                and int(worker.get("applied_version") or 0)
                >= int(worker.get("desired_version") or 0)
                and (worker.get("lifecycle_status") or WORKER_LIFECYCLE_ACTIVE)
                == WORKER_LIFECYCLE_ACTIVE
            ):
                self._finalize_worker_stop_locked(
                    conn, worker, "Worker acknowledged stop command.",
                )
                conn.commit()
                cur.execute(
                    f"SELECT {_WORKER_HOT_COLS} FROM workers WHERE worker_id = %s",
                    (worker_id,),
                )
                worker = self._worker_row_to_dict(cur.fetchone())

            # ── Step 6: claim a job if worker is idle and eligible ───────────
            # FOR UPDATE SKIP LOCKED makes this fully atomic — no two workers
            # can claim the same job even under massive concurrency.
            if (
                status == WORKER_REPORTED_IDLE
                and worker["desired_state"] == WORKER_STATE_RUN
                and not worker["pending"]
                and worker["lifecycle_status"] == WORKER_LIFECYCLE_ACTIVE
            ):
                job = self._claim_job(cur, worker_id, system_metrics or {})
                if job:
                    # Update worker to busy in the SAME transaction as job claim
                    cur.execute(
                        """
                        UPDATE workers SET reported_status = %s, current_job_id = %s
                        WHERE worker_id = %s
                        """,
                        (WORKER_REPORTED_BUSY, job["id"], worker_id),
                    )
                    self._append_worker_history(
                        conn, worker_id,
                        f"Job #{job['id']} assigned to worker.",
                        event="job_assigned",
                        metrics=system_metrics,
                    )
                    conn.commit()

                    # Refresh worker after assignment
                    cur.execute(
                        f"SELECT {_WORKER_HOT_COLS} FROM workers WHERE worker_id = %s",
                        (worker_id,),
                    )
                    worker = self._worker_row_to_dict(cur.fetchone())

                    job_payload = {
                        "job_id": job["id"],
                        "parameters": job.get("parameters", {}),
                        "status": STATUS_SERVED,
                    }

            # ── Read live performance config from DB ──────────────────────────
            # Kept inside the connection context so cur remains valid.
            cur.execute("SELECT key, value FROM server_config WHERE key IN ("
                        "'heartbeat_busy','heartbeat_idle_jobs','heartbeat_idle_none',"
                        "'heartbeat_control_chunk','worker_stale_seconds',"
                        "'status_retry_count','status_retry_base_delay','status_retry_max_delay')")
            pcfg = {row['key']: int(row['value']) for row in cur.fetchall()}
            hb_busy       = pcfg.get("heartbeat_busy",          WORKER_POLL_INTERVAL_BUSY)
            hb_idle_jobs  = pcfg.get("heartbeat_idle_jobs",     WORKER_POLL_INTERVAL_IDLE_JOBS)
            hb_idle_none  = pcfg.get("heartbeat_idle_none",     WORKER_POLL_INTERVAL_IDLE_NONE)
            hb_ctrl_chunk = pcfg.get("heartbeat_control_chunk", 120)
            retry_count   = pcfg.get("status_retry_count",      8)
            retry_base    = pcfg.get("status_retry_base_delay", 5)
            retry_max     = pcfg.get("status_retry_max_delay",  120)

            # ── Adaptive next-heartbeat interval ──────────────────────────────
            if status == WORKER_REPORTED_BUSY or job_payload is not None:
                next_interval = hb_busy
            else:
                cur.execute(
                    "SELECT 1 FROM jobs WHERE status = %s LIMIT 1", (STATUS_PENDING,)
                )
                next_interval = hb_idle_jobs if cur.fetchone() else hb_idle_none

        return {
            "desired_state":   worker["desired_state"],
            "desired_version": worker["desired_version"],
            "applied_version": worker["applied_version"],
            "pending":         worker["pending"],
            "job":             job_payload,
            # ── Timing config (applied by worker dynamically) ──────────────
            "heartbeat_interval":    next_interval,  # primary interval for this tick
            "poll_interval":         next_interval,  # alias kept for older clients
            "control_chunk":         hb_ctrl_chunk,  # idle wait sub-interval
            # ── Status-update retry config ─────────────────────────────────
            "status_retry_count":      retry_count,
            "status_retry_base_delay": retry_base,
            "status_retry_max_delay":  retry_max,
        }

    def _worker_state_rank(self, state: str) -> int:
        return {
            WORKER_STATE_RUN: 0,
            WORKER_STATE_PAUSE: 1,
            WORKER_STATE_DRAIN: 2,
            WORKER_STATE_STOP: 3,
        }.get(state, 0)

    def _worker_is_active_target(self, row: Dict[str, Any]) -> bool:
        return (row.get("lifecycle_status") or WORKER_LIFECYCLE_ACTIVE) == WORKER_LIFECYCLE_ACTIVE

    def _worker_eligible_for_stop(self, row: Dict[str, Any]) -> bool:
        if not self._worker_is_active_target(row):
            return False
        return (row.get("reported_status") or WORKER_REPORTED_IDLE) in (
            WORKER_REPORTED_IDLE,
            WORKER_REPORTED_BUSY,
        )

    @staticmethod
    def _worker_matches_search(row: Dict[str, Any], search: Optional[str]) -> bool:
        """Match host, host_instance, host_instance_slot, or full worker_id prefix."""
        q = (search or "").strip().lower()
        if not q:
            return True
        wid = (row.get("worker_id") or "").lower()
        return wid == q or wid.startswith(q + "_")

    def _worker_matches_list_filters(
        self,
        row: Dict[str, Any],
        *,
        host: Optional[str] = None,
        instance: Optional[str] = None,
        slot: Optional[int] = None,
        search: Optional[str] = None,
    ) -> bool:
        if host and (row.get("host") or "") != host:
            return False
        if instance and (row.get("instance") or "") != instance:
            return False
        if slot is not None and int(row.get("slot") or 0) != int(slot):
            return False
        if not self._worker_matches_search(row, search):
            return False
        return True

    def _worker_matches_scope(
        self,
        row: Dict[str, Any],
        scope: str,
        target: Optional[str],
        worker_ids: Optional[List[str]] = None,
    ) -> bool:
        wid = row["worker_id"]
        host = row.get("host") or ""
        inst = row.get("instance") or ""
        if scope == "workers":
            ids = {w for w in (worker_ids or []) if w}
            return wid in ids
        if scope == "worker":
            return wid == target
        if scope == "host":
            return host == target
        if scope == "instance":
            parts = (target or "").split("|", 1)
            return len(parts) == 2 and host == parts[0] and inst == parts[1]
        if scope == "all":
            return True
        return False

    def _worker_command_would_apply(
        self, row: Dict[str, Any], action: str,
    ) -> bool:
        current = row.get("desired_state") or WORKER_STATE_RUN
        applied = int(row.get("applied_version") or 0)
        desired_v = int(row.get("desired_version") or 0)
        pending = desired_v > applied

        if action == WORKER_STATE_RUN:
            if current == WORKER_STATE_STOP and not pending:
                return False
            if pending and self._worker_state_rank(current) >= self._worker_state_rank(action):
                return True
            if not pending and current == WORKER_STATE_STOP:
                return False
            return not (current == WORKER_STATE_RUN and not pending)
        if self._worker_state_rank(action) >= self._worker_state_rank(current):
            new_state = action
            return not (new_state == current and not pending)
        return False

    def _worker_cancel_would_apply(self, row: Dict[str, Any]) -> bool:
        applied = int(row.get("applied_version") or 0)
        desired_v = int(row.get("desired_version") or 0)
        return desired_v > applied

    def _command_target_rows(
        self,
        rows: List[Dict[str, Any]],
        action: str,
        scope: str,
        target: Optional[str] = None,
        worker_ids: Optional[List[str]] = None,
        *,
        host: Optional[str] = None,
        instance: Optional[str] = None,
        slot: Optional[int] = None,
        search: Optional[str] = None,
        lifecycle: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Rows that would receive a dashboard command (after tab + filter + scope)."""
        out: List[Dict[str, Any]] = []
        for row in rows:
            if lifecycle and not self._worker_row_matches_lifecycle(row, lifecycle):
                continue
            if not self._worker_matches_list_filters(
                row, host=host, instance=instance, slot=slot, search=search,
            ):
                continue
            if not self._worker_matches_scope(row, scope, target, worker_ids):
                continue
            if action == WORKER_STATE_STOP:
                if not self._worker_eligible_for_stop(row):
                    continue
            elif action == "cancel":
                if not self._worker_is_active_target(row):
                    continue
                if not self._worker_cancel_would_apply(row):
                    continue
            elif not self._worker_is_active_target(row):
                continue
            else:
                if not self._worker_command_would_apply(row, action):
                    continue
            out.append(row)
        return out

    def _worker_row_matches_lifecycle(
        self, row: Dict[str, Any], lifecycle: str,
    ) -> bool:
        pending = int(row.get("desired_version") or 0) > int(row.get("applied_version") or 0)
        lc = row.get("lifecycle_status") or WORKER_LIFECYCLE_ACTIVE
        ds = row.get("desired_state") or WORKER_STATE_RUN
        if lifecycle == WORKER_LIST_PENDING:
            return pending and ds != WORKER_STATE_STOP
        if lifecycle == WORKER_LIST_PAUSED:
            return (
                lc == WORKER_LIFECYCLE_ACTIVE
                and not pending
                and ds == WORKER_STATE_PAUSE
            )
        if lifecycle == WORKER_LIFECYCLE_ACTIVE:
            return (
                lc == WORKER_LIFECYCLE_ACTIVE
                and not pending
                and ds in (WORKER_STATE_RUN, WORKER_STATE_DRAIN)
            )
        if lifecycle == WORKER_LIFECYCLE_DISABLED:
            return lc == WORKER_LIFECYCLE_DISABLED or ds == WORKER_STATE_STOP
        return True

    def preview_workers_action(
        self,
        action: str,
        scope: str,
        target: Optional[str] = None,
        worker_ids: Optional[List[str]] = None,
        *,
        lifecycle: Optional[str] = None,
        host: Optional[str] = None,
        instance: Optional[str] = None,
        slot: Optional[int] = None,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List workers that would be affected by a command or cancel."""
        sql_where, sql_params = self._worker_list_filters_sql(
            lifecycle, host, instance, slot, search,
        )
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT {_WORKER_HOT_COLS} FROM workers{sql_where}",
                sql_params,
            )
            rows = [dict(r) for r in cur.fetchall()]
        targets = self._command_target_rows(
            rows, action, scope, target, worker_ids,
            host=host, instance=instance, slot=slot, search=search, lifecycle=lifecycle,
        )
        workers = []
        for row in targets:
            w = dict(row)
            w["pending"] = int(w.get("desired_version") or 0) > int(w.get("applied_version") or 0)
            workers.append({
                "worker_id": w.get("worker_id"),
                "host": w.get("host"),
                "instance": w.get("instance"),
                "slot": w.get("slot"),
                "reported_status": w.get("reported_status"),
                "desired_state": w.get("desired_state"),
                "pending": w["pending"],
                "current_job_id": w.get("current_job_id"),
                "lifecycle_status": w.get("lifecycle_status"),
            })
        return {"workers": workers, "count": len(workers)}

    def set_workers_command(
        self,
        action: str,
        scope: str,
        target: Optional[str] = None,
        worker_ids: Optional[List[str]] = None,
        *,
        host: Optional[str] = None,
        instance: Optional[str] = None,
        slot: Optional[int] = None,
        search: Optional[str] = None,
        lifecycle: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Set desired worker state. Precedence: stop > drain > pause > run."""
        if action not in (
            WORKER_STATE_RUN,
            WORKER_STATE_PAUSE,
            WORKER_STATE_DRAIN,
            WORKER_STATE_STOP,
        ):
            raise ValueError(f"Invalid action: {action}")
        batch_id = secrets.token_hex(8) if scope in ("host", "all") else None
        affected = 0
        labels = {"run": "resume", "pause": "pause", "drain": "drain", "stop": "stop"}

        sql_where, sql_params = self._worker_list_filters_sql(
            lifecycle, host, instance, slot, search,
        )
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT {_WORKER_HOT_COLS} FROM workers{sql_where}",
                sql_params,
            )
            rows = [dict(r) for r in cur.fetchall()]
            targets = self._command_target_rows(
                rows, action, scope, target, worker_ids,
                host=host, instance=instance, slot=slot, search=search, lifecycle=lifecycle,
            )
            for row in targets:
                wid = row["worker_id"]
                current = row.get("desired_state") or WORKER_STATE_RUN
                applied = int(row.get("applied_version") or 0)
                desired_v = int(row.get("desired_version") or 0)
                pending = desired_v > applied

                new_state = WORKER_STATE_RUN if action == WORKER_STATE_RUN else action
                prev = (
                    current if pending
                    else row.get("previous_desired_state") or WORKER_STATE_RUN
                )
                new_version = desired_v + 1
                cur.execute(
                    """
                    UPDATE workers SET
                        previous_desired_state = %s,
                        desired_state = %s,
                        desired_version = %s,
                        pending_batch_id = %s
                    WHERE worker_id = %s
                    """,
                    (prev, new_state, new_version, batch_id, wid),
                )
                self._append_worker_history(
                    conn, wid,
                    f"Dashboard queued {labels.get(action, action)} command "
                    f"(applies on next poll, ~3 min).",
                    event="command_queued",
                )
                affected += 1
            conn.commit()
        return {"affected": affected, "batch_id": batch_id}

    def cancel_pending_worker_commands(
        self,
        scope: str,
        target: Optional[str] = None,
        worker_ids: Optional[List[str]] = None,
        *,
        host: Optional[str] = None,
        instance: Optional[str] = None,
        slot: Optional[int] = None,
        search: Optional[str] = None,
        lifecycle: Optional[str] = None,
    ) -> int:
        """Revert queued (not yet applied) commands to previous_desired_state."""
        reverted = 0
        sql_where, sql_params = self._worker_list_filters_sql(
            lifecycle, host, instance, slot, search,
        )
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT {_WORKER_HOT_COLS} FROM workers{sql_where}",
                sql_params,
            )
            rows = [dict(r) for r in cur.fetchall()]
            targets = self._command_target_rows(
                rows, "cancel", scope, target, worker_ids,
                host=host, instance=instance, slot=slot, search=search, lifecycle=lifecycle,
            )
            for row in targets:
                wid = row["worker_id"]
                prev = row.get("previous_desired_state") or WORKER_STATE_RUN
                cancelled = row.get("desired_state") or WORKER_STATE_RUN
                cur.execute(
                    """
                    UPDATE workers SET
                        desired_state = %s,
                        desired_version = desired_version + 1,
                        previous_desired_state = %s
                    WHERE worker_id = %s
                    """,
                    (prev, cancelled, wid),
                )
                self._append_worker_history(
                    conn, wid,
                    f"Dashboard cancelled queued {cancelled} command.",
                    event="command_cancelled",
                )
                reverted += 1
            conn.commit()
        return reverted

    def handle_cli_worker_stop(
        self,
        worker_id: str,
        source: str = "",
        action: str = "stop",
        job_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Record a CLI-initiated worker stop in server history; abort SERVED job if given."""
        now = time.time()
        host, instance, slot = self.parse_worker_id_parts(worker_id)
        source = (source or "unknown").strip()
        action_label = (action or "stop").replace("_", " ")
        reason = (
            f"Worker stopped from CLI (jd_worker_cli {action_label}) "
            f"on machine '{source}'."
        )
        job_aborted = False

        with self.get_connection() as conn:
            self._backfill_worker_identity_columns(conn)
            cur = conn.cursor()
            cur.execute("SELECT * FROM workers WHERE worker_id = %s", (worker_id,))
            row = cur.fetchone()
            if row:
                row_d = dict(row)
                desired_v = int(row_d.get("desired_version") or 0) + 1
                cur.execute(
                    """
                    UPDATE workers SET
                        host = %s, instance = %s, slot = %s,
                        lifecycle_status = %s, disabled_at = %s,
                        reported_status = %s, current_job_id = NULL,
                        desired_state = %s,
                        desired_version = %s,
                        applied_version = %s
                    WHERE worker_id = %s
                    """,
                    (
                        host, instance, slot,
                        WORKER_LIFECYCLE_DISABLED, now,
                        WORKER_REPORTED_IDLE, WORKER_STATE_STOP,
                        desired_v, desired_v,
                        worker_id,
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO workers
                    (worker_id, host, instance, slot, machine_type, reported_status,
                     last_poll_at, applied_version, desired_state, desired_version,
                     previous_desired_state, lifecycle_status, disabled_at,
                     first_poll_at)
                    VALUES (%s, %s, %s, %s, 'worker', 'idle', %s, 0, 'stop', 0, 'run', %s, %s, %s)
                    """,
                    (
                        worker_id, host, instance, slot, now,
                        WORKER_LIFECYCLE_DISABLED, now, now,
                    ),
                )
            self._append_worker_history(conn, worker_id, reason, event="cli_stop")
            conn.commit()

        if job_id is not None:
            abort_msg = (
                f"Job aborted: worker killed from CLI (jd_worker_cli {action_label}) "
                f"on '{source}' (worker {worker_id})."
            )
            job_aborted = self.update_job_status(int(job_id), STATUS_ABORTED, abort_msg)

        return {
            "worker_id": worker_id,
            "recorded": True,
            "job_aborted": job_aborted,
        }

    def handle_cli_clear_all(
        self,
        workers: List[Dict[str, Any]],
        source: str = "",
    ) -> Dict[str, Any]:
        """Record CLI clear_all for many workers (each gets history + optional job abort)."""
        source = (source or "unknown").strip()
        results = []
        for item in workers:
            wid = (item.get("worker_id") or "").strip()
            if not wid:
                continue
            job_id = item.get("job_id")
            if job_id is not None:
                try:
                    job_id = int(job_id)
                except (TypeError, ValueError):
                    job_id = None
            results.append(
                self.handle_cli_worker_stop(
                    wid, source=source, action="clear_all", job_id=job_id,
                )
            )
        return {"processed": len(results), "workers": results}
