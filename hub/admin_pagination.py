"""Shared pagination helpers for admin list views."""

from __future__ import annotations

from typing import Any

from flask_sqlalchemy.query import Query as SAQuery

DEFAULT_ADMIN_PAGE_SIZE = 20
MAX_ADMIN_PAGE_SIZE = 100
MAX_SEARCH_LEN = 200


def search_term(raw: str | None) -> str:
    """Normalized ?q= value from the request (empty string if absent)."""
    if not raw:
        return ""
    return raw.strip()[:MAX_SEARCH_LEN]


def like_pattern(term: str) -> str | None:
    """SQL LIKE pattern for case-insensitive substring match."""
    if not term:
        return None
    escaped = (
        term.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def paginate_query(
    query: SAQuery,
    page: int = 0,
    page_size: int = DEFAULT_ADMIN_PAGE_SIZE,
) -> dict[str, Any]:
    """Return a page of ORM rows plus pagination metadata (0-based page index)."""
    page = max(0, page)
    page_size = max(1, min(page_size, MAX_ADMIN_PAGE_SIZE))

    total = query.count()
    total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
    if page >= total_pages and total > 0:
        page = total_pages - 1

    items = query.offset(page * page_size).limit(page_size).all()
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }
