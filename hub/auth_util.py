"""Shared helpers for web login redirects and session cookies."""
from urllib.parse import urlparse

from flask import request, url_for

from . import config


def session_cookie_secure() -> bool:
    """
    Whether hub_session should be a Secure cookie.

    - SESSION_COOKIE_SECURE=1|0 in .env forces the value.
    - Otherwise follows the current request (HTTPS or X-Forwarded-Proto via ProxyFix).
    """
    explicit = config.SESSION_COOKIE_SECURE
    if explicit.lower() in ("1", "true", "yes"):
        return True
    if explicit.lower() in ("0", "false", "no"):
        return False
    return request.is_secure


def login_next_param() -> str:
    """Relative path (+ query) to return to after login (for ?next=)."""
    if request.query_string:
        return request.path + "?" + request.query_string.decode()
    return request.path or "/"


def safe_next_url(raw: str | None) -> str:
    """Validate post-login redirect target; block open redirects."""
    default = url_for("dashboard.index")
    if not raw or not str(raw).strip():
        return default
    raw = str(raw).strip()
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    parsed = urlparse(raw)
    host = (request.host or "").lower()
    if parsed.netloc and parsed.netloc.lower() != host:
        return default
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return path
