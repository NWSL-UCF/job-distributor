# jd_worker — install, run, and client library

## Overview

`jd_worker` is the Job Distributor worker CLI. It pulls jobs from the job server, runs your **entry script** with that job’s parameters as CLI flags, sends a **heartbeat every 57 seconds** while the script runs, and reports **`DONE`** or **`ABORTED`** when the subprocess exits.

The same Python package exposes three helpers for use **inside** the entry script: `jd_upload`, `jd_update_checkpoint`, and `jd_get_last_checkpoint`. They talk to the job server over HTTP (`requests`).

---

## Requirements

- **Python** ≥ 3.8  
- **Dependencies** (installed with the package): `requests`, `psutil`  
- A running Job Distributor **job server** (`/request_job`, `/update_job_status`, `/ping`, `/upload`, `/checkpoint`, `/checkpoint/latest`) reachable from the worker machine.

---

## Install

### From a cloned repository (editable)

From the repo root:

```bash
pip install -e ./client
```

This installs the distribution named **`jd-worker`** and registers the console script **`jd_worker`**.

### From GitHub (no clone)

The installable client (`pyproject.toml`, `jd/` package) is on branch **`v2`** (default **`main`** may not include it yet). Pin the branch in the URL:

```bash
pip install "git+https://github.com/NWSL-UCF/job-distributor.git@v2#subdirectory=client"
```

After `v2` is merged into `main`, you can omit `@v2`.

Verify:

```bash
jd_worker help
```

---

## Operate `jd_worker`

### Invocation style

Arguments are **`key=value`** tokens (and optional bare flags such as `once=true`). Run `jd_worker help` for the embedded usage text.

### Required

| Argument | Meaning |
|----------|---------|
| `expId=<id>` | Experiment id — **must match** the job server’s `--expId`. |
| `entry_script=<path>` | Path to the Python file executed **with the same interpreter** as `jd_worker` (venv / conda respected via `sys.executable`). |

### Optional

| Argument | Default | Env fallback |
|----------|---------|----------------|
| `server=<url>` | `http://localhost` | `JD_SERVER` |
| `port=<N>` | `5000` | `JD_PORT` (used if `server` has no port) |
| `log_dir=<path>` | `./jd_logs` | `JD_LOG_DIR` |
| `output_dir=<path>` | `./jd_output` | `JD_OUTPUT_DIR` |
| `machine_type=<label>` | `worker` | `JD_MACHINE_TYPE` |
| `process_id=<N>` | `0` | — |
| `once=true` | off | `JD_ONCE=true` |

### Behaviour

1. **Request job** — `POST /request_job` with runner id and system metrics.  
2. **Run entry script** — For each key/value in the job’s `parameters`, the worker appends `--<key> <value>` (stringified). It always appends `--base_path <dir>` where:

   `<dir> = <output_dir>/<expId>/<job_id>`

   The directory is created before launch.

3. **Heartbeat** — Background thread: `POST /ping` every **57** seconds with `{"id": <job_id>}`.

4. **Status** — Exit code `0` → `POST /update_job_status` with `DONE`; non-zero → `ABORTED` with a short message derived from stderr when useful.

5. **Loop** — After each job, waits 3 seconds and requests the next, until the server returns **404** (no jobs). With **`once=true`**, exits after a single job attempt cycle.

### Examples

```bash
jd_worker expId=my_exp entry_script=train.py
```

```bash
jd_worker expId=my_exp entry_script=train.py server=http://192.168.1.10 port=8000 output_dir=/scratch/runs machine_type=gpu_a
```

```bash
jd_worker expId=my_exp entry_script=train.py once=true
```

### Logs

Logs go under `<log_dir>/<expId>/jd_worker_<runner_id>.log` plus stdout.

---

## Entry script contract

Your script **must** accept:

- One `--<param>` flag per job parameter (names and values come from the dashboard / DB).  
- **`--base_path`** — workspace directory for this job on the worker (already created).

Example: if parameters are `lr=0.01` and `epochs=10`, the worker runs approximately:

```bash
python train.py --lr 0.01 --epochs 10 --base_path ./jd_output/my_exp/42
```

### Environment injected by `jd_worker` (for library calls)

| Variable | Purpose |
|----------|---------|
| `JD_JOB_ID` | Current job id (string). |
| `JD_SERVER` | Job server base URL (e.g. `http://host:5000`). |
| `JD_EXP_ID` | Experiment id (same as `expId`). |

These are set **only** for the entry-script subprocess so `jd_upload` / checkpoint helpers work without extra arguments.

---

## Library API (`jd` package)

Import:

```python
from jd import jd_upload, jd_update_checkpoint, jd_get_last_checkpoint
```

All three resolve **`job_id`** and **`server`** from `JD_JOB_ID` and `JD_SERVER` when omitted. Override with keyword arguments only if you must (e.g. tests).

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

The job server writes under:

`<BASE_DIR>/<expId>/<job_id>/`

- **Uploads:** `result_v{version}_{timestamp}.{ext}`  
- **Checkpoints:** `checkpoint_v{version}_{timestamp}.pt`

With the recommended **`server/start.py`** launcher and **default** `--workspace_path` (the `server/` directory containing `start.py`), `BASE_DIR` resolves to that same `server/` directory, so artifacts sit next to `jobs.db` under `server/<expId>/`. If you use a **custom** `--workspace_path`, confirm your deployment’s job server `BASE_DIR` matches where you expect files (see `server/src/server.py`).

---

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| `JD_JOB_ID` / `JD_SERVER` runtime errors | Library called outside a `jd_worker`-launched entry script, or env overridden incorrectly. |
| `413` / size errors | Payload over 100 MB — shrink checkpoint or split artifacts. |
| `pickle` errors on load | Different library versions, unpickleable objects, or incompatible pickles across Python builds. |
| Connection errors | Wrong `server` / `port`, firewall, or job server not bound to reachable interface. |

---

## Related docs

- [features.md](features.md) — broader product behaviour  
- [update-job.md](update-job.md) — editing pending job parameters from the dashboard  
