"""Authentication routes: login, token verification, logout, MFA setup/manage."""

from urllib.parse import urlparse

from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from auth import login_required, verify_and_create_session
from security import limiter

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


def _safe_next(url: str) -> str | None:
    """Return ``url`` only if it is a same-origin relative path."""
    if not url or "\\" in url or url.startswith("//"):
        return None
    parsed = urlparse(url)
    if parsed.scheme or parsed.netloc or not url.startswith("/"):
        return None
    return url


@auth_bp.route("/login")
def login() -> str:
    """Render the login page (or redirect if already authenticated)."""
    if session.get("user_id"):
        return redirect(url_for("dashboard.index"))

    return render_template("auth/login.html")


@auth_bp.route("/verify-token", methods=["POST"])
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_LOGIN", "5 per minute"))
def verify_token() -> tuple[Response, int]:
    """Verify the Firebase ID token POSTed by the client-side SDK.

    Returns a JSON-like response consumed by the login page JS.
    """
    id_token = request.form.get("id_token", "")
    if not id_token:
        return jsonify({"ok": False, "error": "Jeton manquant."}), 400

    success, error_msg = verify_and_create_session(id_token)
    if success:
        next_url = _safe_next(request.args.get("next", "")) or url_for("dashboard.index")
        return jsonify({"ok": True, "redirect": next_url}), 200

    return jsonify({"ok": False, "error": error_msg or "Accès non autorisé."}), 403


# Les deux pages MFA sont absorbées par « Paramètres → Sécurité »
# (2026-09-07). Les ROUTES et les NOMS d'endpoint survivent pour que tout
# `url_for` et tout signet tiennent — `/auth/mfa-manage` était dans la barre
# de navigation depuis des mois. **302, jamais 301** : un 301 est mis en
# cache indéfiniment par le navigateur et rendrait pénible toute future
# re-séparation.
#
# Ce qui disparaît avec `auth/mfa_manage.html` est un bouton de
# désinscription NU du seul facteur enregistré : sous `REQUIRE_MFA`, il
# annonçait « Vérification en deux étapes désactivée » pour l'acte de se
# verrouiller dehors, la session en cours continuant jusqu'à 12 h de sorte
# que la conséquence arrivait sans lien de cause visible.


@auth_bp.route("/mfa-setup")
@login_required
def mfa_setup() -> Response:
    """Redirige vers « Paramètres → Sécurité »."""
    return redirect(url_for("settings.securite"))


@auth_bp.route("/mfa-manage")
@login_required
def mfa_manage() -> Response:
    """Redirige vers « Paramètres → Sécurité »."""
    return redirect(url_for("settings.securite"))


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout() -> str:
    """Clear the session and redirect to login."""
    from utils.logging_setup import log_auth_event

    session.clear()
    log_auth_event("logout", "success")
    return redirect(url_for("auth.login"))
