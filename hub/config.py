import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


# ── Database ──────────────────────────────────────────────────────────────────
MYSQL_HOST     = _env("MYSQL_HOST", "localhost")
MYSQL_PORT     = int(_env("MYSQL_PORT", "3306"))
MYSQL_USER     = _env("MYSQL_USER", "hub_user")
MYSQL_PASSWORD = _env("MYSQL_PASSWORD", "")
MYSQL_DATABASE = _env("MYSQL_DATABASE", "jd_hub")

DATABASE_URL = (
    f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}"
    f"@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}?charset=utf8mb4"
)

# ── Flask ─────────────────────────────────────────────────────────────────────
SECRET_KEY = _env("FLASK_SECRET_KEY", "dev-change-me-in-production")
FLASK_ENV  = _env("FLASK_ENV", "production")

# ── Hub ───────────────────────────────────────────────────────────────────────
HUB_BASE_URL    = _env("HUB_BASE_URL", "https://hub.jobdistributor.net")
JD_BASE_DOMAIN  = _env("JD_BASE_DOMAIN", "jobdistributor.net")

# ── FRP ───────────────────────────────────────────────────────────────────────
FRPS_TOKEN   = _env("FRPS_TOKEN", "")
FRPS_API_URL = _env("FRPS_API_URL", "http://localhost:7500")   # never public

# ── JWT ───────────────────────────────────────────────────────────────────────
JWT_SECRET_KEY             = _env("JWT_SECRET_KEY", "dev-jwt-change-me")
JWT_WORKER_TOKEN_TTL_HOURS = int(_env("JWT_WORKER_TOKEN_TTL_HOURS", "24"))

# ── Session ───────────────────────────────────────────────────────────────────
HUB_SESSION_TTL_DAYS = int(_env("HUB_SESSION_TTL_DAYS", "30"))
# Leave empty to auto-detect from request (HTTPS). Set 0 for HTTP dev on :5000.
SESSION_COOKIE_SECURE = _env("SESSION_COOKIE_SECURE")

# ── Email (Brevo) ─────────────────────────────────────────────────────────────
BREVO_API_KEY    = _env("BREVO_API_KEY")
BREVO_FROM_EMAIL = _env("BREVO_FROM_EMAIL", "info@jobdistributor.net")
BREVO_FROM_NAME  = _env("BREVO_FROM_NAME",  "JobDistributor Team")

# ── OTP (email verification & password reset) ─────────────────────────────────
OTP_VERIFY_EXPIRE_MINUTES = int(_env("OTP_VERIFY_EXPIRE_MINUTES", "30"))
OTP_RESET_EXPIRE_MINUTES  = int(_env("OTP_RESET_EXPIRE_MINUTES", "15"))

# ── Rate limiting ─────────────────────────────────────────────────────────────
LOGIN_MAX_ATTEMPTS  = 5
LOGIN_WINDOW_SECS   = 900   # 15 minutes

# ── Quotas ────────────────────────────────────────────────────────────────────
DEFAULT_BYTES_IN_PER_MONTH  = 10 * 1024 * 1024 * 1024   # 10 GB
DEFAULT_BYTES_OUT_PER_MONTH = 10 * 1024 * 1024 * 1024   # 10 GB
MAX_EXPERIMENTS_PER_USER    = 5

# ── Background job intervals (seconds) ───────────────────────────────────────
BG_TRAFFIC_POLL_INTERVAL   = 60
BG_USAGE_AGG_INTERVAL      = 300    # 5 min   – keeps MonthlyUsage fresh
BG_DAILY_AGG_INTERVAL      = 300    # 5 min   – upserts today + finalises yesterday
BG_IDLE_CHECK_INTERVAL     = 600    # 10 min
BG_TOKEN_PRUNE_INTERVAL    = 3600   # 1 hr
BG_SNAPSHOT_PRUNE_INTERVAL = 86400  # 24 hr
