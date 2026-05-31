# How to Deploy the JD Hub on a VPS — A to Z

The Hub is the central control plane: user accounts, experiment registration,
FRP tunnel management, quota tracking, and worker authentication. It runs as a
Docker Compose stack of three containers (**Nginx**, **jd-hub**, **frps**) with
**MySQL** on the host.

---

## Architecture

```
Internet
  │
  ├─ HTTPS 443 ──► Nginx ──► hub.yourdomain.com   ──► jd-hub :5000
  │                       └─ *.yourdomain.com    ──► frps vhost :8080
  │                                                       │
  └─ TCP  7000 ───────────────────────────────► frps bind port
                                         (jd_server containers connect here)
```

---

## Step 0 — VPS Requirements

| Item | Minimum | Recommended |
|---|---|---|
| OS | Ubuntu 22.04 LTS | Ubuntu 22.04 LTS |
| RAM | 1 GB | 2 GB |
| vCPU | 1 | 2 |
| Disk | 20 GB | 40 GB |
| Public IP | Required | Required |
| Open ports | 22, 80, 443, 7000 | same |

Any cloud provider works: DigitalOcean, Linode, Hetzner, AWS EC2, etc.

---

## Step 1 — Initial Server Setup

### 1a. Log in and create a non-root user

```bash
# Log in as root
ssh root@<VPS_IP>

# Create a deploy user
adduser deploy
usermod -aG sudo deploy

# Switch to the deploy user for everything from here on
su - deploy
```

### 1b. Harden SSH (disable password login)

On your **local machine**, copy your SSH key to the VPS:

```bash
ssh-copy-id deploy@<VPS_IP>
```

Then on the VPS, disable password authentication:

```bash
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' \
    /etc/ssh/sshd_config
sudo systemctl restart ssh
```

> **Test** that key-based login still works before closing your current session.

### 1c. Set up the firewall

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp      # SSH
sudo ufw allow 80/tcp      # HTTP (certbot + redirect)
sudo ufw allow 443/tcp     # HTTPS (Hub + experiment tunnels via Nginx)
sudo ufw allow 7000/tcp    # FRP tunnel connections from jd_server containers
sudo ufw enable
sudo ufw status
```

> Ports 8080 (frps vhost), 7500 (frps admin API), and 3306 (MySQL) must
> **not** be opened — they are only accessed within the server itself.

---

## Step 2 — Domain Name Setup

You need a domain with **two DNS A-records** pointing to your VPS public IP:

| Record | Type | Value |
|---|---|---|
| `hub.yourdomain.com` | A | `<VPS_IP>` |
| `*.yourdomain.com` | A | `<VPS_IP>` (wildcard) |

The wildcard record `*.yourdomain.com` is essential — every experiment gets its
own subdomain (e.g., `myexp-server.yourdomain.com`) that Nginx routes to frps.

### Where to configure DNS

- If your domain is on **Cloudflare**: Dashboard → your domain → DNS → Add record.
- If on **Namecheap / GoDaddy**: Advanced DNS section in the domain settings.
- If on **Route 53 (AWS)**: Hosted Zones → Create Record.

DNS propagation typically takes 1–5 minutes on Cloudflare and up to 24 hours on
other providers. Verify with:

```bash
dig hub.yourdomain.com +short
dig anything.yourdomain.com +short   # should return the same IP
```

Both should return your VPS IP before continuing.

---

## Step 3 — Install System Packages

```bash
sudo apt update && sudo apt upgrade -y

# Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker   # or log out and back in

# Certbot + MySQL
sudo apt install -y certbot mysql-server
```

---

## Step 4 — Obtain a Wildcard TLS Certificate

A wildcard certificate (`*.yourdomain.com`) is required so that every experiment
subdomain is served over HTTPS. Wildcard certs must use the **DNS-01 challenge**
(not the standard HTTP challenge).

### 4a. Cloudflare DNS (most common)

```bash
sudo pip install certbot-dns-cloudflare
```

Create a Cloudflare API token:
1. Log in to Cloudflare → My Profile → API Tokens → Create Token
2. Use the **"Edit zone DNS"** template, restrict to your domain
3. Copy the token

```bash
# Store the token securely
sudo mkdir -p /etc/letsencrypt
sudo tee /etc/letsencrypt/cloudflare.ini > /dev/null <<'EOF'
dns_cloudflare_api_token = YOUR_CLOUDFLARE_API_TOKEN
EOF
sudo chmod 600 /etc/letsencrypt/cloudflare.ini

# Issue the certificate
sudo certbot certonly \
  --dns-cloudflare \
  --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
  -d "yourdomain.com" \
  -d "*.yourdomain.com" \
  --agree-tos \
  --email you@example.com
