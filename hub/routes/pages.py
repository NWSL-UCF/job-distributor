"""
Public pages — no authentication required.
"""
from flask import Blueprint, render_template

from ..decorators import get_current_user

pages_bp = Blueprint("pages", __name__)


@pages_bp.route("/tutorials")
def tutorials():
    """Public tutorial page — accessible without logging in."""
    user = get_current_user()   # None when not logged in; shows full nav when logged in
    return render_template("tutorials.html", user=user)
