"""Portal blueprint routes (spec L1 §6-7).

Every non-exempt route passes the per-request guard: session present AND the
invitation document re-read from the « portail » database — statut active,
non-expired, type matching. That re-read is what makes revocation instant
without any Firebase token machinery (§6.5).

Error messages are GENERIC on purpose (« Invitation invalide ou expirée »)
— never the precise cause (anti-enumeration, §6.3).
"""

import logging
from datetime import datetime, timezone

from flask import (
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from google.api_core.exceptions import PreconditionFailed

from client import limiter, portail_bp
from client.config import (
    CHUNK_MIB,
    PORTAIL_EXTENSIONS,
    PORTAIL_MAX_FILE_MB,
    PORTAIL_MAX_FILES,
    PORTAIL_MAX_TOTAL_MB,
)
from client.services import invitations, stockage, taches
from utils.logging_setup import log_portail_event
from utils.validators import normalize_email

logger = logging.getLogger(__name__)

_MIB = 1024 * 1024
_REPONSE_RENVOI = (
    "Si l'invitation est valide, un nouveau lien vient d'être transmis."
)

# Endpoints reachable WITHOUT a session (spec §6.5). Everything else goes
# through the guard below.
_EXEMPT_ENDPOINTS = {
    "portail.racine",
    "portail.entree",
    "portail.creer_session",
    "portail.api_renvoi",
    "portail.sante",
}


# ── Per-request guard (§6.5) ─────────────────────────────────────────────


def _refus(reason: str):
    log_portail_event(
        "session_refusee", "refused",
        invitation_id=session.get("inv_id"), reason=reason,
    )
    session.clear()
    if request.path.startswith("/api/"):
        return jsonify({"erreur": "Invitation invalide ou expirée."}), 401
    return redirect(url_for("portail.entree"))


@portail_bp.before_request
def _garde():
    if request.endpoint in _EXEMPT_ENDPOINTS or request.endpoint is None:
        return None
    inv_id = session.get("inv_id")
    if not inv_id:
        return _refus("no_session")
    invitation = invitations.lire(inv_id)
    if invitation is None:
        return _refus("not_found")
    # L1 serves only the « documents » flow; an « intake » invitation (L3)
    # has no business on these routes.
    if invitation.get("type") != "documents":
        return _refus("type_mismatch")
    if request.endpoint == "portail.confirmation":
        # The submission is already acquired — show the confirmation even
        # once the statut moved to « soumise », but never after revocation.
        if invitation.get("statut") in ("révoquée", "refusée"):
            return _refus("inactive")
    elif not invitations.est_active(invitation):
        return _refus("inactive")
    g.invitation = invitation
    return None


# ── Public pages ─────────────────────────────────────────────────────────


@portail_bp.get("/")
def racine():
    return redirect(url_for("portail.entree"))


@portail_bp.get("/entree")
def entree():
    return render_template(
        "entree.html", invitation_id=request.args.get("i", "")
    )


@portail_bp.get("/sante")
def sante():
    return jsonify({"statut": "ok"})


# ── Session (§6.4) ───────────────────────────────────────────────────────


def _session_refusee(reason: str):
    log_portail_event("session_refusee", "refused", reason=reason)
    return jsonify({"erreur": "Invitation invalide ou expirée."}), 403


@portail_bp.post("/session")
@limiter.limit("10 per minute")
def creer_session():
    donnees = request.get_json(silent=True) or {}
    token = donnees.get("token") or ""
    inv_id = (donnees.get("i") or "").strip()
    if not token or not inv_id:
        return _session_refusee("missing_fields")

    try:
        from firebase_admin import auth as fb_auth

        # NO check_revoked (spec §3): authorization freshness comes from the
        # invitation document, re-read on every request — not from Firebase
        # revocation machinery.
        decoded = fb_auth.verify_id_token(token, clock_skew_seconds=10)
    except Exception:
        return _session_refusee("token_invalid")

    if decoded.get("portail") is not True:
        return _session_refusee("claim_missing")
    if decoded.get("email_verified") is not True:
        return _session_refusee("email_unverified")

    invitation = invitations.lire(inv_id)
    if invitation is None or invitation.get("statut") not in invitations.ACTIVE_STATUTS:
        return _session_refusee("inactive")
    if invitations.est_expiree(invitation):
        return _session_refusee("expired")
    if invitation.get("email") != (decoded.get("email") or "").strip().lower():
        return _session_refusee("email_mismatch")

    session.clear()
    session.permanent = True
    session["inv_id"] = inv_id
    session["uid"] = decoded["uid"]
    session["email"] = invitation["email"]
    log_portail_event("session_creee", invitation_id=inv_id)

    if invitation.get("statut") == "envoyée":
        try:
            taches.signaler("ouverte", inv_id)
        except Exception:
            logger.exception("portal enqueue 'ouverte' failed")
            log_portail_event(
                "tache_enfilage_echec", "failure",
                invitation_id=inv_id, evenement="ouverte",
            )
    return jsonify({"ok": True, "suivant": url_for("portail.page_documents")})


# ── Renvoi (§6.3 — anti-enumeration) ─────────────────────────────────────


@portail_bp.post("/api/renvoi")
@limiter.limit("5 per hour")
def api_renvoi():
    donnees = request.get_json(silent=True) or {}
    email = normalize_email(donnees.get("courriel") or "")
    inv_id = (donnees.get("i") or "").strip()

    cible = None
    if email and inv_id:
        inv = invitations.lire(inv_id)
        if inv and inv.get("email") == email and invitations.est_active(inv):
            cible = inv
    elif email:
        actives = [
            i for i in invitations.chercher_par_email(email)
            if invitations.est_active(i) and i.get("created_at")
        ]
        if actives:
            cible = max(actives, key=lambda i: i["created_at"])

    if cible is not None:
        try:
            taches.signaler("renvoi", cible["id"])
        except Exception:
            # Renvoi is not reconstructible from the bucket (§7.4 note): an
            # enqueue failure just means no email — the client re-clicks.
            logger.exception("portal enqueue 'renvoi' failed")
            log_portail_event(
                "tache_enfilage_echec", "failure",
                invitation_id=cible["id"], evenement="renvoi",
            )

    # ALWAYS the identical response, valid or not — anti-enumeration.
    return jsonify({"message": _REPONSE_RENVOI})


# ── Transmission (§7) ────────────────────────────────────────────────────


@portail_bp.get("/documents")
def page_documents():
    invitation = g.invitation
    files_count = session.get("files_count", 0)
    quota_files = int(invitation.get("quota_files") or PORTAIL_MAX_FILES)
    return render_template(
        "documents.html",
        invitation=invitation,
        max_file_mb=PORTAIL_MAX_FILE_MB,
        chunk_mib=CHUNK_MIB,
        fichiers_restants=max(quota_files - files_count, 0),
    )


def _content_type_propre(valeur) -> str:
    ct = "".join(c for c in str(valeur or "") if 32 <= ord(c) < 127)[:100]
    return ct or "application/octet-stream"


@portail_bp.post("/api/televersement")
def api_televersement():
    invitation = g.invitation
    donnees = request.get_json(silent=True) or {}
    nom = str(donnees.get("name") or "")
    content_type = _content_type_propre(donnees.get("content_type"))
    try:
        size = int(donnees.get("size"))
    except (TypeError, ValueError):
        size = -1

    ext = nom.rsplit(".", 1)[-1].lower() if "." in nom else ""
    if not nom or ext not in PORTAIL_EXTENSIONS:
        log_portail_event(
            "televersement_rejete", "refused",
            invitation_id=invitation["id"], reason="extension",
        )
        return jsonify({
            "erreur": "Type de fichier non admis. Formats acceptés : PDF, "
                      "images (JPEG, PNG, HEIC, TIFF), documents Office, "
                      "texte, audio et vidéo."
        }), 422
    if size <= 0 or size > PORTAIL_MAX_FILE_MB * _MIB:
        log_portail_event(
            "televersement_rejete", "refused",
            invitation_id=invitation["id"], reason="taille",
        )
        return jsonify({
            "erreur": f"Chaque fichier doit faire entre 1 octet et "
                      f"{PORTAIL_MAX_FILE_MB} Mo."
        }), 422

    # Session-scoped quota counters (§7.2 — the portal cannot list the
    # bucket; a re-login resets them: accepted risk R-1, the hard per-file
    # cap is enforced by GCS itself via the session's declared size).
    quota_files = int(invitation.get("quota_files") or PORTAIL_MAX_FILES)
    quota_octets = int(invitation.get("quota_mb") or PORTAIL_MAX_TOTAL_MB) * _MIB
    files_count = session.get("files_count", 0)
    total_bytes = session.get("total_bytes", 0)
    if files_count >= quota_files:
        log_portail_event(
            "televersement_rejete", "refused",
            invitation_id=invitation["id"], reason="quota_files",
        )
        return jsonify({
            "erreur": "Nombre maximal de fichiers atteint pour cette transmission."
        }), 422
    if total_bytes + size > quota_octets:
        log_portail_event(
            "televersement_rejete", "refused",
            invitation_id=invitation["id"], reason="quota_volume",
        )
        return jsonify({
            "erreur": "Volume maximal de la transmission atteint."
        }), 422

    batch = session.get("batch")
    if not batch:
        batch = stockage.horodatage_utc_compact()
        session["batch"] = batch
    seq = int(session.get("seq", 0)) + 1
    session["seq"] = seq

    objet = (
        f"submissions/{invitation['id']}/{batch}/files/"
        f"{seq:03d}_{stockage.assainir_nom(nom)}"
    )
    try:
        url = stockage.ouvrir_session_reprenable(objet, content_type, size)
    except Exception:
        logger.exception("portal resumable-session open failed")
        return jsonify({
            "erreur": "Erreur lors de l'ouverture du téléversement. Réessayez."
        }), 503

    session["files_count"] = files_count + 1
    session["total_bytes"] = total_bytes + size
    log_portail_event(
        "televersement_ouvert",
        invitation_id=invitation["id"], batch=batch, taille=size,
    )
    return jsonify({"url": url, "objet": objet})


@portail_bp.post("/api/finaliser")
def api_finaliser():
    invitation = g.invitation
    inv_id = invitation["id"]
    batch = session.get("batch")
    if not batch:
        return jsonify({"erreur": "Aucune transmission en cours."}), 400

    donnees = request.get_json(silent=True) or {}
    fichiers = donnees.get("files")
    if not isinstance(fichiers, list) or not fichiers:
        return jsonify({"erreur": "Aucun fichier reçu."}), 400

    prefixe = f"submissions/{inv_id}/{batch}/files/"
    propres = []
    for f in fichiers:
        if not isinstance(f, dict):
            return jsonify({"erreur": "Requête invalide."}), 400
        objet = str(f.get("objet") or "")
        # The client only ever names objects of ITS OWN batch — anything
        # else is a forged payload.
        if not objet.startswith(prefixe):
            return jsonify({"erreur": "Requête invalide."}), 400
        try:
            size = max(int(f.get("size") or 0), 0)
        except (TypeError, ValueError):
            return jsonify({"erreur": "Requête invalide."}), 400
        propres.append({
            "objet": objet,
            # Nom d'origine INTÉGRAL (valeur probante, §4) — the sanitized
            # form lives in the object name only.
            "name": str(f.get("name") or "")[:500],
            "size": size,
            "content_type": _content_type_propre(f.get("content_type")),
        })

    envelope = {
        "type": "documents",
        "invitation_id": inv_id,
        "batch": batch,
        "dossier_id": invitation.get("dossier_id"),
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "client": {"email": session.get("email"), "uid": session.get("uid")},
        "http": {
            "ip": request.headers.get("CF-Connecting-IP")
            or request.remote_addr or "",
            "user_agent": (request.user_agent.string or "")[:300],
        },
        "files": propres,
    }
    try:
        stockage.ecrire_enveloppe(inv_id, batch, envelope)
    except PreconditionFailed:
        return jsonify({"erreur": "Transmission déjà soumise."}), 409
    except Exception:
        logger.exception("portal envelope write failed")
        return jsonify({
            "erreur": "Erreur lors de la soumission. Réessayez."
        }), 503

    # The envelope is written — the submission is ACQUIRED. An enqueue
    # failure must NOT fail the finalization (§7.4): the reconciliation cron
    # replays any envelope the queue lost.
    try:
        taches.signaler("soumise", inv_id, batch=batch)
    except Exception:
        logger.exception("portal enqueue 'soumise' failed")
        log_portail_event(
            "tache_enfilage_echec", "failure",
            invitation_id=inv_id, batch=batch, evenement="soumise",
        )

    for cle in ("batch", "seq", "files_count", "total_bytes"):
        session.pop(cle, None)
    log_portail_event(
        "soumission_finalisee",
        invitation_id=inv_id, batch=batch, files_count=len(propres),
    )
    return jsonify({"ok": True, "suivant": url_for("portail.confirmation")})


@portail_bp.get("/confirmation")
def confirmation():
    return render_template("confirmation.html", invitation=g.invitation)
