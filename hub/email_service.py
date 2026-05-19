"""
Email sending via Brevo REST API with deduplication.
"""
import logging
from datetime import datetime, timezone

import requests

from . import config
from .db import db
from .models import EmailNotification

log = logging.getLogger(__name__)

_BREVO_SEND_URL = "https://api.brevo.com/v3/smtp/email"


def _already_sent(user_id: int, notification_type: str) -> bool:
    return db.session.execute(
        db.select(EmailNotification).filter_by(
            user_id=user_id, notification_type=notification_type
        )
    ).scalar_one_or_none() is not None


def _mark_sent(user_id: int, notification_type: str) -> None:
    row = EmailNotification(user_id=user_id, notification_type=notification_type)
    db.session.merge(row)
    db.session.commit()


def send_email(to_email: str, subject: str, html: str) -> bool:
    """Send an email via Brevo. Returns True on success."""
    if not config.BREVO_API_KEY:
        log.warning("BREVO_API_KEY not configured — email not sent: %s", subject)
        return False
    payload = {
        "sender":      {"name": config.BREVO_FROM_NAME, "email": config.BREVO_FROM_EMAIL},
        "to":          [{"email": to_email}],
        "subject":     subject,
        "htmlContent": html,
    }
    try:
        r = requests.post(
            _BREVO_SEND_URL,
            json=payload,
            headers={"api-key": config.BREVO_API_KEY, "Content-Type": "application/json"},
            timeout=15,
        )
        if r.status_code in (200, 201):
            return True
        log.error("Brevo error %s: %s", r.status_code, r.text[:300])
        return False
    except Exception as exc:
        log.exception("Email send error: %s", exc)
        return False


def send_once(user_id: int, notification_type: str,
              to_email: str, subject: str, html: str) -> bool:
    """Send email only if this notification_type hasn't been sent before."""
    if _already_sent(user_id, notification_type):
        return False
    ok = send_email(to_email, subject, html)
    if ok:
        _mark_sent(user_id, notification_type)
    return ok


# ── Specific email builders ───────────────────────────────────────────────────

def send_verification(user_email: str, user_id: int, token: str) -> bool:
    link = f"{config.HUB_BASE_URL}/verify?token={token}"
    html = f"""
    <h2>Welcome to JobDistributor!</h2>
    <p>Please verify your email address by clicking the link below:</p>
    <p><a href="{link}" style="background:#2563eb;color:#fff;padding:10px 20px;
       border-radius:6px;text-decoration:none;">Verify Email</a></p>
    <p>This link expires in 24 hours.</p>
    <p>If you did not sign up, ignore this email.</p>
    """
    return send_once(user_id, f"verify_{user_id}", user_email,
                     "Verify your JobDistributor account", html)


def send_password_reset(user_email: str, user_id: int, token: str, date_str: str) -> bool:
    link = f"{config.HUB_BASE_URL}/reset-password?token={token}"
    html = f"""
    <h2>Password Reset</h2>
    <p>Click the link below to reset your password. It expires in 1 hour.</p>
    <p><a href="{link}" style="background:#2563eb;color:#fff;padding:10px 20px;
       border-radius:6px;text-decoration:none;">Reset Password</a></p>
    <p>If you did not request this, ignore this email.</p>
    """
    return send_once(user_id, f"password_reset_{user_id}_{date_str}", user_email,
                     "Reset your JobDistributor password", html)


def send_quota_warning(user_email: str, user_id: int,
                       direction: str, pct: int, year: int, month: int) -> bool:
    """direction is 'in' or 'out'. pct is 80, 95, or 100."""
    notif_type = f"quota_{pct}_{direction}_{year}_{month:02d}"
    if pct == 100:
        subject = f"Data limit reached — {'uploads' if direction == 'in' else 'downloads'} blocked"
        msg = (f"You have reached your monthly data {'upload' if direction == 'in' else 'download'} "
               f"limit for {year}-{month:02d}. "
               f"New {'uploads' if direction == 'in' else 'downloads'} are suspended until "
               f"the 1st of next month or until you request an extension.")
    else:
        subject = f"Data limit warning ({pct}%)"
        msg = (f"You have used {pct}% of your monthly data "
               f"{'upload' if direction == 'in' else 'download'} limit for {year}-{month:02d}.")
    html = f"""
    <h2>{subject}</h2>
    <p>{msg}</p>
    <p><a href="{config.HUB_BASE_URL}/extensions">Request a limit extension</a></p>
    """
    return send_once(user_id, notif_type, user_email, subject, html)


def send_idle_warning(user_email: str, user_id: int, exp_name: str) -> bool:
    html = f"""
    <h2>Experiment tunnel expiring soon</h2>
    <p>Your experiment <strong>{exp_name}</strong> has been idle for 5 days and its
    public tunnel will close in <strong>2 days</strong>.</p>
    <p>To keep it alive, open your dashboard and click <em>Extend</em>:</p>
    <p><a href="{config.HUB_BASE_URL}/experiments/{exp_name}">Extend experiment</a></p>
    """
    return send_once(user_id, f"idle_warn_{exp_name}", user_email,
                     f"Experiment '{exp_name}' tunnel expiring in 2 days", html)


def send_expired(user_email: str, user_id: int, exp_name: str) -> bool:
    html = f"""
    <h2>Experiment tunnel closed</h2>
    <p>Your experiment <strong>{exp_name}</strong> has expired after 7 days of inactivity.
    Its FRP tunnel has been closed.</p>
    <p>You can re-register the experiment from the Hub dashboard.</p>
    """
    return send_once(user_id, f"expired_{exp_name}", user_email,
                     f"Experiment '{exp_name}' has expired", html)


def send_extension_result(user_email: str, user_id: int,
                          req_id: int, approved: bool, note: str = "") -> bool:
    if approved:
        subject = "Your data limit extension was approved"
        body    = "Great news — your data limit extension request has been approved."
    else:
        subject = "Your data limit extension request was declined"
        body    = "Unfortunately your data limit extension request was not approved."
    html = f"""
    <h2>{subject}</h2>
    <p>{body}</p>
    {"<p><strong>Admin note:</strong> " + note + "</p>" if note else ""}
    <p><a href="{config.HUB_BASE_URL}/extensions">View your requests</a></p>
    """
    return send_once(user_id, f"ext_{'approved' if approved else 'declined'}_{req_id}",
                     user_email, subject, html)
