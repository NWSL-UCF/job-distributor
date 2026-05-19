# How to Run the Worker (`jd_worker`)

`jd_worker` is the command-line tool that runs on your compute node (laptop,
GPU server, cluster, etc.). It requests a job from the server, runs your entry
script with the job's parameters as CLI arguments, sends heartbeats every 57
seconds, and reports DONE or ABORTED when the script finishes.

---

## Prerequisites

- Python 3.9+ with `pip`
- The compute environment you want to use (venv, conda, etc.) **already active**
- Your entry script (`train.py`, `main.py`, etc.) in the current directory

---

## 1. Install `jd_worker`

### From PyPI (once published)

```bash
pip install jd-worker
```

### From GitHub (current method)

```bash
pip install "git+https://github.com/NWSL-UCF/job-distributor.git@v2#subdirectory=client"
```

### From local source (development)

```bash
cd job-distributor
pip install -e ./client
```

After installation, `jd_worker` is available as a command in your active
Python environment.

---

## 2. Standalone mode (direct connection to server)

Use this when the server is on your local network or reachable via a known URL.

```bash
jd_worker expId=my_experiment entry_script=train.py
```

### With a remote server

```bash
jd_worker expId=my_experiment entry_script=train.py \
          server=http://10.0.0.5 port=8000
```

### All options

| Argument | Env var | Default | Description |
|---|---|---|---|
| `expId=<name>` | `JD_EXP_ID` | required | Experiment name (must match server) |
| `entry_script=<path>` | `JD_ENTRY_SCRIPT` | required | Python script to run per job |
| `server=<url>` | `JD_SERVER` | `http://localhost` | Job server base URL or hostname |
| `port=<N>` | `JD_PORT` | `5000` | Port (if not in `server`) |
| `machine_type=<label>` | `JD_MACHINE_TYPE` | `worker` | Label shown in dashboard |
| `process_id=<N>` | — | `0` | ID when running multiple workers on same machine |
| `once=true` | `JD_ONCE` | `false` | Exit after completing exactly one job |
| `log_dir=<path>` | `JD_LOG_DIR` | auto | Override log directory |

---

## 3. Hub mode (FRP tunnel — internet access)

Use this when your server is behind NAT and exposed via the Hub's FRP tunnel.
`jd_worker` contacts the Hub first to get a short-lived JWT and the tunnel URL,
then communicates with the server through the public subdomain.

### Step 1 — Get your API key

1. Sign in at `https://hub.jobdistributor.net`
2. Go to **Profile**
3. Click **Generate API key** and copy the full key (shown once)

### Step 2 — Set environment variables

```bash
export JD_HUB_URL=https://hub.jobdistributor.net
export JD_API_KEY=jd_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### Step 3 — Run the worker

```bash
jd_worker expId=my_experiment entry_script=train.py
```

That's it. `jd_worker` will:
1. Call `POST /api/worker/token` on the Hub with your API key
2. Receive a JWT worker token and the public server URL
3. Use the token as `Authorization: Bearer` on every server request
4. Run jobs normally through the FRP tunnel

No `server=` or `port=` arguments are needed in Hub mode — the URL comes from
the Hub.

### Full Hub-mode example

```bash
export JD_HUB_URL=https://hub.jobdistributor.net
export JD_API_KEY=jd_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

jd_worker expId=mnist_tune entry_script=train.py \
          machine_type=gpu_a100
```

---

## 4. Where data is stored locally

`jd_worker` stores logs and a local job workspace under:

```
~/jd_data/<expId>/<job_id>/
```

Override the parent directory by setting `JD_WORKSPACE_PATH`:

```bash
export JD_WORKSPACE_PATH=/scratch
# data goes to: /scratch/jd_data/<expId>/<job_id>/
```

Your entry script receives this path automatically:
- `--base_path <path>` CLI argument
- `JD_WORKER_JOB_DIR` environment variable
- `JD_WORKER_WORKSPACE_ROOT` environment variable (`~/jd_data`)

---

## 5. Entry script contract

Your entry script receives job parameters as `--key value` CLI arguments plus
`--base_path <local_job_dir>`.

```python
# train.py
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--learning_rate", type=float)
parser.add_argument("--batch_size",    type=int)
parser.add_argument("--base_path",     type=str)   # local job directory
args = parser.parse_args()

# Run your training code...
```

### Available environment variables inside the script

| Variable | Value |
|---|---|
| `JD_JOB_ID` | Integer job ID |
| `JD_SERVER` | Job server base URL |
| `JD_EXP_ID` | Experiment name |
| `JD_WORKER_JOB_DIR` | Full path to local job directory |
| `JD_WORKER_WORKSPACE_ROOT` | Root of local jd_data directory |
| `JD_WORKER_TOKEN` | JWT worker token (set in Hub mode; used by `jd_upload` etc.) |

---

## 6. Library functions (inside your entry script)

Import and use these from inside `train.py`:

```python
from jd import jd_upload, jd_update_checkpoint, jd_get_last_checkpoint
```

### `jd_upload(file_path)`

Upload a result file (≤ 100 MB) to the server. Stored as
`result_v{N}_{timestamp}.<ext>` in the job's server-side directory.

```python
jd_upload("outputs/metrics.csv")
jd_upload("outputs/predictions.json")
```

### `jd_update_checkpoint(obj)`

Serialise any Python object with pickle and save it as a versioned checkpoint
(≤ 100 MB) on the server. Each call creates a new version.

```python
jd_update_checkpoint({
    "epoch":      epoch,
    "model":      model.state_dict(),
    "optimizer":  optimizer.state_dict(),
})
```

### `jd_get_last_checkpoint()`

Download the latest checkpoint for this job into memory (no file written to
disk). Returns `None` if no checkpoint exists yet.

```python
ckpt = jd_get_last_checkpoint()
if ckpt is not None:
    model.load_state_dict(ckpt["model"])
    start_epoch = ckpt["epoch"] + 1
else:
    start_epoch = 0
```

---

## 7. Running multiple workers in parallel

Run the command in multiple terminals (or tmux panes), with different
`process_id` values so the dashboard can distinguish them:

```bash
# Terminal 1
jd_worker expId=mnist_tune entry_script=train.py machine_type=gpu process_id=0

# Terminal 2
jd_worker expId=mnist_tune entry_script=train.py machine_type=gpu process_id=1

# Terminal 3
jd_worker expId=mnist_tune entry_script=train.py machine_type=gpu process_id=2
```

Workers stop automatically when no more `PENDING` jobs remain.

---

## 8. Logs

Logs are written to:

```
~/jd_data/<expId>/jd_worker_logs/jd_worker_<runner_id>.log
```

And also printed to stdout. Override with `log_dir=<path>` or `JD_LOG_DIR`.

---

## 9. Quick-start checklist

```
[ ] Server is running (see how_to_run/server.md)
[ ] pip install jd-worker (or install from source)
[ ] entry script uses --base_path and any custom args
[ ] Jobs have been added in the dashboard
[ ] (Hub mode) JD_HUB_URL and JD_API_KEY are exported
[ ] Run: jd_worker expId=<name> entry_script=<script.py>
```
