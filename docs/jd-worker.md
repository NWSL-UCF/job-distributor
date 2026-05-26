# jd_worker_cli — install, run, and client library

## Overview

`jd_worker_cli` is the Job Distributor worker CLI. It pulls jobs from the job server, runs your **entry script** with that job’s parameters as CLI flags, sends a **heartbeat every 57 seconds** while the script runs, and reports **`DONE`** or **`ABORTED`** when the subprocess exits.

The same Python package exposes three helpers for use **inside** the entry script: `jd_upload`, `jd_update_checkpoint`, and `jd_get_last_checkpoint`. They talk to the job server over HTTP (`requests`).

---

## Requirements

- **Python** ≥ 3.8  
- **Dependencies** (installed with the package): `requests`, `psutil`  
- A running Job Distributor **job server** (`/worker/heartbeat`, `/update_job_status`, `/upload`, `/checkpoint`, `/checkpoint/latest`) reachable from the worker machine.

---

## Install

### From a cloned repository (editable)

From the repo root:

```bash
pip install -e ./client
```

This installs the distribution named **`jd-worker`** and registers the console script **`jd_worker_cli`**.

### From GitHub (no clone)

The installable client (`pyproject.toml`, `jd/` package) is on branch **`v2`** (default **`main`** may not include it yet). Pin the branch in the URL:

```bash
pip install "git+https://github.com/NWSL-UCF/job-distributor.git@v2#subdirectory=client"
```

After `v2` is merged into `main`, you can omit `@v2`.

Verify:

```bash
jd_worker_cli help
jd_worker_cli          # interactive shell (mysql-style)
```

### Interactive mode

Run with no arguments (or `interactive` / `-i`) for a REPL:

```bash
jd_worker_cli
jd_worker_cli interactive expId=my_exp
```

```
jd_worker_cli 1.13.0 — interactive mode
Type help for commands, exit or Ctrl-D to quit.
jd> use my_exp
Using experiment 'my_exp'.
jd[my_exp]> worker-list
jd[my_exp]> exp-status
jd[my_exp]> entry_script=train.py num_workers=4
jd[my_exp]> exit
```

Session command `use <expId>` sets the default experiment so you can omit `expId=` on later lines. `JD_EXP_ID` is used as the initial default if set.

---

## Operate `jd_worker_cli`

### Invocation style

Arguments are **`key=value`** tokens (and optional bare flags such as `once=true`). Run `jd_worker_cli help` for the embedded usage text.

### Required

| Argument | Meaning |
|----------|---------|
| `expId=<id>` | Experiment id — **must match** the job server’s `--expId`. |
| `entry_script=<path>` | Path to the Python file executed **with the same interpreter** as `jd_worker_cli` (venv / conda respected via `sys.executable`). |

### Optional

| Argument | Default | Env fallback |
|----------|---------|----------------|
| *(no workspace CLI)* | — | Worker storage is **never** chosen via `key=value`. See **Local storage** below. |
| `server=<url>` | `http://localhost` | `JD_SERVER` — host-only values get `http://` prepended automatically |
| `port=<N>` | `5000` | `JD_PORT` (used if `server` has no port) |
| `log_dir=<path>` | (derived) | `JD_LOG_DIR` — if unset, logs use `<jd_data>/<expId>/jd_worker_logs/` |
| `machine_type=<label>` | `worker` | `JD_MACHINE_TYPE` |
| `process_id=<N>` | `0` | — |
| `num_workers=<N>` | `1` | `JD_NUM_WORKERS` |
| `foreground=true` | off | `JD_FOREGROUND=true` — attach to terminal (default: background) |
| `once=true` | off | `JD_ONCE=true` |

### Background workers (default)

By default, `jd_worker_cli` **detaches to the background** — no tmux required. Process metadata is stored in **`~/.cache/<expId>/workers.db`** (or `<JD_WORKSPACE_PATH>/.cache/<expId>/workers.db`).

```bash
# Start (returns immediately with worker id + pid)
jd_worker_cli expId=my_exp entry_script=train.py num_workers=4

# List running workers for one experiment
jd_worker_cli expId=my_exp worker-list

# List all experiments with worker counts on this machine
jd_worker_cli exp-list

# Stop one worker or all
jd_worker_cli expId=my_exp stop all
jd_worker_cli expId=my_exp stop 0_45231
```

