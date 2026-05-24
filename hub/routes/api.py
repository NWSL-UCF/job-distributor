"""
Hub API routes (consumed by jd_worker_cli and local server.py).

All routes require API key auth unless noted.
"""
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict

import jwt
from flask import Blueprint, g, jsonify, request

from .. import config
from ..db import db
from ..decorators import require_api_key
from ..models import Experiment, WorkerToken

api_bp = Blueprint("api", __name__)
log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z][a-z0-9\-]{1,46}$")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Experiment CRUD ───────────────────────────────────────────────────────────

@api_bp.route("/experiments", methods=["POST"])
@require_api_key
def create_experiment():
    """Create a new experiment (credentials via runtime-config endpoint)."""
    user = g.api_user
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip().lower()

    if not _NAME_RE.match(name):
        return jsonify({"error": "Invalid name. Use lowercase letters, numbers, hyphens. "
                                 "Must start with a letter, max 48 chars."}), 400

    active = user.experiments.filter(
        Experiment.status.notin_(["DELETED", "EXPIRED"])
    ).count()
    if active >= config.MAX_EXPERIMENTS_PER_USER:
        return jsonify({"error": f"Maximum {config.MAX_EXPERIMENTS_PER_USER} active "
                                  "experiments reached."}), 429

    if Experiment.query.filter_by(name=name).first():
        return jsonify({"error": "Experiment name already taken."}), 409

    worker_secret = secrets.token_hex(32)
    frpc_token    = secrets.token_hex(32)
    exp = Experiment(
        user_id               = user.id,
        name                  = name,
        status                = "ACTIVE",
        worker_shared_secret  = worker_secret,
        frpc_token            = frpc_token,
        frpc_subdomain_server = f"{name}-server.{config.JD_BASE_DOMAIN}",
        frpc_subdomain_dashboard = f"{name}-dashboard.{config.JD_BASE_DOMAIN}",
        last_activity_at      = _now(),
    )
    db.session.add(exp)
    db.session.commit()

    return jsonify({
        "experiment_id": exp.id,
        "name":          exp.name,
        "server_url":    f"https://{name}-server.{config.JD_BASE_DOMAIN}",
        "dashboard_url": f"https://{name}-dashboard.{config.JD_BASE_DOMAIN}",
        "message":       "Experiment created. Use GET /api/experiments/<name>/runtime-config "
                         "from your server container to fetch tunnel credentials.",
    }), 201


@api_bp.route("/experiments/<name>/runtime-config", methods=["GET"])
@require_api_key
def experiment_runtime_config(name: str):
    """
    Returns FRP config and worker_shared_secret for the experiment server container.
    Called by hub-bootstrap on startup — not exposed in the Hub web UI.
    """
    exp = _get_owned_exp(name)
    if exp is None:
        return jsonify({"error": "Not found"}), 404
    if exp.status in ("DELETED",):
        return jsonify({"error": f"Experiment is {exp.status}"}), 403
    if not exp.worker_shared_secret:
        return jsonify({"error": "Experiment not provisioned"}), 503

    return jsonify({
        "name":                 exp.name,
        "server_url":           f"https://{exp.name}-server.{config.JD_BASE_DOMAIN}",
        "dashboard_url":        f"https://{exp.name}-dashboard.{config.JD_BASE_DOMAIN}",
        "worker_shared_secret": exp.worker_shared_secret,
        "frpc_config":          _build_frpc_config(exp),
        "frps_server_addr":     f"hub.{config.JD_BASE_DOMAIN}",
        "frps_server_port":     7000,
    }), 200


@api_bp.route("/experiments/<name>", methods=["GET"])
@require_api_key
def get_experiment(name: str):
    exp = _get_owned_exp(name)
    if exp is None:
        return jsonify({"error": "Not found"}), 404
    return jsonify(_exp_to_dict(exp)), 200


@api_bp.route("/experiments/<name>", methods=["DELETE"])
@require_api_key
def delete_experiment(name: str):
    exp = _get_owned_exp(name)
    if exp is None:
        return jsonify({"error": "Not found"}), 404
    exp.status     = "DELETED"
    exp.deleted_at = _now()
    db.session.commit()
    return jsonify({"message": "Experiment deleted"}), 200


