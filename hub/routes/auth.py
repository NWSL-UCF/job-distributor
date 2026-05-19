"""
Auth routes: signup, login, logout, email verification, password reset.
"""
import hashlib
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from flask import (
    Blueprint,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from .. import config
from ..db import db
from ..email_service import send_password_reset, send_verification
from ..models import HubSession, User

auth_bp = Blueprint("auth", __name__)

# ── Rate limiter (in-memory, per IP) ─────────────────────────────────────────
_attempt_log: dict = defaultdict(list)


def _is_rate_limited(ip: str) -> bool:
    now = time.time()
    window_start = now - config.LOGIN_WINDOW_SECS
    _attempt_log[ip] = [t for t in _attempt_log[ip] if t > window_start]
    return len(_attempt_log[ip]) >= config.LOGIN_MAX_ATTEMPTS


def _record_attempt(ip: str) -> None:
    _attempt_log[ip].append(time.time())


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _create_session(user: User, ip: str):
    token = secrets.token_urlsafe(64)
    sess  = HubSession(
        id         = token,
        user_id    = user.id,
        ip_address = ip,
        expires_at = _now() + timedelta(days=config.HUB_SESSION_TTL_DAYS),
    )
    db.session.add(sess)
    db.session.commit()
    return token


def _invalidate_all_sessions(user_id: int) -> None:
    HubSession.query.filter_by(user_id=user_id).delete()
    db.session.commit()


# ── Routes ────────────────────────────────────────────────────────────────────

@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    error = None
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")

        if not email or not password:
            error = "Email and password are required."
        elif len(password) < 10:
            error = "Password must be at least 10 characters."
        elif password != confirm:
            error = "Passwords do not match."
        elif User.query.filter_by(email=email).first():
            error = "An account with this email already exists."
        else:
            token        = secrets.token_urlsafe(48)
            token_hash   = hashlib.sha256(token.encode()).hexdigest()
            user         = User(
                email                      = email,
                password_hash              = generate_password_hash(password),
                verification_token         = token_hash,
                verification_token_expires = _now() + timedelta(hours=24),
            )
            db.session.add(user)
            db.session.commit()
            send_verification(email, user.id, token)
            return render_template("verify_email.html", email=email)

    return render_template("signup.html", error=error)


@auth_bp.route("/verify")
def verify():
    raw_token = request.args.get("token", "")
    if not raw_token:
        return render_template("verify_email.html", email=None,
                               error="Missing token.")

    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    user = User.query.filter_by(verification_token=token_hash).first()

    if not user:
        return render_template("verify_email.html", email=None,
                               error="Invalid or expired verification link.")
    if user.verification_token_expires and user.verification_token_expires < _now():
        return render_template("verify_email.html", email=user.email,
                               error="Verification link expired. Please sign up again.")

    user.is_verified               = 1
    user.verification_token        = None
    user.verification_token_expires= None
    db.session.commit()
    return redirect(url_for("auth.login", verified="1"))


@auth_bp.route("/resend-verification", methods=["POST"])
def resend_verification():
    email = request.form.get("email", "").strip().lower()
    user  = User.query.filter_by(email=email).first()
    if user and not user.is_verified:
        token      = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        user.verification_token         = token_hash
        user.verification_token_expires = _now() + timedelta(hours=24)
        db.session.commit()
        send_verification(email, user.id, token)
    return render_template("verify_email.html", email=email, resent=True)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    verified_msg = "Email verified! You can now log in." if request.args.get("verified") else None
    error = None

    if request.method == "POST":
        ip       = request.remote_addr or "unknown"
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if _is_rate_limited(ip):
            error = "Too many login attempts. Try again in 15 minutes."
        else:
            user = User.query.filter_by(email=email).first()
            if not user or not check_password_hash(user.password_hash, password):
                _record_attempt(ip)
                error = "Invalid email or password."
            elif not user.is_verified:
                error = "Please verify your email address first."
            elif not user.is_active:
                error = "Your account has been suspended."
            else:
                token = _create_session(user, ip)
                next_url = request.args.get("next") or url_for("dashboard.index")
                resp = make_response(redirect(next_url))
                resp.set_cookie(
                    "hub_session", token,
                    httponly=True, samesite="Lax",
                    max_age=config.HUB_SESSION_TTL_DAYS * 86400,
                    secure=(config.FLASK_ENV == "production"),
                )
                return resp

    return render_template("login.html", error=error, verified_msg=verified_msg)


@auth_bp.route("/logout", methods=["POST"])
def logout():
    token = request.cookies.get("hub_session")
    if token:
        HubSession.query.filter_by(id=token).delete()
        db.session.commit()
    resp = make_response(redirect(url_for("auth.login")))
    resp.delete_cookie("hub_session")
    return resp


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    sent  = False
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user  = User.query.filter_by(email=email).first()
        if user and user.is_verified:
            token      = secrets.token_urlsafe(48)
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            user.reset_token_hash    = token_hash
            user.reset_token_expires = _now() + timedelta(hours=1)
            db.session.commit()
            date_str = _now().strftime("%Y%m%d")
            send_password_reset(email, user.id, token, date_str)
        sent = True   # always show "sent" to avoid email enumeration

    return render_template("forgot_password.html", sent=sent, error=error)


@auth_bp.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    raw_token = request.args.get("token", "") or request.form.get("token", "")
    error     = None

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")

        if len(password) < 10:
            error = "Password must be at least 10 characters."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
            user = User.query.filter_by(reset_token_hash=token_hash).first()
            if not user or (user.reset_token_expires and user.reset_token_expires < _now()):
                error = "Invalid or expired reset link."
            else:
                user.password_hash    = generate_password_hash(password)
                user.reset_token_hash = None
                user.reset_token_expires = None
                db.session.commit()
                _invalidate_all_sessions(user.id)
                return redirect(url_for("auth.login") + "?reset=1")

    return render_template("reset_password.html", token=raw_token, error=error)
