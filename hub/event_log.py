"""Append-only user activity log for the Hub dashboard."""

from __future__ import annotations

import json
import logging
from typing import Any

from .db import db
from .models import UserEvent

log = logging.getLogger(__name__)

# event_type → timeline CSS suffix (hub.css .timeline-item.tl-*)
EVENT_CSS_CLASS: dict[str, str] = {
    "experiment_created": "tl-done",
    "experiment_deleted": "tl-deleted",
    "experiment_extended": "tl-restored",
    "experiment_expired": "tl-deleted",
    "experiment_idle_warning": "tl-status",
    "server_online": "tl-done",
    "server_offline": "tl-aborted",
    "pin_updated": "tl-params",
    "quota_warning": "tl-status",
    "quota_exceeded": "tl-aborted",
    "extension_submitted": "tl-status",
    "extension_approved": "tl-done",
    "extension_declined": "tl-aborted",
    "api_key_created": "tl-params",
    "api_key_deleted": "tl-deleted",
}

DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 100


def log_event(
    user_id: int,
    event_type: str,
    message: str,
    *,
    experiment_id: int | None = None,
    experiment_name: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record a user-visible event. Failures are logged but never raise."""
    try:
        meta_json = json.dumps(metadata) if metadata else None
        row = UserEvent(
            user_id=user_id,
            experiment_id=experiment_id,
            experiment_name=experiment_name,
            event_type=event_type,
            message=message,
            metadata_json=meta_json,
        )
        db.session.add(row)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        log.warning("Failed to log event %s for user %s: %s", event_type, user_id, exc)


def get_events_page(
    user_id: int,
    page: int = 0,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """Paginated events for a user, newest first."""
    page = max(0, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))

    q = UserEvent.query.filter_by(user_id=user_id).order_by(
        UserEvent.created_at.desc(),
        UserEvent.id.desc(),
    )
    total = q.count()
    total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
    if page >= total_pages and total > 0:
        page = total_pages - 1

    rows = q.offset(page * page_size).limit(page_size).all()
    entries = []
    for ev in rows:
        meta = None
        if ev.metadata_json:
            try:
                meta = json.loads(ev.metadata_json)
            except json.JSONDecodeError:
                meta = None
        created = ev.created_at
        iso_ts = f"{created.isoformat()}Z" if created else None
        entries.append({
            "id": ev.id,
            "event_type": ev.event_type,
            "message": ev.message,
            "experiment_name": ev.experiment_name,
            "css_class": EVENT_CSS_CLASS.get(ev.event_type, "tl-status"),
            "created_at": iso_ts,
            "metadata": meta,
        })

    return {
        "entries": entries,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }
