"""
User-facing dashboard routes.
"""
import hashlib
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone

from flask import (
    Blueprint,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from .. import config
from ..db import db
from ..decorators import require_login
from ..models import (
    ApiKey,
    DailyTraffic,
    DefaultLimits,
    Experiment,
    LimitExtensionRequest,
    MonthlyUsage,
    User,
    UserLimitOverride,
)

dashboard_bp = Blueprint("dashboard", __name__)

_NAME_RE = re.compile(r"^[a-z][a-z0-9\-]{1,46}$")
_AVATAR_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp"}
_AVATAR_MAX_BYTES  = 2 * 1024 * 1024   # 2 MB


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Main dashboard ────────────────────────────────────────────────────────────

@dashboard_bp.route("/")
@require_login
def index():
    user  = g.current_user
    exps  = user.experiments.filter(Experiment.status != "DELETED").order_by(
        Experiment.created_at.desc()
    ).all()
    usage = _current_month_usage(user)
    limit_in, limit_out = _get_limits(user)
    return render_template(
        "dashboard.html",
        user=user,
        experiments=exps,
        usage=usage,
        limit_in=limit_in,
        limit_out=limit_out,
        now=_now(),
    )


@dashboard_bp.route("/notifications")
@require_login
def notifications():
    return render_template("notifications.html", user=g.current_user)


@dashboard_bp.route("/settings")
@require_login
def settings():
    from ..notification_prefs import get_user_prefs, NOTIFICATION_CATEGORIES
    user = g.current_user
    return render_template(
        "settings.html",
        user=user,
        notification_categories=NOTIFICATION_CATEGORIES,
        notification_prefs=get_user_prefs(user),
        password_step=1,
    )


def _settings_context(user, **kwargs):
    from ..notification_prefs import get_user_prefs, NOTIFICATION_CATEGORIES
    base = {
        "user": user,
        "notification_categories": NOTIFICATION_CATEGORIES,
        "notification_prefs": get_user_prefs(user),
        "password_step": 1,
    }
    base.update(kwargs)
    return base


@dashboard_bp.route("/events")
@require_login
def events_api():
    from ..event_log import get_events_page, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
    page = request.args.get("page", 0, type=int)
    page_size = request.args.get("page_size", DEFAULT_PAGE_SIZE, type=int)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    return jsonify(get_events_page(g.current_user.id, page=page, page_size=page_size))


@dashboard_bp.route("/settings/notifications", methods=["POST"])
@require_login
def update_notification_settings():
    from ..notification_prefs import update_user_prefs
    user = g.current_user
    update_user_prefs(user, request.form)
    db.session.commit()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"success": True})
    return render_template(
        "settings.html",
        **_settings_context(user, notif_success="Email preferences saved."),
    )


# ── Traffic heatmap data ──────────────────────────────────────────────────────

@dashboard_bp.route("/traffic-heatmap")
@require_login
def traffic_heatmap():
    """Return daily traffic data for the heatmap from the DailyTraffic table.

    Historical days (before today) are pre-computed by the daily_aggregator
    background job.  Today's partial row is upserted by the same job every
    5 minutes, so callers always get a near-live reading without a full
    snapshot scan.
    """
    from sqlalchemy import extract

    year = request.args.get("year", _now().year, type=int)
    user = g.current_user

    rows = (
        DailyTraffic.query
        .filter(
            DailyTraffic.user_id == user.id,
            extract("year", DailyTraffic.date) == year,
        )
        .order_by(DailyTraffic.date)
        .all()
    )

    days = [
        {"date": r.date.isoformat(), "bytes_in": r.bytes_in, "bytes_out": r.bytes_out}
        for r in rows
    ]
    active_days = sum(1 for d in days if d["bytes_in"] + d["bytes_out"] > 0)
    return jsonify({"active_days": active_days, "days": days, "year": year})


# ── Experiment views ──────────────────────────────────────────────────────────

@dashboard_bp.route("/experiments/new")
@require_login
def new_experiment():
    return render_template("experiment_new.html", user=g.current_user)


