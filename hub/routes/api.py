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
        f"serverPort = 7000\n"
        f"transport.tls.enable = true\n"
        f"auth.method = \"token\"\n"
        f"auth.token = \"\"\n"
        f"metadatas.exp_token = \"{exp.frpc_token}\"\n\n"
        f"[[proxies]]\n"
        f"name = \"server-{exp.name}\"\n"
        f"type = \"http\"\n"
        f"localIP = \"127.0.0.1\"\n"
        f"localPort = 8000\n"
        f"customDomains = [\"{server_domain}\"]\n\n"
        f"[[proxies]]\n"
        f"name = \"dashboard-{exp.name}\"\n"
        f"type = \"http\"\n"
        f"localIP = \"127.0.0.1\"\n"
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
# NewProxy checks: HTTP type, valid token, domain belongs to experiment.
# Login already authenticates the frpc client; no heartbeat gate on NewProxy.
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


def _frp_reject(reason: str, **context: str):
    """Return a frp plugin rejection response (HTTP 200 with reject=true)."""
    if context:
        log.warning("frp plugin reject: %s (%s)", reason, ", ".join(
            f"{k}={v}" for k, v in context.items()
        ))
    else:
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
        log.info("frp plugin Login allow exp=%s", exp.name)
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
    # Authorisation: valid experiment token + HTTP proxy on an assigned domain.
    # Login already verified the token; do not re-check heartbeat here.

    proxy_name = (content.get("proxy_name") or "").strip()
    proxy_type = (content.get("proxy_type") or "").strip().lower()
    if proxy_type not in ("http", ""):
        return _frp_reject(
            f"Only HTTP proxies are allowed (got '{proxy_type}')",
            proxy=proxy_name,
        )

    domains = _parse_custom_domains(content)
    if not domains:
        return _frp_reject("HTTP proxy must specify custom_domains", proxy=proxy_name)

    if (content.get("subdomain") or "").strip():
        return _frp_reject("subdomain routing is not allowed", proxy=proxy_name)

    remote_port = content.get("remote_port")
    if remote_port is not None and remote_port != 0:
        return _frp_reject("remote_port is not allowed for HTTP proxies", proxy=proxy_name)

    exp_token = _extract_exp_token(content)
    if not exp_token:
        return _frp_reject("Missing experiment token (metadatas.exp_token)", proxy=proxy_name)

    exp = Experiment.query.filter(
        Experiment.frpc_token == exp_token,
        Experiment.status.notin_(["DELETED", "EXPIRED"]),
    ).first()
    if exp is None:
        return _frp_reject("Invalid or expired experiment token", proxy=proxy_name)

    server_domain, dash_domain = _exp_frpc_domains(exp)
    allowed_domains = {server_domain, dash_domain}
    bad_domains = [d for d in domains if d not in allowed_domains]
    if bad_domains:
        return _frp_reject(
            f"Domain(s) not assigned to experiment '{exp.name}': {', '.join(bad_domains)}",
            proxy=proxy_name,
        )

    domain = domains[0]

    # Send "connected" email once per tunnel pair (server proxy, not dashboard)
    if proxy_name.startswith("server-"):
        if _can_notify("connected", exp.name):
            send_server_connected(exp.user.email, exp.name)

    log.info(
        "frp plugin NewProxy allow proxy=%s domain=%s exp=%s",
        proxy_name, domain, exp.name,
    )
    return _frp_allow()