# ── Docker container registration ─────────────────────────────────────────────

@api_bp.route("/experiments/<name>/register", methods=["POST"])
@require_api_key
def register_experiment(name: str):
    """
    Called by start.py inside Docker on first boot.
    Stores the admin_token so Hub can call /admin/override_pin on the dashboard.
    """
    exp = _get_owned_exp(name)
    if exp is None:
        return jsonify({"error": "Not found"}), 404

    data          = request.get_json(silent=True) or {}
    admin_token   = (data.get("admin_token")        or "").strip()
    worker_secret = (data.get("worker_shared_secret") or "").strip()

    # worker_shared_secret in body is optional legacy check; API key ownership is sufficient
    if worker_secret and worker_secret != exp.worker_shared_secret:
        return jsonify({"error": "worker_shared_secret mismatch"}), 403

    if admin_token:
        exp.admin_token = admin_token
    exp.status           = "ACTIVE"
    exp.last_activity_at = _now()
    db.session.commit()
    return jsonify({"message": "Registered", "status": "ACTIVE"}), 200


# ── Heartbeat ─────────────────────────────────────────────────────────────────

@api_bp.route("/experiments/<name>/heartbeat", methods=["POST"])
@require_api_key
def heartbeat(name: str):
    exp = _get_owned_exp(name)
    if exp is None:
        return jsonify({"error": "Not found"}), 404

    # Reject deleted/expired experiments so the server shuts itself down.
    if exp.status in ("DELETED", "EXPIRED"):
        return jsonify({"status": exp.status, "error": f"Experiment is {exp.status}"}), 403

    now = _now()
    exp.last_activity_at    = now
    exp.server_last_ping_at = now
    if exp.status == "IDLE":
        exp.status         = "ACTIVE"
        exp.idle_warned_at = None
    db.session.commit()
    return jsonify({"status": exp.status}), 200


# ── Worker token ──────────────────────────────────────────────────────────────

@api_bp.route("/worker/token", methods=["POST"])
@require_api_key
def issue_worker_token():
    """
    Issue a short-lived JWT for a worker to authenticate against the local server.
    The JWT is signed with the experiment's worker_shared_secret so the local
    server can verify it without calling back to the Hub.
    """
    data    = request.get_json(silent=True) or {}
    name    = (data.get("experiment_name") or "").strip().lower()
    exp     = _get_owned_exp(name)
    if exp is None:
        return jsonify({"error": "Experiment not found or not owned by you"}), 404
    if exp.status == "QUOTA_EXCEEDED":
        return jsonify({"error": "Data quota exceeded for this month"}), 429
    if exp.status in ("EXPIRED", "DELETED"):
        return jsonify({"error": f"Experiment is {exp.status}"}), 403
    if not exp.worker_shared_secret:
        return jsonify({"error": "Experiment not fully registered yet"}), 503

    ttl     = timedelta(hours=config.JWT_WORKER_TOKEN_TTL_HOURS)
    now_utc = datetime.now(timezone.utc)
    jti     = str(uuid.uuid4())

    payload = {
        "sub":      "worker",
        "exp_id":   exp.id,
        "exp_name": exp.name,
        "user_id":  g.api_user.id,
        "jti":      jti,
        "iat":      int(now_utc.timestamp()),
        "exp":      int((now_utc + ttl).timestamp()),
    }
    token = jwt.encode(payload, exp.worker_shared_secret, algorithm="HS256")

    wt = WorkerToken(
        experiment_id = exp.id,
        jti           = jti,
        expires_at    = (now_utc + ttl).replace(tzinfo=None),
    )
    db.session.add(wt)
    exp.last_activity_at = _now()
    db.session.commit()

    return jsonify({
        "worker_token": token,
        "server_url":   f"https://{exp.name}-server.{config.JD_BASE_DOMAIN}",
        "exp_name":     exp.name,
        "expires_at":   (now_utc + ttl).isoformat(),
    }), 200


# ── Revoked JTIs (polled by local server.py) ──────────────────────────────────

@api_bp.route("/experiments/<name>/revoked-tokens", methods=["GET"])
@require_api_key
def revoked_tokens(name: str):
    exp = _get_owned_exp(name)
    if exp is None:
        return jsonify({"error": "Not found"}), 404
    jtis = [
        wt.jti for wt in
        exp.worker_tokens.filter_by(revoked=1).all()
    ]
    return jsonify({"revoked_jtis": jtis}), 200


@api_bp.route("/experiments/<name>/revoke-token", methods=["POST"])
@require_api_key
def revoke_token(name: str):
    exp = _get_owned_exp(name)
    if exp is None:
        return jsonify({"error": "Not found"}), 404
    jti = (request.get_json(silent=True) or {}).get("jti", "")
    wt  = WorkerToken.query.filter_by(experiment_id=exp.id, jti=jti).first()
    if not wt:
        return jsonify({"error": "Token not found"}), 404
    wt.revoked = 1
    db.session.commit()
    return jsonify({"message": "Token revoked"}), 200


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_owned_exp(name: str) -> Experiment | None:
    exp = Experiment.query.filter_by(name=name).first()
    if not exp or exp.user_id != g.api_user.id:
        return None
    return exp


def _exp_to_dict(exp: Experiment) -> dict:
    return {
        "id":           exp.id,
        "name":         exp.name,
        "status":       exp.status,
        "server_url":   f"https://{exp.name}-server.{config.JD_BASE_DOMAIN}",
        "dashboard_url":f"https://{exp.name}-dashboard.{config.JD_BASE_DOMAIN}",
        "created_at":   exp.created_at.isoformat() if exp.created_at else None,
        "last_activity":exp.last_activity_at.isoformat() if exp.last_activity_at else None,
    }


def _exp_frpc_domains(exp: Experiment) -> tuple[str, str]:
    """Return (server_domain, dashboard_domain), deriving defaults when unset."""
    server = (exp.frpc_subdomain_server or "").strip()
    dash   = (exp.frpc_subdomain_dashboard or "").strip()
    if not server:
        server = f"{exp.name}-server.{config.JD_BASE_DOMAIN}"
    if not dash:
        dash = f"{exp.name}-dashboard.{config.JD_BASE_DOMAIN}"
    return server, dash


def _build_frpc_config(exp: Experiment) -> str:
    """
    Generate a frpc TOML config for the given experiment (frp 0.61+).

    Authentication uses a per-experiment token (metadatas.exp_token) sent to
    frps via frp's metadata field.  The Hub validates this token in the Login
    plugin hook — no global shared FRPS token is used or embedded in the config.
    """
    server_domain, dash_domain = _exp_frpc_domains(exp)
    return (
        f"# frpc config for experiment: {exp.name}\n"
        f"# Auto-generated by JobDistributor Hub — do not edit manually.\n\n"
        f"serverAddr = \"hub.{config.JD_BASE_DOMAIN}\"\n"
        f"serverPort = 7000\n\n"
        f"[transport.tls]\n"
        f"enable = true\n\n"
        f"metadatas.exp_token = \"{exp.frpc_token}\"\n\n"
        f"[[proxies]]\n"
        f"name = \"server-{exp.name}\"\n"
        f"type = \"http\"\n"
        f"localPort = 8000\n"
        f"customDomains = [\"{server_domain}\"]\n\n"
        f"[[proxies]]\n"
        f"name = \"dashboard-{exp.name}\"\n"
        f"type = \"http\"\n"
        f"localPort = 8001\n"
        f"customDomains = [\"{dash_domain}\"]\n"
    )


# ── frp Server Plugin — authentication + proxy authorisation ────────────────
#
# frps calls this endpoint for Login, NewProxy, and CloseProxy.
#
#   Login     — validate per-experiment frpc_token (meta_exp_token in frpc config)
#   NewProxy  — allow only HTTP proxies for the token owner's two assigned domains
#   CloseProxy — notify experiment owner when the server tunnel closes
#
# NewProxy checks (see frp_plugin_hook): proxy type, domains, token binding,
# proxy name, heartbeat. Each check is commented in code for manual review.
#
# Security: port 5000 is never exposed outside the Docker network.  We also
# verify the caller's IP is RFC-1918 for defence in depth.
#
# frp plugin response contract:
#   {"reject": false, "unchange": true}       → allow
#   {"reject": true,  "reject_reason": "..."} → deny

