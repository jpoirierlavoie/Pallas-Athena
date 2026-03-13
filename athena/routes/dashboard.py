"""Dashboard route — placeholder for Phase 1."""

from flask import Blueprint, render_template

from auth import login_required

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
@login_required
def index() -> str:
    """Render the main dashboard."""
    return render_template("dashboard/index.html")