@dashboard_bp.route("/experiments", methods=["POST"])
@require_login
def create_experiment():
    user = g.current_user
    name = request.form.get("name", "").strip().lower()

    if not _NAME_RE.match(name):
        return render_template(
            "experiment_new.html", user=user,
            error="Name must be lowercase, start with a letter, and contain only letters, numbers, or hyphens (max 48 chars)."
        )

    count = user.experiments.filter(
        Experiment.status.notin_(["DELETED", "EXPIRED"])
    ).count()
    if count >= config.MAX_EXPERIMENTS_PER_USER:
        return render_template(
            "experiment_new.html", user=user,
            error=f"Maximum of {config.MAX_EXPERIMENTS_PER_USER} active experiments reached."
        )

    if Experiment.query.filter_by(name=name).first():
        return render_template(
            "experiment_new.html", user=user,
            error="Experiment name already taken."
        )

    exp = _provision_experiment(user, name)
    from ..email_service import send_experiment_created
    send_experiment_created(user.email, user.id, exp.name, exp.id)
    return redirect(url_for("dashboard.experiment_detail", name=name))


@dashboard_bp.route("/experiments/<name>")
@require_login
def experiment_detail(name: str):
    exp = Experiment.query.filter_by(name=name).first_or_404()
    if exp.user_id != g.current_user.id and not g.current_user.is_admin:
        return redirect(url_for("dashboard.index"))
    return _render_experiment_detail(exp)


@dashboard_bp.route("/experiments/<name>/delete", methods=["POST"])
@require_login
def delete_experiment(name: str):
    from ..background import _disconnect_frp
    from ..server_shutdown import request_server_shutdown

    exp = Experiment.query.filter_by(name=name).first_or_404()
    if exp.user_id != g.current_user.id:
        return redirect(url_for("dashboard.index"))

    request_server_shutdown(exp)
    exp_id = exp.id
    exp_name = exp.name
    user_id = g.current_user.id
    user_email = g.current_user.email
    exp.status     = "DELETED"
    exp.deleted_at = _now()
    db.session.commit()
    _disconnect_frp(exp)
    from ..email_service import send_experiment_deleted
    send_experiment_deleted(user_email, user_id, exp_name, exp_id)
    return redirect(url_for("dashboard.index"))


@dashboard_bp.route("/experiments/<name>/extend", methods=["POST"])
@require_login
def extend_experiment(name: str):
    exp = Experiment.query.filter_by(name=name).first_or_404()
    if exp.user_id != g.current_user.id:
        return jsonify({"error": "Forbidden"}), 403
    if exp.status not in ("IDLE", "ACTIVE"):
        return jsonify({"error": "Experiment cannot be extended in its current state"}), 400
    exp.expires_at     = _now() + timedelta(days=14)
    exp.idle_warned_at = None
    exp.status         = "ACTIVE"
    db.session.commit()
    from ..event_log import log_event
    log_event(
        g.current_user.id,
        "experiment_extended",
        f"Experiment '{exp.name}' was extended by 14 days.",
        experiment_id=exp.id,
        experiment_name=exp.name,
    )
    return redirect(url_for("dashboard.experiment_detail", name=name))


@dashboard_bp.route("/experiments/<name>/reset-pin", methods=["POST"])
@require_login
def reset_pin(name: str):
    import requests as req
    exp     = Experiment.query.filter_by(name=name).first_or_404()
    if exp.user_id != g.current_user.id:
        return jsonify({"error": "Forbidden"}), 403
    new_pin = request.form.get("new_pin", "").strip()
    if not new_pin or len(new_pin) != 6 or not new_pin.isdigit():
        return _render_experiment_detail(exp, pin_error="PIN must be exactly 6 digits.")
    if not exp.admin_token:
        return _render_experiment_detail(
            exp,
            pin_error="Server not registered yet. Start your Docker stack with Hub credentials.",
        )
    dash_url = f"https://{exp.name}-dashboard.{config.JD_BASE_DOMAIN}/admin/override_pin"
    try:
        r = req.post(
            dash_url,
            json={"new_pin": new_pin},
            headers={"X-Admin-Token": exp.admin_token},
            timeout=10,
        )
        if r.status_code == 200:
            from ..event_log import log_event
            log_event(
                g.current_user.id,
                "pin_updated",
                f"Dashboard PIN updated for experiment '{exp.name}'.",
                experiment_id=exp.id,
                experiment_name=exp.name,
            )
            return _render_experiment_detail(exp, pin_success="Dashboard PIN updated successfully.")
        return _render_experiment_detail(exp, pin_error=f"Dashboard returned: {r.text[:200]}")
    except Exception as exc:
        return _render_experiment_detail(exp, pin_error=f"Could not reach dashboard: {exc}")