# Per-experiment cooldown to avoid spamming users when frpc reconnects rapidly.
# Key: "{event}:{exp_name}"  Value: last notification datetime
_notif_cooldown: Dict[str, datetime] = {}
_NOTIF_COOLDOWN_SECS = 300   # 5 minutes

# Recent Login successes — NewProxy may arrive before the first heartbeat lands.
_login_grace: Dict[str, datetime] = {}
_LOGIN_GRACE_SECS = 120


def _parse_custom_domains(content: dict) -> list[str]:
    """Normalize custom_domains from frp plugin payload (string or list)."""
    raw = content.get("custom_domains")
    if isinstance(raw, str):
        return [d.strip() for d in raw.split(",") if d.strip()]
    if isinstance(raw, list):
        return [str(d).strip() for d in raw if str(d).strip()]
    return []


def _extract_exp_token(content: dict) -> str:
    """Read per-experiment frpc token from a frp plugin Login/NewProxy payload."""
    user_metas = (content.get("user") or {}).get("metas") or {}
    for source in (user_metas, content.get("metas") or {}, content.get("metadatas") or {}):
        token = (source.get("exp_token") or source.get("meta_exp_token") or "").strip()
        if token:
            return token
    return ""


def _is_private_ip(ip: str) -> bool:
    if ip in ("127.0.0.1", "::1"):
        return True
    if ip.startswith("10."):
        return True
    if ip.startswith("192.168."):
        return True
    if ip.startswith("172."):
        try:
            second = int(ip.split(".")[1])
            return 16 <= second <= 31
        except (IndexError, ValueError):
            return False
    return False


def _frp_reject(reason: str):
    """Return a frp plugin rejection response (HTTP 200 with reject=true)."""
    log.warning("frp plugin reject: %s", reason)
    return jsonify({"reject": True, "reject_reason": reason}), 200


def _frp_allow():
    return jsonify({"reject": False, "unchange": True}), 200


def _can_notify(event: str, exp_name: str) -> bool:
    key = f"{event}:{exp_name}"
    last = _notif_cooldown.get(key)
    if last and (_now() - last).total_seconds() < _NOTIF_COOLDOWN_SECS:
        return False
    _notif_cooldown[key] = _now()
    return True


def _exp_from_proxy_name(proxy_name: str) -> "Experiment | None":
    """Derive experiment name from frpc proxy name (server-<name> or dashboard-<name>)."""
    for prefix in ("server-", "dashboard-"):
        if proxy_name.startswith(prefix):
            return Experiment.query.filter_by(name=proxy_name[len(prefix):]).first()
    return None


