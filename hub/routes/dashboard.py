"""
User-facing dashboard routes.
"""
import hashlib
import re
import secrets
from datetime import date, datetime, timezone

from flask import (
    Blueprint,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from .. import config
from ..db import db
from ..decorators import require_login
from ..models import (
    DefaultLimits,
    Experiment,
    LimitExtensionRequest,
    MonthlyUsage,
    User,
    UserLimitOverride,
)

dashboard_bp = Blueprint("dashboard", __name__)

_NAME_RE = re.compile(r"^[a-z][a-z0-9\-]{1,46}$")


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
    )


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
    exp = Experiment.query.filter_by(name=name).first_or_404()
    if exp.user_id != g.current_user.id:
        return redirect(url_for("dashboard.index"))
    exp.status     = "DELETED"
    exp.deleted_at = _now()
    db.session.commit()
    return redirect(url_for("dashboard.index"))


@dashboard_bp.route("/experiments/<name>/extend", methods=["POST"])
@require_login
def extend_experiment(name: str):
    from datetime import timedelta
    exp = Experiment.query.filter_by(name=name).first_or_404()
    if exp.user_id != g.current_user.id:
        return jsonify({"error": "Forbidden"}), 403
    if exp.status not in ("IDLE", "ACTIVE"):
        return jsonify({"error": "Experiment cannot be extended in its current state"}), 400
    exp.expires_at    = _now() + timedelta(days=14)
    exp.idle_warned_at= None
    exp.status        = "ACTIVE"
    db.session.commit()
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
            headers={"Authorization": f"Bearer {exp.admin_token}"},
            timeout=10,
        )
        if r.status_code == 200:
            return _render_experiment_detail(exp, pin_success="Dashboard PIN updated successfully.")
        return _render_experiment_detail(exp, pin_error=f"Dashboard returned: {r.text[:200]}")
    except Exception as exc:
        return _render_experiment_detail(exp, pin_error=f"Could not reach dashboard: {exc}")


# ── Profile / API key ─────────────────────────────────────────────────────────

@dashboard_bp.route("/profile")
@require_login
def profile():
    return render_template("profile.html", user=g.current_user)


@dashboard_bp.route("/api-key/regenerate", methods=["POST"])
@require_login
def regenerate_api_key():
    user   = g.current_user
    raw    = "jd_" + secrets.token_urlsafe(38)
    kh     = hashlib.sha256(raw.encode()).hexdigest()
    user.api_key_hash   = kh
    user.api_key_prefix = raw[:8]
    db.session.commit()
    return render_template("profile.html", user=user, new_api_key=raw)


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
        user_id               = user.id,
        name                  = name,
        status                = "ACTIVE",
        worker_shared_secret  = worker_secret,
        frpc_subdomain_server = f"{name}-server.{config.JD_BASE_DOMAIN}",
        frpc_subdomain_dashboard = f"{name}-dashboard.{config.JD_BASE_DOMAIN}",
        last_activity_at      = _now(),
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
