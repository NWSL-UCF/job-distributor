# Hub Server Deployment Guide

This guide sets up the full hub stack on a fresh Ubuntu 22.04 VPS:

```
Internet
  │
  ├─ HTTPS 443 ──► Nginx ──► hub.jobdistributor.net ──► Gunicorn :5000
  │                       └─ *.jobdistributor.net   ──► frps vhost :8080
  │                                                          │
  │                                                          └─► container tunnel
  └─ TCP 7000 ────────────────────────────────────────────► frps bind port
```

---

## Prerequisites

- Ubuntu 22.04 VPS with a public IP
- Domain with two DNS records pointing to that IP:
  - `hub.jobdistributor.net → <VPS IP>`
  - `*.jobdistributor.net  → <VPS IP>` (wildcard A record)
- Ports **80**, **443**, and **7000** open in your firewall / security group

---

## 1. Install system packages

```bash
sudo apt update && sudo apt install -y \
    nginx certbot python3-certbot-nginx \
    python3.11 python3.11-venv python3-pip \
    mysql-server git wget
```

---

## 2. Obtain a wildcard TLS certificate

A wildcard cert covers both `hub.jobdistributor.net` and every
`*.jobdistributor.net` subdomain with a single certificate.

Wildcard certs require the **DNS-01** challenge (not HTTP-01), so you need
certbot access to your DNS provider. The example below uses Cloudflare; swap
the plugin for your provider.

```bash
# Install the Cloudflare DNS plugin
sudo pip install certbot-dns-cloudflare

# Create an API token file (chmod 600!)
sudo mkdir -p /etc/letsencrypt
sudo tee /etc/letsencrypt/cloudflare.ini > /dev/null <<'EOF'
dns_cloudflare_api_token = YOUR_CLOUDFLARE_API_TOKEN
EOF
sudo chmod 600 /etc/letsencrypt/cloudflare.ini

# Issue the certificate (covers hub. and *.jobdistributor.net)
sudo certbot certonly \
  --dns-cloudflare \
  --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
  -d "jobdistributor.net" \
  -d "*.jobdistributor.net" \
  --agree-tos --email you@example.com
```

> **Other DNS providers**: replace `certbot-dns-cloudflare` with the matching
> plugin, e.g. `certbot-dns-route53`, `certbot-dns-digitalocean`, etc.
> See https://eff-certbot.readthedocs.io/en/stable/using.html#dns-plugins

After issuance the cert lives at:
```
/etc/letsencrypt/live/jobdistributor.net/fullchain.pem
/etc/letsencrypt/live/jobdistributor.net/privkey.pem
```

Auto-renewal is handled by the certbot systemd timer already installed.

---

## 3. Configure Nginx

```bash
# Generate Diffie-Hellman params (one-time, takes ~1 min)
sudo openssl dhparam -out /etc/letsencrypt/ssl-dhparams.pem 2048

# Ensure certbot options file exists (usually created by certbot automatically)
[ -f /etc/letsencrypt/options-ssl-nginx.conf ] || \
  sudo wget -q https://raw.githubusercontent.com/certbot/certbot/master/certbot-nginx/certbot_nginx/_internal/tls_configs/options-ssl-nginx.conf \
       -O /etc/letsencrypt/options-ssl-nginx.conf

# Install the site config
sudo cp deploy/nginx/jobdistributor.net.conf \
        /etc/nginx/sites-available/jobdistributor.net

sudo ln -sf /etc/nginx/sites-available/jobdistributor.net \
            /etc/nginx/sites-enabled/jobdistributor.net

# Remove default site if present
sudo rm -f /etc/nginx/sites-enabled/default

# Test and reload
sudo nginx -t && sudo systemctl reload nginx
```

---

## 4. Install and configure frps

