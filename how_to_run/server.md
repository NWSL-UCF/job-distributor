# How to Run the Job Server

The job server is the backend your workers talk to directly. It serves jobs,
receives status updates, stores checkpoints and uploads, and exposes a dashboard
UI. It runs on **your own machine** (locally or inside Docker) and is exposed
publicly via an FRP tunnel managed by the Hub.

---

## Prerequisites

- Python 3.11+ with `pip`
- A MySQL-free setup — the server uses **SQLite** (auto-created, no config needed)

---

## 1. Install dependencies

```bash
cd job-distributor/server
pip install -r requirements.txt
```

`requirements.txt` includes: `flask`, `gunicorn`, `pandas`, `pytz`, `numpy`,
`requests`, `PyJWT`.

---

## 2. Run the server stack

`start.py` launches three processes together:

- **Job server** (Gunicorn) — handles worker API calls
- **Dashboard** (Gunicorn) — browser UI for monitoring jobs
- **Job cleaner** — background process that resets stale/aborted jobs

```bash
cd job-distributor/server
python start.py \
  --expId          my_experiment \
  --workspace_path /data/experiments
```

### Arguments

| Argument | Required | Default | Description |
|---|---|---|---|
| `--expId` | Yes | — | Unique name for this experiment. Used as the data subdirectory. |
| `--workspace_path` | No | directory of `start.py` | Root directory where all experiment data is stored |
| `--workers` | No | `1` | Gunicorn worker processes per server |
| `--threads` | No | `4` | Threads per Gunicorn worker |

### Minimal example

```bash
python start.py --expId mnist_tune
```

This creates:

```
<workspace_path>/
└── mnist_tune/
    ├── meta/
    │   ├── jobs.db             ← SQLite database
    │   ├── server.log
    │   ├── dashboard.log
    │   ├── pids.json           ← PIDs of running processes
    │   └── ...
    └── data/
        └── <job_id>/
            ├── result_v0_<ts>.csv      ← worker uploads
            └── checkpoint_v0_<ts>.pt  ← checkpoints
```

---

## 3. Hub mode (optional — for public access via FRP tunnel)

If you want workers to reach the server over the internet via the Hub's FRP
tunnel, set these additional environment variables **before** running `start.py`:

```bash
export JD_WORKER_SHARED_SECRET=<secret from Hub experiment page>
export JD_HUB_URL=https://hub.jobdistributor.net
export JD_API_KEY=jd_xxxxxxxxxxxxxxxx         # your Hub API key
export JD_EXP_NAME=my_experiment              # must match --expId

python start.py --expId my_experiment --workspace_path /data/experiments
```

When `JD_WORKER_SHARED_SECRET` is set, the server **verifies the JWT** on every
worker request. Workers must obtain a token from the Hub first (handled
automatically by `jd_worker`).

### Register with the Hub on first boot

After starting, call the Hub's register endpoint once to store the admin token
(enables remote PIN reset from the Hub dashboard):

```bash
curl -X POST https://hub.jobdistributor.net/api/experiments/my_experiment/register \
  -H "Authorization: Bearer jd_xxxxxxxxxxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "admin_token": "<your dashboard admin token>",
    "worker_shared_secret": "<JD_WORKER_SHARED_SECRET value>"
  }'
```

---

## 4. Run inside Docker

For Docker-based deployment (recommended when using the Hub and FRP tunnel),
see **`how_to_run/dockerize_server.md`** for the full guide including the
`Dockerfile`, `docker-compose.yml`, and `frpc.ini` setup.

---

## 5. Stop the server

```bash
cd job-distributor/server
python stop.py --expId my_experiment --workspace_path /data/experiments
```

`stop.py` reads the PID file from `meta/pids.json` and sends SIGTERM to all
three processes.

---

## 6. Access the dashboard

Open in your browser:

| Mode | URL |
|---|---|
| Local | `http://localhost:<dashboard_port>` |
| Hub (FRP tunnel) | `https://dashboard.<expId>.jobdistributor.net` |

The dashboard is PIN-protected. Set or reset the PIN from the **Settings** modal
inside the dashboard, or from the Hub experiment detail page.

---

## 7. Add jobs

Jobs are added through the dashboard UI — click **Add Jobs** and define your
parameter grid. The server generates all combinations automatically.

---

## Logs

All logs go under `<workspace_path>/<expId>/meta/`:

| File | Content |
|---|---|
| `server.log` | Job server application log |
| `server_access.log` | Gunicorn HTTP access log |
| `dashboard.log` | Dashboard application log |
| `dashboard_access.log` | Gunicorn HTTP access log |
| `__start__.log` | start.py orchestration log |
