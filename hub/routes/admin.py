"""
Admin portal routes. All require @require_admin.
"""
from datetime import date, datetime, timezone

from flask import Blueprint, g, redirect, render_template, request, url_for

from ..db import db
from ..decorators import require_admin
from ..models import (
    DefaultLimits,
    Experiment,
    LimitExtensionRequest,
    MonthlyUsage,
    User,
    UserLimitOverride,
)
from .. import config
from ..email_service import send_extension_result

admin_bp = Blueprint("admin", __name__)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Users ─────────────────────────────────────────────────────────────────────

@admin_bp.route("/")
@require_admin
def index():
    return redirect(url_for("admin.users"))


@admin_bp.route("/users")
@require_admin
def users():
    all_users = User.query.order_by(User.created_at.desc()).all()
    now = _now()
    rows = []
    for u in all_users:
        usage = _current_usage(u)
        lim_in, lim_out = _get_limits(u)
        rows.append({
            "user":      u,
            "usage":     usage,
            "limit_in":  lim_in,
            "limit_out": lim_out,
            "exp_count": u.experiments.filter(Experiment.status != "DELETED").count(),
        })
    return render_template("admin/users.html", user=g.current_user, rows=rows)


@admin_bp.route("/users/<int:uid>")
@require_admin
def user_detail(uid: int):
    u = db.session.get(User, uid)
    if not u:
        return redirect(url_for("admin.users"))
    exps    = u.experiments.filter(Experiment.status != "DELETED").all()
    usage   = _current_usage(u)
    lim_in, lim_out = _get_limits(u)
    override = u.limit_override
    return render_template(
        "admin/user_detail.html",
        user=g.current_user,
        target=u,
        exps=exps,
        usage=usage,
        limit_in=lim_in,
        limit_out=lim_out,
        override=override,
    )


@admin_bp.route("/users/<int:uid>/suspend", methods=["POST"])
@require_admin
def suspend_user(uid: int):
    u = db.session.get(User, uid)
    if u and u.id != g.current_user.id:
        u.is_active = 0
        db.session.commit()
    return redirect(url_for("admin.user_detail", uid=uid))


@admin_bp.route("/users/<int:uid>/activate", methods=["POST"])
@require_admin
def activate_user(uid: int):
    u = db.session.get(User, uid)
    if u:
        u.is_active = 1
        db.session.commit()
    return redirect(url_for("admin.user_detail", uid=uid))


@admin_bp.route("/users/<int:uid>/limit", methods=["POST"])
@require_admin
def set_user_limit(uid: int):
    u = db.session.get(User, uid)
    if not u:
        return redirect(url_for("admin.users"))

    def _parse_gb(field: str) -> int | None:
        val = request.form.get(field, "").strip()
        if not val:
            return None
        try:
            return int(float(val) * 1024 * 1024 * 1024)
        except ValueError:
            return None

    in_bytes  = _parse_gb("bytes_in_gb")
    out_bytes = _parse_gb("bytes_out_gb")
    note      = request.form.get("note", "").strip()
    until_str = request.form.get("valid_until", "").strip()
    valid_until = None
    if until_str:
        try:
            valid_until = date.fromisoformat(until_str)
        except ValueError:
            pass

    override = u.limit_override
    if override is None:
        override = UserLimitOverride(user_id=u.id)
        db.session.add(override)

    override.bytes_in_per_month  = in_bytes
    override.bytes_out_per_month = out_bytes
    override.note                = note
    override.valid_until         = valid_until
    override.set_by              = g.current_user.id
    db.session.commit()
    return redirect(url_for("admin.user_detail", uid=uid))


# ── Experiments ───────────────────────────────────────────────────────────────

@admin_bp.route("/experiments")
@require_admin
def experiments():
    exps = (
        Experiment.query
        .filter(Experiment.status != "DELETED")
        .order_by(Experiment.created_at.desc())
        .all()
    )
    return render_template("admin/experiments.html", user=g.current_user, exps=exps)


@admin_bp.route("/experiments/<name>/expire", methods=["POST"])
@require_admin
def expire_experiment(name: str):
    exp = Experiment.query.filter_by(name=name).first_or_404()
    exp.status     = "EXPIRED"
    exp.deleted_at = _now()
    db.session.commit()
    return redirect(url_for("admin.experiments"))


# ── Limit extension requests ──────────────────────────────────────────────────

