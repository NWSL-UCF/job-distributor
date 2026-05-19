FROM python:3.11-slim

# System build deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
        wget \
    && rm -rf /var/lib/apt/lists/*

# ── Bundle frpc (multi-arch) ─────────────────────────────────────────────────
ARG FRPC_VERSION=0.61.1
ARG TARGETARCH
RUN set -eux; \
    case "$TARGETARCH" in \
      amd64)  FRPC_ARCH="amd64"   ;; \
      arm64)  FRPC_ARCH="arm64"   ;; \
      arm)    FRPC_ARCH="arm"     ;; \
      *)      FRPC_ARCH="amd64"   ;; \
    esac; \
    wget -qO /tmp/frpc.tar.gz \
      "https://github.com/fatedier/frp/releases/download/v${FRPC_VERSION}/frp_${FRPC_VERSION}_linux_${FRPC_ARCH}.tar.gz"; \
    tar -xzf /tmp/frpc.tar.gz -C /tmp; \
    mv /tmp/frp_${FRPC_VERSION}_linux_${FRPC_ARCH}/frpc /usr/local/bin/frpc; \
    chmod +x /usr/local/bin/frpc; \
    rm -rf /tmp/frpc.tar.gz /tmp/frp_*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# /workspace is the mount point for user data (jobs DB, logs, checkpoints).
VOLUME ["/workspace"]

ENV JD_WORKSPACE_PATH=/workspace

RUN chmod +x /app/scripts/hub-entrypoint.sh
ENTRYPOINT ["/app/scripts/hub-entrypoint.sh"]
CMD ["--server_port=8000", "--dashboard_port=8001"]
