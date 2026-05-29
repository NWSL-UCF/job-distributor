-- ============================================================
-- JD Hub — MySQL Schema
-- ============================================================
-- All DATETIME columns store UTC.
-- All byte-count columns (BIGINT) store raw bytes.
-- Run this once against an empty database:
--   mysql -u hub_user -p jd_hub < schema.sql
-- ============================================================

SET NAMES utf8mb4;
SET time_zone = '+00:00';

-- ──────────────────────────────────────────────────────────────
-- 1. USERS
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id                          BIGINT          NOT NULL AUTO_INCREMENT,
    email                       VARCHAR(255)    NOT NULL,
    password_hash               VARCHAR(255)    NOT NULL,

    -- Email verification
    is_verified                 TINYINT(1)      NOT NULL DEFAULT 0,
    verification_token          VARCHAR(128)    NULL,
    verification_token_expires  DATETIME        NULL,

    -- Legacy single API key (kept for backward compat; new keys use api_keys table)
    api_key_hash                VARCHAR(255)    NULL,
    api_key_prefix              VARCHAR(8)      NULL,

    -- Password reset / change-password OTP (reused for both flows)
    reset_token_hash            VARCHAR(255)    NULL,
    reset_token_expires         DATETIME        NULL,

    is_admin                    TINYINT(1)      NOT NULL DEFAULT 0,
    is_active                   TINYINT(1)      NOT NULL DEFAULT 1,

    -- Profile fields (all optional)
    display_name                VARCHAR(100)    NULL,
    city                        VARCHAR(100)    NULL,
    country                     VARCHAR(100)    NULL,
    affiliation                 VARCHAR(200)    NULL,
    profile_photo               VARCHAR(255)    NULL,   -- filename in static/uploads/avatars/

    -- Email notification toggles (1 = enabled)
    notify_experiment_lifecycle TINYINT(1)      NOT NULL DEFAULT 1,
    notify_server_status        TINYINT(1)      NOT NULL DEFAULT 1,
    notify_quota                TINYINT(1)      NOT NULL DEFAULT 1,
    notify_extensions           TINYINT(1)      NOT NULL DEFAULT 1,
    email_prefs_customized      TINYINT(1)      NOT NULL DEFAULT 0,

    created_at                  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                                ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (id),
    UNIQUE  KEY uq_users_email (email),
    INDEX       ix_users_api_key_prefix (api_key_prefix)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ──────────────────────────────────────────────────────────────