@admin_bp.route("/extensions")
@require_admin
def extensions():
    reqs = (
        LimitExtensionRequest.query
        .order_by(LimitExtensionRequest.requested_at.desc())
        .all()
    )
    return render_template("admin/extensions.html", user=g.current_user, reqs=reqs)


@admin_bp.route("/extensions/<int:req_id>/approve", methods=["POST"])
@require_admin
def approve_extension(req_id: int):
    req = db.session.get(LimitExtensionRequest, req_id)
    if not req or req.status != "PENDING":
        return redirect(url_for("admin.extensions"))

    def _parse_gb(field):
        val = request.form.get(field, "").strip()
        try:
            return int(float(val) * 1024 * 1024 * 1024) if val else None
        except ValueError:
            return None

    extra_in  = _parse_gb("extra_in_gb")
    extra_out = _parse_gb("extra_out_gb")
    note      = request.form.get("admin_note", "").strip()

    # Update the request
    req.status               = "APPROVED"
    req.admin_note           = note
    req.additional_bytes_in  = extra_in
    req.additional_bytes_out = extra_out
    req.reviewed_at          = _now()
    req.reviewed_by          = g.current_user.id

    # Apply override to the user's limits
    u = db.session.get(User, req.user_id)
    if u:
        override = u.limit_override
        if override is None:
            override = UserLimitOverride(user_id=u.id)
            db.session.add(override)
        base_in  = override.bytes_in_per_month  or config.DEFAULT_BYTES_IN_PER_MONTH
        base_out = override.bytes_out_per_month or config.DEFAULT_BYTES_OUT_PER_MONTH
        override.bytes_in_per_month  = base_in  + (extra_in  or 0)
        override.bytes_out_per_month = base_out + (extra_out or 0)
        override.valid_until         = req.valid_until
        override.set_by              = g.current_user.id
        override.note                = f"Extension request #{req_id} approved"

        # Re-activate QUOTA_EXCEEDED experiments
        u.experiments.filter_by(status="QUOTA_EXCEEDED").update({"status": "ACTIVE"})

        send_extension_result(u.email, u.id, req_id, approved=True, note=note)

    db.session.commit()
    return redirect(url_for("admin.extensions"))


@admin_bp.route("/extensions/<int:req_id>/decline", methods=["POST"])
@require_admin
def decline_extension(req_id: int):
    req = db.session.get(LimitExtensionRequest, req_id)
    if not req or req.status != "PENDING":
        return redirect(url_for("admin.extensions"))
    note          = request.form.get("admin_note", "").strip()
    req.status    = "DECLINED"
    req.admin_note= note
    req.reviewed_at = _now()
    req.reviewed_by = g.current_user.id
    u = db.session.get(User, req.user_id)
    if u:
        send_extension_result(u.email, u.id, req_id, approved=False, note=note)
    db.session.commit()
    return redirect(url_for("admin.extensions"))


# ── Default limits ────────────────────────────────────────────────────────────

@admin_bp.route("/settings", methods=["GET", "POST"])
@require_admin
def settings():
    defaults = db.session.get(DefaultLimits, 1)
    if not defaults:
        defaults = DefaultLimits(id=1)
        db.session.add(defaults)
        db.session.commit()

    saved = False
    if request.method == "POST":
        def _parse_gb(field):
            try:
                return int(float(request.form.get(field, "10")) * 1024 * 1024 * 1024)
            except ValueError:
                return 10 * 1024 * 1024 * 1024

        defaults.bytes_in_per_month  = _parse_gb("in_gb")
        defaults.bytes_out_per_month = _parse_gb("out_gb")
        defaults.updated_by          = g.current_user.id
        db.session.commit()
        saved = True

    return render_template("admin/settings.html", user=g.current_user,
                           defaults=defaults, saved=saved)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _current_usage(user: User) -> MonthlyUsage | None:
    now = _now()
    return MonthlyUsage.query.filter_by(
        user_id=user.id, year=now.year, month=now.month
    ).first()


def _get_limits(user: User):
    override = user.limit_override
    if override:
        if not override.valid_until or override.valid_until >= date.today():
            return (
                override.bytes_in_per_month  or config.DEFAULT_BYTES_IN_PER_MONTH,
                override.bytes_out_per_month or config.DEFAULT_BYTES_OUT_PER_MONTH,
            )
    return config.DEFAULT_BYTES_IN_PER_MONTH, config.DEFAULT_BYTES_OUT_PER_MONTH