# ── Profile ───────────────────────────────────────────────────────────────────

@dashboard_bp.route("/profile")
@require_login
def profile():
    return render_template("profile.html", user=g.current_user)


@dashboard_bp.route("/profile/update", methods=["POST"])
@require_login
def update_profile():
    user = g.current_user
    user.display_name = request.form.get("display_name", "").strip() or None
    user.city         = request.form.get("city", "").strip() or None
    user.country      = request.form.get("country", "").strip() or None
    user.affiliation  = request.form.get("affiliation", "").strip() or None
    db.session.commit()
    return render_template("profile.html", user=user, profile_success="Profile updated.")


@dashboard_bp.route("/profile/photo", methods=["POST"])
@require_login
def update_avatar():
    user = g.current_user
    file = request.files.get("photo")
    if not file or not file.filename:
        return ("No file received.", 400)

    # Accept JPEG blobs from the crop-modal (filename comes as 'crop.jpg')
    raw_name = file.filename or ""
    ext = raw_name.rsplit(".", 1)[-1].lower() if "." in raw_name else "jpg"
    if ext not in _AVATAR_EXTENSIONS:
        return ("Invalid file type. Use JPG, PNG, GIF, or WebP.", 400)

    data = file.read()
    if len(data) > _AVATAR_MAX_BYTES:
        return ("Photo too large. Maximum 2 MB.", 400)

    upload_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "static", "uploads", "avatars",
    )
    os.makedirs(upload_dir, exist_ok=True)

    # Remove old uploaded file if extension changed (skip default SVG avatars)
    if (user.profile_photo
            and not user.profile_photo.startswith("default-avatar-")
            and user.profile_photo != f"{user.id}.{ext}"):
        old_path = os.path.join(upload_dir, user.profile_photo)
        if os.path.exists(old_path):
            os.remove(old_path)

    filename = f"{user.id}.{ext}"
    with open(os.path.join(upload_dir, filename), "wb") as f:
        f.write(data)

    user.profile_photo = filename
    db.session.commit()
    from flask import jsonify
    return jsonify({"url": url_for("static", filename=f"uploads/avatars/{filename}")}), 200


@dashboard_bp.route("/profile/photo/default", methods=["POST"])
@require_login
def set_default_avatar():
    user = g.current_user
    avatar_id = request.form.get("avatar_id", "")
    valid = {f"default-avatar-{i}.svg" for i in range(1, 9)}
    if avatar_id not in valid:
        return redirect(url_for("dashboard.profile"))

    # Remove any previously uploaded file (default avatars have no file)
    if user.profile_photo and not user.profile_photo.startswith("default-avatar-"):
        upload_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static", "uploads", "avatars",
        )
        file_path = os.path.join(upload_dir, user.profile_photo)
        if os.path.exists(file_path):
            os.remove(file_path)

    user.profile_photo = avatar_id
    db.session.commit()
    return render_template("profile.html", user=user, profile_success="Avatar updated.")


@dashboard_bp.route("/profile/photo/delete", methods=["POST"])
@require_login
def delete_avatar():
    user = g.current_user
    if not user.profile_photo:
        return redirect(url_for("dashboard.profile"))

    # Only delete the file if it's an uploaded photo, not a default avatar
    if not user.profile_photo.startswith("default-avatar-"):
        upload_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static", "uploads", "avatars",
        )
        file_path = os.path.join(upload_dir, user.profile_photo)
        if os.path.exists(file_path):
            os.remove(file_path)

    user.profile_photo = None
    db.session.commit()
    return render_template("profile.html", user=user, profile_success="Photo removed.")


# ── Change password (logged-in flow) ─────────────────────────────────────────

