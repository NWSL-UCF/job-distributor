# JD Hub — Design Document

> **Status: design / pre-implementation**
> This document specifies how every key feature of the JD Hub is handled.
> Nothing is implemented yet; this is the blueprint for the build phase.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Database Schema Summary](#2-database-schema-summary)
3. [User Authentication](#3-user-authentication)
4. [API Key Management](#4-api-key-management)
5. [Experiment Registration and Lifecycle](#5-experiment-registration-and-lifecycle)
6. [FRP Tunnel Automation](#6-frp-tunnel-automation)
7. [Worker Authentication Flow](#7-worker-authentication-flow)
8. [Traffic Monitoring and Quota Enforcement](#8-traffic-monitoring-and-quota-enforcement)
9. [Email Notifications (Brevo)](#9-email-notifications-brevo)
10. [Idle Detection and Experiment Expiry](#10-idle-detection-and-experiment-expiry)
11. [Data Limit Extension Requests](#11-data-limit-extension-requests)
12. [Remote Dashboard PIN Update](#12-remote-dashboard-pin-update)
13. [Admin Portal](#13-admin-portal)
14. [Background Jobs](#14-background-jobs)
15. [Security Model](#15-security-model)
16. [Deployment Topology](#16-deployment-topology)

---

## 1. Architecture Overview

```
User browser
    │  HTTPS
    ▼
┌─────────────────────────────────────────────────────────────-┐
│                  hub.jobdistributor.net                      │
│  ┌─────────────┐    ┌───────────────┐    ┌────────────────┐  │
│  │  Hub Flask  │    │  frps server  │    │  nginx reverse │  │
│  │  + Gunicorn │    │  (FRP server) │    │  proxy         │  │
│  └──────┬──────┘    └───────┬───────┘    └───────┬────────┘  │
│         │ TCP               │ TCP                │ HTTPS     │
│         ▼                   ▼                    ▼           │
│       MySQL          FRP proxy tunnels   *.jobdistributor.net│
└─────────────────────────────────────────────────────────────-┘
                              │ frpc tunnel
              ┌───────────────┴────────────────---┐
              │        User's machine             │
              │  Docker container                 │
              │  ├── server.py (Gunicorn :8000)   │
              │  └── dashboard.py (Gunicorn :8001)|
              │  frpc sidecar                     │
              └────────────────────────────────---┘

Worker machines
    │  HTTP → server.<expId>.jobdistributor.net
    ▼
  jd_worker CLI (uses Hub API key to authenticate,
                 receives per-job worker token from Hub)
```

**Key principle:** The Hub is a *relay and control plane only*. All job data,
job parameters, and checkpoint files stay on the user's server; the Hub never
touches them. The Hub manages identity, tunnels, traffic accounting, and email.

---

## 2. Database Schema Summary

Full DDL is in `hub/schema.sql`. Tables at a glance:


| Table                      | Purpose                                                    |
| -------------------------- | ---------------------------------------------------------- |
| `users`                    | Accounts, credentials, email-verification, API key hash    |
| `hub_sessions`             | Browser sessions for Hub web app                           |
| `experiments`              | One row per registered experiment, FRP subdomains, secrets |
| `traffic_snapshots`        | Raw cumulative byte counters polled from frps every 60 s   |
| `monthly_usage`            | Aggregated per-user monthly totals + warning flags         |
| `default_limits`           | Singleton row: system-wide default monthly byte limits     |
| `user_limit_overrides`     | Per-user overrides with optional expiry                    |
| `limit_extension_requests` | User-submitted requests for more quota                     |
| `worker_tokens`            | Issued worker JWTs (for revocation support)                |
| `email_notifications`      | Deduplication log for sent emails                          |


---

## 3. User Authentication

### Signup

1. User submits email + password (min 10 chars).
2. Hub checks email uniqueness → 409 if duplicate.
3. Hub stores `bcrypt(password)` in `users.password_hash`.
4. Hub generates a 64-char URL-safe token, stores its SHA-256 hash in
  `users.verification_token` with a 24-hour expiry.
5. Hub sends a verification email via Brevo with a link:
  `https://hub.jobdistributor.net/verify?token=<raw_token>`
6. Account is created with `is_verified=0`. Unverified accounts cannot log in.

### Email Verification

`GET /verify?token=<token>`

1. Hub finds the user by looking up `SHA-256(token)` in `verification_token`.
2. Checks expiry; if expired → prompt to resend.
3. Sets `is_verified=1`, clears token fields.
4. Redirects to login.

### Login

`POST /login`

1. Look up user by email → 401 if not found.
2. Verify `bcrypt.checkpw(submitted, password_hash)` → 401 on failure.
3. Check `is_verified=1` → 403 with "please verify email" otherwise.
4. Check `is_active=1` → 403 if suspended.
5. Create a `hub_sessions` row:
  - `id` = 32-byte URL-safe random token
  - `expires_at` = now + `HUB_SESSION_TTL_DAYS` (default 30 days)
6. Set `Set-Cookie: hub_session=<id>; HttpOnly; Secure; SameSite=Lax`

### Session Validation (every protected route)

Decorator `@require_hub_login`:

1. Read `hub_session` cookie.
2. Query `hub_sessions` by id where `expires_at > NOW()`.
3. If missing or expired → redirect to `/login` (web) or 401 (API).
4. Slide expiry: update `expires_at = NOW() + TTL` on each request.

### Password Reset

1. `POST /forgot-password` → look up email, generate reset token (same pattern as
  verification), send email with link valid 1 hour.
2. `POST /reset-password` → validate token, update hash, clear token, invalidate
  all existing sessions for that user.

---

## 4. API Key Management

API keys authenticate workers (and potentially external scripts) against Hub
endpoints. They are **separate from the web login session**.

### Key Generation

`POST /api/keys/regenerate` (requires active web session)

1. Generate a 40-char URL-safe random string: `jd_<38 random chars>`.
2. Store `SHA-256(key)` in `users.api_key_hash`.
3. Store first 8 chars in `users.api_key_prefix` (shown in UI for identification).
4. Return the full key **once** — never stored in plaintext.

### Key Validation (Hub API routes)

Middleware `@require_api_key`:

1. Read `Authorization: Bearer <key>` header.
2. Hash the submitted key with SHA-256.
3. Look up `users.api_key_hash` — if not found → 401.
4. Check `is_active=1` → 403 if suspended.
5. Inject `g.api_user` into Flask's request context.

---

## 5. Experiment Registration and Lifecycle

### Creating an Experiment

`POST /api/experiments` (API key auth)

Request body:

```json
{
  "name": "mnist-tune"
}
```

Rules:

- `name` must match `/^[a-z][a-z0-9-]{1,46}$/`.
- Globally unique across all users (subdomains must be unique).
- One user is allowed up to 5 active experiments (configurable in Hub settings).

Response:

```json
{
  "experiment_id": 42,
  "name": "mnist-tune",
  "server_url":    "https://server.mnist-tune.jobdistributor.net",
  "dashboard_url": "https://dashboard.mnist-tune.jobdistributor.net",
  "frpc_config":   "... full frpc.ini content ...",
  "worker_shared_secret": "..."
}
```

The `frpc_config` block is ready to embed in the Docker Compose sidecar.

### Experiment Activation (Docker container registers itself)

`POST /api/experiments/<name>/register` (API key auth)

Called automatically by `start.py` the first time it boots inside Docker.
It sends:

```json
{
  "admin_token": "<the admin_token from start.py>",
  "worker_shared_secret": "<matches what Hub issued>"
}
```

Hub stores both; sets `status=ACTIVE`, `last_activity_at=NOW()`.

### Status Lifecycle

```
ACTIVE ──(7 days no activity)──► IDLE ──(7 more days)──► EXPIRED
  ▲                                │
  └─── extend requested & approved ┘
ACTIVE ──(user deletes)──► DELETED
ACTIVE ──(quota exceeded)──► QUOTA_EXCEEDED ──(new month / extension)──► ACTIVE
```

---

## 6. FRP Tunnel Automation

### What is FRP?

FRP (Fast Reverse Proxy) lets a machine behind NAT expose services publicly.
The Hub runs `frps` (server). The user's Docker container runs `frpc` (client).

### Subdomain Assignment

When an experiment is created, Hub assigns:

- `server.<expId>.jobdistributor.net`   → proxied to `localhost:8000` inside Docker
- `dashboard.<expId>.jobdistributor.net` → proxied to `localhost:8001` inside Docker

nginx on the Hub uses a wildcard TLS cert (`*.jobdistributor.net`) and routes by
`$host` to the correct frps virtual host or directly to the Hub app.

### Generated `frpc.ini`

Hub generates and returns this content for the user to embed as a Docker sidecar:

```ini
[common]
server_addr = hub.jobdistributor.net
server_port = 7000
token       = <FRPS_TOKEN from Hub .env>

[server-<expId>]
type            = http
local_port      = 8000
custom_domains  = server.<expId>.jobdistributor.net

[dashboard-<expId>]
type            = http
local_port      = 8001
custom_domains  = dashboard.<expId>.jobdistributor.net
```

### Port Management

frps multiplexes all experiments over a **single port 7000**. No per-experiment
ports are allocated. nginx routes by `Host:` header.

### Tunnel Teardown

When an experiment expires (status=EXPIRED), Hub calls the frps admin API to
kick any active frpc connection for that experiment's subdomains. New connections
are rejected because frps validates the subdomain against an allow-list that Hub
manages via the frps admin API.

---

## 7. Worker Authentication Flow

Workers need to authenticate against the **user's local server** (via the FRP
tunnel), not the Hub directly. But the Hub acts as the trust broker.

### Step 1 — Worker gets a token from the Hub

`POST /api/worker/token` (API key auth)

Request:

```json
{ "experiment_name": "mnist-tune" }
```

Hub checks:

- API key belongs to the experiment owner.
- Experiment is ACTIVE and not QUOTA_EXCEEDED.

Hub generates a JWT:

```json
{
  "sub": "worker",
  "exp_id": 42,
  "exp_name": "mnist-tune",
  "user_id": 7,
  "jti": "<uuid4>",
  "iat": <now>,
  "exp": <now + JWT_WORKER_TOKEN_TTL_HOURS>
}
```

Signed with `JWT_SECRET_KEY` (HS256). The `jti` is stored in `worker_tokens`.

Response:

```json
{
  "worker_token": "eyJ...",
  "server_url": "https://server.mnist-tune.jobdistributor.net",
  "expires_at": "2025-01-02T12:00:00Z"
}
```

### Step 2 — Worker connects to local server

`jd_worker` sends every request to the server with:

```
Authorization: Bearer <worker_token>
```

The local `server.py` verifies the JWT using the `worker_shared_secret` that was
returned at experiment creation and stored in the Docker environment.

The server **does not call back to the Hub** per request — it only verifies the
JWT signature locally. This keeps latency low and the system functional if the
Hub is temporarily unreachable.

### Token Revocation

If a user revokes a token (via Hub UI) or an experiment goes QUOTA_EXCEEDED, Hub
sets `worker_tokens.revoked=1`. The server periodically (every 5 minutes) calls:

`GET /api/experiments/<name>/revoked-tokens` (server-to-Hub internal call)

and caches the `jti` set to reject mid-session.

---

## 8. Traffic Monitoring and Quota Enforcement

### Data Collection

The Hub runs a background thread (`traffic_poller`) every 60 seconds that:

1. Calls frps admin API: `GET http://localhost:7500/api/proxies/http`
2. Parses `today_traffic_in` and `today_traffic_out` per proxy.
3. For each proxy, matches the subdomain to an experiment.
4. Writes a `traffic_snapshots` row with cumulative counts.

> frps reports cumulative bytes since its last restart. The poller detects
> resets (new value < previous value) and treats the difference as 0 for that
> interval.

### Monthly Aggregation

A second background thread (`usage_aggregator`) runs every 5 minutes:

1. For each active experiment, sums `bytes_in` and `bytes_out` from
  `traffic_snapshots` for the current calendar month.
2. Groups by `user_id` (user may have multiple experiments).
3. UPSERTs into `monthly_usage`.

### Quota Check

At the end of each `usage_aggregator` run:

1. Look up the effective limit for each user:
  - If `user_limit_overrides` has a valid (non-expired) row → use it.
  - Otherwise use `default_limits`.
2. If `total_bytes_in >= limit_in`:
  - Set all user's experiments to `QUOTA_EXCEEDED`.
  - Send "quota exhausted" email (if `warned_100_in=0`).
3. If `total_bytes_out >= limit_out`: same for out.
4. At 80% and 95%: send warning emails.

### Quota Reset

On the first day of each month, `usage_aggregator` detects a new (year, month)
combination, inserts fresh `monthly_usage` rows with zeros, and re-activates
any `QUOTA_EXCEEDED` experiments.

---

## 9. Email Notifications (Brevo)

### Library

Hub uses the [Brevo Python SDK](https://github.com/getbrevo/brevo-python) or
direct REST calls to `https://api.brevo.com/v3/smtp/email`.

### Authentication

The `BREVO_API_KEY` from `.env` is passed as `api-key` header.

### Deduplication

Before sending **any** email, Hub inserts into `email_notifications`:

```sql
INSERT IGNORE INTO email_notifications (user_id, notification_type) VALUES (?, ?)
```

If the insert affects 0 rows the email was already sent; skip.

### Email Types and Triggers


| `notification_type` pattern       | Trigger                     | Subject                                |
| --------------------------------- | --------------------------- | -------------------------------------- |
| `verify_<user_id>`                | Signup                      | Verify your JobDistributor account     |
| `password_reset_<user_id>_<date>` | Password reset request      | Reset your password                    |
| `quota_80_in_<year>_<month>`      | Usage hits 80% of in-limit  | Data limit warning (80%)               |
| `quota_80_out_<year>_<month>`     | Usage hits 80% of out-limit | Data limit warning (80%)               |
| `quota_95_in_<year>_<month>`      | Usage hits 95% of in-limit  | Data limit warning (95%)               |
| `quota_95_out_<year>_<month>`     | Usage hits 95% of out-limit | Data limit warning (95%)               |
| `quota_100_in_<year>_<month>`     | In-limit exhausted          | Data limit reached — uploads blocked   |
| `quota_100_out_<year>_<month>`    | Out-limit exhausted         | Data limit reached — downloads blocked |
| `idle_warn_<exp_name>`            | Experiment idle 5 days      | Your experiment will expire in 2 days  |
| `expired_<exp_name>`              | Experiment expired          | Experiment tunnel closed               |
| `ext_approved_<request_id>`       | Admin approves extension    | Your data limit extension was approved |
| `ext_declined_<request_id>`       | Admin declines extension    | Your data limit extension request      |


---

## 10. Idle Detection and Experiment Expiry

### Idle Detection

Background job `idle_checker` runs every 10 minutes:

1. Find `ACTIVE` experiments where `last_activity_at < NOW() - 5 days`.
2. For those where `idle_warned_at IS NULL`:
  - Send idle warning email (type: `idle_warn_<exp_name>`).
  - Set `idle_warned_at = NOW()`.
  - Set `expires_at = NOW() + 2 days`.
3. For those where `expires_at < NOW()`:
  - Set `status = EXPIRED`.
  - Call frps admin API to disconnect tunnel.
  - Send "expired" email.

### Extension Button

User can click "Extend 2 weeks" in the Hub UI for any experiment in IDLE status:

`POST /api/experiments/<name>/extend`

1. Hub checks the experiment is IDLE (not EXPIRED).
2. Sets `expires_at = NOW() + 14 days`, `idle_warned_at = NULL`.
3. Sets `status = ACTIVE`.

### Activity Heartbeat

The local `server.py` sends a heartbeat to the Hub every 5 minutes:

`POST /api/experiments/<name>/heartbeat` (Bearer worker_token)

Hub updates `experiments.last_activity_at = NOW()` and clears IDLE state if needed.

---

## 11. Data Limit Extension Requests

### Submitting a Request

`POST /api/limit-extensions` (API key auth)

```json
{
  "description": "Training ResNet-50 for CVPR deadline",
  "affiliation": "University of Central Florida"
}
```

Hub automatically calculates `valid_until` as the last day of the current month
(so the extension is for the current billing period) and saves with
`status=PENDING`.

### Admin Review

Admin sees all pending requests in the admin portal.

`PATCH /admin/limit-extensions/<id>` (admin web session)

```json
{
  "action": "approve",
  "additional_bytes_in":  53687091200,
  "additional_bytes_out": 53687091200,
  "admin_note": "Approved for CVPR submission"
}
```

On approval:

1. Upsert `user_limit_overrides` — add the extra bytes on top of any existing limit.
2. If experiment was QUOTA_EXCEEDED → set back to ACTIVE.
3. Send approval email.

On decline:

1. Set `status=DECLINED`, save `admin_note`.
2. Send decline email.

---

## 12. Remote Dashboard PIN Update

The Hub can update the dashboard PIN on behalf of a user **without** them
entering the current PIN. This is only possible via the admin token.

### Flow

1. User requests PIN reset from Hub UI:
  `POST /api/experiments/<name>/reset-pin` (API key auth + must be the owner)
2. Hub looks up `experiments.admin_token` for the experiment.
3. Hub calls the local dashboard's admin override endpoint:
  ```
   POST https://dashboard.<expId>.jobdistributor.net/admin/override_pin
   Authorization: Bearer <admin_token>
   Content-Type: application/json

   { "new_pin": "<6-digit pin supplied by user>" }
  ```
4. Dashboard verifies the `admin_token` and updates the stored PIN hash.
5. Hub returns success or propagates any error to the user.

> The `admin_token` is set once at experiment registration and never changes.
> It is stored encrypted (AES-256) in `experiments.admin_token` in MySQL,
> using a key derived from `FLASK_SECRET_KEY`.

---

## 13. Admin Portal

Accessible at `/admin/*`. All routes protected by `@require_admin` decorator,
which checks `g.current_user.is_admin=1` from the web session.

### Pages / Endpoints


| Route                                   | Purpose                                                       |
| --------------------------------------- | ------------------------------------------------------------- |
| `GET /admin/users`                      | List all users, usage bars, active/suspended status           |
| `GET /admin/users/<id>`                 | User detail: experiments, monthly usage chart, limit override |
| `POST /admin/users/<id>/suspend`        | Set `is_active=0`, kills all their sessions                   |
| `POST /admin/users/<id>/activate`       | Set `is_active=1`                                             |
| `POST /admin/users/<id>/limit`          | Upsert `user_limit_overrides`                                 |
| `GET /admin/experiments`                | All experiments, status, tunnel health                        |
| `POST /admin/experiments/<name>/expire` | Force-expire an experiment                                    |
| `GET /admin/limit-extensions`           | Pending extension requests                                    |
| `PATCH /admin/limit-extensions/<id>`    | Approve or decline                                            |
| `GET /admin/default-limits`             | Show and edit system-wide defaults                            |
| `POST /admin/default-limits`            | Update `default_limits` singleton                             |


---

## 14. Background Jobs

All background jobs run as threads inside the Gunicorn worker using
`threading.Timer` / `threading.Event` loops. For production, each can be
extracted to a separate Celery beat task.


| Thread             | Interval | Function                                                           |
| ------------------ | -------- | ------------------------------------------------------------------ |
| `traffic_poller`   | 60 s     | Poll frps admin API → insert `traffic_snapshots`                   |
| `usage_aggregator` | 5 min    | Aggregate snapshots → update `monthly_usage`, check quotas         |
| `idle_checker`     | 10 min   | Detect idle experiments, send warnings, expire tunnels             |
| `token_pruner`     | 1 hour   | Delete expired/revoked `worker_tokens` older than 24 h             |
| `snapshot_pruner`  | 24 h     | Delete `traffic_snapshots` older than 90 days (keep monthly_usage) |


---

## 15. Security Model

### Threat model summary


| Threat                             | Mitigation                                                                               |
| ---------------------------------- | ---------------------------------------------------------------------------------------- |
| Stolen API key                     | Key stored as SHA-256 hash; regeneration invalidates old key immediately                 |
| Session hijacking                  | `HttpOnly; Secure; SameSite=Lax` cookie; session sliding expiry                          |
| Brute-force login                  | Max 5 attempts per IP per 15 minutes (Redis or in-memory rate limiter)                   |
| Worker JWT tampering               | HS256 signed with `JWT_SECRET_KEY`; verified locally by server.py                        |
| Worker JWT replay after revocation | Server polls Hub every 5 min for revoked JTIs                                            |
| Unauthorized PIN reset             | Only the registered `admin_token` (stored per-experiment) can call `/admin/override_pin` |
| Third-party workers                | Worker must first authenticate via Hub with a valid API key to receive a JWT             |
| CSRF on Hub web forms              | Flask-WTF CSRF tokens on all state-changing forms                                        |
| SQL injection                      | SQLAlchemy parameterized queries throughout                                              |
| Sensitive env vars                 | `hub/.env` is `.gitignore`d; BREVO key and DB password never in source                   |


---

## 16. Deployment Topology

### Hub server (single VPS)

```
hub.jobdistributor.net  (static IP)
├── nginx
│   ├── hub.jobdistributor.net → localhost:5000 (Gunicorn)
│   └── *.jobdistributor.net  → frps HTTP vhost proxy (port 8080)
├── frps (FRP server)
│   ├── bind_port   7000   (frpc clients connect here)
│   ├── vhost_http_port 8080 (nginx proxies here)
│   └── dashboard_port 7500 (localhost only — Hub reads traffic stats)
├── Gunicorn (Hub Flask app, 4 workers, unix socket)
└── MySQL 8.x
```

### User's machine (inside Docker)

```
docker-compose.yml
services:
  jd_server:
    image: python:3.11-slim
    command: python start.py ...
    ports: []   # NOT exposed publicly — frpc handles that

  frpc:
    image: snowdreamtech/frpc
    volumes:
      - ./frpc.ini:/etc/frp/frpc.ini
    depends_on: [jd_server]
```

### `jd_worker` (researcher's machine, outside Docker)

```bash
pip install "git+https://github.com/NWSL-UCF/job-distributor.git#subdirectory=client"

export JD_API_KEY=jd_xxxxxxxxxxxxxxxx
jd_worker expId=mnist-tune entry_script=train.py ...
```

`jd_worker` calls `POST hub.jobdistributor.net/api/worker/token` first to get
the JWT and the tunnel URL, then communicates exclusively with the local server
via `server.<expId>.jobdistributor.net`.

---

*End of design document.*