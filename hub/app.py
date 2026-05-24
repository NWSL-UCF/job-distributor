"""
Hub Flask application factory.
"""
import logging
import os

from flask import Flask, redirect, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from . import config
from .db import db
from .models import DefaultLimits


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")

    app.config.update(
        SQLALCHEMY_DATABASE_URI        = config.DATABASE_URL,
        SQLALCHEMY_TRACK_MODIFICATIONS = False,
        SQLALCHEMY_ENGINE_OPTIONS      = {
            "pool_recycle": 280,
            "pool_pre_ping": True,
        },
        SECRET_KEY          = config.SECRET_KEY,
        MAX_CONTENT_LENGTH  = 2 * 1024 * 1024,   # 2 MB limit for avatar uploads
    )

    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    # ── Extensions ────────────────────────────────────────────────────────────
    db.init_app(app)

    # ── Blueprints ────────────────────────────────────────────────────────────
    from .routes.auth      import auth_bp
    from .routes.dashboard import dashboard_bp
    from .routes.api       import api_bp
    from .routes.admin     import admin_bp
    from .routes.pages     import pages_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(api_bp,    url_prefix="/api")
    app.register_blueprint(admin_bp,  url_prefix="/admin")
    app.register_blueprint(pages_bp)

    # Trust X-Forwarded-* from nginx so request.is_secure works behind TLS termination.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

    # ── DB init ───────────────────────────────────────────────────────────────
    with app.app_context():
        db.create_all()
        _apply_migrations()
        _seed_defaults()

    # ── Background threads ────────────────────────────────────────────────────
    # Only start in the main process to avoid duplicate threads under Gunicorn.
    # Run Gunicorn with --workers=1 or use hub_worker.py for multi-process setups.
    if os.environ.get("HUB_NO_BACKGROUND") != "1":
        from .background import start_background_threads
        start_background_threads(app)

    # ── Ensure upload directories exist on every startup ──────────────────────
    upload_dir = os.path.join(app.static_folder, "uploads", "avatars")
    os.makedirs(upload_dir, exist_ok=True)

    # ── Jinja helpers ─────────────────────────────────────────────────────────
    @app.template_global()
    def avatar_url(user):
        """Return the URL for a user's avatar (custom upload, default SVG, or generic fallback)."""
        from flask import url_for as _url_for
        if not user or not user.profile_photo:
            return _url_for("static", filename="default-avatar.png")
        if user.profile_photo.startswith("default-avatar-"):
            return _url_for("static", filename=user.profile_photo)
        return _url_for("static", filename=f"uploads/avatars/{user.profile_photo}")

    @app.template_filter("fmt_bytes")
    def fmt_bytes(n):
        if n is None:
            return "0 B"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if abs(n) < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} PB"

    @app.template_filter("pct")
    def pct_filter(used, total):
        if not total:
            return 0
        return min(100, round(used / total * 100))

    return app


def _apply_migrations() -> None:
    """Idempotent schema migrations for columns added after initial deployment.

    Uses information_schema checks for MySQL 5.7 compatibility
    (ADD COLUMN IF NOT EXISTS requires MySQL 8.0+).
    """
    import logging
    log = logging.getLogger(__name__)

    # Each entry: (table, column, ALTER TABLE statement to run if column is absent)
    migrations = [
        # v2: server heartbeat tracking
        (
            "experiments",
            "server_last_ping_at",
            "ALTER TABLE experiments ADD COLUMN "
            "server_last_ping_at DATETIME NULL AFTER last_activity_at",
        ),
        # v3: user profile fields
        ("users", "display_name",  "ALTER TABLE users ADD COLUMN display_name  VARCHAR(100) NULL"),
        ("users", "city",          "ALTER TABLE users ADD COLUMN city          VARCHAR(100) NULL"),
        ("users", "country",       "ALTER TABLE users ADD COLUMN country       VARCHAR(100) NULL"),
        ("users", "affiliation",   "ALTER TABLE users ADD COLUMN affiliation   VARCHAR(200) NULL"),
        ("users", "profile_photo", "ALTER TABLE users ADD COLUMN profile_photo VARCHAR(255) NULL"),
        # v4: per-experiment frpc authentication token
        (
            "experiments",
            "frpc_token",
            "ALTER TABLE experiments ADD COLUMN frpc_token VARCHAR(64) NULL AFTER admin_token",
        ),
    ]

    for table, column, stmt in migrations:
        check = db.text(
            "SELECT COUNT(*) FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = :tbl AND COLUMN_NAME = :col"
        )
        try:
            exists = db.session.execute(check, {"tbl": table, "col": column}).scalar()
            if not exists:
                db.session.execute(db.text(stmt))
                db.session.commit()
                log.info("Migration applied: added %s.%s", table, column)
        except Exception as exc:
            db.session.rollback()
            log.warning("Migration failed for %s.%s: %s", table, column, exc)

    # New-table migrations (checked via information_schema.TABLES)
    table_migrations = [
        (
            "daily_traffic",
            """CREATE TABLE IF NOT EXISTS daily_traffic (
                id        BIGINT  NOT NULL AUTO_INCREMENT,
                user_id   BIGINT  NOT NULL,
                date      DATE    NOT NULL,
                bytes_in  BIGINT  NOT NULL DEFAULT 0,
                bytes_out BIGINT  NOT NULL DEFAULT 0,
                PRIMARY KEY (id),
                UNIQUE KEY uq_daily_traffic_user_date (user_id, date),
                INDEX ix_daily_traffic_user_id (user_id),
                CONSTRAINT fk_daily_traffic_user
                    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
        ),
    ]
    for tbl_name, create_stmt in table_migrations:
        check_tbl = db.text(
            "SELECT COUNT(*) FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :tbl"
        )
        try:
            exists = db.session.execute(check_tbl, {"tbl": tbl_name}).scalar()
            if not exists:
                db.session.execute(db.text(create_stmt))
                db.session.commit()
                log.info("Migration applied: created table %s", tbl_name)
        except Exception as exc:
            db.session.rollback()
            log.warning("Migration failed for table %s: %s", tbl_name, exc)

    # Backfill frpc_token for existing experiments that have NULL after the v4 migration
    try:
        import secrets as _secrets
        from .models import Experiment as _Exp
        null_exps = _Exp.query.filter(_Exp.frpc_token.is_(None)).all()
        if null_exps:
            for exp in null_exps:
                exp.frpc_token = _secrets.token_hex(32)
            db.session.commit()
            log.info("Migration v4: backfilled frpc_token for %d experiments", len(null_exps))
    except Exception as exc:
        db.session.rollback()
        log.warning("Migration v4 backfill failed: %s", exc)


def _seed_defaults() -> None:
    if not db.session.get(DefaultLimits, 1):
        db.session.add(DefaultLimits(
            id                   = 1,
            bytes_in_per_month   = config.DEFAULT_BYTES_IN_PER_MONTH,
            bytes_out_per_month  = config.DEFAULT_BYTES_OUT_PER_MONTH,
        ))
        db.session.commit()
