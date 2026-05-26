import hashlib
import os
import re
import secrets
import sqlite3
import json
import logging
import threading
import time
from contextlib import contextmanager
from typing import List, Dict, Any, Optional

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
WORKER_STALE_SECONDS = 600
WORKER_POLL_INTERVAL_IDLE = 180
WORKER_POLL_INTERVAL_BUSY = 57
WORKER_POLL_INTERVAL = WORKER_POLL_INTERVAL_IDLE  # backward-compatible alias
WORKER_LIFECYCLE_ACTIVE = "active"
WORKER_LIFECYCLE_DISABLED = "disabled"
WORKER_LIST_PENDING = "pending"  # dashboard list filter (queued commands)
_WORKER_INSTANCE_RE = re.compile(r"^(?:[a-z]{1,6}|[0-9A-Za-z]{6})$")


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


class JobDatabase:
    """SQLite database handler for job distribution system."""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.lock = threading.Lock()
        self._init_database()
    
    def _init_database(self):
        """Initialize the database with the jobs and api_stats tables."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY,
                    requested_by TEXT DEFAULT '',
                    request_timestamp REAL DEFAULT 0,
                    completion_timestamp REAL DEFAULT 0,
                    required_time REAL DEFAULT 0,
                    predicted_runtime REAL DEFAULT 0,
                    last_ping_timestamp REAL DEFAULT 0,
                    status TEXT DEFAULT 'PENDING',
                    message TEXT DEFAULT '[]',
                    parameters TEXT NOT NULL,
                    system_metrics TEXT DEFAULT '{}'
                )
            ''')
            
            # Add system_metrics column if it doesn't exist (for existing databases)
            try:
                cursor.execute('ALTER TABLE jobs ADD COLUMN system_metrics TEXT DEFAULT "{}"')
            except sqlite3.OperationalError:
                # Column already exists, ignore
                pass
            
            # Add predicted_runtime column if it doesn't exist (for existing databases)
            try:
                cursor.execute('ALTER TABLE jobs ADD COLUMN predicted_runtime REAL DEFAULT 0')
            except sqlite3.OperationalError:
                # Column already exists, ignore
                pass
            
            # Add initialization_timestamp column if it doesn't exist (for existing databases)
            try:
                cursor.execute('ALTER TABLE jobs ADD COLUMN initialization_timestamp REAL DEFAULT 0')
            except sqlite3.OperationalError:
                # Column already exists, ignore
                pass
            try:
                cursor.execute('ALTER TABLE jobs ADD COLUMN worker_id TEXT DEFAULT ""')
            except sqlite3.OperationalError:
                pass
            cursor.execute(
                'UPDATE jobs SET worker_id = requested_by '
                'WHERE COALESCE(worker_id, "") = "" '
                'AND COALESCE(requested_by, "") != ""'
            )
            cursor.execute(
                'CREATE INDEX IF NOT EXISTS idx_jobs_worker_id ON jobs(worker_id)'
            )
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS api_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    endpoint TEXT NOT NULL,
                    method TEXT NOT NULL,
                    request_count INTEGER DEFAULT 0,
                    last_updated REAL DEFAULT 0,
                    UNIQUE(endpoint, method)
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS server_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            ''')

            # Seed defaults only when the rows do not yet exist
            cursor.execute('''
                INSERT OR IGNORE INTO server_config (key, value)
                VALUES
                    ('idle_timeout',              '600'),
                    ('aborted_job_reset_timeout', '1200'),
                    ('traffic_server_in',    '0'),
                    ('traffic_server_out',   '0'),
                    ('traffic_dashboard_in', '0'),
                    ('traffic_dashboard_out','0')
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sessions (
                    token      TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
            ''')
            cursor.execute(
                'CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)'
            )

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS uploads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    uploaded_at REAL NOT NULL,
                    UNIQUE(job_id, version)
                )
            ''')
            cursor.execute(
                'CREATE INDEX IF NOT EXISTS idx_uploads_job_id ON uploads(job_id)'
            )

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS workers (
                    worker_id              TEXT PRIMARY KEY,
                    host                   TEXT NOT NULL DEFAULT '',
                    machine_type           TEXT NOT NULL DEFAULT 'worker',
                    reported_status        TEXT NOT NULL DEFAULT 'idle',
                    current_job_id         INTEGER,
                    jd_worker_version      TEXT NOT NULL DEFAULT '',
                    last_poll_at           REAL NOT NULL DEFAULT 0,
                    applied_version        INTEGER NOT NULL DEFAULT 0,
                    desired_state          TEXT NOT NULL DEFAULT 'run',
                    desired_version        INTEGER NOT NULL DEFAULT 0,
                    previous_desired_state TEXT NOT NULL DEFAULT 'run',
                    pending_batch_id       TEXT,
                    system_metrics         TEXT NOT NULL DEFAULT '{}'
                )
            ''')
            cursor.execute(
                'CREATE INDEX IF NOT EXISTS idx_workers_host ON workers(host)'
            )
            cursor.execute(
                'CREATE INDEX IF NOT EXISTS idx_workers_last_poll ON workers(last_poll_at)'
            )
            for col, typedef in (
                ("lifecycle_status", "TEXT NOT NULL DEFAULT 'active'"),
                ("instance", "TEXT NOT NULL DEFAULT ''"),
                ("slot", "INTEGER NOT NULL DEFAULT 0"),
                ("history", "TEXT NOT NULL DEFAULT '[]'"),
                ("disabled_at", "REAL NOT NULL DEFAULT 0"),
                ("first_poll_at", "REAL NOT NULL DEFAULT 0"),
            ):
                try:
                    cursor.execute(f"ALTER TABLE workers ADD COLUMN {col} {typedef}")
                except sqlite3.OperationalError:
                    pass
            cursor.execute(
                'CREATE INDEX IF NOT EXISTS idx_workers_lifecycle ON workers(lifecycle_status)'
            )
            
            # Create indexes for optimal query performance
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_jobs_status_id ON jobs(status, id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_jobs_last_ping ON jobs(last_ping_timestamp)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_jobs_status_ping ON jobs(status, last_ping_timestamp)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_jobs_requested_by ON jobs(requested_by)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_jobs_request_timestamp ON jobs(request_timestamp)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_jobs_completion_timestamp ON jobs(completion_timestamp)')
            
            conn.commit()
            logging.info(f"Database initialized with indexes at {self.db_path}")
    
    @contextmanager
    def get_connection(self):
        """Get a database connection with proper error handling."""
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.row_factory = sqlite3.Row  # Enable dict-like access to rows
            yield conn
        except Exception as e:
            if conn:
                conn.rollback()
            logging.error(f"Database error: {e}")
            raise
        finally:
            if conn:
                conn.close()
    
    def create_jobs(self, parameters_list: List[str], clear_api_stats: bool = True) -> int:
        """Replace ALL existing jobs with a fresh set. Also clears API stats by default."""
        with self.lock:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                # Clear existing jobs
                cursor.execute("DELETE FROM jobs")
                
                # Clear API stats if requested (for fresh starts)
                if clear_api_stats:
                    cursor.execute("DELETE FROM api_stats")
                    logging.info("API stats cleared for fresh start")
                
                # Insert new jobs
                jobs_data = []
                for i, params in enumerate(parameters_list):
                    jobs_data.append((
                        i,  # id
                        '',  # requested_by
                        0,   # request_timestamp
                        0,   # completion_timestamp
                        0,   # required_time
                        0,   # predicted_runtime
                        0,   # last_ping_timestamp
                        STATUS_PENDING,  # status
                        '[]',  # message
                        params,  # parameters
                        '{}',  # system_metrics
                        0    # initialization_timestamp
                    ))
                
                cursor.executemany('''
                    INSERT INTO jobs 
                    (id, requested_by, request_timestamp, completion_timestamp, 
                     required_time, predicted_runtime, last_ping_timestamp, status, message, parameters, system_metrics, initialization_timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', jobs_data)
                
                conn.commit()
                total_jobs = len(parameters_list)
                logging.info(f"Created {total_jobs} jobs in database")
                return total_jobs

    def append_jobs(self, parameters_list: List[str]) -> int:
        """Append new PENDING jobs without touching existing ones. IDs continue from the current maximum."""
        with self.lock:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                cursor.execute("SELECT COALESCE(MAX(id), -1) AS max_id FROM jobs")
                start_id = cursor.fetchone()['max_id'] + 1

                jobs_data = [
                    (
                        start_id + i,
                        '', 0, 0, 0, 0, 0,
                        STATUS_PENDING,
                        '[]',
                        params,
                        '{}',
                        0
                    )
                    for i, params in enumerate(parameters_list)
                ]

                cursor.executemany('''
                    INSERT INTO jobs
                    (id, requested_by, request_timestamp, completion_timestamp,
                     required_time, predicted_runtime, last_ping_timestamp,
                     status, message, parameters, system_metrics, initialization_timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', jobs_data)

                conn.commit()
                total_new = len(parameters_list)
                logging.info(f"Appended {total_new} jobs (starting at id={start_id})")
                return total_new

    def get_all_jobs(self) -> List[Dict[str, Any]]:
        """Get all jobs from the database."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM jobs ORDER BY id")
            rows = cursor.fetchall()
            
            jobs = []
            for row in rows:
                job = dict(row)
                job['message'] = _parse_job_message(job['message'])
                try:
                    job['parameters'] = json.loads(job['parameters'])
                except json.JSONDecodeError:
                    job['parameters'] = {}
                try:
                    job['system_metrics'] = json.loads(job.get('system_metrics', '{}'))
                except (json.JSONDecodeError, KeyError):
                    job['system_metrics'] = {}
                enrich_job_record(job)
                jobs.append(job)
            
            return jobs
    
    def get_config_value(self, key: str, default: str = "") -> str:
        """Read a value from server_config. Returns default if key is absent."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM server_config WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row['value'] if row else default

    def set_config_value(self, key: str, value: str) -> None:
        """Insert or update a key in server_config."""
        with self.lock:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO server_config (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value)
                )
                conn.commit()

    def get_all_config(self) -> Dict[str, str]:
        """Return the full server_config table as a dict."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM server_config")
            return {row['key']: row['value'] for row in cursor.fetchall()}

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
        with self.lock:
            with self.get_connection() as conn:
                conn.execute(
                    'INSERT INTO sessions (token, created_at, expires_at) VALUES (?, ?, ?)',
                    (token, now, now + self.SESSION_DURATION)
                )
                conn.commit()
        return token

    def validate_session(self, token: str) -> bool:
        """Return True if the token exists and has not expired."""
        if not token:
            return False
        with self.get_connection() as conn:
            row = conn.execute(
                'SELECT expires_at FROM sessions WHERE token = ?', (token,)
            ).fetchone()
        return bool(row and time.time() < row['expires_at'])

    def delete_session(self, token: str) -> None:
        """Delete a single session (logout)."""
        with self.lock:
            with self.get_connection() as conn:
                conn.execute('DELETE FROM sessions WHERE token = ?', (token,))
                conn.commit()

    def clear_all_sessions(self) -> None:
        """Invalidate every active session (used after PIN override)."""
        with self.lock:
            with self.get_connection() as conn:
                conn.execute('DELETE FROM sessions')
                conn.commit()

    def cleanup_expired_sessions(self) -> None:
        """Prune expired sessions from the table."""
        with self.lock:
            with self.get_connection() as conn:
                conn.execute('DELETE FROM sessions WHERE expires_at < ?', (time.time(),))
                conn.commit()

    # ─────────────────────────────────────────────────────────────────────────

    def add_traffic(self, source: str, bytes_in: int, bytes_out: int) -> None:
        """Atomically add bytes_in / bytes_out to the named source counters.
        source should be 'server' or 'dashboard'.
        Uses a single SQL upsert per counter so the increment is atomic
        even when multiple threads write concurrently.
        """
        if bytes_in <= 0 and bytes_out <= 0:
            return
        with self.lock:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                for key, delta in [
                    (f"traffic_{source}_in",  bytes_in),
                    (f"traffic_{source}_out", bytes_out),
                ]:
                    if delta > 0:
                        cursor.execute(
                            "INSERT INTO server_config (key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + ?",
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
        with self.lock:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                now = time.time()
                
                # Insert or update the API stats
                cursor.execute('''
                    INSERT INTO api_stats (endpoint, method, request_count, last_updated)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(endpoint, method) 
                    DO UPDATE SET 
                        request_count = request_count + 1,
                        last_updated = ?
                ''', (endpoint, method, now, now))
                
                conn.commit()
    
    def get_api_stats(self) -> List[Dict[str, Any]]:
        """Get API request statistics."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT endpoint, method, request_count, last_updated 
                FROM api_stats 
                ORDER BY request_count DESC
            ''')
            rows = cursor.fetchall()
            
            stats = []
            for row in rows:
                stats.append({
                    'endpoint': row['endpoint'],
                    'method': row['method'],
                    'request_count': row['request_count'],
                    'last_updated': row['last_updated']
                })
            
            return stats
    
    def clear_api_stats(self) -> bool:
        """Clear all API request statistics."""
        with self.lock:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM api_stats")
                conn.commit()
                logging.info("API stats cleared")
                return True
    
    def get_database_info(self) -> Dict[str, Any]:
        """Get database information including indexes and table sizes."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Get table sizes
            cursor.execute("SELECT COUNT(*) as count FROM jobs")
            jobs_count = cursor.fetchone()['count']
            
            cursor.execute("SELECT COUNT(*) as count FROM api_stats")
            api_stats_count = cursor.fetchone()['count']
            
            # Get indexes
            cursor.execute("""
                SELECT name, sql FROM sqlite_master 
                WHERE type='index' AND tbl_name='jobs'
                ORDER BY name
            """)
            indexes = [{'name': row['name'], 'sql': row['sql']} for row in cursor.fetchall()]
            
            # Get table schema
            cursor.execute("PRAGMA table_info(jobs)")
            schema = [{'name': row['name'], 'type': row['type']} for row in cursor.fetchall()]
            
            return {
                'jobs_count': jobs_count,
                'api_stats_count': api_stats_count,
                'indexes': indexes,
                'schema': schema
            }
    
    def get_job_by_id(self, job_id: int) -> Optional[Dict[str, Any]]:
        """Get a specific job by ID."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            row = cursor.fetchone()
            
            if row:
                job = dict(row)
                job['message'] = _parse_job_message(job['message'])
                try:
                    job['parameters'] = json.loads(job['parameters'])
                except json.JSONDecodeError:
                    job['parameters'] = {}
                try:
                    job['system_metrics'] = json.loads(job.get('system_metrics', '{}'))
                except (json.JSONDecodeError, KeyError):
                    job['system_metrics'] = {}
                return enrich_job_record(job)
            return None

    def get_jobs_paginated(self, page: int = 1, per_page: int = 50, status: str = None, search_job_id: str = None) -> Dict[str, Any]:
        """
        Get jobs with pagination support.
        
        Args:
            page: Page number (1-based)
            per_page: Number of jobs per page
            status: Filter by status (optional)
            search_job_id: Search by job ID (optional)
        
        Returns:
            Dict with jobs, total_count, total_pages, current_page
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Build WHERE clause
            where_conditions = []
            params = []
            
            if status:
                where_conditions.append("status = ?")
                params.append(status)
            
            if search_job_id:
                where_conditions.append("id = ?")
                params.append(int(search_job_id))
            
            where_clause = " WHERE " + " AND ".join(where_conditions) if where_conditions else ""
            
            # Get total count
            count_query = f"SELECT COUNT(*) as count FROM jobs{where_clause}"
            cursor.execute(count_query, params)
            total_count = cursor.fetchone()['count']
            
            # Calculate pagination
            total_pages = (total_count + per_page - 1) // per_page
            offset = (page - 1) * per_page
            
            # Get jobs for current page
            jobs_query = f"""
                SELECT * FROM jobs{where_clause}
                ORDER BY id
                LIMIT ? OFFSET ?
            """
            cursor.execute(jobs_query, params + [per_page, offset])
            rows = cursor.fetchall()
            
            jobs = []
            for row in rows:
                job = dict(row)
                job['message'] = _parse_job_message(job['message'])
                try:
                    job['parameters'] = json.loads(job['parameters'])
                except json.JSONDecodeError:
                    job['parameters'] = {}
                try:
                    job['system_metrics'] = json.loads(job.get('system_metrics', '{}'))
                except (json.JSONDecodeError, KeyError):
                    job['system_metrics'] = {}
                enrich_job_record(job)
                jobs.append(job)
            
            return {
                'jobs': jobs,
                'total_count': total_count,
                'total_pages': total_pages,
                'current_page': page,
                'per_page': per_page
            }
    
    def request_job(self, worker_id: str, system_metrics: Optional[Dict[str, Any]] = None, 
                    job_id: Optional[int] = None, predicted_runtime: Optional[float] = None,
                    initialization_timestamp: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """
        Assign a PENDING job to a requester and mark it as SERVED.
        
        Args:
            worker_id: Full worker id (``host_instance_slot``)
            system_metrics: System metrics from the worker (optional)
            job_id: Specific job ID to assign (if None, selects based on prediction)
            predicted_runtime: Predicted runtime in seconds (optional)
        
        Returns:
            Assigned job dictionary or None if no jobs available
        """
        with self.lock:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                # Find PENDING job (either specific ID or any)
                if job_id is not None:
                    cursor.execute(
                        "SELECT * FROM jobs WHERE id = ? AND status = ?",
                        (job_id, STATUS_PENDING)
                    )
                else:
                    cursor.execute(
                        "SELECT * FROM jobs WHERE status = ? ORDER BY id LIMIT 1",
                        (STATUS_PENDING,)
                    )
                
                row = cursor.fetchone()
                
                if not row:
                    return None
                
                job = dict(row)
                timestamp = time.time()
                
                # Parse existing messages
                try:
                    messages = json.loads(job['message'])
                except json.JSONDecodeError:
                    messages = []
                
                # Add new message
                wid = (worker_id or "").strip()
                messages.append({
                    "reason": f"{wid} requests this job for execution",
                    "timestamp": timestamp
                })
                
                # Store system_metrics as JSON string
                system_metrics_json = json.dumps(system_metrics) if system_metrics else '{}'
                
                # Update job with predicted_runtime if provided
                predicted_runtime_value = predicted_runtime if predicted_runtime is not None else 0.0
                
                # Use initialization_timestamp if provided, otherwise use current timestamp
                init_timestamp = initialization_timestamp if initialization_timestamp is not None else timestamp
                
                cursor.execute('''
                    UPDATE jobs 
                    SET worker_id = ?, requested_by = ?, status = ?, request_timestamp = ?, 
                        message = ?, system_metrics = ?, predicted_runtime = ?,
                        initialization_timestamp = ?
                    WHERE id = ?
                ''', (wid, wid, STATUS_SERVED, timestamp, json.dumps(messages), 
                      system_metrics_json, predicted_runtime_value, init_timestamp, job['id']))
                
                conn.commit()
                
                # Return updated job
                job['worker_id'] = wid
                job['requested_by'] = wid
                job['status'] = STATUS_SERVED
                job['request_timestamp'] = timestamp
                job['initialization_timestamp'] = init_timestamp
                job['message'] = messages
                job['system_metrics'] = system_metrics if system_metrics else {}
                try:
                    job['parameters'] = json.loads(job['parameters'])
                except json.JSONDecodeError:
                    job['parameters'] = {}
                
                return job
    
    def get_pending_jobs(self) -> List[Dict[str, Any]]:
        """Get all PENDING jobs."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM jobs WHERE status = ? ORDER BY id", (STATUS_PENDING,))
            rows = cursor.fetchall()
            
            jobs = []
            for row in rows:
                job = dict(row)
                try:
                    job['parameters'] = json.loads(job['parameters'])
                except json.JSONDecodeError:
                    job['parameters'] = {}
                try:
                    job['system_metrics'] = json.loads(job.get('system_metrics', '{}'))
                except (json.JSONDecodeError, KeyError):
                    job['system_metrics'] = {}
                jobs.append(job)
            
            return jobs
    
    def update_job_status(self, job_id: int, status: str, message: str = "") -> bool:
        """Update job status to DONE or ABORTED."""
        if status not in [STATUS_DONE, STATUS_ABORTED]:
            return False
        
        with self.lock:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                # Get current job
                cursor.execute(
                    "SELECT * FROM jobs WHERE id = ? AND status = ?",
                    (job_id, STATUS_SERVED)
                )
                row = cursor.fetchone()
                
                if not row:
                    return False
                
                job = dict(row)
                now = time.time()
                required_time = now - job['request_timestamp']
                
                # Parse existing messages
                try:
                    messages = json.loads(job['message'])
                except json.JSONDecodeError:
                    messages = []
                
                # Add new message
                messages.append({
                    "reason": message if message else "No reason provided",
                    "timestamp": now
                })
                
                # Update job
                cursor.execute('''
                    UPDATE jobs 
                    SET status = ?, completion_timestamp = ?, required_time = ?, message = ?
                    WHERE id = ?
                ''', (status, now, required_time, json.dumps(messages), job_id))

                wid = job_worker_id(job)
                if wid:
                    self._touch_worker_presence_locked(
                        conn, wid,
                        current_job_id=None,
                        reported_status=WORKER_REPORTED_IDLE,
                    )
                
                conn.commit()
                return True
    
    def change_job_status(self, job_id: int, new_status: str, reason: str = "") -> bool:
        """Change job status for DONE, ABORTED, or PENDING jobs."""
        if new_status not in [STATUS_DONE, STATUS_ABORTED, STATUS_PENDING]:
            return False
        
        with self.lock:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                # Get current job
                cursor.execute(
                    "SELECT * FROM jobs WHERE id = ? AND status IN (?, ?, ?)",
                    (job_id, STATUS_DONE, STATUS_ABORTED, STATUS_PENDING)
                )
                row = cursor.fetchone()
                
                if not row:
                    return False
                
                job = dict(row)
                now = time.time()
                old_status = job['status']
                
                # Parse existing messages
                try:
                    messages = json.loads(job['message'])
                except json.JSONDecodeError:
                    messages = []
                
                # Add status change message
                status_change_message = f"Manual Status Change: {old_status} → {new_status}"
                if reason:
                    status_change_message += f" | Reason: {reason}"
                else:
                    status_change_message += " | No reason provided"
                
                messages.append({
                    "reason": status_change_message,
                    "timestamp": now
                })
                
                        # Update job status and reset timestamps if going to PENDING
                if new_status == STATUS_PENDING:
                    cursor.execute('''
                        UPDATE jobs 
                        SET status = ?, message = ?, request_timestamp = 0, 
                            completion_timestamp = 0, required_time = 0, 
                            last_ping_timestamp = 0, initialization_timestamp = 0,
                            worker_id = '', requested_by = ''
                        WHERE id = ?
                    ''', (new_status, json.dumps(messages), job_id))
                else:
                    cursor.execute('''
                        UPDATE jobs 
                        SET status = ?, message = ?
                        WHERE id = ?
                    ''', (new_status, json.dumps(messages), job_id))
                
                conn.commit()
                return True
    
    def reset_aborted_jobs(self) -> int:
        """Reset all ABORTED jobs to PENDING."""
        with self.lock:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                current_time = time.time()
                
                # Get all aborted jobs
                cursor.execute("SELECT * FROM jobs WHERE status = ?", (STATUS_ABORTED,))
                aborted_jobs = cursor.fetchall()
                
                count = 0
                for row in aborted_jobs:
                    job = dict(row)
                    prev_requester = job_worker_id(job)
                    
                    # Parse existing messages
                    try:
                        messages = json.loads(job['message'])
                    except json.JSONDecodeError:
                        messages = []
                    
                    # Add reset message
                    messages.append({
                        "reason": f"Job Cleaner: Reset job to PENDING status. Previous worker '{prev_requester}'. Job is now available for reassignment.",
                        "timestamp": current_time
                    })
                    
                    # Reset job
                    cursor.execute('''
                        UPDATE jobs 
                        SET status = ?, worker_id = '', requested_by = '',
                            request_timestamp = 0, 
                            completion_timestamp = 0, required_time = 0, 
                            last_ping_timestamp = 0, initialization_timestamp = 0, message = ?
                        WHERE id = ?
                    ''', (STATUS_PENDING, json.dumps(messages), job['id']))
                    
                    count += 1
                
                conn.commit()
                return count
    
    def reset_stale_served_jobs(self, idle_timeout: int) -> int:
        """Reset SERVED jobs that haven't pinged within the timeout."""
        with self.lock:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                current_time = time.time()
                cutoff_time = current_time - idle_timeout
                
                # Get stale served jobs
                cursor.execute(
                    "SELECT * FROM jobs WHERE status = ? AND last_ping_timestamp < ?",
                    (STATUS_SERVED, cutoff_time)
                )
                stale_jobs = cursor.fetchall()
                
                count = 0
                for row in stale_jobs:
                    job = dict(row)
                    prev_requester = job_worker_id(job)
                    last_ping = job['last_ping_timestamp']
                    minutes_silent = round((current_time - last_ping) / 60)
                    
                    # Parse existing messages
                    try:
                        messages = json.loads(job['message'])
                    except json.JSONDecodeError:
                        messages = []
                    
                    # Add reset message
                    messages.append({
                        "reason": f"Job Cleaner: Reset job to PENDING status. Worker '{prev_requester}' stopped responding ({minutes_silent} minutes of inactivity). Job is now available for reassignment.",
                        "timestamp": current_time
                    })
                    
                    # Reset job
                    cursor.execute('''
                        UPDATE jobs 
                        SET status = ?, worker_id = '', requested_by = '',
                            request_timestamp = 0, 
                            completion_timestamp = 0, required_time = 0, 
                            last_ping_timestamp = 0, initialization_timestamp = 0, message = ?
                        WHERE id = ?
                    ''', (STATUS_PENDING, json.dumps(messages), job['id']))
                    
                    count += 1
                
                conn.commit()
                return count
    
    def delete_job(self, job_id: int, reason: str = "") -> bool:
        """Delete a PENDING job by setting its status to DELETED. Only works on PENDING jobs."""
        with self.lock:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM jobs WHERE id = ? AND status = ?",
                    (job_id, STATUS_PENDING)
                )
                row = cursor.fetchone()
                if not row:
                    return False

                job = dict(row)
                now = time.time()
                try:
                    messages = json.loads(job['message'])
                except json.JSONDecodeError:
                    messages = []

                messages.append({
                    "reason": f"Job Deleted: {reason if reason else 'No reason provided'}. Job will not be assigned to any worker.",
                    "timestamp": now
                })

                cursor.execute(
                    "UPDATE jobs SET status = ?, message = ? WHERE id = ?",
                    (STATUS_DELETED, json.dumps(messages), job_id)
                )
                conn.commit()
                return True

    def restore_deleted_job(self, job_id: int, reason: str = "") -> bool:
        """Restore a DELETED job back to PENDING. Only works on DELETED jobs."""
        with self.lock:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM jobs WHERE id = ? AND status = ?",
                    (job_id, STATUS_DELETED)
                )
                row = cursor.fetchone()
                if not row:
                    return False

                job = dict(row)
                now = time.time()
                try:
                    messages = json.loads(job['message'])
                except json.JSONDecodeError:
                    messages = []

                messages.append({
                    "reason": f"Job Restored to PENDING: {reason if reason else 'No reason provided'}. Job is now available for assignment.",
                    "timestamp": now
                })

                cursor.execute('''
                    UPDATE jobs
                    SET status = ?, message = ?, worker_id = '', requested_by = '',
                        request_timestamp = 0,
                        completion_timestamp = 0, required_time = 0,
                        last_ping_timestamp = 0, initialization_timestamp = 0
                    WHERE id = ?
                ''', (STATUS_PENDING, json.dumps(messages), job_id))
                conn.commit()
                return True

    def update_job_parameters(self, job_id: int, updates: Dict[str, Any], reason: str = "") -> bool:
        """Update parameters of a PENDING job. Only works on PENDING jobs."""
        with self.lock:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM jobs WHERE id = ? AND status = ?",
                    (job_id, STATUS_PENDING)
                )
                row = cursor.fetchone()
                if not row:
                    return False

                job = dict(row)
                now = time.time()

                try:
                    current_params = json.loads(job['parameters'])
                except json.JSONDecodeError:
                    current_params = {}

                try:
                    messages = json.loads(job['message'])
                except json.JSONDecodeError:
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

                messages.append({
                    "reason": audit_reason,
                    "timestamp": now
                })

                cursor.execute(
                    "UPDATE jobs SET parameters = ?, message = ? WHERE id = ?",
                    (json.dumps(current_params), json.dumps(messages), job_id)
                )
                conn.commit()
                return True

    def get_job_counts_by_status(self) -> Dict[str, int]:
        """Get job counts by status efficiently."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT status, COUNT(*) as count 
                FROM jobs 
                GROUP BY status
            """)
            rows = cursor.fetchall()
            
            counts = {STATUS_PENDING: 0, STATUS_SERVED: 0, STATUS_DONE: 0, STATUS_ABORTED: 0, STATUS_DELETED: 0}
            for row in rows:
                counts[row['status']] = row['count']
            
            return counts
    
    def record_upload(
        self,
        job_id: int,
        version: int,
        filename: str,
        size_bytes: int,
        uploaded_at: float,
    ) -> None:
        """Persist metadata for a worker result upload."""
        with self.lock:
            with self.get_connection() as conn:
                conn.execute(
                    '''
                    INSERT INTO uploads (job_id, version, filename, size_bytes, uploaded_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(job_id, version) DO UPDATE SET
                        filename = excluded.filename,
                        size_bytes = excluded.size_bytes,
                        uploaded_at = excluded.uploaded_at
                    ''',
                    (job_id, version, filename, size_bytes, uploaded_at),
                )
                conn.commit()

    def list_uploads(self, job_id: int) -> List[Dict[str, Any]]:
        """Return upload rows for a job, newest version first."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT job_id, version, filename, size_bytes, uploaded_at
                FROM uploads
                WHERE job_id = ?
                ORDER BY version DESC
                ''',
                (job_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def backfill_uploads(self, rows: List[Dict[str, Any]]) -> None:
        """Insert disk-scanned uploads that are not yet in SQLite."""
        if not rows:
            return
        with self.lock:
            with self.get_connection() as conn:
                for row in rows:
                    conn.execute(
                        '''
                        INSERT OR IGNORE INTO uploads
                        (job_id, version, filename, size_bytes, uploaded_at)
                        VALUES (?, ?, ?, ?, ?)
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
        """
        Get the percentile runtime from completed jobs.
        
        Args:
            percentile: Percentile value between 0 and 1 (e.g., 0.99 for 99th percentile)
        
        Returns:
            Runtime at the specified percentile, or 0 if no completed jobs
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Get all completed jobs with valid required_time
            cursor.execute("""
                SELECT required_time 
                FROM jobs 
                WHERE status = ? AND required_time > 0
                ORDER BY required_time
            """, (STATUS_DONE,))
            
            runtimes = [row['required_time'] for row in cursor.fetchall()]
            
            if not runtimes:
                return 0.0
            
            # Calculate percentile index
            n = len(runtimes)
            index = int(percentile * (n - 1))
            index = max(0, min(index, n - 1))  # Clamp to valid range
            
            return runtimes[index]
    
    def get_jobs_by_status(self, status: str) -> List[Dict[str, Any]]:
        """Get all jobs with a specific status."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM jobs WHERE status = ? ORDER BY id", (status,))
            rows = cursor.fetchall()
            
            jobs = []
            for row in rows:
                job = dict(row)
                job['message'] = _parse_job_message(job['message'])
                try:
                    job['parameters'] = json.loads(job['parameters'])
                except json.JSONDecodeError:
                    job['parameters'] = {}
                try:
                    job['system_metrics'] = json.loads(job.get('system_metrics', '{}'))
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
        conn: sqlite3.Connection,
        worker_id: str,
        reason: str,
        *,
        event: str = "",
        metrics: Optional[Dict[str, Any]] = None,
    ) -> None:
        cur = conn.cursor()
        cur.execute("SELECT history FROM workers WHERE worker_id = ?", (worker_id,))
        row = cur.fetchone()
        if not row:
            return
        entries = self._parse_worker_history(row["history"])
        entry: Dict[str, Any] = {
            "reason": reason,
            "timestamp": time.time(),
            "event": event or "info",
        }
        if metrics:
            entry["metrics"] = metrics
        entries.append(entry)
        cur.execute(
            "UPDATE workers SET history = ? WHERE worker_id = ?",
            (json.dumps(entries), worker_id),
        )

    def _touch_worker_presence_locked(
        self,
        conn: sqlite3.Connection,
        worker_id: str,
        *,
        system_metrics: Optional[Dict[str, Any]] = None,
        current_job_id: Optional[int] = None,
        reported_status: str = WORKER_REPORTED_IDLE,
        machine_type: Optional[str] = None,
    ) -> None:
        """Refresh worker row from job lifecycle events (e.g. update_job_status)."""
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
        cur.execute("SELECT worker_id FROM workers WHERE worker_id = ?", (worker_id,))
        if cur.fetchone():
            cur.execute(
                """
                UPDATE workers SET
                    host = ?, instance = ?, slot = ?, machine_type = ?,
                    reported_status = ?, current_job_id = ?,
                    last_poll_at = ?, system_metrics = ?,
                    lifecycle_status = ?, disabled_at = 0
                WHERE worker_id = ?
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
                 lifecycle_status, disabled_at, history, first_poll_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'run', 0, 'run', ?, ?, 0, '[]', ?)
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
        with self.lock:
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

    def _disable_stale_workers(self, conn: sqlite3.Connection) -> None:
        """Move active workers with no recent poll to disabled lifecycle."""
        now = time.time()
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM workers WHERE lifecycle_status = ?",
            (WORKER_LIFECYCLE_ACTIVE,),
        )
        for row in cur.fetchall():
            row = dict(row)
            last_poll = float(row.get("last_poll_at") or 0)
            if last_poll <= 0 or (now - last_poll) <= WORKER_STALE_SECONDS:
                continue
            wid = row["worker_id"]
            mins = max(1, round((now - last_poll) / 60))
            self._append_worker_history(
                conn,
                wid,
                f"Worker disabled — no poll for {mins} minutes "
                f"(machine shutdown, process stopped, or network loss).",
                event="disabled",
            )
            cur.execute(
                """
                UPDATE workers SET lifecycle_status = ?, disabled_at = ?
                WHERE worker_id = ?
                """,
                (WORKER_LIFECYCLE_DISABLED, now, wid),
            )

    @staticmethod
    def _history_entry_for_list(entry: Dict[str, Any]) -> Dict[str, Any]:
        """History row for the dashboard timeline (omit large metrics blobs)."""
        return {
            "reason": entry.get("reason"),
            "timestamp": entry.get("timestamp"),
            "event": entry.get("event"),
        }

    def _sorted_worker_history(self, raw: Any) -> List[Dict[str, Any]]:
        history = self._parse_worker_history(raw)
        return sorted(
            history,
            key=lambda e: float(e.get("timestamp") or 0),
            reverse=True,
        )

    def _worker_row_to_dict(
        self, row: sqlite3.Row, *, include_history: bool = True,
    ) -> Dict[str, Any]:
        d = dict(row)
        now = time.time()
        d["stale"] = (now - float(d.get("last_poll_at") or 0)) > WORKER_STALE_SECONDS
        d["pending"] = int(d.get("desired_version") or 0) > int(d.get("applied_version") or 0)
        d["lifecycle_status"] = d.get("lifecycle_status") or WORKER_LIFECYCLE_ACTIVE
        try:
            d["system_metrics"] = json.loads(d.get("system_metrics") or "{}")
        except json.JSONDecodeError:
            d["system_metrics"] = {}
        sorted_history = self._sorted_worker_history(d.get("history"))
        d["history_total"] = len(sorted_history)
        if include_history:
            d["history"] = sorted_history
        h, inst, slot = self.parse_worker_id_parts(d.get("worker_id", ""))
        if not d.get("host"):
            d["host"] = h
        if not d.get("instance"):
            d["instance"] = inst
        if d.get("slot") in (None, ""):
            d["slot"] = slot
        return d

    def _backfill_worker_identity_columns(self, conn: sqlite3.Connection) -> None:
        cur = conn.cursor()
        cur.execute(
            "SELECT worker_id, host, instance, slot FROM workers "
            "WHERE instance = '' OR host = ''"
        )
        for row in cur.fetchall():
            h, inst, slot = self.parse_worker_id_parts(row["worker_id"])
            cur.execute(
                "UPDATE workers SET host = ?, instance = ?, slot = ? WHERE worker_id = ?",
                (h, inst, slot, row["worker_id"]),
            )

    def _list_worker_rows(
        self,
        lifecycle: Optional[str] = None,
        host: Optional[str] = None,
        instance: Optional[str] = None,
        slot: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        with self.lock:
            with self.get_connection() as conn:
                self._backfill_worker_identity_columns(conn)
                self._disable_stale_workers(conn)
                conn.commit()
                cur = conn.execute("SELECT * FROM workers ORDER BY host, instance, slot, worker_id")
                rows = [
                    self._worker_row_to_dict(r, include_history=False)
                    for r in cur.fetchall()
                ]
        if lifecycle == WORKER_LIST_PENDING:
            rows = [r for r in rows if r.get("pending")]
        elif lifecycle == WORKER_LIFECYCLE_ACTIVE:
            rows = [
                r for r in rows
                if r.get("lifecycle_status") == WORKER_LIFECYCLE_ACTIVE
                and not r.get("pending")
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
        rows = self._list_worker_rows(lifecycle=WORKER_LIFECYCLE_ACTIVE)
        pending = len(self._list_worker_rows(lifecycle=WORKER_LIST_PENDING))
        busy = sum(1 for r in rows if r["reported_status"] == WORKER_REPORTED_BUSY)
        idle = sum(1 for r in rows if r["reported_status"] == WORKER_REPORTED_IDLE)
        disabled = len(self._list_worker_rows(lifecycle=WORKER_LIFECYCLE_DISABLED))
        return {
            "total": len(rows),
            "busy": busy,
            "idle": idle,
            "pending_commands": pending,
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
    ) -> tuple:
        clauses: List[str] = []
        params: List[Any] = []
        if lifecycle == WORKER_LIST_PENDING:
            clauses.append("desired_version > applied_version")
        elif lifecycle == WORKER_LIFECYCLE_ACTIVE:
            clauses.append("lifecycle_status = ?")
            params.append(WORKER_LIFECYCLE_ACTIVE)
            clauses.append("desired_version <= applied_version")
        elif lifecycle == WORKER_LIFECYCLE_DISABLED:
            clauses.append("lifecycle_status = ?")
            params.append(WORKER_LIFECYCLE_DISABLED)
        elif lifecycle:
            clauses.append("lifecycle_status = ?")
            params.append(lifecycle)
        if host:
            clauses.append("host = ?")
            params.append(host)
        if instance:
            clauses.append("instance = ?")
            params.append(instance)
        if slot is not None:
            clauses.append("slot = ?")
            params.append(int(slot))
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
    ) -> Dict[str, Any]:
        page = max(1, int(page))
        per_page = max(1, min(int(per_page), 50))
        where, params = self._worker_list_filters_sql(
            lifecycle, host, instance, slot,
        )
        with self.lock:
            with self.get_connection() as conn:
                self._backfill_worker_identity_columns(conn)
                self._disable_stale_workers(conn)
                conn.commit()
                cur = conn.execute(
                    f"SELECT COUNT(*) FROM workers{where}", params,
                )
                total_count = int(cur.fetchone()[0])
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
                cur = conn.execute(
                    f"SELECT * FROM workers{where} "
                    "ORDER BY host, instance, slot, worker_id "
                    "LIMIT ? OFFSET ?",
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
        """Count DONE jobs per worker id (``worker_id``, else legacy ``requested_by``)."""
        assignee = "COALESCE(NULLIF(worker_id, ''), requested_by)"
        with self.lock:
            with self.get_connection() as conn:
                if worker_ids:
                    ids = [w for w in worker_ids if w]
                    if not ids:
                        return {}
                    placeholders = ",".join("?" * len(ids))
                    cur = conn.execute(
                        f"""
                        SELECT {assignee} AS wid, COUNT(*) AS n
                        FROM jobs
                        WHERE status = ? AND {assignee} IN ({placeholders})
                        GROUP BY wid
                        """,
                        [STATUS_DONE, *ids],
                    )
                else:
                    cur = conn.execute(
                        f"""
                        SELECT {assignee} AS wid, COUNT(*) AS n
                        FROM jobs
                        WHERE status = ? AND {assignee} != ''
                        GROUP BY wid
                        """,
                        (STATUS_DONE,),
                    )
                return {row[0]: int(row[1]) for row in cur.fetchall()}

    def get_worker(
        self, worker_id: str, *, include_history: bool = True,
    ) -> Optional[Dict[str, Any]]:
        with self.lock:
            with self.get_connection() as conn:
                self._backfill_worker_identity_columns(conn)
                self._disable_stale_workers(conn)
                conn.commit()
                cur = conn.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,))
                row = cur.fetchone()
        if not row:
            return None
        return self._worker_row_to_dict(row, include_history=include_history)

    def get_worker_history_page(
        self,
        worker_id: str,
        page: int = 0,
        page_size: int = 10,
        metrics_only: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Paginated worker history (newest first)."""
        with self.lock:
            with self.get_connection() as conn:
                cur = conn.execute(
                    "SELECT history FROM workers WHERE worker_id = ?",
                    (worker_id,),
                )
                row = cur.fetchone()
        if not row:
            return None
        entries = self._sorted_worker_history(row["history"])
        if metrics_only:
            entries = [
                e for e in entries
                if isinstance(e.get("metrics"), dict) and e["metrics"]
            ]
        total = len(entries)
        page = max(0, page)
        page_size = max(1, min(int(page_size), 100))
        start = page * page_size
        slice_entries = entries[start:start + page_size]
        if metrics_only:
            out_entries = slice_entries
        else:
            out_entries = [self._history_entry_for_list(e) for e in slice_entries]
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
        """Register worker heartbeat; return desired state and optional job."""
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
        with self.lock:
            with self.get_connection() as conn:
                self._disable_stale_workers(conn)
                cur = conn.cursor()
                cur.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,))
                row = cur.fetchone()
                prev = dict(row) if row else None
                prev_applied = int(prev.get("applied_version") or 0) if prev else 0
                prev_status = prev.get("reported_status") if prev else None
                prev_job = prev.get("current_job_id") if prev else None
                was_disabled = (
                    prev and (prev.get("lifecycle_status") or WORKER_LIFECYCLE_ACTIVE)
                    == WORKER_LIFECYCLE_DISABLED
                )

                if row:
                    cur.execute(
                        """
                        UPDATE workers SET
                            host = ?, instance = ?, slot = ?, machine_type = ?,
                            reported_status = ?, current_job_id = ?,
                            jd_worker_version = ?, last_poll_at = ?,
                            applied_version = MAX(applied_version, ?),
                            system_metrics = ?, lifecycle_status = ?,
                            disabled_at = 0
                        WHERE worker_id = ?
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
                            f"Worker applied dashboard command: {desired} "
                            f"(version {applied_version}).",
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
                else:
                    cur.execute(
                        """
                        INSERT INTO workers
                        (worker_id, host, instance, slot, machine_type, reported_status,
                         current_job_id, jd_worker_version, last_poll_at, applied_version,
                         desired_state, desired_version, previous_desired_state,
                         system_metrics, lifecycle_status, history, first_poll_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'run', ?, ?, '[]', ?)
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

                if status == WORKER_REPORTED_BUSY and current_job_id is not None:
                    cur.execute(
                        """
                        UPDATE jobs SET last_ping_timestamp = ?, system_metrics = ?
                        WHERE id = ? AND status = ?
                        """,
                        (now, metrics_json, int(current_job_id), STATUS_SERVED),
                    )

                conn.commit()
                cur.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,))
                worker = self._worker_row_to_dict(cur.fetchone())

        job_payload = None
        if (
            status == WORKER_REPORTED_IDLE
            and worker["desired_state"] == WORKER_STATE_RUN
            and not worker["pending"]
            and worker["lifecycle_status"] == WORKER_LIFECYCLE_ACTIVE
        ):
            job = self.request_job(worker_id, system_metrics or {})
            if job:
                job_payload = {
                    "job_id": job["id"],
                    "parameters": job.get("parameters", {}),
                    "status": STATUS_SERVED,
                }
                with self.lock:
                    with self.get_connection() as conn:
                        conn.execute(
                            """
                            UPDATE workers SET reported_status = ?, current_job_id = ?
                            WHERE worker_id = ?
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
                        cur = conn.execute(
                            "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
                        )
                        worker = self._worker_row_to_dict(cur.fetchone())

        return {
            "desired_state": worker["desired_state"],
            "desired_version": worker["desired_version"],
            "applied_version": worker["applied_version"],
            "pending": worker["pending"],
            "heartbeat_interval": (
                WORKER_POLL_INTERVAL_BUSY
                if status == WORKER_REPORTED_BUSY
                else WORKER_POLL_INTERVAL_IDLE
            ),
            "poll_interval": (
                WORKER_POLL_INTERVAL_BUSY
                if status == WORKER_REPORTED_BUSY
                else WORKER_POLL_INTERVAL_IDLE
            ),
            "job": job_payload,
        }

    def _worker_state_rank(self, state: str) -> int:
        return {
            WORKER_STATE_RUN: 0,
            WORKER_STATE_PAUSE: 1,
            WORKER_STATE_DRAIN: 2,
            WORKER_STATE_STOP: 3,
        }.get(state, 0)

    def _worker_is_actionable(self, row: Dict[str, Any], now: float) -> bool:
        if (row.get("lifecycle_status") or WORKER_LIFECYCLE_ACTIVE) != WORKER_LIFECYCLE_ACTIVE:
            return False
        last_poll = float(row.get("last_poll_at") or 0)
        return last_poll > 0 and (now - last_poll) <= WORKER_STALE_SECONDS

    def set_workers_command(
        self,
        action: str,
        scope: str,
        target: Optional[str] = None,
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
        labels = {
            "run": "resume",
            "pause": "pause",
            "drain": "drain",
            "stop": "stop",
        }

        with self.lock:
            with self.get_connection() as conn:
                self._disable_stale_workers(conn)
                cur = conn.cursor()
                cur.execute("SELECT * FROM workers")
                rows = [dict(r) for r in cur.fetchall()]
                now = time.time()
                for row in rows:
                    if not self._worker_is_actionable(row, now):
                        continue
                    wid = row["worker_id"]
                    host = row.get("host") or ""
                    inst = row.get("instance") or ""
                    if scope == "worker":
                        if wid != target:
                            continue
                    elif scope == "host":
                        if host != target:
                            continue
                    elif scope == "instance":
                        parts = (target or "").split("|", 1)
                        if len(parts) != 2 or host != parts[0] or inst != parts[1]:
                            continue
                    elif scope != "all":
                        continue

                    current = row.get("desired_state") or WORKER_STATE_RUN
                    applied = int(row.get("applied_version") or 0)
                    desired_v = int(row.get("desired_version") or 0)
                    pending = desired_v > applied

                    if action == WORKER_STATE_RUN:
                        if current == WORKER_STATE_STOP and not pending:
                            continue
                        if pending and self._worker_state_rank(current) >= self._worker_state_rank(
                            action
                        ):
                            new_state = WORKER_STATE_RUN
                        elif not pending and current == WORKER_STATE_STOP:
                            continue
                        else:
                            new_state = WORKER_STATE_RUN
                    elif self._worker_state_rank(action) >= self._worker_state_rank(current):
                        new_state = action
                    else:
                        continue

                    if new_state == current and not pending:
                        continue

                    prev = current if pending else row.get("previous_desired_state") or WORKER_STATE_RUN
                    new_version = desired_v + 1
                    cur.execute(
                        """
                        UPDATE workers SET
                            previous_desired_state = ?,
                            desired_state = ?,
                            desired_version = ?,
                            pending_batch_id = ?
                        WHERE worker_id = ?
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
    ) -> int:
        """Revert queued (not yet applied) commands to previous_desired_state."""
        reverted = 0
        with self.lock:
            with self.get_connection() as conn:
                self._disable_stale_workers(conn)
                cur = conn.cursor()
                cur.execute("SELECT * FROM workers")
                for row in cur.fetchall():
                    row = dict(row)
                    if not self._worker_is_actionable(row, time.time()):
                        continue
                    applied = int(row.get("applied_version") or 0)
                    desired_v = int(row.get("desired_version") or 0)
                    if desired_v <= applied:
                        continue
                    wid = row["worker_id"]
                    host = row.get("host") or ""
                    inst = row.get("instance") or ""
                    if scope == "worker" and wid != target:
                        continue
                    if scope == "host" and host != target:
                        continue
                    if scope == "instance":
                        parts = (target or "").split("|", 1)
                        if len(parts) != 2 or host != parts[0] or inst != parts[1]:
                            continue
                    if scope == "all":
                        pass
                    elif scope not in ("worker", "host", "instance"):
                        continue
                    prev = row.get("previous_desired_state") or WORKER_STATE_RUN
                    cancelled = row.get("desired_state") or WORKER_STATE_RUN
                    cur.execute(
                        """
                        UPDATE workers SET
                            desired_state = ?,
                            desired_version = desired_version + 1,
                            previous_desired_state = ?
                        WHERE worker_id = ?
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

        with self.lock:
            with self.get_connection() as conn:
                self._backfill_worker_identity_columns(conn)
                cur = conn.cursor()
                cur.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,))
                row = cur.fetchone()
                if row:
                    cur.execute(
                        """
                        UPDATE workers SET
                            host = ?, instance = ?, slot = ?,
                            lifecycle_status = ?, disabled_at = ?,
                            reported_status = ?, current_job_id = NULL,
                            desired_state = ?
                        WHERE worker_id = ?
                        """,
                        (
                            host, instance, slot,
                            WORKER_LIFECYCLE_DISABLED, now,
                            WORKER_REPORTED_IDLE, WORKER_STATE_STOP,
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
                         history, first_poll_at)
                        VALUES (?, ?, ?, ?, 'worker', 'idle', ?, 0, 'stop', 0, 'run', ?, ?, '[]', ?)
                        """,
                        (
                            worker_id, host, instance, slot, now,
                            WORKER_LIFECYCLE_DISABLED, now, now,
                        ),
                    )
                self._append_worker_history(
                    conn, worker_id, reason, event="cli_stop",
                )
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
