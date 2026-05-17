# Job Distributor — Feature Reference

## Purpose

A distributed hyperparameter search system. A central server holds a SQLite database of all parameter combinations as jobs. Multiple worker machines pull jobs one at a time, run the corresponding experiment (arbitrary CLI command), and report results back. A web dashboard provides live monitoring.

---

## System Architecture

```
server/start.py
  ├── src/create_job_db.py   (run once on fresh_start)
  ├── src/server.py          (job API, port 5000)
  ├── src/dashboard.py       (monitoring UI, port 5050)
  └── src/job_cleaner.py     (background maintenance process)

client/runner.py             (runs on each worker machine)
```

All four server processes are launched and supervised by `start.py`. The database file (`jobs.db`) is shared between `server.py`, `dashboard.py`, and `create_job_db.py` via `database.py`.

---

## Server — `server/config.json`

| Field | Purpose |
|---|---|
| `expId` | Experiment name; used as subdirectory for all logs and the DB file |
| `jobDB` | SQLite filename |
| `host` | Bind host for both server and dashboard |
| `server_port` | Job API port (default 5000) |
| `dashboard_port` | Dashboard UI port (default 5050) |
| `fresh_start` | If true, regenerates the job DB from scratch on startup (backs up previous DB with timestamp) |
| `idleTimeout` | Seconds of no heartbeat before a SERVED job is reclaimed by the cleaner (default 600) |
| `abortedJobResetTimeout` | Seconds between sweeps that reset ABORTED jobs back to PENDING (default 1200) |
| `parameters` | Dict of parameter name → list of values; Cartesian product becomes the full job set |

---

## `create_job_db.py`

Generates the SQLite job database on startup. Takes the `parameters` dict, computes the full Cartesian product of all parameter lists (e.g., 6×3×5×5×6 = 2700 jobs), and inserts each combination as one row with status `PENDING`. Backs up any existing DB file with a timestamp suffix before overwriting.

---

## `database.py` — `JobDatabase`

SQLite-backed data layer. All write methods hold a `threading.Lock()`, making them safe for concurrent Flask threads within a single process (not safe across multiple OS processes, e.g., Gunicorn multi-worker).

### Tables

**`jobs`** — one row per parameter combination.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Sequential job index |
| `status` | TEXT | `PENDING`, `SERVED`, `DONE`, `ABORTED` |
| `parameters` | TEXT (JSON) | Parameter key-value dict for this job |
| `requested_by` | TEXT | `runner_id` of the worker that claimed it |
| `request_timestamp` | REAL | Unix time when job was claimed |
| `initialization_timestamp` | REAL | Unix time when the job request arrived at the server |
| `completion_timestamp` | REAL | Unix time when job finished |
| `required_time` | REAL | Wall-clock seconds from request to completion |
| `predicted_runtime` | REAL | Reserved field (unused, kept for schema compatibility) |
| `last_ping_timestamp` | REAL | Unix time of most recent heartbeat |
| `message` | TEXT (JSON array) | Append-only audit log of all status transitions |
| `system_metrics` | TEXT (JSON) | Worker hardware snapshot at time of job claim |

**`api_stats`** — per-endpoint request counters.

| Column | Description |
|---|---|
| `endpoint` | Endpoint label string |
| `method` | HTTP method |
| `request_count` | Total hits |
| `last_updated` | Unix time of last hit |

### Indexes

`status`, `(status, id)`, `last_ping_timestamp`, `(status, last_ping_timestamp)`, `requested_by`, `request_timestamp`, `completion_timestamp`.

### Key Methods

| Method | Description |
|---|---|
| `create_jobs` | Wipes and repopulates the jobs table from a list of JSON parameter strings |
| `request_job` | Atomically (under lock) selects the lowest-id PENDING job, marks it SERVED, records `requested_by`, `system_metrics`, timestamps, and appends an audit message |
| `update_job_status` | Marks a SERVED job DONE or ABORTED; records `completion_timestamp` and `required_time` |
| `change_job_status` | Manual override; moves DONE/ABORTED/PENDING jobs to any of those three states; resets all timestamps and `requested_by` when moving to PENDING |
| `ping_job` | Updates `last_ping_timestamp` for a SERVED job |
| `reset_aborted_jobs` | Resets all ABORTED jobs to PENDING, clearing timestamps and appending an audit message |
| `reset_stale_served_jobs(idle_timeout)` | Resets SERVED jobs whose `last_ping_timestamp` is older than `idle_timeout` seconds |
| `get_jobs_paginated` | Paginated query with optional status filter and job ID search |
| `get_job_counts_by_status` | Single `GROUP BY` query returning counts for all four statuses |
| `get_percentile_runtime` | Computes a runtime percentile across all DONE jobs |
| `track_api_request` / `get_api_stats` | Upsert and read endpoint hit counters |

---

## `server.py` — Job API (port 5000)

Synchronous Flask server with `threaded=True`. Every endpoint responds in a single HTTP round-trip. All endpoints call `db.track_api_request()` to log hits.

| Endpoint | Method | Request Body | Success Response | Error Responses |
|---|---|---|---|---|
| `/request_job` | POST | `{requested_by, system_metrics}` | 200 `{job_id, parameters, status}` | 400 missing requester, 404 no jobs, 500 DB error |
| `/update_job_status` | POST | `{job_id, status, message}` | 200 `{message, job_id, status}` | 400 invalid input or job not SERVED, 404 job not found |
| `/ping` | POST | `{job_id}` (or `id`) | 200 `{message, timestamp}` | 400 invalid id, 404 job not found |
| `/cleanup/reset_aborted_jobs` | POST | `{}` | 200 `{message, jobs_reset}` | 500 DB error |
| `/cleanup/reset_stale_served_jobs` | POST | `{idle_timeout}` | 200 `{message, jobs_reset, idle_timeout}` | 400 invalid timeout, 500 DB error |

