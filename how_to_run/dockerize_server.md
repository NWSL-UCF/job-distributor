# How to Dockerize and Run the Job Server

This guide covers creating a Docker image for the job server stack and running
it with a mounted workspace. Ports are **not** published to the host — the
`frpc` sidecar exposes them publicly through the Hub's FRP tunnel instead.

---

## Directory layout (server folder)

```
job-distributor/
└── server/
    ├── start.py
    ├── stop.py
    ├── requirements.txt
    ├── config.json
    └── src/
        ├── server.py
        ├── dashboard.py
        ├── database.py
        ├── job_cleaner.py
        ├── workspace_layout.py
        ├── templates/
        └── static/
```

---

## 1. Write the Dockerfile

Create `job-distributor/server/Dockerfile`:

```dockerfile
FROM python:3.11-slim

# System deps (for numpy/pandas C extensions)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy server source
COPY . .

# Workspace mount point — the actual data directory is injected at runtime
# via the JD_WORKSPACE_PATH env var and a volume mount.
VOLUME ["/workspace"]

# Ports are intentionally NOT EXPOSED here.
# frpc (sidecar) proxies traffic from the public subdomain to these ports
# inside the Docker network. Publishing them would bypass the tunnel.

ENV JD_WORKSPACE_PATH=/workspace

ENTRYPOINT ["python", "start.py"]
```

---

## 2. Write `docker-compose.yml`

Create `docker-compose.yml` next to the `server/` folder (or wherever you keep
your workspace):

```yaml
version: "3.9"

services:

  # ── Job server stack ────────────────────────────────────────────────────────
  jd_server:
    build:
      context: ./server
      dockerfile: Dockerfile
    image: jd-server:latest
    container_name: jd_server
    restart: unless-stopped
    command:
      - "--expId=my_experiment"
      - "--server_port=8000"
      - "--dashboard_port=8001"
    volumes:
      - ./workspace:/workspace      # all DB, logs, uploads live here on the host
    environment:
      - JD_WORKSPACE_PATH=/workspace
      # Hub integration (optional — remove if running standalone)
      - JD_WORKER_SHARED_SECRET=${JD_WORKER_SHARED_SECRET}
      - JD_HUB_URL=${JD_HUB_URL}
      - JD_API_KEY=${JD_API_KEY}
      - JD_EXP_NAME=my_experiment
    # No `ports:` block — frpc proxies traffic internally

  # ── FRP client (exposes server + dashboard publicly) ────────────────────────
  frpc:
    image: snowdreamtech/frpc:latest
    container_name: frpc
    restart: unless-stopped
    volumes:
      - ./frpc.ini:/etc/frp/frpc.ini:ro
    depends_on:
      - jd_server
    network_mode: "service:jd_server"   # shares jd_server's network namespace
                                        # so frpc reaches localhost:8000/8001
```

> **`network_mode: "service:jd_server"`** makes frpc see `localhost:8000` and
> `localhost:8001` as if it were running inside `jd_server`. This is how frpc
> can proxy to those ports without any `ports:` declaration.

---

## 3. Create `frpc.ini`

Get the exact content from the Hub → Experiments → your experiment → **frpc.ini**
section. It looks like this (already filled in by the Hub):

```ini
[common]
server_addr = hub.jobdistributor.net
server_port = 7000
token       = <frps-token>

[server-my_experiment]
type            = http
local_port      = 8000
custom_domains  = server.my_experiment.jobdistributor.net

[dashboard-my_experiment]
type            = http
local_port      = 8001
custom_domains  = dashboard.my_experiment.jobdistributor.net
```

Save it as `frpc.ini` next to `docker-compose.yml`.

---

## 4. Create a `.env` file for Docker Compose secrets

```bash
# .env  (next to docker-compose.yml)
JD_WORKER_SHARED_SECRET=<worker_shared_secret from Hub experiment page>
JD_HUB_URL=https://hub.jobdistributor.net
JD_API_KEY=jd_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Docker Compose automatically reads `.env` and substitutes `${VAR}` values.

---

## 5. Build the image

```bash
# From the directory containing docker-compose.yml
docker compose build
```

Or build the image standalone (useful for pushing to a registry):

```bash
docker build -t jd-server:latest ./server
```

---

## 6. Run

```bash
docker compose up -d
```

This starts both `jd_server` and `frpc` in the background.

Your experiment is now reachable at:

| Service | Public URL |
|---|---|
| Job server | `https://server.my_experiment.jobdistributor.net` |
| Dashboard  | `https://dashboard.my_experiment.jobdistributor.net` |

All data is persisted on the **host** under `./workspace/`:

```
./workspace/
└── my_experiment/
    ├── meta/
    │   ├── jobs.db
    │   ├── server.log
    │   ├── dashboard.log
    │   ├── pids.json
    │   └── ...
    └── data/
        └── <job_id>/
            ├── result_v0_<ts>.csv
            └── checkpoint_v0_<ts>.pt
```

Even if the container is removed or rebuilt, your data stays intact because it
lives on the host volume, not inside the container.

---

## 7. Change the experiment name or ports

Edit `docker-compose.yml` and update the `command:` block:

```yaml
command:
  - "--expId=new_experiment_name"
  - "--server_port=8000"
  - "--dashboard_port=8001"
```

No image rebuild needed — these are runtime arguments.

---

## 8. View logs

```bash
# Live logs from both containers
docker compose logs -f

# Only the server container
docker compose logs -f jd_server

# Only frpc
docker compose logs -f frpc
```

Application logs are also written to the host under `./workspace/<expId>/meta/`.

---

## 9. Stop

```bash
docker compose down
```

Data in `./workspace/` is preserved. To also remove the volume:

```bash
docker compose down -v    # removes named volumes (not bind mounts)
```

---

## 10. Rebuild after code changes

```bash
docker compose build jd_server
docker compose up -d --force-recreate jd_server
```

---

## Full file layout for deployment

```
my_project/
├── docker-compose.yml
├── .env                    ← secrets (gitignore this)
├── frpc.ini                ← from Hub experiment page
├── workspace/              ← created automatically on first run
│   └── my_experiment/
│       ├── meta/
│       └── data/
└── server/                 ← the job-distributor server source
    ├── Dockerfile
    ├── start.py
    ├── requirements.txt
    └── src/
```

---

## Standalone (no Hub / no FRP)

If you only need local access and don't need a public tunnel, remove the `frpc`
service and add a `ports` block to `jd_server`:

```yaml
  jd_server:
    ...
    ports:
      - "8000:8000"    # job server
      - "8001:8001"    # dashboard
    # remove or comment out all JD_HUB_URL / JD_API_KEY / JD_WORKER_SHARED_SECRET
```

Workers then connect with:

```bash
jd_worker expId=my_experiment entry_script=train.py \
          server=http://<host-ip> port=8000
```
