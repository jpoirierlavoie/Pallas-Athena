"""« Réception » — revue des transmissions du portail client (spec L1 §9).

Tout est @login_required, français, POST+redirect avec messages en query
(motif maison sans flash). RIEN n'est ingéré automatiquement : chaque octet
client n'entre au stockage canonique que sur clic « Verser » du juriste, et
seuls les fichiers conformes au vocabulaire documents EXISTANT (6 types,
≤ 25 Mo — décision utilisateur 2026-07-25) sont versables ; les autres se
téléchargent (attachment forcé, §7.5) et se traitent hors application.

Fail-open d'affichage : la base « portail » ou le bucket absents (infra pas
encore créée) rendent des états vides et un avertissement, jamais un 500.
"""

import io
import json
import logging
import time
from typing import Optional

from firebase_admin import storage
from flask import (
    Blueprint,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from auth import login_required
from client.config import PORTAIL_BUCKET
from models import portail_invitation as pi
from models.document import (
    ALLOWED_EXTENSIONS,
    CATEGORY_LABELS,
    MAX_FILE_SIZE,
    PORTAL_FOLDER_NAME,
    upload_document,
)
from models.dossier import get_dossier, list_dossiers
from models.folder import get_or_create_folder
from models.partie import get_partie
from security import sanitize
from services import portail_emission as emission
from utils.logging_setup import log_portail_event

logger = logging.getLogger(__name__)

reception_bp = Blueprint("reception", __name__, url_prefix="/reception")

_ONGLETS = ("documents", "rdv", "ouvertures")
_JOURS_CHOIX = (14, 30, 60, 90)


# ── Pastille de navigation (compteur mis en cache, fail-open) ────────────

_BADGE_TTL_SECONDS = 60
_badge_cache: dict = {"at": 0.0, "n": None}


def compteur_reception() -> Optional[int]:
    """Invitations « soumise » en attente de revue — pour la pastille de nav.

    Un COUNT Firestore par page serait payé sur CHAQUE rendu de gabarit ;
    cache processus de 60 s. Fail-open : None (base absente, IAM, panne) →
    aucune pastille, jamais une page cassée.
    """
    now = time.monotonic()
    if now - _badge_cache["at"] > _BADGE_TTL_SECONDS:
        _badge_cache["n"] = pi.compter_soumises()
        _badge_cache["at"] = now
    return _badge_cache["n"]


# ── Accès quarantaine (côté principal : objectAdmin) ─────────────────────


def _bucket():
    return storage.bucket(PORTAIL_BUCKET)


def _prefix(inv_id: str, batch: str) -> str:
    return f"submissions/{inv_id}/{batch}/"


def _lire_manifeste(inv_id: str, batch: str) -> Optional[dict]:
    blob = _bucket().blob(_prefix(inv_id, batch) + "manifeste.json")
    if not blob.exists():
        return None
    return json.loads(blob.download_as_bytes())


def _ecrire_manifeste(inv_id: str, batch: str, manifeste: dict) -> None:
    _bucket().blob(_prefix(inv_id, batch) + "manifeste.json").upload_from_string(
        json.dumps(manifeste, ensure_ascii=False),
        content_type="application/json",
    )


def _versable(entree: dict) -> bool:
    """Précontrôle du versement (décision 2026-07-25 : vocabulaire actuel).

    Le verdict final reste celui d'upload_document (sniff des octets) — ce
    contrôle n'existe que pour l'UI et un message français précis.
    """
    nom = entree.get("name") or ""
    ext = "." + nom.rsplit(".", 1)[1].lower() if "." in nom else ""
    taille = int(entree.get("size_gcs") or 0)
    return (
        entree.get("etat") == "reçu"
        and ext in ALLOWED_EXTENSIONS
        and 0 < taille <= MAX_FILE_SIZE
    )


def _rediriger(message: str = "", erreur: str = ""):
    args = {}
    if message:
        args["message"] = message
    if erreur:
        args["erreur"] = erreur
    return redirect(url_for("reception.index", **args))


# ── Page principale ──────────────────────────────────────────────────────


@reception_bp.get("/")
@login_required
def index():
    onglet = request.args.get("onglet", "documents")
    if onglet not in _ONGLETS:
        onglet = "documents"
    contexte = {
        "onglet": onglet,
        "message": sanitize(request.args.get("message", ""), max_length=300),
        "erreur": sanitize(request.args.get("erreur", ""), max_length=300),
        "lots": [],
        "actives": [],
        "traitees": [],
        "erreur_bucket": False,
        "est_expiree": pi.est_expiree,
    }
    if onglet == "documents":
        toutes = pi.lister_invitations(type_="documents")
        contexte["actives"] = [
            i for i in toutes if i.get("statut") in ("envoyée", "ouverte")
        ]
        contexte["traitees"] = [
            i for i in toutes
            if i.get("statut") in ("traitée", "refusée", "révoquée")
        ][:20]
        for inv in (i for i in toutes if i.get("statut") == "soumise"):
            for soumission in inv.get("soumissions") or []:
                batch = soumission.get("batch") or ""
                manifeste = None
                try:
                    manifeste = _lire_manifeste(inv["id"], batch)
                except Exception:
                    logger.exception("reception: manifest read failed")
                    contexte["erreur_bucket"] = True
                fichiers = []
                if manifeste:
                    for seq, entree in enumerate(manifeste.get("files") or []):
                        fichiers.append(
                            {**entree, "seq": seq, "versable": _versable(entree)}
                        )
                contexte["lots"].append({
                    "invitation": inv,
                    "batch": batch,
                    "soumission": soumission,
                    "manifeste": manifeste,
                    "fichiers": fichiers,
                })
    return render_template(
        "reception/index.html",
        dossiers=list_dossiers(status_filter="actif") if onglet == "documents" else [],
        category_labels=CATEGORY_LABELS,
        **contexte,
    )


# ── Émission d'invitations (§6.2) ────────────────────────────────────────


def _email_du_dossier(dossier: dict) -> str:
    for client in dossier.get("clients") or []:
        partie = get_partie(client.get("id", ""))
        if partie and (partie.get("email") or partie.get("email_work")):
            return partie.get("email") or partie.get("email_work")
    return ""


@reception_bp.get("/inviter")
@login_required
def inviter_form():
    dossier_id = request.args.get("dossier_id", "")
    dossier = get_dossier(dossier_id) if dossier_id else None
    return render_template(
        "reception/inviter.html",
        dossiers=list_dossiers(status_filter="actif"),
        dossier=dossier,
        email_prefill=_email_du_dossier(dossier) if dossier else "",
        jours_choix=_JOURS_CHOIX,
        errors=[],
        form={},
    )


@reception_bp.post("/inviter")
@login_required
def inviter_submit():
    f = request.form
    dossier_id = f.get("dossier_id", "").strip()
    dossier = get_dossier(dossier_id) if dossier_id else None
    display_label = sanitize(f.get("display_label", ""), max_length=120).strip()
    if not display_label and dossier:
        display_label = f"Dossier {dossier.get('file_number', '')}".strip()
    try:
        jours = int(f.get("jours", "30"))
    except ValueError:
        jours = 30

    invitation, errors, lien_manuel = emission.emettre_invitation(
        "documents",
        f.get("email", ""),
        dossier_id=dossier["id"] if dossier else None,
        display_label=display_label,
        jours=jours,
    )
    if errors:
        return render_template(
            "reception/inviter.html",
            dossiers=list_dossiers(status_filter="actif"),
            dossier=dossier,
            email_prefill="",
            jours_choix=_JOURS_CHOIX,
            errors=errors,
            form=f,
        ), 400
    if lien_manuel:
        # Graph absent/en panne : l'invitation existe, le juriste transmet
        # le lien lui-même (jamais d'invitation morte silencieuse).
        return render_template(
            "reception/lien.html", invitation=invitation, lien=lien_manuel
        )
    return _rediriger(message="Invitation transmise par courriel.")


@reception_bp.post("/invitations/<inv_id>/renvoyer")
@login_required
def renvoyer(inv_id: str):
    ok, message, lien_manuel = emission.renvoyer_invitation(inv_id)
    if not ok:
        return _rediriger(erreur=message or "Renvoi impossible.")
    if lien_manuel:
        invitation = pi.lire_invitation(inv_id)
        return render_template(
            "reception/lien.html", invitation=invitation, lien=lien_manuel
        )
    return _rediriger(message="Nouveau lien transmis par courriel.")


@reception_bp.post("/invitations/<inv_id>/revoquer")
@login_required
def revoquer(inv_id: str):
    if not pi.revoquer(inv_id):
        return _rediriger(erreur="Révocation impossible.")
    log_portail_event("invitation_revoquee", invitation_id=inv_id)
    return _rediriger(
        message="Invitation révoquée — le lien du client est immédiatement inopérant."
    )


# ── Fichiers d'un lot ────────────────────────────────────────────────────


def _entree_ou_none(inv_id: str, batch: str, seq: int):
    try:
        manifeste = _lire_manifeste(inv_id, batch)
    except Exception:
        logger.exception("reception: manifest read failed")
        return None, None
    if manifeste is None:
        return None, None
    fichiers = manifeste.get("files") or []
    if not 0 <= seq < len(fichiers):
        return manifeste, None
    return manifeste, fichiers[seq]


@reception_bp.get("/lots/<inv_id>/<batch>/fichiers/<int:seq>")
@login_required
def telecharger(inv_id: str, batch: str, seq: int):
    _manifeste, entree = _entree_ou_none(inv_id, batch, seq)
    if entree is None:
        return render_template("errors/404.html"), 404
    blob = _bucket().blob(entree.get("objet", ""))
    if not blob.exists():
        return render_template("errors/404.html"), 404
    # §7.5 — téléchargement FORCÉ (attachment), content_type DÉCLARÉ,
    # jamais de rendu en ligne depuis la quarantaine.
    return send_file(
        blob.open("rb"),
        as_attachment=True,
        download_name=entree.get("name") or "document",
        mimetype=entree.get("content_type") or "application/octet-stream",
    )


@reception_bp.post("/lots/<inv_id>/<batch>/fichiers/<int:seq>/verser")
@login_required
def verser(inv_id: str, batch: str, seq: int):
    manifeste, entree = _entree_ou_none(inv_id, batch, seq)
    if entree is None:
        return _rediriger(erreur="Fichier introuvable.")
    if entree.get("etat") != "reçu":
        return _rediriger(erreur="Ce fichier a déjà été traité.")
    if not _versable(entree):
        return _rediriger(erreur=(
            "Ce fichier ne peut pas être versé tel quel : seuls les PDF, "
            "Word (doc/docx), JPEG, PNG et TIFF de 25 Mo ou moins sont admis "
            "au dossier. Téléchargez-le et traitez-le hors application."
        ))

    dossier_id = request.form.get("dossier_id", "").strip()
    dossier = get_dossier(dossier_id) if dossier_id else None
    if dossier is None:
        return _rediriger(erreur="Choisissez le dossier de destination.")

    try:
        octets = _bucket().blob(entree["objet"]).download_as_bytes()
    except Exception:
        logger.exception("reception: quarantine download failed")
        return _rediriger(erreur="Lecture du fichier en quarantaine impossible.")

    dossier_reel = dossier["id"]
    folder = get_or_create_folder(dossier_reel, PORTAL_FOLDER_NAME)
    metadata = {
        "category": request.form.get("category", "autre"),
        "display_name": sanitize(request.form.get("display_name", ""),
                                 max_length=200) or entree.get("name", ""),
        # Provenance dans les champs EXISTANTS (décision 2026-07-25 — aucun
        # champ nouveau) : description + tag « portail ».
        "description": (
            f"Reçu via le portail — invitation {inv_id}, lot {batch}. "
            f"SHA-512 : {entree.get('sha512') or ''}"
        ),
        "tags": ["portail"],
        "folder_id": folder["id"] if folder else None,
    }
    document, errors = upload_document(
        dossier_reel,
        dossier.get("file_number", ""),
        io.BytesIO(octets),
        entree.get("name") or "document",
        len(octets),
        metadata,
        session["user_id"],
    )
    if errors or document is None:
        return _rediriger(erreur=" ".join(errors) or "Versement impossible.")

    entree["etat"] = "versé"
    try:
        _ecrire_manifeste(inv_id, batch, manifeste)
    except Exception:
        # Le document EST au dossier ; seul l'état du manifeste retarde.
        logger.exception("reception: manifest update failed after ingest")
    log_portail_event(
        "document_verse",
        invitation_id=inv_id, batch=batch,
        dossier_id=dossier_reel, document_id=document["id"],
    )
    return _rediriger(
        message=f"Fichier versé au dossier {dossier.get('file_number', '')} "
                f"(dossier « {PORTAL_FOLDER_NAME} »)."
    )


@reception_bp.post("/lots/<inv_id>/<batch>/fichiers/<int:seq>/refuser")
@login_required
def refuser(inv_id: str, batch: str, seq: int):
    manifeste, entree = _entree_ou_none(inv_id, batch, seq)
    if entree is None:
        return _rediriger(erreur="Fichier introuvable.")
    if entree.get("etat") not in ("reçu", "manquant"):
        return _rediriger(erreur="Ce fichier a déjà été traité.")
    entree["etat"] = "refusé"
    try:
        _ecrire_manifeste(inv_id, batch, manifeste)
    except Exception:
        logger.exception("reception: manifest update failed")
        return _rediriger(erreur="Mise à jour du manifeste impossible.")
    log_portail_event("document_refuse", invitation_id=inv_id, batch=batch)
    return _rediriger(message="Fichier refusé.")


@reception_bp.post("/lots/<inv_id>/<batch>/traiter")
@login_required
def traiter_lot(inv_id: str, batch: str):
    manifeste, _ = _entree_ou_none(inv_id, batch, 0)
    if manifeste is None:
        return _rediriger(erreur="Lot introuvable.")
    # Chaque fichier reçu exige une décision EXPLICITE avant la purge —
    # marquer traité supprime les objets de quarantaine (revue humaine
    # systématique, jamais de suppression d'un fichier non examiné).
    if any(f.get("etat") == "reçu" for f in manifeste.get("files") or []):
        return _rediriger(erreur=(
            "Chaque fichier du lot doit d'abord être versé ou refusé."
        ))

    manifeste["etat_lot"] = "traité"
    bucket = _bucket()
    prefix = _prefix(inv_id, batch)
    archive = f"archive/{inv_id}/{batch}/"
    try:
        _ecrire_manifeste(inv_id, batch, manifeste)
        # Enveloppe + manifeste → archive/ (trace conservée 365 j, cycle de
        # vie du bucket), puis purge des objets files/.
        for nom in ("envelope.json", "manifeste.json"):
            src = bucket.blob(prefix + nom)
            if src.exists():
                bucket.copy_blob(src, bucket, archive + nom)
                src.delete()
        for blob in list(bucket.list_blobs(prefix=prefix + "files/")):
            blob.delete()
    except Exception:
        logger.exception("reception: lot archive failed")
        return _rediriger(erreur="Archivage du lot impossible. Réessayez.")

    pi.maj_statut(inv_id, "traitée")
    log_portail_event("lot_traite", invitation_id=inv_id, batch=batch)
    return _rediriger(message="Lot traité — fichiers de quarantaine purgés, "
                              "enveloppe et manifeste archivés.")
