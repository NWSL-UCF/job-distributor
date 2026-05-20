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
        SECRET_KEY = config.SECRET_KEY,
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

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(api_bp,    url_prefix="/api")
    app.register_blueprint(admin_bp,  url_prefix="/admin")

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

    # ── Jinja helpers ─────────────────────────────────────────────────────────
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


def _seed_defaults() -> None:
    if not db.session.get(DefaultLimits, 1):
        db.session.add(DefaultLimits(
            id                   = 1,
            bytes_in_per_month   = config.DEFAULT_BYTES_IN_PER_MONTH,
            bytes_out_per_month  = config.DEFAULT_BYTES_OUT_PER_MONTH,
        ))
        db.session.commit()
