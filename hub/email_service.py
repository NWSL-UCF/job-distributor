"""
Email sending via Brevo REST API with deduplication.
"""
import logging
from datetime import datetime, timezone
from urllib.parse import quote

import requests

from . import config
from .db import db
from .models import EmailNotification

log = logging.getLogger(__name__)

_BREVO_SEND_URL = "https://api.brevo.com/v3/smtp/email"


def _email_header() -> str:
    logo_url = f"{config.HUB_BASE_URL}/static/favicon-192.png"
    return f"""
    <div style="font-family:Arial,sans-serif; max-width:520px; margin:0 auto;">
    <div style="padding:20px 0 12px 0; border-bottom:1px solid #e9ecef; margin-bottom:20px;">
      <img src="{logo_url}" alt="JobDistributor" width="32" height="32"
           style="vertical-align:middle; border-radius:6px; margin-right:10px;">
      <span style="font-size:1.05rem; font-weight:700; color:#1a1f2e;
                   vertical-align:middle;">JobDistributor</span>
    </div>
    """


def _email_footer() -> str:
    return """
    <p style="margin-top:28px; font-size:0.78em; color:#adb5bd; border-top:1px solid #e9ecef; padding-top:12px;">
      University of Central Florida &mdash; Networking and Wireless System Lab (NWSL)
    </p>
    </div>
    """


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

def send_verification_otp(user_email: str, otp: str) -> bool:
    verify_url = f"{config.HUB_BASE_URL}/verify-email?email={quote(user_email)}"
    html = _email_header() + f"""
    <h2>Welcome to JobDistributor!</h2>
    <p>Your email verification code is:</p>
    <p style="font-size:28px;font-weight:700;letter-spacing:6px;margin:16px 0;">{otp}</p>
    <p>Enter this code on the verification page. It expires in
       {config.OTP_VERIFY_EXPIRE_MINUTES} minutes.</p>
    <p><a href="{verify_url}">Open verification page</a></p>
    <p>If you did not sign up, ignore this email.</p>
    """ + _email_footer()
    return send_email(user_email, "Your JobDistributor verification code", html)


def send_password_reset_otp(user_email: str, otp: str) -> bool:
    reset_url = f"{config.HUB_BASE_URL}/reset-password?email={quote(user_email)}"
    html = _email_header() + f"""
    <h2>Password reset</h2>
    <p>Your password reset code is:</p>
    <p style="font-size:28px;font-weight:700;letter-spacing:6px;margin:16px 0;">{otp}</p>
    <p>Enter this code on the reset password page. It expires in
       {config.OTP_RESET_EXPIRE_MINUTES} minutes.</p>
    <p><a href="{reset_url}">Open reset password page</a></p>
    <p>If you did not request this, ignore this email.</p>
    """ + _email_footer()
    return send_email(user_email, "Your JobDistributor password reset code", html)


def send_password_change_otp(user_email: str, otp: str) -> bool:
    html = _email_header() + f"""
    <h2>Password change confirmation</h2>
    <p>Your verification code to change your password is:</p>
    <p style="font-size:28px;font-weight:700;letter-spacing:6px;margin:16px 0;">{otp}</p>
    <p>Enter this code on the password change page. It expires in
       {config.OTP_RESET_EXPIRE_MINUTES} minutes.</p>
    <p>If you did not request this, your account may be at risk — change your password immediately.</p>
    """ + _email_footer()
    return send_email(user_email, "Your JobDistributor password change code", html)


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
    html = _email_header() + f"""
    <h2>{subject}</h2>
    <p>{msg}</p>
    <p><a href="{config.HUB_BASE_URL}/extensions">Request a limit extension</a></p>
    """ + _email_footer()
    return send_once(user_id, notif_type, user_email, subject, html)


def send_idle_warning(user_email: str, user_id: int, exp_name: str) -> bool:
    html = _email_header() + f"""
    <h2>Experiment expiring soon</h2>
    <p>Your experiment <strong>{exp_name}</strong> has been idle for 5 days and will
    expire in <strong>2 days</strong>.</p>
    <p><a href="{config.HUB_BASE_URL}/experiments/{exp_name}">Extend experiment</a></p>
    """ + _email_footer()
    return send_once(user_id, f"idle_warn_{exp_name}", user_email,
                     f"Experiment '{exp_name}' expiring in 2 days", html)


