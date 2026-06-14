"""
Public pages — no authentication required.
"""
from flask import Blueprint, redirect, render_template, url_for

from ..decorators import get_current_user

pages_bp = Blueprint("pages", __name__)


def _user():
    return get_current_user()   # None when not logged in


@pages_bp.route("/learn/quick-start")
def quick_start():
    return render_template("learn_quick_start.html", user=_user())


@pages_bp.route("/learn/getting-started")
def getting_started():
    return render_template("learn_getting_started.html", user=_user())


@pages_bp.route("/learn/library")
def library_reference():
    return render_template("learn_library.html", user=_user())


@pages_bp.route("/learn/server-dashboard")
def server_dashboard_guide():
    return render_template("learn_server_dashboard.html", user=_user())


@pages_bp.route("/learn/checkpoints")
def checkpoints_guide():
    return render_template("learn_checkpoints.html", user=_user())


@pages_bp.route("/learn/job-management-library")
def job_management_library():
    return render_template("learn_job_management_library.html", user=_user())


# Legacy redirect — keeps old links working
@pages_bp.route("/tutorials")
def tutorials():
    return redirect(url_for("pages.getting_started"), code=301)
