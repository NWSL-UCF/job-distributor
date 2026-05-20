"""
Auth decorators for Hub routes.

  @require_login   — valid Hub web session cookie required
  @require_admin   — same + is_admin = 1
  @require_api_key — valid JD API key in Authorization: Bearer header
"""
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import g, jsonify, redirect, request, url_for

from . import config
from .auth_util import login_next_param
from .db import db
from .models import HubSession, User


# ── Session helpers ───────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_current_user() -> User | None:
    """Look up the logged-in user from the hub_session cookie."""
    token = request.cookies.get("hub_session")
    if not token:
        return None
    sess = db.session.get(HubSession, token)
    if not sess or sess.expires_at < _now():
        return None
    # slide expiry
    sess.expires_at = _now() + timedelta(days=config.HUB_SESSION_TTL_DAYS)
    db.session.commit()
    return sess.user


# ── Decorators ────────────────────────────────────────────────────────────────

def _wants_json() -> bool:
    """True only for API-style requests that prefer JSON over HTML."""
    best = request.accept_mimetypes.best_match(["application/json", "text/html"])
    return best == "application/json"


def require_login(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user:
            if _wants_json():
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("auth.login", next=login_next_param()))
        g.current_user = user
        return fn(*args, **kwargs)
    return wrapper


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user:
            if _wants_json():
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("auth.login"))
        if not user.is_admin:
            if _wants_json():
                return jsonify({"error": "Forbidden"}), 403
            return redirect(url_for("dashboard.index"))
        g.current_user = user
        return fn(*args, **kwargs)
    return wrapper


def require_api_key(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid Authorization header"}), 401
        raw_key = auth_header[7:].strip()
        user = _lookup_api_key(raw_key)
        if not user:
            return jsonify({"error": "Invalid API key"}), 401
        if not user.is_active:
            return jsonify({"error": "Account suspended"}), 403
        g.api_user = user
        return fn(*args, **kwargs)
    return wrapper


def _lookup_api_key(raw_key: str) -> User | None:
    """Verify a raw API key against stored hashes.

    Checks the new api_keys table first (named multi-key), then falls back
    to the legacy single key stored on the users table.
    """
    import hashlib
    from .models import ApiKey
    if not raw_key or len(raw_key) < 10:
        return None
    prefix   = raw_key[:8]
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    # Check named api_keys table (new multi-key system)
    named_key = ApiKey.query.filter_by(key_prefix=prefix, key_hash=key_hash).first()
    if named_key:
        return named_key.user

    # Fall back to legacy single key on users table
    return User.query.filter_by(
        api_key_prefix=prefix, api_key_hash=key_hash
    ).first()