---

## `dashboard.py` — Monitoring UI (port 5050)

Read-only Flask server plus one write endpoint for manual status overrides. Serves a self-contained single-page HTML dashboard built with Chart.js and DataTables.

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Full dashboard HTML. Shows experiment summary (total/pending/served/done/aborted counts), per-machine completion stats, average completion time, and total API request count. |
| `/jobs_paginated` | GET | Paginated job table. Params: `page`, `per_page` (1–1000), `status` filter, `search_job_id`. |
| `/job_stats` | GET | Completion time-series for Chart.js. Param `interval`: `minutely` (last 30 min), `hourly` (last 24 h), `daily` (since first job). Param `machine`: filter by worker. Returns `{labels, values, total_jobs}`. |
| `/api_stats` | GET | All endpoint hit counts from `api_stats` table. |
| `/database_info` | GET | Table row counts, index list, and jobs table schema. |
| `/change_job_status` | POST | Manual status override. Body: `{job_id, new_status, reason}`. Accepts PENDING/DONE/ABORTED as target status. |

The dashboard UI auto-refreshes job data and charts. Machine labels are derived from the `requested_by` field (format: `user@hostname(machine_type)_processId_random`).

---

## `job_cleaner.py` — Background Maintenance

Standalone process (not a Flask app). Runs two periodic cleanup tasks via HTTP calls to `server.py`:

1. **Aborted job reset** — every `abortedJobResetTimeout` seconds: `POST /cleanup/reset_aborted_jobs`. Returns failed jobs to PENDING so they can be retried.
2. **Stale SERVED job reset** — every `idleTimeout` seconds: `POST /cleanup/reset_stale_served_jobs`. Reclaims jobs from workers that crashed or lost connectivity without sending a final status update.

CLI args: `--serverUrl`, `--serverPort`, `--expId`, `--abortedJobResetTimeout`, `--idleTimeout`, `--pollingInterval`.

---

## `start.py` — Process Supervisor

Reads `server/config.json`, optionally runs `create_job_db.py` synchronously (if `fresh_start: true`), then launches `server.py`, `dashboard.py`, and `job_cleaner.py` as parallel subprocesses. Registers `atexit` and `SIGINT`/`SIGTERM` handlers to kill all child processes on shutdown. Saves PIDs to `{expId}/pids.json`.

---

## Client — `client/config.json`

| Field | Purpose |
|---|---|
| `expId` | Must match server's `expId` |
| `job_server` | Server base URL (e.g., `http://192.168.1.10`) |
| `port` | Server port; appended to URL unless the URL already contains an explicit port |
| `number_of_parallel_process` | How many `runner.py` instances to run in parallel on this machine |
| `heartBitInterval` | Seconds between heartbeat pings (internally reduced by 0.3 s to avoid timing edge cases) |
| `run_command` | Base command list, e.g., `["python", "main.py"]` |
| `machine_type` | Label for this machine: `hpc`, `htc`, `desktop`, or `laptop` |

---

## `client/runner.py` — Worker

One instance per parallel job slot. Each instance has a unique `runner_id` = `user@hostname(machine_type)_processId_random`.

### Main Loop

1. **Collect system metrics** — 5 samples over ~12 seconds (3 s apart), then averaged. Captures: CPU utilization, RAM utilization and available GB, load averages (1/5/15 min), idle CPU slots, disk I/O utilization, CPU core/thread count, CPU frequency.
2. **Request job** — `POST /request_job` with `runner_id` and averaged system metrics. Receives `{job_id, parameters}`. If 404, no jobs remain and the runner exits cleanly.
3. **Build command** — `run_command + [--key value, ...] + [--base_path ~/data/raw/{expId}/{job_id}]`.
4. **Start heartbeat thread** — pings `POST /ping` every `heartBitInterval` seconds while the subprocess runs.
5. **Run subprocess** — `Popen` with captured stdout/stderr. On Windows uses `CREATE_NEW_PROCESS_GROUP`; on POSIX uses `os.setsid` for clean process-group termination.
6. **Stop heartbeat thread** — signals stop event and joins the thread.
7. **Report result**:
   - Exit code 0 → `POST /update_job_status` with `DONE`.
   - Non-zero → `POST /update_job_status` with `ABORTED` + structured error message containing return code, signal description, and relevant stderr/stdout excerpts (only if they contain error keywords).
8. Wait 5 seconds, then repeat from step 1.

**Special case:** if `machine_type == "htc"`, the runner exits after completing one job (HTC cluster nodes are single-job-per-allocation).

**Signal handling:** `SIGINT`/`SIGTERM` terminate the current subprocess and exit cleanly.

---

## Job Lifecycle

```
PENDING  →  SERVED  →  DONE
                    ↘  ABORTED  →  PENDING   (job_cleaner: abortedJobResetTimeout)
SERVED   →  PENDING              (job_cleaner: idleTimeout / no heartbeat)

DONE, ABORTED, PENDING  →  DONE, ABORTED, PENDING   (manual: dashboard)
```

Every status transition appends a timestamped entry to the job's `message` JSON array, preserving the full audit history across retries.

---

## Concurrency Model

- `server.py` runs with `threaded=True` — Flask spawns one thread per request.
- `JobDatabase.lock` (`threading.Lock`) serializes all writes within the process, preventing two threads from assigning the same job.
- **Safe for:** multi-threaded single-process Flask.
- **Not safe for:** multi-process deployments (e.g., Gunicorn `-w N`) — each process has its own lock instance.