```bash
# Download frp (same version as bundled in the jd-server image)
FRPC_VERSION=0.61.1
wget -q "https://github.com/fatedier/frp/releases/download/v${FRPC_VERSION}/frp_${FRPC_VERSION}_linux_amd64.tar.gz" \
     -O /tmp/frp.tar.gz
tar -xzf /tmp/frp.tar.gz -C /tmp
sudo mv /tmp/frp_${FRPC_VERSION}_linux_amd64/frps /usr/local/bin/frps
sudo chmod +x /usr/local/bin/frps
rm -rf /tmp/frp.tar.gz /tmp/frp_*

# Install config
sudo mkdir -p /etc/frp /var/log/frps
sudo cp deploy/frps/frps.toml /etc/frp/frps.toml

# Set your shared secret (must match FRPS_TOKEN in hub .env)
sudo sed -i 's/CHANGE_ME_FRPS_TOKEN/your-actual-secret-here/' /etc/frp/frps.toml

# Install and enable systemd service
sudo cp deploy/systemd/frps.service /etc/systemd/system/frps.service
sudo systemctl daemon-reload
sudo systemctl enable --now frps
sudo systemctl status frps
```

---

## 5. Set up MySQL

```bash
sudo mysql -u root <<'SQL'
CREATE DATABASE jd_hub CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'hub_user'@'localhost' IDENTIFIED BY 'CHANGE_ME_DB_PASSWORD';
GRANT ALL PRIVILEGES ON jd_hub.* TO 'hub_user'@'localhost';
FLUSH PRIVILEGES;
SQL
```

---

## 6. Deploy the Hub app

```bash
# Create a dedicated system user
sudo useradd -r -m -d /opt/hub -s /bin/bash hub

# Clone (or copy) the repo
sudo -u hub git clone https://github.com/jobdistributor/job-distributor.git /opt/hub
cd /opt/hub

# Create virtualenv and install deps
sudo -u hub python3.11 -m venv /opt/hub/venv
sudo -u hub /opt/hub/venv/bin/pip install -r hub/requirements.txt

# Configure environment
sudo -u hub cp hub/.env.example hub/.env
sudo chmod 600 /opt/hub/hub/.env
# ↓ Edit the .env now — fill in every value
sudo -u hub nano /opt/hub/hub/.env

# Prepare log directory
sudo mkdir -p /var/log/hub
sudo chown hub:hub /var/log/hub

# Install and start the systemd service
sudo cp deploy/systemd/hub.service /etc/systemd/system/hub.service
sudo systemctl daemon-reload
sudo systemctl enable --now hub
sudo systemctl status hub
```

---

## 7. Wire up static files

The Nginx config serves `/static/` directly from disk. Create a symlink so
Nginx can find the files without going through Gunicorn:

```bash
sudo ln -sf /opt/hub/hub/static /opt/hub/hub/static
# Nginx user needs read access
sudo chmod o+rx /opt/hub /opt/hub/hub /opt/hub/hub/static
```

---

## 8. Verify everything is running

```bash
# All four services should be active
sudo systemctl status nginx frps hub mysql

# Hub responds
curl -I https://hub.jobdistributor.net/

# frps admin API is reachable locally
curl http://127.0.0.1:7500/api/info
```

---

## 9. Promote the first admin user

Sign up at `https://hub.jobdistributor.net/signup`, then:

```bash
sudo mysql -u hub_user -p jd_hub \
  -e "UPDATE users SET is_admin = 1 WHERE email = 'your@email.com';"
```

---

## Firewall rules summary

| Port | Protocol | Source      | Purpose                         |
|------|----------|-------------|---------------------------------|
| 22   | TCP      | your IP     | SSH                             |
| 80   | TCP      | anywhere    | HTTP (redirects to HTTPS)       |
| 443  | TCP      | anywhere    | HTTPS (hub + experiment tunnels)|
| 7000 | TCP      | anywhere    | frpc tunnel connections         |
| 8080 | TCP      | localhost   | frps vhost HTTP (Nginx → frps)  |
| 5000 | TCP      | localhost   | Gunicorn hub app (Nginx → app)  |
| 7500 | TCP      | localhost   | frps admin API (hub → frps)     |
| 3306 | TCP      | localhost   | MySQL                           |

Ports 8080, 5000, 7500, and 3306 must **not** be open to the internet.

---

## Updating the Hub

```bash
cd /opt/hub
sudo -u hub git pull
sudo -u hub /opt/hub/venv/bin/pip install -r hub/requirements.txt
sudo systemctl restart hub
```