@dashboard_bp.route("/change-password", methods=["GET", "POST"])
@require_login
def change_password():
    from ..email_service import send_password_change_otp
    from ..routes.auth import generate_otp as _generate_otp, hash_otp as _hash_otp, otp_valid as _otp_valid

    user = g.current_user

    if request.method == "GET":
        return redirect(url_for("dashboard.settings", _anchor="password"))

    step = request.form.get("step", "1")

    # ── Step 1: verify current password, send OTP ────────────────────────────
    if step == "1":
        current_pw = request.form.get("current_password", "")
        if not check_password_hash(user.password_hash, current_pw):
            return render_template(
                "settings.html",
                **_settings_context(
                    user,
                    password_error="Current password is incorrect.",
                ),
            )
        otp = _generate_otp()
        user.reset_token_hash    = _hash_otp(otp)
        user.reset_token_expires = _now() + timedelta(minutes=config.OTP_RESET_EXPIRE_MINUTES)
        db.session.commit()
        send_password_change_otp(user.email, otp)
        return render_template(
            "settings.html",
            **_settings_context(
                user,
                password_step=2,
                password_info=f"A verification code was sent to {user.email}.",
            ),
        )

    # ── Step 2: verify OTP and set new password ──────────────────────────────
    if step == "2":
        otp         = request.form.get("otp", "").strip()
        new_pw      = request.form.get("new_password", "")
        confirm_pw  = request.form.get("confirm_password", "")

        if not _otp_valid(user.reset_token_hash, otp):
            return render_template(
                "settings.html",
                **_settings_context(
                    user,
                    password_step=2,
                    password_error="Invalid verification code.",
                    password_info=f"Code was sent to {user.email}.",
                ),
            )
        if user.reset_token_expires and user.reset_token_expires < _now():
            return render_template(
                "settings.html",
                **_settings_context(
                    user,
                    password_error="Verification code expired. Please start again.",
                ),
            )
        if len(new_pw) < 10:
            return render_template(
                "settings.html",
                **_settings_context(
                    user,
                    password_step=2,
                    password_error="New password must be at least 10 characters.",
                    password_info=f"Code was sent to {user.email}.",
                ),
            )
        if new_pw != confirm_pw:
            return render_template(
                "settings.html",
                **_settings_context(
                    user,
                    password_step=2,
                    password_error="Passwords do not match.",
                    password_info=f"Code was sent to {user.email}.",
                ),
            )

        user.password_hash       = generate_password_hash(new_pw)
        user.reset_token_hash    = None
        user.reset_token_expires = None
        db.session.commit()

        current_token = request.cookies.get("hub_session")
        from ..models import HubSession
        HubSession.query.filter(
            HubSession.user_id == user.id,
            HubSession.id != current_token,
        ).delete()
        db.session.commit()

        return render_template(
            "settings.html",
            **_settings_context(user, password_success="Password changed successfully."),
        )

    return redirect(url_for("dashboard.settings", _anchor="password"))


# ── Legacy single-key regenerate (kept for backward compat) ──────────────────

@dashboard_bp.route("/api-key/regenerate", methods=["POST"])
@require_login
def regenerate_api_key():
    return redirect(url_for("dashboard.api_keys"))


# ── API Keys management ───────────────────────────────────────────────────────

@dashboard_bp.route("/api-keys")
@require_login
def api_keys():
    keys = g.current_user.api_keys.order_by(ApiKey.created_at.desc()).all()
    return render_template("api_keys.html", user=g.current_user, keys=keys)


@dashboard_bp.route("/api-keys/create", methods=["POST"])
@require_login
def create_api_key():
    user = g.current_user
    name = request.form.get("name", "").strip()
    if not name:
        keys = user.api_keys.order_by(ApiKey.created_at.desc()).all()
        return render_template("api_keys.html", user=user, keys=keys,
                               error="Key name is required.")
    if len(name) > 100:
        keys = user.api_keys.order_by(ApiKey.created_at.desc()).all()
        return render_template("api_keys.html", user=user, keys=keys,
                               error="Key name must be 100 characters or fewer.")

    raw       = "jd_" + secrets.token_urlsafe(38)
    key_hash  = hashlib.sha256(raw.encode()).hexdigest()
    key_prefix= raw[:8]
    key = ApiKey(
        user_id    = user.id,
        name       = name,
        key_hash   = key_hash,
        key_prefix = key_prefix,
    )
    db.session.add(key)
    db.session.commit()

    from ..event_log import log_event
    log_event(
        user.id,
        "api_key_created",
        f"API key '{name}' was created.",
        metadata={"key_prefix": key_prefix},
    )

    keys = user.api_keys.order_by(ApiKey.created_at.desc()).all()
    return render_template("api_keys.html", user=user, keys=keys,
                           new_key_name=name, new_key_value=raw)


