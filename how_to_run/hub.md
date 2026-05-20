# How to Run the JD Hub (Docker)

The Hub is the central control plane: user accounts, experiment registration,
FRP tunnel management, quota tracking, and worker authentication. It is deployed
as a Docker Compose stack consisting of **Nginx**, **jd-hub**, and **frps**, with
**MySQL** running on the host.

---

## Architecture

```
Internet
  │
  ├─ HTTPS 443 ──► Nginx (container) ──► hub.jobdistributor.net ──► jd-hub :5000
  │                                   └─ *.jobdistributor.net   ──► frps vhost :8080
  │                                                                       │
  └─ TCP  7000 ───────────────────────────────────────────────► frps bind port
                                                        (jd_server containers connect here)
```

---

## Prerequisites

- Ubuntu 22.04 VPS with a public IP
- Ports **80**, **443**, and **7000** open in the firewall
- Docker + Docker Compose installed
- Domain with two DNS A-records pointing to the VPS:
  - `hub.yourdomain.com → <VPS IP>`
  - `*.yourdomain.com  → <VPS IP>` (wildcard)

---

## 1. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # log out and back in after this
```

---

## 2. Obtain a wildcard TLS certificate

Wildcard certs require the **DNS-01** challenge. The example uses Cloudflare;
replace the plugin for your DNS provider.

```bash
# Install certbot + Cloudflare plugin
sudo apt update && sudo apt install -y certbot
sudo pip install certbot-dns-cloudflare

# Create an API token credentials file (chmod 600!)
sudo mkdir -p /etc/letsencrypt
sudo tee /etc/letsencrypt/cloudflare.ini > /dev/null <<'EOF'
dns_cloudflare_api_token = YOUR_CLOUDFLARE_API_TOKEN
EOF
sudo chmod 600 /etc/letsencrypt/cloudflare.ini

# Issue the certificate (covers hub. and *.yourdomain.com)
sudo certbot certonly \
  --dns-cloudflare \
  --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
  -d "yourdomain.com" \
  -d "*.yourdomain.com" \
  --agree-tos --email you@example.com
```

> **Other providers:** replace `certbot-dns-cloudflare` with the matching plugin,
> e.g. `certbot-dns-route53`, `certbot-dns-digitalocean`.
> See <https://eff-certbot.readthedocs.io/en/stable/using.html#dns-plugins>

Cert files will be at:
```
/etc/letsencrypt/live/yourdomain.com/fullchain.pem
/etc/letsencrypt/live/yourdomain.com/privkey.pem
```

Generate DH params once (takes ~1 min):
```bash
sudo openssl dhparam -out /etc/letsencrypt/ssl-dhparams.pem 2048
```

---

## 3. Set up MySQL on the host

MySQL runs on the host (not inside Docker). The hub container reaches it via
`host.docker.internal`.

```bash
sudo apt install -y mysql-server

sudo mysql -u root <<'SQL'
CREATE DATABASE jd_hub CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'hub_user'@'localhost' IDENTIFIED BY 'CHANGE_ME_DB_PASSWORD';
-- Allow connection from Docker containers (via host.docker.internal)
CREATE USER 'hub_user'@'%' IDENTIFIED BY 'CHANGE_ME_DB_PASSWORD';
GRANT ALL PRIVILEGES ON jd_hub.* TO 'hub_user'@'localhost';
GRANT ALL PRIVILEGES ON jd_hub.* TO 'hub_user'@'%';
FLUSH PRIVILEGES;
SQL
```

Make MySQL listen on all interfaces so Docker containers can reach it:

```bash
# Find the bind-address line and change it to 0.0.0.0
sudo sed -i 's/^bind-address\s*=.*/bind-address = 0.0.0.0/' \
    /etc/mysql/mysql.conf.d/mysqld.cnf
sudo systemctl restart mysql
```

---

## 4. Configure the hub environment

```bash
cd /path/to/job-distributor/deploy

cp hub.env.example hub.env
```

Edit `hub.env` and fill in every value:

| Variable | Description |
|---|---|
| `MYSQL_HOST` | `host.docker.internal` (reaches host MySQL from container) |
| `MYSQL_PASSWORD` | Password you set in step 3 |
| `FLASK_SECRET_KEY` | Long random string — `openssl rand -hex 32` |
| `FLASK_ENV` | `production` |
| `HUB_BASE_URL` | `https://hub.yourdomain.com` |
| `JD_BASE_DOMAIN` | `yourdomain.com` |
| `FRPS_TOKEN` | Shared secret for FRP auth — `openssl rand -hex 24` |
| `FRPS_API_URL` | `http://frps:7500` (Docker service name, **do not change**) |
| `JWT_SECRET_KEY` | Long random string — `openssl rand -hex 32` |
| `BREVO_API_KEY` | Brevo transactional email API key (for OTP emails) |
| `BREVO_FROM_EMAIL` | Sender address, e.g. `info@yourdomain.com` |

---

## 5. Configure FRP server token

Edit `deploy/frps/frps.toml` and replace the placeholder token with the same
value you used for `FRPS_TOKEN` in `hub.env`:

```bash
sed -i 's/CHANGE_ME_FRPS_TOKEN/your-actual-frps-token/' \
    deploy/frps/frps.toml
```

---

## 6. Update Nginx config for your domain

If your domain is not `jobdistributor.net`, edit
`deploy/nginx/hub-docker.conf` and replace every occurrence:

```bash
sed -i 's/jobdistributor\.net/yourdomain.com/g' \
    deploy/nginx/hub-docker.conf
```

Also update the cert paths in `hub-docker.conf` if your cert was issued under a
different name (e.g. `hub.yourdomain.com` vs `yourdomain.com`).

---

## 7. Start the stack

```bash
cd /path/to/job-distributor/deploy

docker compose -f hub-compose.yml pull
docker compose -f hub-compose.yml up -d
```

Check that all three containers are healthy:

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
# Hub responds
curl -I https://hub.yourdomain.com/

# frps admin API is reachable from the hub container
docker exec hub_app python3 -c \
  "import requests; print(requests.get('http://frps:7500/api/info').json())"
```

---

## Updating the Hub

When a new `jobdistributor/jd-hub:latest` image is pushed:

```bash
cd /path/to/job-distributor/deploy

docker compose -f hub-compose.yml pull hub
docker compose -f hub-compose.yml up -d --no-deps hub
```

---

## Firewall rules summary

| Port | Protocol | Source      | Purpose                          |
|------|----------|-------------|----------------------------------|
| 22   | TCP      | your IP     | SSH                              |
| 80   | TCP      | anywhere    | HTTP → HTTPS redirect            |
| 443  | TCP      | anywhere    | HTTPS (Hub + experiment tunnels) |
| 7000 | TCP      | anywhere    | frpc tunnel connections          |
| 8080 | TCP      | localhost   | frps vhost HTTP (Nginx → frps)   |
| 7500 | TCP      | localhost   | frps admin API (hub → frps)      |
| 3306 | TCP      | localhost   | MySQL                            |

Ports 8080, 7500, and 3306 must **not** be open to the internet.

---

## Directory layout used

```
deploy/
├── hub-compose.yml          ← Docker Compose stack definition
├── hub.env                  ← your secrets (gitignore this, not hub.env.example)
├── hub.env.example          ← template
├── nginx/
│   └── hub-docker.conf      ← Nginx TLS + proxy config
└── frps/
    └── frps.toml            ← FRP server config
```