```

### 4b. Other DNS providers

Replace the plugin — the command structure is the same:

| Provider | Plugin |
|---|---|
| AWS Route 53 | `certbot-dns-route53` |
| DigitalOcean | `certbot-dns-digitalocean` |
| Namecheap / others | Use `--dns-manual` and add the TXT record yourself |

See https://eff-certbot.readthedocs.io/en/stable/using.html#dns-plugins

### 4c. Generate DH parameters

```bash
sudo openssl dhparam -out /etc/letsencrypt/ssl-dhparams.pem 2048
# Takes about 1 minute
```

### 4d. Verify the certificate was issued

```bash
sudo certbot certificates
# Should show: yourdomain.com + *.yourdomain.com, Expiry: 90 days
```

Cert files will be at:
```
/etc/letsencrypt/live/yourdomain.com/fullchain.pem
/etc/letsencrypt/live/yourdomain.com/privkey.pem
```

### 4e. Set up automatic renewal

Certbot installs a systemd timer by default. Verify it:

```bash
sudo systemctl status certbot.timer
# Should show: active (waiting)
```

Test that renewal works:

```bash
sudo certbot renew --dry-run
```

If you use the Cloudflare plugin, renewal is fully automatic — no action needed
when the cert expires every 90 days.

---

## Step 5 — Set Up MySQL

MySQL runs on the **host** (not in Docker). The hub container reaches it via
`host.docker.internal`.

```bash
sudo mysql -u root <<'SQL'
CREATE DATABASE jd_hub CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Hub container user
CREATE USER 'hub_user'@'localhost' IDENTIFIED BY 'CHANGE_ME_DB_PASSWORD';
CREATE USER 'hub_user'@'%'         IDENTIFIED BY 'CHANGE_ME_DB_PASSWORD';

GRANT ALL PRIVILEGES ON jd_hub.* TO 'hub_user'@'localhost';
GRANT ALL PRIVILEGES ON jd_hub.* TO 'hub_user'@'%';

FLUSH PRIVILEGES;
SQL
```

Allow Docker containers to connect to MySQL on the host:

```bash
sudo sed -i 's/^bind-address\s*=.*/bind-address = 0.0.0.0/' \
    /etc/mysql/mysql.conf.d/mysqld.cnf
sudo systemctl restart mysql

# Verify MySQL is listening on all interfaces
sudo ss -tlnp | grep 3306
```

---

## Step 6 — Clone the Repository

```bash
git clone https://github.com/NWSL-UCF/job-distributor.git
cd job-distributor
```

---

## Step 7 — Configure the Hub Environment

```bash
cd deploy
cp hub.env.example hub.env
nano hub.env   # fill in every value
```

Generate the required secrets:

```bash
openssl rand -hex 32   # use this for FLASK_SECRET_KEY
openssl rand -hex 32   # use this for JWT_SECRET_KEY
openssl rand -hex 24   # use this for FRPS_TOKEN
```

| Variable | What to set |
|---|---|
| `MYSQL_HOST` | `host.docker.internal` |
| `MYSQL_PASSWORD` | The password from Step 5 |
| `MYSQL_USER` | `hub_user` |
| `MYSQL_DATABASE` | `jd_hub` |
| `FLASK_SECRET_KEY` | 32-byte random hex (generated above) |
| `FLASK_ENV` | `production` |
| `HUB_BASE_URL` | `https://hub.yourdomain.com` |
| `JD_BASE_DOMAIN` | `yourdomain.com` |
| `FRPS_TOKEN` | 24-byte random hex (generated above — keep it, needed in Step 8) |
| `FRPS_API_URL` | `http://frps:7500` (Docker service name — do not change) |
| `JWT_SECRET_KEY` | 32-byte random hex |
| `BREVO_API_KEY` | Brevo transactional email API key |
| `BREVO_FROM_EMAIL` | e.g. `info@yourdomain.com` |
| `BREVO_FROM_NAME` | e.g. `YourApp Team` |
| `HUB_ADMIN_EMAIL` | Admin inbox for extension-request alerts (comma-separated). Falls back to all `is_admin=1` users if unset. |

> **Never commit `hub.env` to git.** It is already in `.gitignore`.

---

## Step 8 — Configure FRP Server Token

The same token must appear in both `hub.env` (`FRPS_TOKEN`) and `frps.toml`
(`auth.token`). Set it now:

```bash
# Replace the placeholder with the token you generated in Step 7
sed -i 's/CHANGE_ME_FRPS_TOKEN/your-actual-frps-token/' \
    frps/frps.toml
```

