"""
Background threads for Hub:
  - traffic_poller:    polls frps admin API every 60s
  - usage_aggregator:  aggregates monthly usage every 5 min
  - idle_checker:      detects idle experiments every 10 min
  - token_pruner:      deletes expired tokens every hour
  - snapshot_pruner:   prunes old snapshots daily
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

from . import config
from .db import db
from .models import (
    DefaultLimits,
    Experiment,
    MonthlyUsage,
    TrafficSnapshot,
    User,
    UserLimitOverride,
    WorkerToken,
)

log = logging.getLogger(__name__)

_started = False
_lock    = threading.Lock()


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def start_background_threads(app) -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True

    def _loop(name: str, fn, interval: int):
        log.info("Background thread started: %s (interval=%ss)", name, interval)
        while True:
            time.sleep(interval)
            try:
                with app.app_context():
                    fn()
            except Exception as exc:
                log.exception("Background %s error: %s", name, exc)

    threads = [
        ("traffic_poller",   _poll_traffic,    config.BG_TRAFFIC_POLL_INTERVAL),
        ("usage_aggregator", _aggregate_usage, config.BG_USAGE_AGG_INTERVAL),
        ("idle_checker",     _check_idle,      config.BG_IDLE_CHECK_INTERVAL),
        ("token_pruner",     _prune_tokens,    config.BG_TOKEN_PRUNE_INTERVAL),
        ("snapshot_pruner",  _prune_snapshots, config.BG_SNAPSHOT_PRUNE_INTERVAL),
    ]
    for name, fn, interval in threads:
        t = threading.Thread(target=_loop, args=(name, fn, interval), daemon=True)
        t.start()


# ── Traffic polling ───────────────────────────────────────────────────────────

def _poll_traffic() -> None:
    """Query frps admin API and store a snapshot per active experiment."""
    try:
        r = requests.get(f"{config.FRPS_API_URL}/api/proxy/http", timeout=10)
        if r.status_code != 200:
            log.warning("frps admin API returned %s", r.status_code)
            return
        proxies = r.json().get("proxies") or []
    except Exception as exc:
        log.debug("frps poll error (frps may not be running): %s", exc)
        return

    # Build {custom_domain -> (bytes_in, bytes_out)}
    domain_map = {}
    for p in proxies:
        for domain in (p.get("conf", {}).get("custom_domains") or []):
            domain_map[domain] = (
                p.get("today_traffic_in",  0),
                p.get("today_traffic_out", 0),
            )

    now = _now()
    exps = Experiment.query.filter(
        Experiment.status.in_(["ACTIVE", "IDLE", "QUOTA_EXCEEDED"])
    ).all()

    for exp in exps:
        server_domain = (exp.frpc_subdomain_server or
                         f"{exp.name}-server.{config.JD_BASE_DOMAIN}")
        dash_domain   = (exp.frpc_subdomain_dashboard or
                         f"{exp.name}-dashboard.{config.JD_BASE_DOMAIN}")

        s_in,  s_out  = domain_map.get(server_domain,    (0, 0))
        d_in,  d_out  = domain_map.get(dash_domain,      (0, 0))
        total_in      = s_in  + d_in
        total_out     = s_out + d_out

        snap = TrafficSnapshot(
            experiment_id=exp.id,
            recorded_at=now,
            bytes_in=total_in,
            bytes_out=total_out,
        )
        db.session.add(snap)

    db.session.commit()


# ── Monthly usage aggregation ─────────────────────────────────────────────────

def _aggregate_usage() -> None:
    now   = _now()
    year  = now.year
    month = now.month

    users = User.query.filter_by(is_active=1).all()
    for user in users:
        exps = list(user.experiments.filter(
            Experiment.status != "DELETED"
        ).all())
        if not exps:
            continue

        total_in = total_out = 0
        for exp in exps:
            total_in  += _delta_bytes(exp.id, year, month, "bytes_in")
            total_out += _delta_bytes(exp.id, year, month, "bytes_out")

        usage = MonthlyUsage.query.filter_by(
            user_id=user.id, year=year, month=month
        ).first()
        if not usage:
            usage = MonthlyUsage(user_id=user.id, year=year, month=month)
            db.session.add(usage)

        usage.total_bytes_in  = total_in
        usage.total_bytes_out = total_out

        # Enforce quota & send warnings
        limit_in, limit_out = _get_limits(user)
        _check_quota(user, usage, limit_in, limit_out, year, month)

    db.session.commit()


def _delta_bytes(exp_id: int, year: int, month: int, col: str) -> int:
    from sqlalchemy import func, extract
    from .models import TrafficSnapshot

    snaps = (
        db.session.query(TrafficSnapshot)
        .filter(
            TrafficSnapshot.experiment_id == exp_id,
            extract("year",  TrafficSnapshot.recorded_at) == year,
            extract("month", TrafficSnapshot.recorded_at) == month,
        )
        .order_by(TrafficSnapshot.recorded_at)
        .all()
    )
    if not snaps:
        return 0

    total = 0
    prev  = getattr(snaps[0], col)
    for s in snaps[1:]:
        cur = getattr(s, col)
        diff = cur - prev
        if diff > 0:
            total += diff
        elif diff < 0:
            # frps restarted — treat current reading as the delta
            total += cur
        prev = cur
    return total


def _get_limits(user: User):
    override = user.limit_override
    if override:
        from datetime import date
        if not override.valid_until or override.valid_until >= date.today():
            in_lim  = override.bytes_in_per_month  or config.DEFAULT_BYTES_IN_PER_MONTH
            out_lim = override.bytes_out_per_month or config.DEFAULT_BYTES_OUT_PER_MONTH
            return in_lim, out_lim
    return config.DEFAULT_BYTES_IN_PER_MONTH, config.DEFAULT_BYTES_OUT_PER_MONTH


def _check_quota(user: User, usage: MonthlyUsage,
                 limit_in: int, limit_out: int,
                 year: int, month: int) -> None:
    from . import email_service

    def _warn_if_needed(bytes_used, limit, direction, flag_80, flag_95, flag_100):
        pct = bytes_used / limit if limit > 0 else 0
        if pct >= 1.0 and not getattr(usage, flag_100):
            setattr(usage, flag_100, 1)
            email_service.send_quota_warning(user.email, user.id, direction, 100, year, month)
            # Suspend all experiments
            for exp in user.experiments.filter(Experiment.status == "ACTIVE").all():
                exp.status = "QUOTA_EXCEEDED"
        elif pct >= 0.95 and not getattr(usage, flag_95):
            setattr(usage, flag_95, 1)
            email_service.send_quota_warning(user.email, user.id, direction, 95, year, month)
        elif pct >= 0.80 and not getattr(usage, flag_80):
            setattr(usage, flag_80, 1)
            email_service.send_quota_warning(user.email, user.id, direction, 80, year, month)

    _warn_if_needed(usage.total_bytes_in,  limit_in,  "in",
                    "warned_80_in",  "warned_95_in",  "warned_100_in")
    _warn_if_needed(usage.total_bytes_out, limit_out, "out",
                    "warned_80_out", "warned_95_out", "warned_100_out")


# ── Idle detection ────────────────────────────────────────────────────────────

def _check_idle() -> None:
    from . import email_service
    now = _now()

    active_exps = Experiment.query.filter_by(status="ACTIVE").all()
    for exp in active_exps:
        last_act = exp.last_activity_at or exp.created_at
        if not last_act:
            continue
        idle_days = (now - last_act).total_seconds() / 86400

        if idle_days >= 5 and exp.idle_warned_at is None:
            exp.idle_warned_at = now
            exp.expires_at     = now + timedelta(days=2)
            email_service.send_idle_warning(exp.user.email, exp.user_id, exp.name)
            log.info("Idle warning sent for experiment %s", exp.name)

    # Expire timed-out experiments
    idle_exps = Experiment.query.filter(
        Experiment.status == "ACTIVE",
        Experiment.expires_at < now,
    ).all()
    for exp in idle_exps:
        exp.status     = "EXPIRED"
        exp.deleted_at = now
        _disconnect_frp(exp)
        email_service.send_expired(exp.user.email, exp.user_id, exp.name)
        log.info("Experiment %s expired", exp.name)

    db.session.commit()


def _disconnect_frp(exp: Experiment) -> None:
    """Ask frps to kick connections for this experiment's proxies."""
    for proxy_name in (
        f"server-{exp.name}", f"dashboard-{exp.name}"
    ):
        try:
            requests.delete(
                f"{config.FRPS_API_URL}/api/proxy/{proxy_name}",
                timeout=5,
            )
        except Exception:
            pass


# ── Token pruner ──────────────────────────────────────────────────────────────

def _prune_tokens() -> None:
    cutoff = _now() - timedelta(hours=24)
    deleted = (
        db.session.query(WorkerToken)
        .filter(
            (WorkerToken.revoked == 1) | (WorkerToken.expires_at < cutoff)
        )
        .delete()
    )
    db.session.commit()
    if deleted:
        log.info("Pruned %d expired/revoked worker tokens", deleted)


# ── Snapshot pruner ───────────────────────────────────────────────────────────

def _prune_snapshots() -> None:
    cutoff = _now() - timedelta(days=90)
    deleted = (
        db.session.query(TrafficSnapshot)
        .filter(TrafficSnapshot.recorded_at < cutoff)
        .delete()
    )
    db.session.commit()
    if deleted:
        log.info("Pruned %d old traffic snapshots", deleted)
