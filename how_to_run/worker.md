# How to Run the Worker (`jd_worker_cli`)

`jd_worker_cli` is the command-line tool that runs on your compute node (laptop,
GPU server, cluster, etc.). It requests jobs from the server, runs your entry
script with each job's parameters, sends heartbeats every 57 seconds, and
reports DONE or ABORTED when the script finishes.

---

## Prerequisites

- Python 3.8+
- Your compute environment (venv, conda, etc.) **already active**
- Your entry script (`train.py`, `main.py`, etc.) present in the working directory

---

## 1. Install `jd_worker_cli`

```bash
pip install jd-worker
```

After installation, `jd_worker_cli` is available as a command in your active
Python environment.

### Upgrade to the latest version

```bash
pip install --upgrade jd-worker
```

### Install from source (development)

```bash
git clone https://github.com/NWSL-UCF/job-distributor.git
pip install -e job-distributor/client
```

---

## 2. Hub mode (recommended — internet access via FRP tunnel)

Use this when your server is running via Docker and exposed through the Hub's
FRP tunnel. `jd_worker_cli` contacts the Hub first to get a JWT and the server URL,
then communicates with the server through the public subdomain.

### Step 1 — Get your API key

1. Sign in at `https://hub.jobdistributor.net`
2. Go to **Profile**
3. Click **Generate API key** and copy the full key (shown once)

### Step 2 — Set your API key

```bash
export JD_API_KEY=jd_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### Step 3 — Run the worker

```bash
jd_worker_cli expId=my-experiment entry_script=train.py
```

That's it. `jd_worker_cli` will:
1. Call the Hub with your API key to get a JWT worker token and the server URL
2. Use the token as `Authorization: Bearer` on every server request
3. Request jobs, run your script, report results

No `server=` argument needed — the Hub provides the URL automatically.

### Full example with machine label

```bash
export JD_API_KEY=jd_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

jd_worker_cli expId=my-experiment entry_script=train.py machine_type=gpu_a100
```

---

## 3. Standalone mode (local / direct connection)

Use this when the server is on your local network or reachable directly.

```bash
jd_worker_cli expId=my-experiment entry_script=train.py \
          server=http://10.0.0.5 port=8000
```

For a server on the same machine:

```bash
jd_worker_cli expId=my-experiment entry_script=train.py
# defaults to server=http://localhost port=5000
```

---

## 4. All CLI options

| Argument | Env var | Default | Description |
|---|---|---|---|
| `expId=<name>` | `JD_EXP_ID` | required | Experiment name (must match server) |
| `entry_script=<path>` | `JD_ENTRY_SCRIPT` | required | Python script to run per job |
| `api_key=<key>` | `JD_API_KEY` | — | Hub API key (enables Hub mode) |
| `hub=<url>` | `JD_HUB_URL` | `https://hub.jobdistributor.net` | Hub base URL |
| `server=<url>` | `JD_SERVER` | `http://localhost` | Job server URL (standalone mode) |
| `port=<N>` | `JD_PORT` | `5000` | Port (standalone mode, if not in `server`) |
| `machine_type=<label>` | `JD_MACHINE_TYPE` | `worker` | Label shown in dashboard |
| `process_id=<N>` | — | `0` | ID for running multiple workers on same machine |
| `once=true` | `JD_ONCE` | `false` | Exit after completing exactly one job |
| `log_dir=<path>` | `JD_LOG_DIR` | auto | Override log directory |

---

## 5. Entry script contract

Your entry script receives job parameters as `--key value` CLI arguments.

```python
# train.py
import argparse
import os

parser = argparse.ArgumentParser()
parser.add_argument("--learning_rate", type=float)
parser.add_argument("--batch_size",    type=int)
args = parser.parse_args()

# Access the local job directory via environment variable
job_dir = os.environ["JD_WORKER_JOB_DIR"]

# Run your training code...
```

### Environment variables available inside the script

| Variable | Value |
|---|---|
| `JD_JOB_ID` | Integer job ID |
| `JD_SERVER` | Job server base URL |
| `JD_EXP_ID` | Experiment name |
| `JD_WORKER_JOB_DIR` | Full path to local job directory |
| `JD_WORKER_WORKSPACE_ROOT` | Root of local jd_data directory |
| `JD_WORKER_TOKEN` | JWT worker token (Hub mode; used by `jd_upload` etc.) |

### Path helpers (inside the script)

```python
from jd import jd_job_dir, jd_worker_workspace, jd_exp_dir

job_dir  = jd_job_dir()         # same as os.environ["JD_WORKER_JOB_DIR"]
root_dir = jd_worker_workspace() # ~/jd_data
exp_dir  = jd_exp_dir()          # ~/jd_data/<expId>
```

---

## 6. Library functions

Import these from inside your entry script to interact with the server:

```python
from jd import jd_upload, jd_update_checkpoint, jd_get_last_checkpoint
```

### `jd_upload(file_path)`

Upload a result file (≤ 100 MB) to the server. Stored versioned under the
job's server-side directory.

```python
jd_upload("outputs/metrics.csv")
jd_upload("outputs/predictions.json")
```

### `jd_update_checkpoint(obj)`

Serialise any Python object with pickle and save it as a versioned checkpoint
(≤ 100 MB) on the server. Each call creates a new version.

```python
jd_update_checkpoint({
    "epoch":     epoch,
    "model":     model.state_dict(),
    "optimizer": optimizer.state_dict(),
})
```

### `jd_get_last_checkpoint()`

Download the latest checkpoint for this job into memory. Returns `None` if no
checkpoint exists yet.

```python
ckpt = jd_get_last_checkpoint()
if ckpt is not None:
    model.load_state_dict(ckpt["model"])
    start_epoch = ckpt["epoch"] + 1
else:
    start_epoch = 0
```

---

## 7. Where data is stored locally

`jd_worker_cli` stores logs and a local job workspace under:

```
~/jd_data/<expId>/<job_id>/
```

Override the parent directory:

```bash
export JD_WORKSPACE_PATH=/scratch
# data goes to: /scratch/jd_data/<expId>/<job_id>/
```

---

## 8. Running multiple workers in parallel

Run the command in multiple terminals with different `process_id` values so
the dashboard can distinguish them:

```bash
# Terminal 1
jd_worker_cli expId=my-experiment entry_script=train.py machine_type=gpu process_id=0

# Terminal 2
jd_worker_cli expId=my-experiment entry_script=train.py machine_type=gpu process_id=1

# Terminal 3
jd_worker_cli expId=my-experiment entry_script=train.py machine_type=gpu process_id=2
```

Workers stop automatically when no more `PENDING` jobs remain.

---

## 9. Logs

Logs are written to stdout and to:

```
~/jd_data/<expId>/jd_worker_logs/jd_worker_<runner_id>.log
```

Override with `log_dir=<path>` or `JD_LOG_DIR`.

---

## Quick-start checklist

```
[ ] Server is running (see how_to_run/server.md)
[ ] pip install jd-worker
[ ] Jobs have been added in the dashboard
[ ] (Hub mode) export JD_API_KEY=jd_xxxx
[ ] Run: jd_worker_cli expId=<name> entry_script=<script.py>
```