-- 2. HUB SESSIONS  (web-app login sessions)
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hub_sessions (
    id              VARCHAR(128)    NOT NULL,
    user_id         BIGINT          NOT NULL,
    ip_address      VARCHAR(45)     NULL,
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at      DATETIME        NOT NULL,

    PRIMARY KEY (id),
    INDEX ix_hub_sessions_user_id   (user_id),
    INDEX ix_hub_sessions_expires   (expires_at),
    CONSTRAINT fk_sessions_user FOREIGN KEY (user_id) REFERENCES users (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ──────────────────────────────────────────────────────────────
-- 3. API KEYS  (named, multi-key per user)
-- ──────────────────────────────────────────────────────────────
-- § Daily aggregated traffic per user (UTC date)
CREATE TABLE IF NOT EXISTS daily_traffic (
    id        BIGINT  NOT NULL AUTO_INCREMENT,
    user_id   BIGINT  NOT NULL,
    date      DATE    NOT NULL,
    bytes_in  BIGINT  NOT NULL DEFAULT 0,
    bytes_out BIGINT  NOT NULL DEFAULT 0,

    PRIMARY KEY (id),
    UNIQUE KEY uq_daily_traffic_user_date (user_id, date),
    INDEX ix_daily_traffic_user_id (user_id),
    CONSTRAINT fk_daily_traffic_user FOREIGN KEY (user_id) REFERENCES users (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS api_keys (
    id              BIGINT          NOT NULL AUTO_INCREMENT,
    user_id         BIGINT          NOT NULL,
    name            VARCHAR(100)    NOT NULL,
    key_hash        VARCHAR(255)    NOT NULL,   -- sha256 for auth lookup
    key_prefix      VARCHAR(8)      NOT NULL,   -- first 8 chars shown in UI
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (id),
    INDEX ix_api_keys_user_id   (user_id),
    INDEX ix_api_keys_prefix    (key_prefix),
    CONSTRAINT fk_api_keys_user FOREIGN KEY (user_id) REFERENCES users (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ──────────────────────────────────────────────────────────────
-- 5. EXPERIMENTS
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS experiments (
    id                      BIGINT          NOT NULL AUTO_INCREMENT,
    user_id                 BIGINT          NOT NULL,

    -- Name: lowercase, alphanumeric + hyphens, must start with letter, max 48 chars
    name                    VARCHAR(48)     NOT NULL,

    -- Status lifecycle: ACTIVE → IDLE → EXPIRED | DELETED | QUOTA_EXCEEDED
    status                  ENUM(
                                'ACTIVE',
                                'IDLE',
                                'EXPIRED',
                                'DELETED',
                                'QUOTA_EXCEEDED'
                            ) NOT NULL DEFAULT 'ACTIVE',

    -- Secrets set by Docker container on first register call
    worker_shared_secret    VARCHAR(256)    NULL,   -- used to sign/verify worker JWTs
    admin_token             VARCHAR(256)    NULL,   -- used by Hub to call /admin/override_pin

    -- Per-experiment frpc authentication token (sent as meta_exp_token on frpc Login)
    -- Validated by Hub's frp Login plugin hook — replaces the global FRPS token for auth
    frpc_token              VARCHAR(64)     NULL,

    -- FRP subdomains (set at creation)
    frpc_subdomain_server   VARCHAR(128)    NULL,   -- e.g. "server.myexp"
    frpc_subdomain_dashboard VARCHAR(128)   NULL,   -- e.g. "dashboard.myexp"

    -- Timing
    last_activity_at        DATETIME        NULL,   -- last job API call or heartbeat
    server_last_ping_at     DATETIME        NULL,   -- last heartbeat ping from jd_server
    idle_warned_at          DATETIME        NULL,   -- when 5-day idle warning email was sent
    expires_at              DATETIME        NULL,   -- set when experiment enters IDLE
    created_at              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                                    ON UPDATE CURRENT_TIMESTAMP,
    deleted_at              DATETIME        NULL,

    PRIMARY KEY (id),
    UNIQUE  KEY uq_experiments_name (name),
    INDEX       ix_experiments_user    (user_id),
    INDEX       ix_experiments_status  (status),
    INDEX       ix_experiments_activity (last_activity_at),
    CONSTRAINT fk_experiments_user FOREIGN KEY (user_id) REFERENCES users (id)
        ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ──────────────────────────────────────────────────────────────
-- 6. TRAFFIC SNAPSHOTS
-- One row per experiment per polling cycle (every 60 seconds).
-- Cumulative frps values; processed by Hub to derive deltas.
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS traffic_snapshots (
    id              BIGINT      NOT NULL AUTO_INCREMENT,
    experiment_id   BIGINT      NOT NULL,
    recorded_at     DATETIME    NOT NULL,
    bytes_in        BIGINT      NOT NULL DEFAULT 0,
    bytes_out       BIGINT      NOT NULL DEFAULT 0,

    PRIMARY KEY (id),
    INDEX ix_traffic_exp_time (experiment_id, recorded_at),
    CONSTRAINT fk_traffic_experiment FOREIGN KEY (experiment_id)
        REFERENCES experiments (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ──────────────────────────────────────────────────────────────
-- 7. MONTHLY USAGE  (aggregated per user per calendar month)
-- Rebuilt from traffic_snapshots by a background job.
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS monthly_usage (
    id                  BIGINT      NOT NULL AUTO_INCREMENT,
    user_id             BIGINT      NOT NULL,
    year                SMALLINT    NOT NULL,
    month               TINYINT     NOT NULL,   -- 1–12
    total_bytes_in      BIGINT      NOT NULL DEFAULT 0,
    total_bytes_out     BIGINT      NOT NULL DEFAULT 0,

    -- Warning flags (reset to 0 each month)
    warned_80_in        TINYINT(1)  NOT NULL DEFAULT 0,
    warned_80_out       TINYINT(1)  NOT NULL DEFAULT 0,
    warned_95_in        TINYINT(1)  NOT NULL DEFAULT 0,
    warned_95_out       TINYINT(1)  NOT NULL DEFAULT 0,
    warned_100_in       TINYINT(1)  NOT NULL DEFAULT 0,
    warned_100_out      TINYINT(1)  NOT NULL DEFAULT 0,

    updated_at          DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP
                                             ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (id),
    UNIQUE KEY uq_monthly_usage (user_id, year, month),
    CONSTRAINT fk_monthly_usage_user FOREIGN KEY (user_id) REFERENCES users (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ──────────────────────────────────────────────────────────────
-- 8. DEFAULT LIMITS  (singleton — always exactly one row, id=1)
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS default_limits (
    id                      INT         NOT NULL DEFAULT 1,
    bytes_in_per_month      BIGINT      NOT NULL DEFAULT 10737418240,   -- 10 GB
    bytes_out_per_month     BIGINT      NOT NULL DEFAULT 10737418240,   -- 10 GB
    ext_default_in_gb       INT         NOT NULL DEFAULT 50,
    ext_default_out_gb      INT         NOT NULL DEFAULT 50,
    updated_at              DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP
                                                 ON UPDATE CURRENT_TIMESTAMP,
    updated_by              BIGINT      NULL,    -- admin user id

    PRIMARY KEY (id),
    CONSTRAINT chk_singleton CHECK (id = 1),
    CONSTRAINT fk_default_limits_admin FOREIGN KEY (updated_by) REFERENCES users (id)
        ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Seed the single row
INSERT IGNORE INTO default_limits (id) VALUES (1);


-- ──────────────────────────────────────────────────────────────
-- 9. USER LIMIT OVERRIDES
-- One row per user (UPSERT on each admin change).
-- NULL in bytes columns means "fall back to default_limits".
-- valid_until NULL means the override never expires.
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_limit_overrides (
    id                      BIGINT      NOT NULL AUTO_INCREMENT,
    user_id                 BIGINT      NOT NULL,
    bytes_in_per_month      BIGINT      NULL,   -- NULL = use default
    bytes_out_per_month     BIGINT      NULL,   -- NULL = use default
    valid_until             DATE        NULL,   -- end-of-month date or NULL (permanent)
    note                    TEXT        NULL,
    set_by                  BIGINT      NULL,   -- admin user id
    created_at              DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP
                                                ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (id),
    UNIQUE KEY uq_user_limit (user_id),
    CONSTRAINT fk_limit_override_user  FOREIGN KEY (user_id) REFERENCES users (id)
        ON DELETE CASCADE,
    CONSTRAINT fk_limit_override_admin FOREIGN KEY (set_by)  REFERENCES users (id)
        ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ──────────────────────────────────────────────────────────────
-- 10. LIMIT EXTENSION REQUESTS
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS limit_extension_requests (
    id                      BIGINT      NOT NULL AUTO_INCREMENT,
    user_id                 BIGINT      NOT NULL,
    description             TEXT        NOT NULL,
    affiliation             VARCHAR(200) NOT NULL,
    status                  ENUM('PENDING','APPROVED','DECLINED') NOT NULL DEFAULT 'PENDING',

    -- Filled by admin on review
    admin_note              TEXT        NULL,
    additional_bytes_in     BIGINT      NULL,
    additional_bytes_out    BIGINT      NULL,
    valid_until             DATE        NULL,   -- end-of-month date set by Hub at submission

    requested_at            DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_at             DATETIME    NULL,
    reviewed_by             BIGINT      NULL,

    PRIMARY KEY (id),
    INDEX ix_limit_req_user   (user_id),
    INDEX ix_limit_req_status (status),
    CONSTRAINT fk_limit_req_user  FOREIGN KEY (user_id)     REFERENCES users (id)
        ON DELETE CASCADE,
    CONSTRAINT fk_limit_req_admin FOREIGN KEY (reviewed_by) REFERENCES users (id)
        ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ──────────────────────────────────────────────────────────────
-- 11. WORKER TOKENS  (issued JWTs; for explicit revocation support)
-- jti = JWT ID claim; unique per token.
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS worker_tokens (
    id              BIGINT          NOT NULL AUTO_INCREMENT,
    experiment_id   BIGINT          NOT NULL,
    jti             VARCHAR(64)     NOT NULL,
    issued_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at      DATETIME        NOT NULL,
    revoked         TINYINT(1)      NOT NULL DEFAULT 0,

    PRIMARY KEY (id),
    UNIQUE KEY uq_worker_token_jti (jti),
    INDEX ix_worker_token_exp (experiment_id, revoked, expires_at),
    CONSTRAINT fk_worker_token_exp FOREIGN KEY (experiment_id)
        REFERENCES experiments (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ──────────────────────────────────────────────────────────────
-- 12. EMAIL NOTIFICATIONS LOG
-- Deduplication: one row per (user_id, notification_type).
-- notification_type examples:
--   quota_80_in_2025_01, quota_95_out_2025_01,
--   quota_100_in_2025_01, idle_warn_myexp, expired_myexp
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS email_notifications (
    id                  BIGINT          NOT NULL AUTO_INCREMENT,
    user_id             BIGINT          NOT NULL,
    notification_type   VARCHAR(96)     NOT NULL,
    sent_at             DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (id),
    UNIQUE KEY uq_email_notif (user_id, notification_type),
    CONSTRAINT fk_email_notif_user FOREIGN KEY (user_id) REFERENCES users (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ──────────────────────────────────────────────────────────────
-- 13. USER EVENTS (activity log)
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_events (
    id              BIGINT          NOT NULL AUTO_INCREMENT,
    user_id         BIGINT          NOT NULL,
    experiment_id   BIGINT          NULL,
    experiment_name VARCHAR(48)     NULL,
    event_type      VARCHAR(64)     NOT NULL,
    message         TEXT            NOT NULL,
    metadata_json   TEXT            NULL,
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (id),
    INDEX ix_user_events_user_created (user_id, created_at),
    CONSTRAINT fk_user_events_user FOREIGN KEY (user_id) REFERENCES users (id)
        ON DELETE CASCADE,
    CONSTRAINT fk_user_events_experiment FOREIGN KEY (experiment_id) REFERENCES experiments (id)
        ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ──────────────────────────────────────────────────────────────
-- END
-- ──────────────────────────────────────────────────────────────