Each worker gets a **`worker_id`** like `gpunode_egg_0` (`{host}_{instance}_{slot}`).
        The **instance** segment is a short random object name (e.g. `egg`, `table`, `moon`) from a local pool in `workers.db`, max 6 letters. Legacy workers may still use 6-character alphanumeric tokens.
        Standalone launches always use slot **`0`**; `num_workers=N` uses slots `0 … N-1`.
        The same id is used locally, on the server (`requested_by`), and in log filenames.
        Use **`foreground=true`** for attached/debug mode.

### Management commands

| Command | Description |
|---------|-------------|
| `exp-list` | All experiments on this machine with worker counts |
| `expId=<id> worker-list` | List workers for one experiment |
| `expId=<id> worker-status <worker-id>` | Detailed status for one worker |
| `expId=<id> worker-logs <worker-id> [lines=N] [follow=true]` | Tail worker log file |
| `expId=<id> exp-status` | Experiment summary (busy/idle, draining) |
| `expId=<id> show-config <worker-id>` | Launch config stored at registration |
| `expId=<id> where` | Paths: registry DB, jd_data, logs |
| `expId=<id> server-info` | Job counts by status from the server |
| `health [expId=<id>]` | Hub + server connectivity check |
| `version` | Package version and Python environment |
| `expId=<id> stop all\|<worker-id>` | Stop workers (notifies server for dashboard history) |
| `expId=<id> stop job=<job-id>` | Stop the worker running a specific job (aborts SERVED job on server) |
| `expId=<id> stop all confirm-stop=true` | Require typing experiment name before stop all |
| `expId=<id> confirm-stop` | Same as stop all with confirmation |
| `stop all-experiments` | Stop workers for every experiment on this machine |
| `clear_all [confirm-clear-all=true]` | Wipe **all** local experiment cache; kills active workers and notifies server |
| `expId=<id> restart all\|<worker-id>` | Stop and respawn with stored config |
| `expId=<id> scale num_workers=<N>` | Scale up/down to N workers |
| `expId=<id> drain` | Finish current jobs then exit (no new work) |
| `prune` | Remove stale registry rows and orphaned token dirs |

**Stop / clear_all and the server:** When the job server is reachable, `stop`, `restart`, `scale` (scale-down), and `clear_all` call `POST /workers/cli/stop` or `POST /workers/cli/clear_all` so the dashboard **Workers** tab records the action (source machine, CLI command) in worker history. If a stopped worker was running a job, the server marks that job **ABORTED** with a message indicating the worker was killed from the CLI. If the server is down or auth fails, local stop/cache cleanup still proceeds.

```bash
jd_worker_cli version
jd_worker_cli health expId=my_exp
jd_worker_cli expId=my_exp exp-status
jd_worker_cli expId=my_exp worker-status 0_45231
jd_worker_cli expId=my_exp worker-logs 0_45231 lines=100 follow=true
jd_worker_cli expId=my_exp server-info
jd_worker_cli expId=my_exp scale num_workers=8
jd_worker_cli expId=my_exp drain
jd_worker_cli prune
jd_worker_cli clear_all
```

### Local storage (worker)

All job files live under:

`<parent>/jd_data/<expId>/<job_id>/` (absolute paths)

- **`parent`** = **`JD_WORKSPACE_PATH`** if set, otherwise **`~`** (home).
- So the default **`jd_data`** root is **`~/jd_data/`**.

Worker registry (SQLite, drain flag, Hub tokens) lives under:

`<cache>/.cache/<expId>/workers.db`

- **`cache`** = **`JD_CACHE_PATH`** if set, otherwise the same as **`parent`**.
- On a normal laptop you usually set only **`JD_WORKSPACE_PATH`** (or neither) — behavior is unchanged.

There is **no** `workspace_path=…` CLI argument.  
**Note:** The job server **`start.py`** also uses an env var named **`JD_WORKSPACE_PATH`** for **server** paths — use separate shells or unset between server and worker so they don’t pick up each other’s value.

#### HPC: Lustre (or NFS) for jobs, local disk for registry

SQLite on shared filesystems often causes `disk I/O error` when many workers update the same `workers.db`. Put **job sandboxes** on Lustre and the **registry** on node-local scratch:

