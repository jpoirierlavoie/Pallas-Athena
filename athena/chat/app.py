"""La fabrique du service « chat » (Phase N) — worker seulement.

Un SEUL blueprint (le gestionnaire machine des tours) + le warmup. Aucune
route navigateur, donc **aucun CSRFProtect du tout** — plus strict que
l'exemption : il n'y a pas de formulaire dans ce processus. Pas de
limiter, pas d'App Check (il ne gate que le trafic ``HX-Request``, que
Cloud Tasks n'émet jamais).

Contrairement au portail, ce service tourne sous le **SA par défaut** avec
le bloc d'environnement du service principal : importer ``models``/
``config`` est légitime ici (le ban du portail tenait à son SA à moindre
privilège et à la résolution des secrets à l'import — voir
client/__init__.py). ``init_tracing`` avant ``init_logging``, l'ordre des
deux autres fabriques.

L'isolement de la carte des routes est épinglé par
``tests/test_chat_app.py`` (le motif ``test_portail_app``).
"""

from __future__ import annotations

import firebase_admin
from firebase_admin import credentials
from flask import Flask, jsonify

from config import Config


def create_chat_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = Config.SECRET_KEY
    app.config["MAX_CONTENT_LENGTH"] = Config.MAX_CONTENT_LENGTH

    from utils import logging_setup, tracing_setup

    tracing_setup.init_app(app)   # OTel middleware wraps the WSGI app first
    logging_setup.init_app(app)

    # Guarded like client/app.py: a credential-less boot (local tests, CI)
    # degrades instead of crashing — the Storage offload path then fails
    # loudly at first use, which is the honest place.
    try:
        if not firebase_admin._apps:
            firebase_admin.initialize_app(
                credentials.ApplicationDefault(),
                {"storageBucket": Config.FIREBASE_STORAGE_BUCKET},
            )
    except Exception:  # pragma: no cover — absent ADC in local/CI runs
        import logging

        logging.getLogger(__name__).warning(
            "firebase_admin not initialized (no ADC) — Storage offload "
            "unavailable in this process"
        )

    # The shared edge hooks, WITHOUT init_security: that helper also wires
    # CSRFProtect + the limiter, and this process must have NO CSRF object
    # at all (stricter than exempting — there is no browser form here, and
    # a CSRF layer would 400 every Cloud Tasks POST). Origin secret keeps
    # its is_appengine_internal_request bypass, which is what admits the
    # queue; the size cap and headers ride along unchanged.
    from security import (
        _add_security_headers,
        _enforce_origin_secret,
        _enforce_request_size,
    )

    app.before_request(_enforce_origin_secret)
    app.before_request(_enforce_request_size)
    app.after_request(_add_security_headers)

    from routes.taches_chat import taches_chat_bp

    app.register_blueprint(taches_chat_bp)  # THE ONLY blueprint

    @app.route("/_ah/warmup")
    def _warmup():  # pragma: no cover — trivial
        return "", 200

    # JSON-only error handlers: no templates exist in this process, and a
    # Cloud Tasks caller only reads the status code anyway.
    @app.errorhandler(404)
    def _not_found(_e):
        return jsonify({"erreur": "introuvable"}), 404

    @app.errorhandler(Exception)
    def _unexpected(exc):
        from werkzeug.exceptions import HTTPException

        if isinstance(exc, HTTPException):
            # abort(403) etc. must keep their own status — a blanket
            # Exception handler catches HTTPException too in Flask.
            return exc
        from utils.logging_setup import log_unexpected

        log_unexpected("chat service unhandled exception")
        return jsonify({"erreur": "erreur interne"}), 500

    return app
