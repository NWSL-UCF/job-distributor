"""User email notification preference categories."""

from __future__ import annotations

from .models import User

# Each category maps to a boolean column on users (notify_<key>).
NOTIFICATION_CATEGORIES = (
    {
        "key": "experiment_lifecycle",
        "label": "Experiment lifecycle",
        "description": (
            "Experiment created, deleted, extended, idle warnings, and expiration"
        ),
    },
    {
        "key": "server_status",
        "label": "Server status",
        "description": "Server goes online or offline",
    },
    {
        "key": "quota",
        "label": "Data quota",
        "description": "Monthly upload/download limit warnings (80%, 95%, 100%)",
    },
    {
        "key": "extensions",
        "label": "Limit extensions",
        "description": "Extension request approved or declined",
    },
)

# event_type → notification category (None = always email, not toggleable)
EVENT_EMAIL_CATEGORY: dict[str, str | None] = {
    "experiment_created": "experiment_lifecycle",
    "experiment_deleted": "experiment_lifecycle",
    "experiment_extended": "experiment_lifecycle",
    "experiment_expired": "experiment_lifecycle",
    "experiment_idle_warning": "experiment_lifecycle",
    "server_online": "server_status",
    "server_offline": "server_status",
    "pin_updated": None,
    "quota_warning": "quota",
    "quota_exceeded": "quota",
    "extension_submitted": None,
    "extension_approved": "extensions",
    "extension_declined": "extensions",
    "api_key_created": None,
    "api_key_deleted": None,
}

DEFAULT_PREFS = {cat["key"]: True for cat in NOTIFICATION_CATEGORIES}


def _pref_column(category: str) -> str:
    return f"notify_{category}"


def user_wants_email(user_id: int, event_type: str) -> bool:
    """Return True if the user wants email for this event type."""
    category = EVENT_EMAIL_CATEGORY.get(event_type)
    if category is None:
        return False
    user = User.query.get(user_id)
    if user is None:
        return True
    return bool(getattr(user, _pref_column(category), 1))


def get_user_prefs(user: User) -> dict[str, bool]:
    return {
        cat["key"]: bool(getattr(user, _pref_column(cat["key"]), 1))
        for cat in NOTIFICATION_CATEGORIES
    }


def update_user_prefs(user: User, form_data: dict) -> None:
    for cat in NOTIFICATION_CATEGORIES:
        key = cat["key"]
        setattr(user, _pref_column(key), 1 if form_data.get(key) == "on" else 0)