```bash
# Shared — checkpoints, uploads, job dirs
export JD_WORKSPACE_PATH=/lustre/fs1/home/you/jd_client/jd_data

# Node-local — workers.db only (per job or per node)
export JD_CACHE_PATH="${TMPDIR:-/tmp}/jd_cache_${USER}_${SLURM_JOB_ID:-$$}"
mkdir -p "$JD_CACHE_PATH"

jd_worker_cli expId=my_exp entry_script=train.py ...
```

Use the same exports when running management commands (`exp-status`, `where`, `drain`) on a login node if you need them to see that registry — compute-node workers use the cache on the node where they run.

Worker logs at startup include **`Registry DB:`** with the resolved path.

### Behaviour

1. **Heartbeat** — `POST /worker/heartbeat` every **180** seconds when idle (liveness + optional job assignment + dashboard control). While a job runs, a **single background thread** sends `POST /worker/heartbeat` with `reported_status=busy` every **57** seconds — the server updates **both** the worker row and the current job (`last_ping_timestamp`).
2. **Run entry script** — For each key/value in the job’s `parameters`, the worker appends `--<key> <value>` (stringified). It always appends `--base_path <dir>` where:

   `<dir> = <parent>/jd_data/<expId>/<job_id>` (absolute path on the worker)

   The directory is created before launch. Treat this directory as the sandbox for local reads/writes/deletes for that job.

3. **Status** — Exit code `0` → `POST /update_job_status` with `DONE`; non-zero → `ABORTED` with a short message derived from stderr when useful.

4. **Loop** — After each job, waits 3 seconds and polls again. When no job is available (or idle heartbeat fails), waits **3 minutes** before the next idle heartbeat. With **`once=true`**, exits when no job is available or after one completed job.

6. **Dashboard control** — The server dashboard can queue **run** (resume), **pause**, **drain**, or **stop** per worker, host, or all workers. **Pause** finishes the current job and keeps the worker process idle without new jobs. **Drain** finishes the current job and exits the worker. Commands apply on the next poll; cancel reverts queued commands before they are applied.

### Examples

```bash
# Default: ~/jd_data/<expId>/<job_id>/
jd_worker_cli expId=my_exp entry_script=train.py
```

```bash
jd_worker_cli expId=my_exp entry_script=train.py server=http://192.168.1.10 port=8000 machine_type=gpu_a
```

```bash
# Put jd_data under /scratch/jd_data (parent=/scratch)
JD_WORKSPACE_PATH=/scratch jd_worker_cli expId=my_exp entry_script=train.py
```

```bash
jd_worker_cli expId=my_exp entry_script=train.py once=true
```

### Logs

If **`log_dir`** / **`JD_LOG_DIR`** is set: **`<log_dir>/<expId>/jd_worker_<worker_id>.log`**. Otherwise: **`<parent>/jd_data/<expId>/jd_worker_logs/jd_worker_<worker_id>.log`**. Foreground mode also mirrors logs to stdout; background workers log to file only.

---

## Entry script contract

Your script **must** accept:

- One `--<param>` flag per job parameter (names and values come from the dashboard / DB).  
- **`--base_path`** — Absolute job workspace (`<parent>/jd_data/<expId>/<job_id>/`, created before launch).

Example: if parameters are `lr=0.01` and `epochs=10`, the worker runs approximately:

```bash
python train.py --lr 0.01 --epochs 10 --base_path /Users/you/jd_data/my_exp/42
```

### Environment injected by `jd_worker_cli` (for library calls)

| Variable | Purpose |
|----------|---------|
| `JD_JOB_ID` | Current job id (string). |
| `JD_SERVER` | Job server base URL (e.g. `http://host:5000`). |
| `JD_EXP_ID` | Experiment id (same as `expId`). |
| `JD_WORKER_JOB_DIR` | Absolute path to the job workspace (same as `--base_path`). |
| `JD_WORKER_WORKSPACE_ROOT` | Absolute `<parent>/jd_data` (see **Local storage** above). |

These are set **only** for the entry-script subprocess so `jd_upload` / checkpoint helpers work without extra arguments.

Use **`jd_job_dir()`** / **`jd_worker_workspace()`** from the `jd` package instead of reading env vars directly when building paths.

---

## Library API (`jd` package)

Import:

```python
from jd import (
    jd_upload,
    jd_update_checkpoint,
    jd_get_last_checkpoint,
    jd_job_dir,
    jd_worker_workspace,
    jd_exp_dir,
)
```

