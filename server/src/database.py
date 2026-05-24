import hashlib
import os
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
                return job
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
                jobs.append(job)
            
            return {
                'jobs': jobs,
                'total_count': total_count,
                'total_pages': total_pages,
                'current_page': page,
                'per_page': per_page
            }
    
    def request_job(self, requested_by: str, system_metrics: Optional[Dict[str, Any]] = None, 
                    job_id: Optional[int] = None, predicted_runtime: Optional[float] = None,
                    initialization_timestamp: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """
        Assign a PENDING job to a requester and mark it as SERVED.
        
        Args:
            requested_by: Identifier of the requester
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
                messages.append({
                    "reason": f"{requested_by} requests this job for execution",
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
                    SET requested_by = ?, status = ?, request_timestamp = ?, 
                        message = ?, system_metrics = ?, predicted_runtime = ?,
                        initialization_timestamp = ?
                    WHERE id = ?
                ''', (requested_by, STATUS_SERVED, timestamp, json.dumps(messages), 
                      system_metrics_json, predicted_runtime_value, init_timestamp, job['id']))
                
                conn.commit()
                
                # Return updated job
                job['requested_by'] = requested_by
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
                            last_ping_timestamp = 0, initialization_timestamp = 0, requested_by = ''
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
    
    def ping_job(
        self,
        job_id: int,
        system_metrics: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Update last_ping_timestamp (and optional system_metrics) for a SERVED job."""
        with self.lock:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                # Check if job exists and is SERVED
                cursor.execute(
                    "SELECT id FROM jobs WHERE id = ? AND status = ?",
                    (job_id, STATUS_SERVED)
                )
                row = cursor.fetchone()
                
                if not row:
                    return False
                
                now = round(time.time())
                if system_metrics:
                    cursor.execute(
                        "UPDATE jobs SET last_ping_timestamp = ?, system_metrics = ? "
                        "WHERE id = ?",
                        (now, json.dumps(system_metrics), job_id),
                    )
                else:
                    cursor.execute(
                        "UPDATE jobs SET last_ping_timestamp = ? WHERE id = ?",
                        (now, job_id),
                    )
                
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
                    prev_requester = job['requested_by']
                    
                    # Parse existing messages
                    try:
                        messages = json.loads(job['message'])
                    except json.JSONDecodeError:
                        messages = []
                    
                    # Add reset message
                    messages.append({
                        "reason": f"Job Cleaner: Reset job to PENDING status. Previous execution failed on machine '{prev_requester}'. Job is now available for reassignment.",
                        "timestamp": current_time
                    })
                    
                    # Reset job
                    cursor.execute('''
                        UPDATE jobs 
                        SET status = ?, requested_by = '', request_timestamp = 0, 
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
                    prev_requester = job['requested_by']
                    last_ping = job['last_ping_timestamp']
                    minutes_silent = round((current_time - last_ping) / 60)
                    
                    # Parse existing messages
                    try:
                        messages = json.loads(job['message'])
                    except json.JSONDecodeError:
                        messages = []
                    
                    # Add reset message
                    messages.append({
                        "reason": f"Job Cleaner: Reset job to PENDING status. Machine '{prev_requester}' stopped responding ({minutes_silent} minutes of inactivity). Job is now available for reassignment.",
                        "timestamp": current_time
                    })
                    
                    # Reset job
                    cursor.execute('''
                        UPDATE jobs 
                        SET status = ?, requested_by = '', request_timestamp = 0, 
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
                    SET status = ?, message = ?, requested_by = '', request_timestamp = 0,
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
                jobs.append(job)
            
            return jobs