@dashboard_bp.route("/api-keys/<int:key_id>/delete", methods=["POST"])
@require_login
def delete_api_key(key_id: int):
    key = ApiKey.query.filter_by(id=key_id, user_id=g.current_user.id).first_or_404()
    key_name = key.name
    db.session.delete(key)
    db.session.commit()
    from ..event_log import log_event
    log_event(
        g.current_user.id,
        "api_key_deleted",
        f"API key '{key_name}' was deleted.",
    )
    return redirect(url_for("dashboard.api_keys"))


# ── Limit extensions ──────────────────────────────────────────────────────────

@dashboard_bp.route("/extensions")
@require_login
def extensions():
    reqs = (
        g.current_user.ext_requests
        .order_by(LimitExtensionRequest.requested_at.desc())
        .all()
    )
    return render_template("extensions.html", user=g.current_user, requests=reqs)


@dashboard_bp.route("/extensions", methods=["POST"])
@require_login
def submit_extension():
    desc  = request.form.get("description", "").strip()
    affil = request.form.get("affiliation", "").strip()
    if not desc or not affil:
        reqs = (g.current_user.ext_requests
                .order_by(LimitExtensionRequest.requested_at.desc()).all())
        return render_template("extensions.html", user=g.current_user,
                               requests=reqs, error="All fields are required.")
    today    = date.today()
    import calendar
    last_day = calendar.monthrange(today.year, today.month)[1]
    req = LimitExtensionRequest(
        user_id     = g.current_user.id,
        description = desc,
        affiliation = affil,
        valid_until = date(today.year, today.month, last_day),
    )
    db.session.add(req)
    db.session.commit()
    from ..event_log import log_event
    from ..email_service import send_extension_request_admin_alert
    log_event(
        g.current_user.id,
        "extension_submitted",
        "Data limit extension request submitted.",
        metadata={"request_id": req.id},
    )
    send_extension_request_admin_alert(
        g.current_user.email,
        req.id,
        desc,
        affil,
    )
    reqs = (g.current_user.ext_requests
            .order_by(LimitExtensionRequest.requested_at.desc()).all())
    return render_template("extensions.html", user=g.current_user,
                           requests=reqs, success="Request submitted.")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _render_experiment_detail(exp: Experiment, **kwargs):
    return render_template(
        "experiment_detail.html",
        user=g.current_user,
        exp=exp,
        **kwargs,
    )


def _provision_experiment(user: User, name: str) -> Experiment:
    worker_secret = secrets.token_hex(32)
    exp = Experiment(
        user_id                  = user.id,
        name                     = name,
        status                   = "ACTIVE",
        worker_shared_secret     = worker_secret,
        frpc_token               = secrets.token_hex(32),
        frpc_subdomain_server    = f"{name}-server.{config.JD_BASE_DOMAIN}",
        frpc_subdomain_dashboard = f"{name}-dashboard.{config.JD_BASE_DOMAIN}",
        last_activity_at         = _now(),
    )
    db.session.add(exp)
    db.session.commit()
    return exp


def _current_month_usage(user: User) -> MonthlyUsage | None:
    now = _now()
    return MonthlyUsage.query.filter_by(
        user_id=user.id, year=now.year, month=now.month
    ).first()


def _get_limits(user: User):
    override = user.limit_override
    if override:
        if not override.valid_until or override.valid_until >= date.today():
            in_lim  = override.bytes_in_per_month  or config.DEFAULT_BYTES_IN_PER_MONTH
            out_lim = override.bytes_out_per_month or config.DEFAULT_BYTES_OUT_PER_MONTH
            return in_lim, out_lim
    return config.DEFAULT_BYTES_IN_PER_MONTH, config.DEFAULT_BYTES_OUT_PER_MONTH
