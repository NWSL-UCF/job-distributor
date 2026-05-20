# How to Run the Job Server (Docker)

The job server is the per-experiment backend your workers talk to. It serves
jobs, receives status updates, stores checkpoints and uploads, and exposes a
browser dashboard. It runs in a Docker container and is exposed to the internet
automatically through the Hub's FRP tunnel — no manual port forwarding needed.

**frpc is bundled inside the image.** No separate sidecar container is required.

---

## Prerequisites

- Docker installed on your machine
- A Hub account at `https://hub.jobdistributor.net`
- An experiment created on the Hub (gives you an API key)

---

## Quick start — `run.sh` (recommended)

```bash
# Clone the repo (one-time)
git clone https://github.com/NWSL-UCF/job-distributor.git
cd job-distributor/server

# Start the server for your experiment
JD_API_KEY=jd_xxxxxxxxxxxxxxxx ./run.sh my-experiment
```

That's it. The container:
1. Pulls `jobdistributor/jd-server:latest` if not cached
2. Fetches the FRP config and worker secret from the Hub
3. Starts frpc to open the tunnel
4. Registers the dashboard admin token with the Hub
5. Starts the job server and dashboard
6. Sends heartbeats to the Hub every 3 minutes

Your experiment is now publicly reachable at:

| Service | URL |
|---|---|
| Job server | `https://<expName>-server.jobdistributor.net` |
| Dashboard  | `https://<expName>-dashboard.jobdistributor.net` |

Data is persisted at `~/jd_server/<expName>/` on the host.

---

## `run.sh` commands

```bash
# Start (default)
JD_API_KEY=jd_xxx ./run.sh my-experiment

# Explicit start
JD_API_KEY=jd_xxx ./run.sh my-experiment start

# Tail container logs
./run.sh my-experiment logs

# Check running status
./run.sh my-experiment status

# Stop and remove the container (data is preserved on disk)
./run.sh my-experiment stop
```

### Optional environment variables

| Variable | Default | Description |
|---|---|---|
| `JD_HUB_URL` | `https://hub.jobdistributor.net` | Hub base URL |
| `JD_WORKSPACE` | `~/jd_server` | Host root for experiment data |
| `JD_IMAGE` | `jobdistributor/jd-server:latest` | Docker image to use |

---

## Alternative — `docker compose`

Use the provided `server/docker-compose.yml` for a more declarative setup.
Pass the experiment name and API key inline — no `.env` file needed:

```bash
cd job-distributor/server

EXP=my-experiment JD_API_KEY=jd_xxxxxxxxxxxxxxxx docker compose up -d
```

### Running multiple experiments on the same machine

Each experiment gets its own container and workspace:

```bash
EXP=tuning-01 JD_API_KEY=jd_key1 docker compose up -d
EXP=tuning-02 JD_API_KEY=jd_key2 docker compose up -d
```

Stop a specific experiment:

```bash
docker stop jd-tuning-01 && docker rm jd-tuning-01
```

---

## Workspace layout

All data is written on the **host** (survives container restarts and rebuilds):

```
~/jd_server/
└── <expName>/
    ├── meta/
    │   ├── jobs.db              ← SQLite job queue
    │   ├── server.log
    │   ├── dashboard.log
    │   ├── server_access.log
    │   ├── dashboard_access.log
    │   ├── pids.json
    │   └── ...
    └── data/
        └── <job_id>/
            ├── result_v0_<ts>.csv     ← worker uploads
            └── checkpoint_v0_<ts>.pt  ← checkpoints
```

Override the root by setting `JD_WORKSPACE`:

```bash
JD_API_KEY=jd_xxx JD_WORKSPACE=/data/experiments ./run.sh my-experiment
```

---

## View logs

```bash
# Live container logs (entrypoint + frpc + start.py)
./run.sh my-experiment logs

# Or directly
docker logs -f jd-my-experiment

# Application logs on disk
tail -f ~/jd_server/my-experiment/meta/server.log
tail -f ~/jd_server/my-experiment/meta/dashboard.log
```

---

## Add jobs

Jobs are defined from the dashboard UI:

1. Open `https://<expName>-dashboard.jobdistributor.net` (PIN prompted on first visit)
2. Click **Add Jobs** and enter your parameter grid
3. The server generates all parameter combinations automatically

Alternatively, add jobs via the Hub experiment page.

---

## Update to the latest image

```bash
docker pull jobdistributor/jd-server:latest

# Restart with the new image
./run.sh my-experiment stop
JD_API_KEY=jd_xxx ./run.sh my-experiment start
```

---

## Troubleshooting

### Container exits immediately

Check logs for the error:
```bash
docker logs jd-my-experiment
```

Common causes:
- `JD_API_KEY` is wrong or for a different experiment
- Experiment does not exist on the Hub — create it first
- Hub is unreachable

### Dashboard PIN locked / PIN reset from Hub not working

The Hub sends a PIN reset request using the admin token registered at startup.
If the server just started, wait ~30 seconds for `hub_register.py` to complete,
then retry from the Hub dashboard.

### frpc tunnel not connecting

Check the server logs for `frpc` errors:
```bash
docker logs jd-my-experiment 2>&1 | grep -i "frpc\|tunnel\|bootstrap"
```

If `hub_bootstrap` failed, the `frpc.ini` was not fetched — verify `JD_API_KEY`
and that the experiment is `ACTIVE` on the Hub.
