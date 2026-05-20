# Hub Deployment Guide

This guide sets up the full Hub stack on a fresh Ubuntu 22.04 VPS using
**Docker Compose**. The stack is three containers:

- **nginx** — TLS termination and routing
- **jd-hub** — Hub Flask application (Gunicorn)
- **frps** — FRP server for experiment tunnels

MySQL runs on the **host** (not inside Docker) and is reached by the hub
container via `host.docker.internal`.

```
Internet
  │
  ├─ HTTPS 443 ──► nginx ──► hub.yourdomain.com  ──► jd-hub :5000
  │                        └─ *.yourdomain.com   ──► frps vhost :8080
  │                                                       │
  └─ TCP  7000 ──────────────────────────────────► frps bind port
                                             (jd_server containers connect here)
```

---

## Prerequisites

- Ubuntu 22.04 VPS with a public IP
- Ports **80**, **443**, and **7000** open in the firewall
- Domain with two DNS A-records:
  - `hub.yourdomain.com → <VPS IP>`
  - `*.yourdomain.com  → <VPS IP>` (wildcard)

---

## 1. Install system packages

```bash
sudo apt update && sudo apt install -y \
    docker.io docker-compose-plugin \
    certbot \
    mysql-server

sudo usermod -aG docker $USER   # log out and back in
```

---

## 2. Obtain a wildcard TLS certificate

Wildcard certs require the **DNS-01** challenge. The example uses Cloudflare;
swap the plugin for your DNS provider.

```bash
sudo pip install certbot-dns-cloudflare

# Create API token credentials file
sudo tee /etc/letsencrypt/cloudflare.ini > /dev/null <<'EOF'
dns_cloudflare_api_token = YOUR_CLOUDFLARE_API_TOKEN
EOF
sudo chmod 600 /etc/letsencrypt/cloudflare.ini

# Issue the cert (covers yourdomain.com and *.yourdomain.com)
sudo certbot certonly \
  --dns-cloudflare \
  --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
  -d "yourdomain.com" \
  -d "*.yourdomain.com" \
  --agree-tos --email you@example.com
```

> See <https://eff-certbot.readthedocs.io/en/stable/using.html#dns-plugins>
> for other providers (Route53, DigitalOcean, etc.)

Generate DH params (one-time, takes ~1 min):

```bash
sudo openssl dhparam -out /etc/letsencrypt/ssl-dhparams.pem 2048
```

---

## 3. Set up MySQL on the host

```bash
sudo mysql -u root <<'SQL'
CREATE DATABASE jd_hub CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'hub_user'@'localhost' IDENTIFIED BY 'CHANGE_ME_DB_PASSWORD';
CREATE USER 'hub_user'@'%'         IDENTIFIED BY 'CHANGE_ME_DB_PASSWORD';
GRANT ALL PRIVILEGES ON jd_hub.* TO 'hub_user'@'localhost';
GRANT ALL PRIVILEGES ON jd_hub.* TO 'hub_user'@'%';
FLUSH PRIVILEGES;
SQL
```

Allow Docker containers to reach MySQL on the host:

```bash
sudo sed -i 's/^bind-address\s*=.*/bind-address = 0.0.0.0/' \
    /etc/mysql/mysql.conf.d/mysqld.cnf
sudo systemctl restart mysql
```

---

## 4. Configure the Hub environment

```bash
cd /path/to/job-distributor/deploy

cp hub.env.example hub.env
nano hub.env   # fill in every value
```

Key values to set:

| Variable | Value |
|---|---|
| `MYSQL_HOST` | `host.docker.internal` |
| `MYSQL_PASSWORD` | Password from step 3 |
| `FLASK_SECRET_KEY` | `openssl rand -hex 32` |
| `HUB_BASE_URL` | `https://hub.yourdomain.com` |
| `JD_BASE_DOMAIN` | `yourdomain.com` |
| `FRPS_TOKEN` | `openssl rand -hex 24` (use same value in frps.toml) |
| `FRPS_API_URL` | `http://frps:7500` (Docker service name — do not change) |
| `JWT_SECRET_KEY` | `openssl rand -hex 32` |
| `BREVO_API_KEY` | Brevo transactional email API key |

---

## 5. Configure FRP token

Set the same token in `frps/frps.toml` as `FRPS_TOKEN` in `hub.env`:

```bash
sed -i 's/CHANGE_ME_FRPS_TOKEN/your-actual-token/' frps/frps.toml
```

---

## 6. Update Nginx config for your domain

If your domain is not `jobdistributor.net`, update the nginx config:

```bash
sed -i 's/jobdistributor\.net/yourdomain.com/g' nginx/hub-docker.conf
```

Also verify the TLS cert paths in `nginx/hub-docker.conf` match where
certbot stored your certificates.

---

## 7. Start the stack

```bash
cd /path/to/job-distributor/deploy

docker compose -f hub-compose.yml pull
docker compose -f hub-compose.yml up -d
```

Verify all three containers are running:

```bash
docker compose -f hub-compose.yml ps
docker compose -f hub-compose.yml logs hub --tail=50
```

The Hub auto-creates all database tables on first startup.

---

## 8. Promote the first admin user

Sign up at `https://hub.yourdomain.com/signup`, then:

```bash
mysql -u hub_user -p jd_hub \
  -e "UPDATE users SET is_admin = 1 WHERE email = 'your@email.com';"
```

---

## 9. Verify everything works

```bash
# Hub responds with HTTP 200
curl -I https://hub.yourdomain.com/

# frps admin API is reachable from the hub container
docker exec hub_app python3 -c \
  "import requests; print(requests.get('http://frps:7500/api/info').json())"
```

---

## Updating the Hub

When a new `jobdistributor/jd-hub:latest` image is pushed to Docker Hub:

```bash
cd /path/to/job-distributor/deploy

docker compose -f hub-compose.yml pull hub
docker compose -f hub-compose.yml up -d --no-deps hub
```

---

## Firewall rules summary

| Port | Protocol | Source    | Purpose                          |
|------|----------|-----------|----------------------------------|
| 22   | TCP      | your IP   | SSH                              |
| 80   | TCP      | anywhere  | HTTP → HTTPS redirect            |
| 443  | TCP      | anywhere  | HTTPS (Hub + experiment tunnels) |
| 7000 | TCP      | anywhere  | frpc tunnel connections          |
| 8080 | TCP      | localhost | frps vhost HTTP (Nginx → frps)   |
| 7500 | TCP      | localhost | frps admin API (hub → frps)      |
| 3306 | TCP      | localhost | MySQL                            |

Ports 8080, 7500, and 3306 must **not** be open to the internet.

---

## File layout

```
deploy/
├── hub-compose.yml          ← Docker Compose stack
├── hub.env                  ← your secrets (gitignore, never commit)
├── hub.env.example          ← template
├── nginx/
│   ├── hub-docker.conf      ← Nginx TLS + proxy config (Docker)
│   └── jobdistributor.net.conf  ← bare-metal Nginx reference
└── frps/
    └── frps.toml            ← FRP server config
```
