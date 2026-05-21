from datetime import datetime, timezone

from .db import db


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(db.Model):
    __tablename__ = "users"

    id                         = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    email                      = db.Column(db.String(255), unique=True, nullable=False)
    password_hash              = db.Column(db.String(255), nullable=False)
    is_verified                = db.Column(db.SmallInteger, nullable=False, default=0)
    verification_token         = db.Column(db.String(128))
    verification_token_expires = db.Column(db.DateTime)
    api_key_hash               = db.Column(db.String(255))
    api_key_prefix             = db.Column(db.String(8))
    reset_token_hash           = db.Column(db.String(255))
    reset_token_expires        = db.Column(db.DateTime)
    is_admin                   = db.Column(db.SmallInteger, nullable=False, default=0)
    is_active                  = db.Column(db.SmallInteger, nullable=False, default=1)
    # Profile fields
    display_name               = db.Column(db.String(100))
    city                       = db.Column(db.String(100))
    country                    = db.Column(db.String(100))
    affiliation                = db.Column(db.String(200))
    profile_photo              = db.Column(db.String(255))
    created_at                 = db.Column(db.DateTime, default=_now)
    updated_at                 = db.Column(db.DateTime, default=_now, onupdate=_now)

    experiments     = db.relationship("Experiment",    back_populates="user", lazy="dynamic")
    monthly_usages  = db.relationship("MonthlyUsage",  back_populates="user", lazy="dynamic")
    limit_override  = db.relationship("UserLimitOverride", back_populates="user", uselist=False,
                                      foreign_keys="UserLimitOverride.user_id")
    ext_requests    = db.relationship("LimitExtensionRequest", back_populates="user",
                                      foreign_keys="LimitExtensionRequest.user_id", lazy="dynamic")
    daily_traffic   = db.relationship("DailyTraffic", back_populates="user", lazy="dynamic",
                                      cascade="all, delete-orphan")
    api_keys        = db.relationship("ApiKey", back_populates="user", lazy="dynamic",
                                      cascade="all, delete-orphan")


class HubSession(db.Model):
    __tablename__ = "hub_sessions"

    id         = db.Column(db.String(128), primary_key=True)
    user_id    = db.Column(db.BigInteger, db.ForeignKey("users.id", ondelete="CASCADE"),
                           nullable=False)
    ip_address = db.Column(db.String(45))
    created_at = db.Column(db.DateTime, default=_now)
    expires_at = db.Column(db.DateTime, nullable=False)

    user = db.relationship("User")


class Experiment(db.Model):
    __tablename__ = "experiments"

    id                      = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    user_id                 = db.Column(db.BigInteger, db.ForeignKey("users.id", ondelete="RESTRICT"),
                                        nullable=False)
    name                    = db.Column(db.String(48), unique=True, nullable=False)
    status                  = db.Column(
        db.Enum("ACTIVE", "IDLE", "EXPIRED", "DELETED", "QUOTA_EXCEEDED"),
        nullable=False, default="ACTIVE",
    )
    worker_shared_secret    = db.Column(db.String(256))
    admin_token             = db.Column(db.String(256))
    frpc_subdomain_server   = db.Column(db.String(128))
    frpc_subdomain_dashboard= db.Column(db.String(128))
    last_activity_at        = db.Column(db.DateTime)
    server_last_ping_at     = db.Column(db.DateTime)
    idle_warned_at          = db.Column(db.DateTime)
    expires_at              = db.Column(db.DateTime)
    created_at              = db.Column(db.DateTime, default=_now)
    updated_at              = db.Column(db.DateTime, default=_now, onupdate=_now)
    deleted_at              = db.Column(db.DateTime)

    user             = db.relationship("User", back_populates="experiments")
    traffic_snapshots= db.relationship("TrafficSnapshot", back_populates="experiment",
                                       lazy="dynamic", cascade="all, delete-orphan")
    worker_tokens    = db.relationship("WorkerToken", back_populates="experiment",
                                       lazy="dynamic", cascade="all, delete-orphan")

    @property
    def server_is_online(self) -> bool:
        if not self.server_last_ping_at:
            return False
        from datetime import timedelta
        cutoff = _now() - timedelta(minutes=10)
        return self.server_last_ping_at > cutoff


class TrafficSnapshot(db.Model):
    __tablename__ = "traffic_snapshots"

    id            = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    experiment_id = db.Column(db.BigInteger,
                              db.ForeignKey("experiments.id", ondelete="CASCADE"),
                              nullable=False)
    recorded_at   = db.Column(db.DateTime, nullable=False, default=_now)
    bytes_in      = db.Column(db.BigInteger, nullable=False, default=0)
    bytes_out     = db.Column(db.BigInteger, nullable=False, default=0)

    experiment = db.relationship("Experiment", back_populates="traffic_snapshots")


class MonthlyUsage(db.Model):
    __tablename__ = "monthly_usage"
    __table_args__ = (db.UniqueConstraint("user_id", "year", "month"),)

    id              = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    user_id         = db.Column(db.BigInteger, db.ForeignKey("users.id", ondelete="CASCADE"),
                                nullable=False)
    year            = db.Column(db.SmallInteger, nullable=False)
    month           = db.Column(db.SmallInteger, nullable=False)
    total_bytes_in  = db.Column(db.BigInteger, nullable=False, default=0)
    total_bytes_out = db.Column(db.BigInteger, nullable=False, default=0)
    warned_80_in    = db.Column(db.SmallInteger, nullable=False, default=0)
    warned_80_out   = db.Column(db.SmallInteger, nullable=False, default=0)
    warned_95_in    = db.Column(db.SmallInteger, nullable=False, default=0)
    warned_95_out   = db.Column(db.SmallInteger, nullable=False, default=0)
    warned_100_in   = db.Column(db.SmallInteger, nullable=False, default=0)
    warned_100_out  = db.Column(db.SmallInteger, nullable=False, default=0)
    updated_at      = db.Column(db.DateTime, default=_now, onupdate=_now)

    user = db.relationship("User", back_populates="monthly_usages")


