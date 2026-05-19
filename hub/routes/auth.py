"""
Auth routes: signup, login, logout, email verification (OTP), password reset (OTP).
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
from ..email_service import send_password_reset_otp, send_verification_otp
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


# ── OTP helpers ───────────────────────────────────────────────────────────────

def _generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _hash_otp(otp: str) -> str:
    return hashlib.sha256(otp.strip().encode()).hexdigest()


def _otp_valid(stored_hash: str | None, otp: str) -> bool:
    if not stored_hash or not otp:
        return False
    return secrets.compare_digest(stored_hash, _hash_otp(otp))


def _set_verification_otp(user: User) -> str:
    otp = _generate_otp()
    user.verification_token         = _hash_otp(otp)
    user.verification_token_expires = _now() + timedelta(
        minutes=config.OTP_VERIFY_EXPIRE_MINUTES
    )
    return otp


def _set_reset_otp(user: User) -> str:
    otp = _generate_otp()
    user.reset_token_hash    = _hash_otp(otp)
    user.reset_token_expires = _now() + timedelta(
        minutes=config.OTP_RESET_EXPIRE_MINUTES
    )
    return otp


# ── Session helpers ───────────────────────────────────────────────────────────

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
            user = User(
                email         = email,
                password_hash = generate_password_hash(password),
            )
            otp = _set_verification_otp(user)
            db.session.add(user)
            db.session.commit()
            send_verification_otp(email, otp)
            return redirect(url_for("auth.verify_email", email=email))

    return render_template("signup.html", error=error)


@auth_bp.route("/verify")
def verify_legacy():
    """Redirect old verification links to the OTP page."""
    return redirect(url_for("auth.verify_email", email=request.args.get("email", "")))


@auth_bp.route("/verify-email", methods=["GET", "POST"])
def verify_email():
    email = (request.form.get("email") or request.args.get("email") or "").strip().lower()
    error = None

    if request.method == "POST":
        otp = request.form.get("otp", "").strip()
        user = User.query.filter_by(email=email).first() if email else None

        if not user:
            error = "No account found for that email."
        elif user.is_verified:
            return redirect(url_for("auth.login", verified="1"))
        elif not _otp_valid(user.verification_token, otp):
            error = "Invalid verification code."
        elif user.verification_token_expires and user.verification_token_expires < _now():
            error = "Verification code expired. Request a new code below."
        else:
            user.is_verified                = 1
            user.verification_token         = None
            user.verification_token_expires = None
            db.session.commit()
            return redirect(url_for("auth.login", verified="1"))

    return render_template("verify_email.html", email=email or None, error=error)


@auth_bp.route("/resend-verification", methods=["POST"])
def resend_verification():
    email = request.form.get("email", "").strip().lower()
    user  = User.query.filter_by(email=email).first()
    if user and not user.is_verified:
        otp = _set_verification_otp(user)
        db.session.commit()
        send_verification_otp(email, otp)
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
                error = "Please verify your email first."
                return render_template(
                    "login.html",
                    error=error,
                    verified_msg=verified_msg,
                    unverified_email=email,
                )
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
    email = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user  = User.query.filter_by(email=email).first()
        if user and user.is_verified:
            otp = _set_reset_otp(user)
            db.session.commit()
            send_password_reset_otp(email, otp)
        sent = True

    return render_template("forgot_password.html", sent=sent, error=error, email=email)


@auth_bp.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    email = (request.form.get("email") or request.args.get("email") or "").strip().lower()
    error = None

    if request.method == "POST":
        otp      = request.form.get("otp", "").strip()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")

        if len(password) < 10:
            error = "Password must be at least 10 characters."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            user = User.query.filter_by(email=email).first() if email else None
            if not user:
                error = "No account found for that email."
            elif not _otp_valid(user.reset_token_hash, otp):
                error = "Invalid reset code."
            elif user.reset_token_expires and user.reset_token_expires < _now():
                error = "Reset code expired. Request a new code from forgot password."
            else:
                user.password_hash       = generate_password_hash(password)
                user.reset_token_hash    = None
                user.reset_token_expires = None
                db.session.commit()
                _invalidate_all_sessions(user.id)
                return redirect(url_for("auth.login") + "?reset=1")

    return render_template("reset_password.html", email=email or None, error=error)
