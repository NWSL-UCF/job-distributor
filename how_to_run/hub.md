# How to Run the JD Hub

The Hub is the central web application that handles user accounts, experiment
registration, FRP tunnel configuration, data-quota tracking, and worker
authentication.

---

## Prerequisites

- Python 3.11+
- MySQL 8.x (running and accessible)
- An FRP server (`frps`) running on the same host (for tunnel management)
- A domain with wildcard DNS pointing to this server  
  e.g. `*.jobdistributor.net → <your VPS IP>`

---

## 1. Install dependencies

```bash
cd job-distributor/hub
pip install -r requirements.txt
```

---

## 2. Configure environment

Copy the example file and fill in every value:

```bash
cp .env.example .env
```

Open `.env` and set:

| Variable | Description |
|---|---|
| `MYSQL_HOST` | MySQL host (usually `localhost`) |
| `MYSQL_PORT` | MySQL port (default `3306`) |
| `MYSQL_USER` | MySQL username |
| `MYSQL_PASSWORD` | MySQL password |
| `MYSQL_DATABASE` | Database name (e.g. `jd_hub`) |
| `FLASK_SECRET_KEY` | Long random string for session signing |
| `FLASK_ENV` | `production` or `development` |
| `HUB_BASE_URL` | Public URL of the Hub (e.g. `https://hub.jobdistributor.net`) |
| `JD_BASE_DOMAIN` | Base domain (e.g. `jobdistributor.net`) |
| `FRPS_TOKEN` | Shared token used in generated `frpc.ini` files |
| `FRPS_API_URL` | frps admin API URL (default `http://localhost:7500`) |
| `JWT_SECRET_KEY` | Long random string for JWT signing (keep secret) |
| `JWT_WORKER_TOKEN_TTL_HOURS` | Worker token lifetime in hours (default `24`) |
| `HUB_SESSION_TTL_DAYS` | Web login session lifetime in days (default `30`) |
| `BREVO_API_KEY` | Brevo API key for sending emails |
| `BREVO_FROM_EMAIL` | Sender email address (e.g. `info@jobdistributor.net`) |
| `BREVO_FROM_NAME` | Sender display name (e.g. `JobDistributor Team`) |

---

## 3. Create the MySQL database

```sql
CREATE DATABASE jd_hub CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'hub_user'@'localhost' IDENTIFIED BY 'your-password';
GRANT ALL PRIVILEGES ON jd_hub.* TO 'hub_user'@'localhost';
FLUSH PRIVILEGES;
```

The Hub auto-creates all tables on first startup using SQLAlchemy `create_all`.
Alternatively, for exact production DDL use the reference schema:

```bash
mysql -u hub_user -p jd_hub < schema.sql
```

---

## 4. Create the first admin user

Start the Hub once in development mode to trigger table creation, then create
an admin directly in MySQL:

```sql
-- After running the Hub once so the table exists:
UPDATE users SET is_admin = 1 WHERE email = 'your@email.com';
```

Or sign up normally via `/signup` and then promote via SQL.

---

## 5. Run the Hub

### Development (Flask dev server)

```bash
cd job-distributor/hub
FLASK_APP=app:create_app flask run --port 5000
```

### Production (Gunicorn — recommended)

```bash
cd job-distributor   # repo root, so `hub` package is importable
gunicorn "hub.wsgi:app" \
  --workers=1 \
  --threads=4 \
  --bind=0.0.0.0:5000 \
  --timeout=120 \
  --access-logfile=hub_access.log \
  --error-logfile=hub_error.log
```

> **Important:** Use `--workers=1`. The Hub starts background threads
> (traffic poller, usage aggregator, idle checker) inside the single worker.
> Multiple workers would create duplicate threads. Use `--threads` for
> concurrency instead.

---

## 6. Put nginx in front (production)

Minimal nginx config:

```nginx
server {
    listen 443 ssl;
    server_name hub.jobdistributor.net;

    ssl_certificate     /etc/letsencrypt/live/jobdistributor.net/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/jobdistributor.net/privkey.pem;

    location / {
        proxy_pass         http://127.0.0.1:5000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_read_timeout 120;
    }
}

# Wildcard subdomains → frps vhost proxy
server {
    listen 443 ssl;
    server_name *.jobdistributor.net;

    ssl_certificate     /etc/letsencrypt/live/jobdistributor.net/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/jobdistributor.net/privkey.pem;

    location / {
        proxy_pass         http://127.0.0.1:8080;  # frps vhost_http_port
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_read_timeout 300;
    }
}
```

---

## 7. Stop the Hub

```bash
pkill -f "hub.wsgi:app"
```

Or if running under a process manager (systemd, supervisor), use its stop command.

---

## Directory structure created at runtime

```
job-distributor/
└── hub/
    ├── .env                  ← your secrets (gitignored)
    ├── hub_access.log        ← gunicorn access log
    └── hub_error.log         ← gunicorn error log
```

All database state is in MySQL (`jd_hub` database).

---

## Environment variable reference (quick copy)

```bash
export MYSQL_HOST=localhost
export MYSQL_PORT=3306
export MYSQL_USER=hub_user
export MYSQL_PASSWORD=your-password
export MYSQL_DATABASE=jd_hub
export FLASK_SECRET_KEY=$(openssl rand -hex 32)
export FLASK_ENV=production
export HUB_BASE_URL=https://hub.jobdistributor.net
export JD_BASE_DOMAIN=jobdistributor.net
export FRPS_TOKEN=your-frps-token
export FRPS_API_URL=http://localhost:7500
export JWT_SECRET_KEY=$(openssl rand -hex 32)
export BREVO_API_KEY=your-brevo-key
export BREVO_FROM_EMAIL=info@jobdistributor.net
export BREVO_FROM_NAME="JobDistributor Team"
```