Verify:

```bash
grep "auth.token" frps/frps.toml
```

### Transport TLS between frps and frpc

All traffic between `frps` (running on the hub VPS) and `frpc` (running inside
every `jd_server` container) is **encrypted with TLS**. This is enabled by
default — no certificates need to be provisioned manually.

How it works:
- `frps.toml` has `transport.tls.force = true` → frps rejects any frpc that
  connects without TLS.
- The frpc config generated by the Hub (fetched by `jd_server` on startup)
  has `transport.tls.enable = true` → frpc always opens a TLS connection.
- frp generates its own built-in self-signed certificate automatically. Both
  sides use it, so no manual cert management is needed.

The result: the control plane and all tunnelled data between frpc and frps are
encrypted end-to-end, in addition to the existing token-based authentication.

---

## Step 9 — Update Nginx Config for Your Domain

If your domain is not `jobdistributor.net`, replace it in the Nginx config:

```bash
sed -i 's/jobdistributor\.net/yourdomain.com/g' \
    nginx/hub-docker.conf
```

Also verify the TLS certificate paths inside `nginx/hub-docker.conf` match
where certbot stored your cert. Look for `ssl_certificate` lines and confirm
they point to `/etc/letsencrypt/live/yourdomain.com/`.

The apex domain (`yourdomain.com`) needs to be on the certificate — a wildcard
(`*.yourdomain.com`) alone does **not** cover the bare domain. Request both when
running certbot.

### Marketing landing page (`yourdomain.com`)

Static files live in `deploy/landing/` (served at `https://jobdistributor.net`).
The Hub stays at `https://hub.jobdistributor.net`; the landing page links to
**Log in** and **Create account** there.

After updating `deploy/landing/`, restart nginx only:

```bash
./run.sh restart   # or: docker compose -f hub-compose.yml restart nginx
```

For native nginx (non-Docker), copy the folder to `/var/www/jobdistributor-landing/`
as configured in `nginx/jobdistributor.net.conf`.

**DNS:** point an **A record** for `jobdistributor.net` (and optionally `www`) to the
same VPS IP as `hub.jobdistributor.net`.

**If every URL stops loading after a config update**, nginx is probably crash-looping
because the TLS cert path is wrong or missing. On the VPS:

```bash
cd /path/to/job-distributor/deploy
sudo certbot certificates
# Must list jobdistributor.net and *.jobdistributor.net
ls -l /etc/letsencrypt/live/jobdistributor.net/fullchain.pem

./run.sh nginx-test    # validates config + cert files before restart
./run.sh restart nginx
docker logs hub_nginx --tail 30   # look for "cannot load certificate"
```

All three HTTPS server blocks in `nginx/hub-docker.conf` use the **same**
`/etc/letsencrypt/live/jobdistributor.net/` cert (not a separate
`hub.jobdistributor.net` directory).

---

## Step 10 — Start the Stack

The `deploy/` directory ships with a `run.sh` helper that wraps the Docker
Compose commands for you:

```bash
cd /path/to/job-distributor/deploy
./run.sh start
```

This pulls the latest images and starts all three containers (Nginx, hub, frps)
in the background. It is equivalent to running `docker compose pull` followed
by `docker compose up -d`.

Check that all three containers started successfully:

```bash
./run.sh status
```

Expected output:

```
NAME         IMAGE                           STATUS
hub_nginx    nginx:1.27-alpine               Up
hub_app      jobdistributor/jd-hub:latest    Up
hub_frps     snowdreamtech/frps:0.61.1       Up
```

Check Hub startup logs:

```bash
./run.sh logs hub
```

The Hub auto-creates all database tables on first startup. You should see
`Migration applied` and `Background threads started` in the logs.

### run.sh command reference

| Command | What it does |
|---------|--------------|
| `./run.sh` or `./run.sh start` | Pull latest images and start the full stack |
| `./run.sh stop` | Stop and remove all hub containers |
| `./run.sh restart` | Restart only the hub app (no Nginx/frps downtime) |
| `./run.sh restart frps` | Restart a specific service |
| `./run.sh logs` | Tail logs from all containers |
| `./run.sh logs hub` | Tail logs from a specific service |
| `./run.sh status` | Show running containers |
| `./run.sh pull` | Pull latest images without restarting |

---

## Step 11 — Auto-Start on Reboot

Docker's `restart: unless-stopped` policy in `hub-compose.yml` means containers
restart automatically after a reboot **as long as the Docker daemon starts first**.
Ensure the Docker daemon is enabled:

```bash
sudo systemctl enable docker
sudo systemctl status docker
```