### Worker paths (`pathlib.Path`)

| Function | Meaning |
|----------|---------|
| `jd_job_dir()` | `<parent>/jd_data/<expId>/<job_id>/` — **default place for local outputs** (matches `--base_path`). |
| `jd_worker_workspace()` | Absolute `<parent>/jd_data` (parent from `JD_WORKSPACE_PATH` or `~`). |
| `jd_exp_dir()` | `<parent>/jd_data/<expId>/` (parent of `jd_job_dir()`). |

Example:

```python
csv_path = jd_job_dir() / "metrics.csv"
```

Older `jd_worker_cli` builds without `JD_WORKER_WORKSPACE_ROOT` still work: `jd_worker_workspace()` falls back to deriving the root from `JD_WORKER_JOB_DIR`.

### Uploads and checkpoints

All upload/checkpoint helpers resolve **`job_id`** and **`server`** from `JD_JOB_ID` and `JD_SERVER` when omitted. Override with keyword arguments only if you must (e.g. tests).

### Limits

- **100 MB** max per upload or checkpoint (checked client-side before send; server also enforces **413** over limit).  
- **Pickle** is used for checkpoints — only **pickle-compatible** objects; **same Python version / compatible code** on producer and consumer is strongly recommended.

### `jd_upload(file_path, job_id=None, server=None) -> dict`

Uploads a local file to the server.

- **Server path:** `result_v{N}_{unix_timestamp}<ext>`  
  - `<ext>` from the original filename (lower-case; default `.bin` if none).  
  - `N` increments per job directory so nothing is overwritten.

**Returns:** JSON dict, e.g. `success`, `filename`, `version`, `size_bytes`.

**Raises:** `FileNotFoundError`, `ValueError` (size), `requests` errors.

### `jd_update_checkpoint(obj, job_id=None, server=None) -> dict`

Pickles `obj` with `pickle.HIGHEST_PROTOCOL` and uploads.

- **Server filename:** `checkpoint_v{N}_{unix_timestamp}.pt`  
- Each call creates a **new** version; older checkpoints remain.

**Returns:** dict with `success`, `filename`, `version`, `size_bytes`.

### `jd_get_last_checkpoint(job_id=None, server=None)`

Downloads the **latest** checkpoint for the job (by **version number** embedded in the filename), unpickles **in memory** — **no worker-side file**.

**Returns:** The Python object saved earlier, or **`None`** if the server responds **404** (no checkpoint).

---

## Server-side storage layout

Under **`<workspace_path>/<expId>/`**:

| Subfolder | Contents |
|-----------|----------|
| **`meta/`** | `jobs.db`, Flask/Gunicorn-related logs (`server.log`, `dashboard.log`, `job_cleaner.log`, …), `start.py` log (`__start__.log`), `pids.json`, Gunicorn access/stderr logs (`server_access.log`, `server.stderr.log`, …). |
| **`data/`** | Worker payloads only: **`<job_id>/`** with `result_*` uploads and `checkpoint_*` files. |

(`workspace_path` is **`--workspace_path`** on **`server/start.py`**, exported as **`JD_WORKSPACE_PATH`** to Gunicorn; **`BASE_DIR`** in `server.py` is that workspace root. This is **independent** from the **worker** env var of the same name, which sets the **parent** of `jd_data/` on worker machines.)

If you run **`python server/src/server.py`** directly, pass **`--workspacePath`** (same **`meta/`** + **`data/`** layout; default workspace is the parent of `src/` when omitted).

**Migrating from older layouts:** if `jobs.db` or worker files previously lived directly under `<expId>/`, move them into **`meta/jobs.db`** and **`data/<job_id>/`** respectively before restarting.

---

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| `JD_JOB_ID` / `JD_SERVER` runtime errors | Library called outside a `jd_worker_cli`-launched entry script, or env overridden incorrectly. |
| `413` / size errors | Payload over 100 MB — shrink checkpoint or split artifacts. |
| `pickle` errors on load | Different library versions, unpickleable objects, or incompatible pickles across Python builds. |
| Connection errors | Wrong `server` / `port`, firewall, or job server not bound to reachable interface. |

---

## Related docs

- [features.md](features.md) — broader product behaviour  
- [update-job.md](update-job.md) — editing pending job parameters from the dashboard  
