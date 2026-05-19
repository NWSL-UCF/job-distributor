"""
Hub API routes (consumed by jd_worker and local server.py).

All routes require API key auth unless noted.
"""
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from flask import Blueprint, g, jsonify, request

from .. import config
from ..db import db
from ..decorators import require_api_key
from ..models import Experiment, WorkerToken

api_bp = Blueprint("api", __name__)

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
    exp = Experiment(
        user_id               = user.id,
        name                  = name,
        status                = "ACTIVE",
        worker_shared_secret  = worker_secret,
        frpc_subdomain_server = f"server.{name}.{config.JD_BASE_DOMAIN}",
        frpc_subdomain_dashboard = f"dashboard.{name}.{config.JD_BASE_DOMAIN}",
        last_activity_at      = _now(),
    )
    db.session.add(exp)
    db.session.commit()

    return jsonify({
        "experiment_id": exp.id,
        "name":          exp.name,
        "server_url":    f"https://server.{name}.{config.JD_BASE_DOMAIN}",
        "dashboard_url": f"https://dashboard.{name}.{config.JD_BASE_DOMAIN}",
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
        "server_url":           f"https://server.{exp.name}.{config.JD_BASE_DOMAIN}",
        "dashboard_url":        f"https://dashboard.{exp.name}.{config.JD_BASE_DOMAIN}",
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
    exp.last_activity_at = _now()
    if exp.status == "IDLE":
        exp.status        = "ACTIVE"
        exp.idle_warned_at = None
    db.session.commit()
    return jsonify({"message": "OK"}), 200


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
        "server_url":   f"https://server.{exp.name}.{config.JD_BASE_DOMAIN}",
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
        "server_url":   f"https://server.{exp.name}.{config.JD_BASE_DOMAIN}",
        "dashboard_url":f"https://dashboard.{exp.name}.{config.JD_BASE_DOMAIN}",
        "created_at":   exp.created_at.isoformat() if exp.created_at else None,
        "last_activity":exp.last_activity_at.isoformat() if exp.last_activity_at else None,
    }


def _build_frpc_config(exp: Experiment) -> str:
    return (
        f"[common]\n"
        f"server_addr = hub.{config.JD_BASE_DOMAIN}\n"
        f"server_port = 7000\n"
        f"token       = {config.FRPS_TOKEN}\n\n"
        f"[server-{exp.name}]\n"
        f"type            = http\n"
        f"local_port      = 8000\n"
        f"custom_domains  = {exp.frpc_subdomain_server}\n\n"
        f"[dashboard-{exp.name}]\n"
        f"type            = http\n"
        f"local_port      = 8001\n"
        f"custom_domains  = {exp.frpc_subdomain_dashboard}\n"
    )