@api_bp.route("/internal/frp/new-proxy", methods=["POST"])
def frp_plugin_hook():
    from ..email_service import send_server_connected, send_server_disconnected

    remote_ip = request.remote_addr or ""
    if not _is_private_ip(remote_ip):
        return _frp_reject(f"Forbidden (caller {remote_ip})")

    data    = request.get_json(silent=True) or {}
    op      = data.get("op", "NewProxy")
    content = data.get("content", {})

    # ── Login ─────────────────────────────────────────────────────────────────
    # frps calls this when a frpc client connects, before accepting it.
    # We validate the per-experiment token sent in frpc's metadatas field.
    if op == "Login":
        exp_token = _extract_exp_token(content)
        if not exp_token:
            return _frp_reject("Missing experiment token (metadatas.exp_token)")
        exp = Experiment.query.filter(
            Experiment.frpc_token == exp_token,
            Experiment.status.notin_(["DELETED", "EXPIRED"]),
        ).first()
        if exp is None:
            return _frp_reject("Invalid or expired experiment token")
        _login_grace[exp_token] = _now()
        log.info("frp plugin Login allow exp_token=…%s", exp_token[-8:])
        return _frp_allow()

    # ── CloseProxy ────────────────────────────────────────────────────────────
    if op == "CloseProxy":
        proxy_name = content.get("proxy_name", "")
        # Only notify once per tunnel pair (server proxy, not dashboard proxy)
        if proxy_name.startswith("server-"):
            exp = _exp_from_proxy_name(proxy_name)
            if exp and exp.status not in ("DELETED", "EXPIRED"):
                if _can_notify("disconnected", exp.name):
                    send_server_disconnected(exp.user.email, exp.name)
        # CloseProxy responses are ignored by frps — just return OK
        return _frp_allow()

    # ── NewProxy ──────────────────────────────────────────────────────────────
    # Tightened authorisation: only HTTP tunnels for the Login token's
    # experiment, on its two pre-assigned custom_domains.  Each check below
    # is labelled [NP-1] … [NP-10] so you can verify behaviour in logs/tests.

    # [NP-1] Proxy type must be HTTP — reject tcp/udp/stcp/tcpmux/etc.
    proxy_type = (content.get("proxy_type") or "").strip().lower()
    if proxy_type != "http":
        return _frp_reject(f"Only HTTP proxies are allowed (got '{proxy_type or 'empty'}')")

    # [NP-2] custom_domains is required — reject proxies that omit it (old bypass).
    domains = _parse_custom_domains(content)
    if not domains:
        return _frp_reject("HTTP proxy must specify custom_domains")

    # [NP-3] Exactly one domain per proxy — matches Hub-generated frpc config.
    if len(domains) != 1:
        return _frp_reject(
            f"Exactly one custom_domain is allowed per proxy (got {len(domains)})"
        )
    domain = domains[0].strip()

    # [NP-4] Reject subdomain-based routing (alternative to custom_domains).
    if (content.get("subdomain") or "").strip():
        return _frp_reject("subdomain routing is not allowed; use custom_domains")

    # [NP-5] Reject TCP/UDP remote_port (non-HTTP tunnel indicator).
    if content.get("remote_port"):
        return _frp_reject("remote_port is not allowed for HTTP proxies")

    # [NP-6] Bind to Login token — global metadatas.exp_token → user.metas on NewProxy.
    exp_token = _extract_exp_token(content)
    if not exp_token:
        return _frp_reject("Missing experiment token in user.metas (metadatas.exp_token)")

    exp = Experiment.query.filter(
        Experiment.frpc_token == exp_token,
        Experiment.status.notin_(["DELETED", "EXPIRED"]),
    ).first()
    # [NP-7] Token must match an active experiment (same lookup as Login).
    if exp is None:
        return _frp_reject("Invalid or expired experiment token for NewProxy")

    # [NP-8] Domain must belong to *this* experiment — not merely any experiment.
    server_domain, dash_domain = _exp_frpc_domains(exp)
    allowed_domains = {server_domain, dash_domain}
    if domain not in allowed_domains:
        return _frp_reject(
            f"Domain '{domain}' is not assigned to experiment '{exp.name}'"
        )

    # [NP-9] proxy_name must be one of the two Hub-provisioned section names.
    proxy_name = (content.get("proxy_name") or "").strip()
    allowed_names = {f"server-{exp.name}", f"dashboard-{exp.name}"}
    if proxy_name not in allowed_names:
        return _frp_reject(
            f"Proxy name '{proxy_name}' is not allowed for experiment '{exp.name}'"
        )

    # [NP-9b] proxy_name must match the domain (server-* → server URL, etc.).
    if proxy_name == f"server-{exp.name}" and domain != server_domain:
        return _frp_reject(
            f"Proxy '{proxy_name}' must use custom_domain '{server_domain}'"
        )
    if proxy_name == f"dashboard-{exp.name}" and domain != dash_domain:
        return _frp_reject(
            f"Proxy '{proxy_name}' must use custom_domain '{dash_domain}'"
        )

    # [NP-10] Experiment must have sent a Hub heartbeat in the last 10 minutes,
    # or have logged in via frpc within the grace window (bootstrap race).
    if not exp.server_is_online:
        login_at = _login_grace.get(exp_token)
        in_grace = (
            login_at is not None
            and (_now() - login_at).total_seconds() < _LOGIN_GRACE_SECS
        )
        if not in_grace:
            return _frp_reject(
                f"Experiment '{exp.name}' has not sent a heartbeat in the "
                "last 10 minutes — proxy registration denied"
            )

    # Send "connected" email once per tunnel pair (server proxy, not dashboard)
    if proxy_name.startswith("server-"):
        if _can_notify("connected", exp.name):
            send_server_connected(exp.user.email, exp.name)

    log.info(
        "frp plugin NewProxy allow proxy=%s domain=%s exp=%s",
        proxy_name, domain, exp.name,
    )
    return _frp_allow()
