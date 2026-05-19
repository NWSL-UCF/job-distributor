FROM python:3.11-slim

# System build deps (needed by numpy/pandas C extensions)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first so Docker can cache this layer
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the server source
COPY . .

# /workspace is the mount point for the host data directory.
# All DB files, logs, uploads, and checkpoints are written here.
VOLUME ["/workspace"]

# Ports are NOT exposed — frpc (sidecar) proxies traffic to these ports
# from the public FRP tunnel without any host-port mapping.
# If you want direct local access, add -p 8000:8000 -p 8001:8001 at runtime.

ENV JD_WORKSPACE_PATH=/workspace

# start.py accepts --expId, --server_port, --dashboard_port at runtime.
# Pass them via docker compose `command:` or docker run args.
ENTRYPOINT ["python", "start.py"]
CMD ["--expId=default", "--server_port=8000", "--dashboard_port=8001"]