To verify after a reboot, run:

```bash
cd /path/to/job-distributor/deploy
./run.sh status
```

---

## Step 12 — Promote the First Admin User

Sign up at `https://hub.yourdomain.com/signup`, verify your email, then promote
your account to admin:

```bash
mysql -u hub_user -p jd_hub \
  -e "UPDATE users SET is_admin = 1 WHERE email = 'your@email.com';"
```

---

## Step 13 — Verify Everything Works

```bash
# 1. Hub responds over HTTPS
curl -I https://hub.yourdomain.com/
# Expect: HTTP/2 200

# 2. Wildcard subdomain resolves and is served by the same Nginx
curl -I https://anything.yourdomain.com/
# Expect: HTTP/2 200 (frps vhost responds)

# 3. frps admin API is reachable from inside the hub container
docker exec hub_app python3 -c \
  "import requests; print(requests.get('http://frps:7500/api/info').json())"
# Expect: {'version': '...', 'bindPort': 7000, ...}

# 4. frp tunnel port is open to the internet
nc -zv <VPS_IP> 7000
# Expect: succeeded
```

---

## Updating the Hub

When a new `jobdistributor/jd-hub:latest` image is released:

```bash
cd /path/to/job-distributor/deploy
./run.sh pull
./run.sh restart
```

This restarts only the `hub` container with zero downtime for frps and Nginx.

---

## Troubleshooting

**Hub container exits immediately:**
```bash
./run.sh logs hub
# Look for: missing env var, DB connection refused, or import error
```

**Cannot connect to MySQL from the container:**
```bash
# Confirm hub_user can log in from any host
mysql -u hub_user -p'CHANGE_ME_DB_PASSWORD' -h 127.0.0.1 jd_hub -e "SELECT 1;"
# If this fails, re-check the GRANT and bind-address in Step 5
```

**Nginx returns 502 Bad Gateway:**
```bash
./run.sh status                  # check hub and frps are Up
./run.sh logs nginx
```

**Cert not trusted (SSL error in browser):**
```bash
sudo certbot certificates    # verify expiry and domain coverage
# If expired: sudo certbot renew
```

**frpc connections rejected (tunnel auth error in jd_server logs):**
- Confirm `FRPS_TOKEN` in `hub.env` matches `auth.token` in `frps.toml`
- Confirm both sides have TLS enabled: `frps.toml` must have
  `transport.tls.force = true` and the generated frpc config must have
  `transport.tls.enable = true` — a mismatch causes frps to reject the
  connection before authentication even runs.
- Restart both containers after any config change:
  ```bash
  ./run.sh restart frps
  ./run.sh restart hub
  ```

---

## Firewall Rules Summary

| Port | Protocol | Source    | Purpose                          |
|------|----------|-----------|----------------------------------|
| 22   | TCP      | your IP   | SSH                              |
| 80   | TCP      | anywhere  | HTTP → HTTPS redirect (certbot)  |
| 443  | TCP      | anywhere  | HTTPS (Hub + experiment tunnels) |
| 7000 | TCP      | anywhere  | frpc tunnel connections          |
| 8080 | TCP      | localhost | frps vhost HTTP (Nginx → frps)   |
| 7500 | TCP      | localhost | frps admin API (hub → frps)      |
| 3306 | TCP      | localhost | MySQL                            |

Ports 8080, 7500, and 3306 must **never** be open to the internet.

---

## Directory Layout

```
deploy/
├── run.sh                   ← quick-start helper (start/stop/logs/status)
├── hub-compose.yml          ← Docker Compose stack definition
├── hub.env                  ← your secrets (gitignored — never commit)
├── hub.env.example          ← template to copy from
├── nginx/
│   └── hub-docker.conf      ← Nginx TLS + proxy config
└── frps/
    ├── frps.toml            ← FRP server config
    └── offline.html         ← custom page when experiment server is offline
```

### Custom offline page (experiment subdomains)

When a user opens `https://{exp}-dashboard.{domain}` but no container is connected,
frps used to show a generic **"The page you requested was not found"** page
(powered by frp). Instead, `deploy/frps/offline.html` is served via
`custom404Page` in `frps.toml`.

The page tells users to check the Hub Dashboard and links to
`https://hub.{your-domain}` (derived automatically from the hostname). If the
URL matches `{exp}-server` or `{exp}-dashboard`, it also links to that experiment
on the Hub.

After updating these files, restart frps:

```bash
./run.sh restart frps
```

For bare-metal frps (systemd), copy both files to `/etc/frp/` and set
`custom404Page = "/etc/frp/offline.html"` in `/etc/frp/frps.toml`.