class DefaultLimits(db.Model):
    __tablename__ = "default_limits"

    id                   = db.Column(db.Integer, primary_key=True, default=1)
    bytes_in_per_month   = db.Column(db.BigInteger, nullable=False,
                                     default=10 * 1024 * 1024 * 1024)
    bytes_out_per_month  = db.Column(db.BigInteger, nullable=False,
                                     default=10 * 1024 * 1024 * 1024)
    updated_at           = db.Column(db.DateTime, default=_now, onupdate=_now)
    updated_by           = db.Column(db.BigInteger, db.ForeignKey("users.id", ondelete="SET NULL"))


class UserLimitOverride(db.Model):
    __tablename__ = "user_limit_overrides"

    id                  = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    user_id             = db.Column(db.BigInteger, db.ForeignKey("users.id", ondelete="CASCADE"),
                                    unique=True, nullable=False)
    bytes_in_per_month  = db.Column(db.BigInteger)
    bytes_out_per_month = db.Column(db.BigInteger)
    valid_until         = db.Column(db.Date)
    note                = db.Column(db.Text)
    set_by              = db.Column(db.BigInteger, db.ForeignKey("users.id", ondelete="SET NULL"))
    created_at          = db.Column(db.DateTime, default=_now)
    updated_at          = db.Column(db.DateTime, default=_now, onupdate=_now)

    user = db.relationship("User", back_populates="limit_override", foreign_keys=[user_id])


class LimitExtensionRequest(db.Model):
    __tablename__ = "limit_extension_requests"

    id                   = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    user_id              = db.Column(db.BigInteger, db.ForeignKey("users.id", ondelete="CASCADE"),
                                     nullable=False)
    description          = db.Column(db.Text, nullable=False)
    affiliation          = db.Column(db.String(200), nullable=False)
    status               = db.Column(
        db.Enum("PENDING", "APPROVED", "DECLINED"), nullable=False, default="PENDING"
    )
    admin_note           = db.Column(db.Text)
    additional_bytes_in  = db.Column(db.BigInteger)
    additional_bytes_out = db.Column(db.BigInteger)
    valid_until          = db.Column(db.Date)
    requested_at         = db.Column(db.DateTime, default=_now)
    reviewed_at          = db.Column(db.DateTime)
    reviewed_by          = db.Column(db.BigInteger, db.ForeignKey("users.id", ondelete="SET NULL"))

    user = db.relationship("User", back_populates="ext_requests", foreign_keys=[user_id])


class WorkerToken(db.Model):
    __tablename__ = "worker_tokens"

    id            = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    experiment_id = db.Column(db.BigInteger,
                              db.ForeignKey("experiments.id", ondelete="CASCADE"),
                              nullable=False)
    jti           = db.Column(db.String(64), unique=True, nullable=False)
    issued_at     = db.Column(db.DateTime, default=_now)
    expires_at    = db.Column(db.DateTime, nullable=False)
    revoked       = db.Column(db.SmallInteger, nullable=False, default=0)

    experiment = db.relationship("Experiment", back_populates="worker_tokens")


class EmailNotification(db.Model):
    __tablename__ = "email_notifications"
    __table_args__ = (db.UniqueConstraint("user_id", "notification_type"),)

    id                = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    user_id           = db.Column(db.BigInteger, db.ForeignKey("users.id", ondelete="CASCADE"),
                                  nullable=False)
    notification_type = db.Column(db.String(96), nullable=False)
    sent_at           = db.Column(db.DateTime, default=_now)


class DailyTraffic(db.Model):
    """Aggregated per-user traffic totals for a completed calendar day (UTC).

    Populated nightly by the daily_aggregator background job.  Today's partial
    record is upserted every few minutes so the heatmap always shows current
    activity.
    """
    __tablename__ = "daily_traffic"
    __table_args__ = (db.UniqueConstraint("user_id", "date"),)

    id        = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    user_id   = db.Column(db.BigInteger, db.ForeignKey("users.id", ondelete="CASCADE"),
                          nullable=False)
    date      = db.Column(db.Date, nullable=False)
    bytes_in  = db.Column(db.BigInteger, nullable=False, default=0)
    bytes_out = db.Column(db.BigInteger, nullable=False, default=0)

    user = db.relationship("User", back_populates="daily_traffic")


class ApiKey(db.Model):
    __tablename__ = "api_keys"

    id         = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    user_id    = db.Column(db.BigInteger, db.ForeignKey("users.id", ondelete="CASCADE"),
                           nullable=False)
    name       = db.Column(db.String(100), nullable=False)
    key_value  = db.Column(db.String(255), nullable=False)
    key_hash   = db.Column(db.String(255), nullable=False)
    key_prefix = db.Column(db.String(8),   nullable=False)
    created_at = db.Column(db.DateTime, default=_now)

    user = db.relationship("User", back_populates="api_keys")