def send_expired(user_email: str, user_id: int, exp_name: str) -> bool:
    html = _email_header() + f"""
    <h2>Experiment expired</h2>
    <p>Your experiment <strong>{exp_name}</strong> has expired after 7 days of inactivity.</p>
    <p>You can create a new experiment from the Hub dashboard.</p>
    """ + _email_footer()
    return send_once(user_id, f"expired_{exp_name}", user_email,
                     f"Experiment '{exp_name}' has expired", html)


def send_experiment_created(user_email: str, exp_name: str) -> bool:
    dashboard_url = f"{config.HUB_BASE_URL}/experiments/{exp_name}"
    html = _email_header() + f"""
    <h2>Experiment created — <em>{exp_name}</em></h2>
    <p>Your new experiment <strong>{exp_name}</strong> is ready.</p>
    <p>Start your server with:</p>
    <pre style="background:#f4f4f4; padding:10px; border-radius:4px;
                font-family:monospace; font-size:0.88em; display:inline-block;">JD_API_KEY=&lt;your-key&gt; ./run.sh {exp_name}</pre>
    <p style="margin:12px 0;">
      <a href="{dashboard_url}"
         style="padding:9px 18px; background:#007bff; color:#fff;
                text-decoration:none; border-radius:4px;">View Experiment</a>
    </p>
    """ + _email_footer()
    return send_email(user_email, f"[JobDistributor] Experiment created — {exp_name}", html)


def send_experiment_deleted(user_email: str, exp_name: str) -> bool:
    html = _email_header() + f"""
    <h2>Experiment deleted — <em>{exp_name}</em></h2>
    <p>Your experiment <strong>{exp_name}</strong> has been deleted from the Hub.</p>
    <p>If you have a server container still running on your machine, stop it with:</p>
    <pre style="background:#f4f4f4; padding:10px; border-radius:4px;
                font-family:monospace; font-size:0.88em; display:inline-block;">docker stop jd-{exp_name}</pre>
    """ + _email_footer()
    return send_email(user_email, f"[JobDistributor] Experiment deleted — {exp_name}", html)


def send_server_connected(user_email: str, exp_name: str) -> bool:
    server_dashboard_url = f"https://{exp_name}-dashboard.{config.JD_BASE_DOMAIN}"
    html = _email_header() + f"""
    <p>&#x1F7E2; Your server for experiment <strong>{exp_name}</strong> is now
    <strong>online</strong> and ready to accept workers.</p>
    <p style="margin:12px 0;">
      <a href="{server_dashboard_url}"
         style="padding:9px 18px; background:#007bff; color:#fff;
                text-decoration:none; border-radius:4px;">Open Server Dashboard</a>
    </p>
    """ + _email_footer()
    return send_email(user_email, f"[JobDistributor] Server online — {exp_name}", html)


def send_server_disconnected(user_email: str, exp_name: str) -> bool:
    html = _email_header() + f"""
    <p>&#x1F534; Your server for experiment <strong>{exp_name}</strong> has gone
    <strong>offline</strong>.</p>
    <p>Check the container logs on your machine:</p>
    <pre style="background:#f4f4f4; padding:10px; border-radius:4px;
                font-family:monospace; font-size:0.88em; display:inline-block;">docker logs jd-{exp_name}</pre>
    <p>To bring it back online:</p>
    <pre style="background:#f4f4f4; padding:10px; border-radius:4px;
                font-family:monospace; font-size:0.88em; display:inline-block;">JD_API_KEY=&lt;your-key&gt; ./run.sh {exp_name}</pre>
    """ + _email_footer()
    return send_email(user_email, f"[JobDistributor] Server offline — {exp_name}", html)


def send_extension_result(user_email: str, user_id: int,
                          req_id: int, approved: bool, note: str = "") -> bool:
    if approved:
        subject = "Your data limit extension was approved"
        body    = "Great news — your data limit extension request has been approved."
    else:
        subject = "Your data limit extension request was declined"
        body    = "Unfortunately your data limit extension request was not approved."
    html = _email_header() + f"""
    <h2>{subject}</h2>
    <p>{body}</p>
    {"<p><strong>Admin note:</strong> " + note + "</p>" if note else ""}
    <p><a href="{config.HUB_BASE_URL}/extensions">View your requests</a></p>
    """ + _email_footer()
    return send_once(user_id, f"ext_{'approved' if approved else 'declined'}_{req_id}",
                     user_email, subject, html)
