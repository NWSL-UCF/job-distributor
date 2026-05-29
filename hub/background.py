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
    DailyTraffic,
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
        ("traffic_poller",    _poll_traffic,    config.BG_TRAFFIC_POLL_INTERVAL),
        ("usage_aggregator",  _aggregate_usage, config.BG_USAGE_AGG_INTERVAL),
        ("daily_aggregator",  _aggregate_daily, config.BG_DAILY_AGG_INTERVAL),
        ("idle_checker",      _check_idle,      config.BG_IDLE_CHECK_INTERVAL),
        ("token_pruner",      _prune_tokens,    config.BG_TOKEN_PRUNE_INTERVAL),
        ("snapshot_pruner",   _prune_snapshots, config.BG_SNAPSHOT_PRUNE_INTERVAL),
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

    domain_map = {}
    for p in proxies:
        if not p:
            continue
        for domain in ((p.get("conf") or {}).get("customDomains") or []):
            domain_map[domain] = (
                p.get("todayTrafficIn",  0),
                p.get("todayTrafficOut", 0),
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

        s_in,  s_out  = domain_map.get(server_domain, (0, 0))
        d_in,  d_out  = domain_map.get(dash_domain,   (0, 0))

        db.session.add(TrafficSnapshot(
            experiment_id=exp.id,
            recorded_at=now,
            bytes_in=s_in  + d_in,
            bytes_out=s_out + d_out,
        ))

    db.session.commit()


# ── Daily traffic aggregation ─────────────────────────────────────────────────

def _aggregate_daily() -> None:
    """Upsert per-user daily traffic totals into DailyTraffic.

    Runs every 5 minutes.  Two things happen each call:
      1. TODAY's partial record is recomputed from snapshots and upserted,
         so the heatmap shows near-live activity throughout the day.
      2. YESTERDAY's record is finalised once (after midnight UTC) for any
         user who does not yet have a row for that date.

    Implementation uses bulk queries — 3–4 DB round-trips total regardless
    of user count, avoiding the N+1 pattern that would occur with a per-user
    loop at scale.
    """
    from collections import defaultdict
    from datetime import timedelta

    from sqlalchemy import func

    today     = _now().date()
    yesterday = today - timedelta(days=1)

    # ── Step 1: load today's snapshots for ALL experiments in one query ──────
    # Join experiments so we know which user each snapshot belongs to.
    today_snaps = (
        db.session.query(
            TrafficSnapshot.experiment_id,
            Experiment.user_id,
            TrafficSnapshot.bytes_in,
            TrafficSnapshot.bytes_out,
            TrafficSnapshot.recorded_at,
        )
        .join(Experiment, TrafficSnapshot.experiment_id == Experiment.id)
        .filter(func.date(TrafficSnapshot.recorded_at) == today)
        .order_by(
            Experiment.user_id,
            TrafficSnapshot.experiment_id,
            TrafficSnapshot.recorded_at,
        )
        .all()
    )

    # Aggregate today's deltas per user in Python
    today_by_user: dict[int, tuple[int, int]] = _bulk_delta_by_user(today_snaps)

    # ── Step 2: upsert today rows ────────────────────────────────────────────
    if today_by_user:
        existing_today = {
            r.user_id: r
            for r in DailyTraffic.query.filter(
                DailyTraffic.user_id.in_(today_by_user.keys()),
                DailyTraffic.date == today,
            ).all()
        }
        for user_id, (bi, bo) in today_by_user.items():
            if bi == 0 and bo == 0:
                continue
            row = existing_today.get(user_id)
            if row:
                row.bytes_in  = bi
                row.bytes_out = bo
            else:
                db.session.add(DailyTraffic(
                    user_id=user_id, date=today, bytes_in=bi, bytes_out=bo,
                ))

    # ── Step 3: finalise yesterday (only for users missing a row) ────────────
    # Find user IDs that have snapshots yesterday but no DailyTraffic record.
    already_done_yesterday: set[int] = {
        r.user_id
        for r in DailyTraffic.query.filter(DailyTraffic.date == yesterday).all()
    }

    yesterday_snaps = (
        db.session.query(
            TrafficSnapshot.experiment_id,
            Experiment.user_id,
            TrafficSnapshot.bytes_in,
            TrafficSnapshot.bytes_out,
            TrafficSnapshot.recorded_at,
        )
        .join(Experiment, TrafficSnapshot.experiment_id == Experiment.id)
        .filter(
            func.date(TrafficSnapshot.recorded_at) == yesterday,
            Experiment.user_id.notin_(already_done_yesterday),
        )
        .order_by(
            Experiment.user_id,
            TrafficSnapshot.experiment_id,
            TrafficSnapshot.recorded_at,
        )
        .all()
    )

    yesterday_by_user = _bulk_delta_by_user(yesterday_snaps)
    for user_id, (bi, bo) in yesterday_by_user.items():
        if bi > 0 or bo > 0:
            db.session.add(DailyTraffic(
                user_id=user_id, date=yesterday, bytes_in=bi, bytes_out=bo,
            ))

    db.session.commit()


def _bulk_delta_by_user(
    rows: list,
) -> dict[int, tuple[int, int]]:
    """Given a flat list of snapshot rows (already sorted by user_id, experiment_id,
    recorded_at), return {user_id: (bytes_in, bytes_out)} using the same
    positive-increment delta logic that handles frps counter resets."""
    from collections import defaultdict

    # Group by (user_id, experiment_id) preserving order
    by_exp: dict[tuple[int, int], list] = defaultdict(list)
    for r in rows:
        by_exp[(r.user_id, r.experiment_id)].append(r)

    user_totals: dict[int, list[int]] = defaultdict(lambda: [0, 0])

    for (user_id, _), snaps in by_exp.items():
        if len(snaps) == 1:
            user_totals[user_id][0] += snaps[0].bytes_in
            user_totals[user_id][1] += snaps[0].bytes_out
            continue
        prev_in, prev_out = snaps[0].bytes_in, snaps[0].bytes_out
        for s in snaps[1:]:
            di = s.bytes_in  - prev_in
            do = s.bytes_out - prev_out
            if di > 0:   user_totals[user_id][0] += di
            elif di < 0: user_totals[user_id][0] += s.bytes_in
            if do > 0:   user_totals[user_id][1] += do
            elif do < 0: user_totals[user_id][1] += s.bytes_out
            prev_in, prev_out = s.bytes_in, s.bytes_out

    return {uid: (v[0], v[1]) for uid, v in user_totals.items()}


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
            email_service.send_idle_warning(
                exp.user.email, exp.user_id, exp.name, experiment_id=exp.id,
            )
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
        email_service.send_expired(
            exp.user.email, exp.user_id, exp.name, experiment_id=exp.id,
        )
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
