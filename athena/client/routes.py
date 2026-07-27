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
    STATUTS_FERMES,
)
from client.services import invitations, stockage, taches
from utils.logging_setup import log_portail_event
from utils.validators import normalize_email

logger = logging.getLogger(__name__)

_MIB = 1024 * 1024
# Réponse CONSTANTE de /api/renvoi — identique que l'invitation existe ou non,
# soit active, ou ait épuisé son plafond de renvois (§6.3 anti-énumération).
# Elle porte le canal de secours (D-5) précisément parce qu'elle ne peut PAS
# dire au client que son plafond est atteint : sans cela, il attendrait un
# courriel qui n'arrivera jamais.
_REPONSE_RENVOI = (
    "Si l'invitation est valide, un nouveau lien vient d'être transmis. "
    "Si vous ne recevez rien, joignez-nous au (514) 737-2525."
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


# Statuts d'où l'on ne revient jamais — DÉFINIS DANS client/config.py, parce
# que le service principal en dépend aussi (``ajouter_soumission``). « traitée »
# en fait partie : ses fichiers de quarantaine sont purgés, donc une
# finalisation tardive écrirait dans un lot qui n'existe plus — et pire, elle
# rappellerait ``ajouter_soumission``, qui remet le statut à « soumise », un
# état d'où le client peut de nouveau téléverser. Le lot resté en vol que la
# dérogation de _garde protégeait est couvert autrement : l'alerte
# ``lot_abandonne`` de la réconciliation le rend visible.
_STATUTS_TERMINAUX = STATUTS_FERMES


def _refus(reason: str):
    log_portail_event(
        "session_refusee", "refused",
        invitation_id=session.get("inv_id"), reason=reason,
    )
    # Pop the IDENTITY keys rather than session.clear(): clearing also drops
    # flask-wtf's CSRF secret, so the client's next POST failed CSRF and
    # stacked a second, contradictory error (« Session invalide. Rechargez la
    # page. ») on top of the first. Popping these IS the logout — _garde still
    # denies at `if not inv_id`.
    for cle in ("inv_id", "uid", "email", "batch", "seq",
                "files_count", "total_bytes"):
        session.pop(cle, None)
    if request.path.startswith("/api/"):
        return jsonify({"erreur": "Invitation invalide ou expirée."}), 401
    return redirect(url_for("portail.entree"))


def _indisponible():
    """A read failed — deny WITHOUT destroying the session (fail closed, but
    not destructively: the invitation is re-read on the next request)."""
    log_portail_event(
        "session_refusee", "failure",
        invitation_id=session.get("inv_id"), reason="lecture_indisponible",
    )
    if request.path.startswith("/api/"):
        return jsonify({
            "erreur": "Service momentanément indisponible. "
                      "Réessayez dans un instant."
        }), 503
    return render_template("erreur.html", message=(
        "Service momentanément indisponible. Réessayez dans un instant."
    )), 503


@portail_bp.before_request
def _garde():
    if request.endpoint in _EXEMPT_ENDPOINTS or request.endpoint is None:
        return None
    inv_id = session.get("inv_id")
    if not inv_id:
        return _refus("no_session")
    try:
        invitation = invitations.lire(inv_id)
    except invitations.LectureIndisponible:
        return _indisponible()
    if invitation is None:
        return _refus("not_found")
    # L1 serves only the « documents » flow; an « intake » invitation (L3)
    # has no business on these routes.
    if invitation.get("type") != "documents":
        return _refus("type_mismatch")
    if request.endpoint in ("portail.confirmation", "portail.api_finaliser"):
        # The bytes are ALREADY in GCS and envelope.json is the first durable
        # record: refusing finalisation here would orphan the whole batch
        # (reconciliation skips envelope-less prefixes, so it is purged at 90
        # days, unseen). Accept any NON-TERMINAL statut — the declared objects
        # are already pinned to the caller's own batch prefix in api_finaliser.
        if invitation.get("statut") in _STATUTS_TERMINAUX:
            return _refus("inactive")
    elif not invitations.peut_televerser(invitation):
        return _refus("inactive")
    g.invitation = invitation
    return None


# ── Public pages ─────────────────────────────────────────────────────────


@portail_bp.get("/")
def racine():
    return redirect(url_for("portail.entree"))


@portail_bp.get("/entree")
def entree():
    # « J'ai fermé le navigateur et je suis revenu » / a second click on the
    # email: the client may ALREADY hold a valid pa_portail cookie, in which
    # case sending them to a sign-in they cannot complete (the link is
    # single-use) is a pure dead end. Validate their OWN session here and let
    # them straight through. No new access: /documents re-reads the invitation
    # through _garde anyway, so revocation and expiry stay instant.
    demande = (request.args.get("i") or "").strip()
    inv_id = session.get("inv_id")
    # …but ONLY for its own invitation. Every invitation email points at
    # /entree?i={id} (the Firebase link AND the fallback URL), so a « ?i= »
    # naming a DIFFERENT invitation is the ordinary arrival of a SECOND client
    # on a shared browser — or of the same client's next invitation. Reusing
    # the cookie there would drop that visitor inside the previous holder's
    # session: their files would be written under the other invitation's
    # prefix and the accusé (names, sizes, SHA-512) mailed to the other
    # client. _garde proves the INVITATION is live, never that the VISITOR is
    # its invitee. Still no session.clear(): a foreign URL must not kill an
    # upload in progress — the successful POST /session clears it properly.
    if inv_id and demande in ("", inv_id):
        try:
            invitation = invitations.lire(inv_id)
        except invitations.LectureIndisponible:
            invitation = None  # render the page normally; never clear
        if invitation is not None and invitation.get("type") == "documents":
            if invitations.peut_televerser(invitation):
                return redirect(url_for("portail.page_documents"))
            # Only a lot that was really SUBMITTED may see the confirmation —
            # it states « Transmission reçue ». An EXPIRED invitation still
            # reads « envoyée »/« ouverte », so a statut-only test sent it
            # there: a false receipt, and a closed loop (/ → /entree →
            # /confirmation) with no way back to « Demander un nouveau lien »
            # — the one case where a new link IS the answer.
            if invitation.get("statut") == "soumise":
                return redirect(url_for("portail.confirmation"))
    return render_template("entree.html", invitation_id=demande)


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

    try:
        invitation = invitations.lire(inv_id)
    except invitations.LectureIndisponible:
        # The single-use oobCode is ALREADY spent by the time this POST lands,
        # so answering « Invitation invalide ou expirée » on a transient
        # Firestore blip would burn the client's link for good. 503 tells the
        # page to invite a retry instead.
        log_portail_event(
            "session_refusee", "failure", reason="lecture_indisponible",
        )
        return jsonify({
            "erreur": "Service momentanément indisponible. "
                      "Réessayez dans un instant."
        }), 503
    # Distinct log reasons (internal diagnostics only — the CLIENT always sees
    # the same generic message, §6.3).
    if invitation is None or invitation.get("statut") not in invitations.STATUTS_SESSION:
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
    try:
        if email and inv_id:
            inv = invitations.lire(inv_id)
            if inv and inv.get("email") == email and invitations.peut_relancer(inv):
                cible = inv
        # FALL THROUGH (not elif): a stale/inactive « i » in the URL must not
        # swallow the request when the SAME address has another live
        # invitation — the client would otherwise be told a link was sent and
        # wait forever.
        if cible is None and email:
            candidates = [
                i for i in invitations.chercher_par_email(email)
                if invitations.peut_relancer(i) and i.get("created_at")
            ]
            if candidates:
                cible = max(candidates, key=lambda i: i["created_at"])
    except invitations.LectureIndisponible:
        # The byte-identical-response invariant must survive the very outage
        # it exists to survive: fall through to the constant reply below.
        logger.warning("portal renvoi lookup unavailable")
        cible = None

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


def _purger_lot() -> None:
    """Drop the in-flight batch's session state.

    Called on BOTH terminal outcomes of a finalisation — the write and the
    409 that means someone already wrote it. Leaving these behind on the 409
    wedges the client permanently (see api_finaliser).
    """
    for cle in ("batch", "seq", "files_count", "total_bytes"):
        session.pop(cle, None)


def _deja_transmis(invitation: dict) -> tuple[int, int]:
    """(files, bytes) already ACQUIRED by previous batches of this invitation.

    Since D-2 lets a client come back after submitting, the session counters
    alone would hand each re-entry a fresh full quota for the invitation's 14
    days. The durable record of past batches is ``soumissions[]``.

    NOT a watertight cap, and it must not be described as one (risque R-1
    assumé). ``soumissions[]`` is written by the MAIN service when the Cloud
    Task lands, so two windows stay open: bytes uploaded but never finalised
    are recorded nowhere durable, and between a finalisation and the task
    landing (seconds normally, but 15 minutes whenever the queue is degraded
    and the reconciliation cron is doing the rescuing) the consumed budget
    reads zero on both sides. ``creer_session`` clears the session counters,
    and nothing rate-limits re-opening a session with a retained Firebase
    refresh token. This bounds the honest client, not a determined one; the
    real ceilings are the per-file size check and the bucket lifecycle.
    """
    fichiers = octets = 0
    for s in invitation.get("soumissions") or []:
        try:
            fichiers += int(s.get("files_count") or 0)
            octets += int(s.get("total_bytes") or 0)
        except (TypeError, ValueError):
            continue
    return fichiers, octets


@portail_bp.get("/documents")
def page_documents():
    invitation = g.invitation
    passes_f, _passes_o = _deja_transmis(invitation)
    files_count = session.get("files_count", 0) + passes_f
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

    # Quota spans the WHOLE invitation, not just this session: since D-2 lets a
    # client come back after submitting, session-only counters would hand each
    # re-entry a fresh 50-file / 1 GiB budget. `session_*` stay session-scoped
    # (they are written back below); the durable part is added only for the
    # comparison — folding it into the stored value would re-add the past
    # batches on every subsequent upload.
    quota_files = int(invitation.get("quota_files") or PORTAIL_MAX_FILES)
    quota_octets = int(invitation.get("quota_mb") or PORTAIL_MAX_TOTAL_MB) * _MIB
    passes_f, passes_o = _deja_transmis(invitation)
    session_files = session.get("files_count", 0)
    session_bytes = session.get("total_bytes", 0)
    files_count = session_files + passes_f
    total_bytes = session_bytes + passes_o
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

    session["files_count"] = session_files + 1
    session["total_bytes"] = session_bytes + size
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
        # L'enveloppe EXISTE : ce lot est acquis. C'est le chemin de reprise du
        # scénario le plus banal — la réponse du premier POST s'est perdue sur
        # un lien mobile, le navigateur a affiché « Erreur réseau. Réessayez »
        # et réarmé le bouton, mais le Set-Cookie n'a jamais été appliqué. Il
        # faut donc purger les MÊMES clés que sur le succès : autrement
        # ``session["batch"]`` désigne encore un lot déjà manifesté, les
        # téléversements suivants y atterrissent sans jamais être hachés ni
        # listés (puis sont purgés au « traiter »), chaque envoi re-409 à
        # l'infini, et les compteurs restés en place comptent le lot deux fois
        # dans le quota. Du point de vue du client c'est un SUCCÈS — on le mène
        # à la confirmation plutôt que de lui montrer une erreur.
        _purger_lot()
        log_portail_event(
            "soumission_finalisee", "refused",
            invitation_id=inv_id, batch=batch, reason="deja_soumise",
        )
        return jsonify({
            "ok": True, "suivant": url_for("portail.confirmation")
        }), 200
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

    _purger_lot()
    log_portail_event(
        "soumission_finalisee",
        invitation_id=inv_id, batch=batch, files_count=len(propres),
    )
    return jsonify({"ok": True, "suivant": url_for("portail.confirmation")})


@portail_bp.get("/confirmation")
def confirmation():
    return render_template("confirmation.html", invitation=g.invitation)
