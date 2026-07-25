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
from datetime import datetime, timezone
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
from config import Config
from dav.sync import (
    bump_ctag,
    collection_for,
    record_tombstone,
    remove_tombstone,
)
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
from models.hearing import get_hearing, list_hearings, update_hearing
from models.partie import (
    ROLE_LABELS,
    display_name,
    get_partie,
    list_parties,
)
from security import sanitize
from services import portail_emission as emission
from utils import graph_calendrier
from utils.graph import GraphError, GraphNotConfigured
from utils.logging_setup import log_bookings_event, log_portail_event

logger = logging.getLogger(__name__)

reception_bp = Blueprint("reception", __name__, url_prefix="/reception")

_ONGLETS = ("documents", "rdv", "ouvertures")
_JOURS_CHOIX = (14, 30, 60, 90)


# ── Pastille de navigation (compteur mis en cache, fail-open) ────────────

_BADGE_TTL_SECONDS = 60
_badge_cache: dict = {"at": 0.0, "n": None}


def _compter_rdv() -> int:
    """Rendez-vous « à_confirmer » (Bookings L2) awaiting review.

    A bounded stream over the hearings collection (single-practice scale),
    behind the same 60 s badge cache. Fail-open: 0 on any error.
    """
    try:
        return sum(
            1 for h in list_hearings(include_unconfirmed=True)
            if h.get("confirmation") == "à_confirmer"
        )
    except Exception:
        logger.exception("reception: rdv count failed")
        return 0


def compteur_reception() -> Optional[int]:
    """Transmissions « soumise » + rendez-vous « à_confirmer » en attente de
    revue — pour la pastille de nav.

    Un COUNT Firestore par page serait payé sur CHAQUE rendu de gabarit ;
    cache processus de 60 s. Fail-open : la base « portail » absente rend
    ``compter_soumises`` None — on affiche quand même les RDV le cas échéant,
    et None (les deux indisponibles) → aucune pastille, jamais une page cassée.
    """
    now = time.monotonic()
    if now - _badge_cache["at"] > _BADGE_TTL_SECONDS:
        soumises = pi.compter_soumises()
        rdv = _compter_rdv()
        if soumises is None and rdv == 0:
            _badge_cache["n"] = None
        else:
            _badge_cache["n"] = (soumises or 0) + rdv
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


def _rediriger(message: str = "", erreur: str = "", onglet: str = ""):
    args = {}
    if onglet:
        args["onglet"] = onglet
    if message:
        args["message"] = message
    if erreur:
        args["erreur"] = erreur
    return redirect(url_for("reception.index", **args))


# ── Onglet « Rendez-vous » (Bookings L2 §5) ──────────────────────────────


def _index_parties_par_courriel() -> dict:
    """Index parties by their (lowercased) email / email_work — one bounded
    read (single-practice scale, no index), for the §5.1 partie linkage."""
    idx: dict = {}
    for p in list_parties():
        for key in ("email", "email_work"):
            v = (p.get(key) or "").strip().lower()
            if v:
                idx.setdefault(v, p)
    return idx


def _lier_parties(hearings: list[dict]) -> None:
    """Attach the recognized partie (exact courriel match) to each rendez-vous
    as ``_partie_id`` / ``_partie_nom`` — precomputed so the template stays
    logic-free."""
    if not hearings:
        return
    idx = _index_parties_par_courriel()
    for h in hearings:
        courriel = (h.get("client_email") or "").strip().lower()
        p = idx.get(courriel) if courriel else None
        h["_partie_id"] = p["id"] if p else ""
        h["_partie_nom"] = display_name(p) if p else ""


def _contexte_rdv() -> dict:
    """Build the « Rendez-vous » tab context: à_confirmer + annulée_client
    cards, plus confirmed events carrying an unseen divergence."""
    try:
        tous = list_hearings(include_unconfirmed=True)
    except Exception:
        logger.exception("reception: hearings read failed")
        return {"rdvs": [], "divergences": [], "erreur_rdv": True}
    bookings = [h for h in tous if h.get("source") == "bookings"]
    rdvs = [
        h for h in bookings
        if h.get("confirmation") in ("à_confirmer", "annulée_client")
    ]
    floor = datetime.min.replace(tzinfo=timezone.utc)
    rdvs.sort(key=lambda h: h.get("start_datetime") or floor)
    divergences = [
        h for h in bookings
        if (h.get("bookings_divergence") or {}).get("motif")
        and not (h.get("bookings_divergence") or {}).get("vu")
    ]
    _lier_parties(rdvs + divergences)
    return {"rdvs": rdvs, "divergences": divergences, "erreur_rdv": False}


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
        "rdvs": [],
        "divergences": [],
        "erreur_rdv": False,
        "feature_intake": Config.FEATURE_INTAKE,
        "erreur_bucket": False,
        "est_expiree": pi.est_expiree,
    }
    if onglet == "rdv":
        contexte.update(_contexte_rdv())
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


@reception_bp.get("/partie-search")
@login_required
def partie_search():
    """HTMX autocomplete for the invitation client picker.

    Rows carry data-id/data-name/data-email; a nonce'd delegated listener on
    the inviter form fills the hidden partie_id + the name/email inputs.
    """
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return render_template("reception/_partie_resultats.html",
                               parties=None, role_labels=ROLE_LABELS)
    parties = list_parties(search=q)[:10]
    rows = [{"id": p["id"], "name": display_name(p),
             "email": p.get("email") or p.get("email_work") or "",
             "role": p.get("contact_role", "")} for p in parties]
    return render_template("reception/_partie_resultats.html",
                           parties=rows, role_labels=ROLE_LABELS)


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

    # A chosen party links partie_id (richer contact resolved main-side at
    # accusé time). An unresolvable id is ignored, never a blocking error —
    # client_name + email still carry the manual case.
    partie_id = f.get("partie_id", "").strip() or None
    if partie_id and get_partie(partie_id) is None:
        partie_id = None
    client_name = sanitize(f.get("client_name", ""), max_length=200).strip()

    invitation, errors, lien_manuel = emission.emettre_invitation(
        "documents",
        f.get("email", ""),
        dossier_id=dossier["id"] if dossier else None,
        partie_id=partie_id,
        client_name=client_name,
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


def _lire_manifeste_archive(inv_id: str, batch: str) -> Optional[dict]:
    """Manifeste d'un lot traité : archive/ d'abord, submissions/ en repli."""
    bucket = _bucket()
    for chemin in (f"archive/{inv_id}/{batch}/manifeste.json",
                   _prefix(inv_id, batch) + "manifeste.json"):
        blob = bucket.blob(chemin)
        if blob.exists():
            return json.loads(blob.download_as_bytes())
    return None


@reception_bp.get("/invitations/<inv_id>/archive")
@login_required
def invitation_archive(inv_id: str):
    """Fenêtre modale : le détail conservé d'une invitation traitée —
    par lot, la liste des fichiers avec empreinte SHA-512 et le choix de
    l'avocat (versé au dossier / refusé). Lu des manifestes archivés."""
    invitation = pi.lire_invitation(inv_id)
    if invitation is None:
        return render_template("reception/_archive_modal.html",
                               invitation=None, lots=[], erreur=True)
    lots = []
    erreur = False
    for soumission in invitation.get("soumissions") or []:
        batch = soumission.get("batch") or ""
        try:
            manifeste = _lire_manifeste_archive(inv_id, batch)
        except Exception:
            logger.exception("reception: archive manifest read failed")
            erreur = True
            manifeste = None
        lots.append({"batch": batch, "soumission": soumission,
                     "fichiers": (manifeste or {}).get("files") or []})
    return render_template("reception/_archive_modal.html",
                           invitation=invitation, lots=lots, erreur=erreur)


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
    # En-tête Content-Disposition : un CR/LF dans le nom (charge falsifiée
    # à la finalisation — le nom d'origine est conservé VERBATIM dans
    # l'enveloppe) ferait lever werkzeug. Contrôles retirés AU POINT
    # D'USAGE seulement — l'enveloppe et le manifeste probants restent
    # intacts.
    nom = "".join(
        c for c in (entree.get("name") or "document") if ord(c) >= 32
    ) or "document"
    # §7.5 — téléchargement FORCÉ (attachment), content_type DÉCLARÉ,
    # jamais de rendu en ligne depuis la quarantaine.
    return send_file(
        blob.open("rb"),
        as_attachment=True,
        download_name=nom,
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
    # Trace du choix de l'avocat dans le manifeste (visible plus tard dans
    # la fenêtre d'archive) : le dossier de destination + le nom au dossier.
    entree["verse_dossier"] = dossier.get("file_number", "")
    entree["verse_nom"] = metadata["display_name"]
    log_portail_event(
        "document_verse",
        invitation_id=inv_id, batch=batch,
        dossier_id=dossier_reel, document_id=document["id"],
    )
    try:
        _ecrire_manifeste(inv_id, batch, manifeste)
    except Exception:
        # Le document EST au dossier, mais l'état de quarantaine ne le
        # reflète pas : un succès silencieux inviterait à re-verser le même
        # fichier (le garde etat == « reçu » repasserait). Avertir.
        logger.exception("reception: manifest update failed after ingest")
        return _rediriger(erreur=(
            f"Le fichier a bien été versé au dossier "
            f"{dossier.get('file_number', '')}, mais l'état de quarantaine "
            "n'a pas pu être mis à jour : il apparaîtra encore comme "
            "« reçu ». Ne le versez pas une seconde fois — refusez-le pour "
            "clore le lot."
        ))
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
    bucket = _bucket()
    prefix = _prefix(inv_id, batch)
    archive = f"archive/{inv_id}/{batch}/"

    deja_archive = False
    try:
        manifeste = _lire_manifeste(inv_id, batch)
        if manifeste is None:
            # Peut-être archivé lors d'un essai précédent qui a échoué APRÈS
            # l'archivage (p. ex. mise à jour du statut) : reprendre depuis
            # archive/ pour que l'opération soit rejouable de bout en bout.
            src = bucket.blob(archive + "manifeste.json")
            if src.exists():
                manifeste = json.loads(src.download_as_bytes())
                deja_archive = True
    except Exception:
        logger.exception("reception: manifest read failed")
        return _rediriger(erreur="Lecture du lot impossible. Réessayez.")
    if manifeste is None:
        return _rediriger(erreur="Lot introuvable.")

    if not deja_archive:
        # Chaque fichier reçu exige une décision EXPLICITE avant la purge —
        # marquer traité supprime les objets de quarantaine (revue humaine
        # systématique, jamais de suppression d'un fichier non examiné).
        if any(f.get("etat") == "reçu" for f in manifeste.get("files") or []):
            return _rediriger(erreur=(
                "Chaque fichier du lot doit d'abord être versé ou refusé."
            ))
        manifeste["etat_lot"] = "traité"
        try:
            _ecrire_manifeste(inv_id, batch, manifeste)
            # Purge des objets files/ D'ABORD, l'enveloppe puis le manifeste
            # en DERNIER : le manifeste sous submissions/ est la clé de
            # relecture d'un nouvel essai — le retirer en premier rendrait
            # un échec partiel non rejouable (« Lot introuvable »). Les
            # gardes exists() rendent chaque étape idempotente.
            for blob in list(bucket.list_blobs(prefix=prefix + "files/")):
                blob.delete()
            for nom in ("envelope.json", "manifeste.json"):
                src = bucket.blob(prefix + nom)
                if src.exists():
                    bucket.copy_blob(src, bucket, archive + nom)
                    src.delete()
        except Exception:
            logger.exception("reception: lot archive failed")
            return _rediriger(erreur="Archivage du lot impossible. Réessayez.")
        log_portail_event("lot_traite", invitation_id=inv_id, batch=batch)

    # Ne fermer l'invitation que si AUCUN autre lot vivant ne subsiste :
    # l'enveloppe de CE lot vient d'être déplacée vers archive/, donc toute
    # envelope.json encore sous submissions/{inv}/ est un lot FRÈRE (y
    # compris un lot soumis dont la tâche n'a pas encore été traitée) — le
    # statut « traitée » le rendrait invisible en Réception et la
    # réconciliation ne le rejouerait jamais. Échec du balayage → fail
    # closed (statut inchangé, le lot frère reste listé).
    try:
        freres = [
            b for b in bucket.list_blobs(prefix=f"submissions/{inv_id}/")
            if b.name.endswith("/envelope.json")
        ]
    except Exception:
        logger.exception("reception: sibling-lot scan failed")
        freres = [batch]
    if freres:
        return _rediriger(message=(
            "Lot traité et archivé — un autre lot de cette invitation "
            "reste à examiner."
        ))
    if not pi.maj_statut(inv_id, "traitée"):
        return _rediriger(erreur=(
            "Lot archivé, mais mise à jour du statut impossible. Cliquez de "
            "nouveau « Marquer le lot traité » pour réessayer."
        ))
    return _rediriger(message="Lot traité — fichiers de quarantaine purgés, "
                              "enveloppe et manifeste archivés.")


# ── Actions « Rendez-vous » (Bookings L2 §5.2-5.4) ───────────────────────


def _rdv_ou_erreur(hid: str) -> Optional[dict]:
    """Fetch a Bookings hearing or None. get_hearing does NOT filter on
    confirmation, so an à_confirmer/annulée_client import is reachable."""
    hearing = get_hearing(hid)
    if not hearing or hearing.get("source") != "bookings":
        return None
    return hearing


@reception_bp.post("/rdv/<hid>/confirmer")
@login_required
def rdv_confirmer(hid: str):
    hearing = _rdv_ou_erreur(hid)
    if hearing is None:
        return _rediriger(erreur="Rendez-vous introuvable.", onglet="rdv")

    data = {"confirmation": ""}
    partie_liee = False
    if request.form.get("lier") == "on":
        pid = request.form.get("partie_id", "").strip()
        if pid and get_partie(pid):
            data["partie_id"] = pid
            partie_liee = True

    _updated, errors = update_hearing(hid, data)
    if errors:
        return _rediriger(erreur=" ".join(errors), onglet="rdv")

    # The event is now confirmed (confirmation="") → it enters DAV/Calendar.
    # dossier_id is "" for a Bookings import → the « Général » collection. Drop
    # any stale tombstone (mirrors the create paths) so one sync REPORT never
    # reports the resource as both live and deleted.
    sync_name = collection_for(hearing.get("dossier_id"))
    remove_tombstone(sync_name, hid)
    bump_ctag(sync_name)
    log_bookings_event("reception_rdv_confirme", hearing_id=hid,
                       partie_liee=partie_liee)
    # Intake (L3): the « envoyer le formulaire d'ouverture » checkbox is inert
    # in L2 — FEATURE_INTAKE is False, and no invitation is ever emitted here.
    return _rediriger(
        message="Rendez-vous confirmé — il apparaît au calendrier et se "
                "synchronise avec vos appareils.",
        onglet="rdv",
    )


@reception_bp.post("/rdv/<hid>/refuser")
@login_required
def rdv_refuser(hid: str):
    hearing = _rdv_ou_erreur(hid)
    if hearing is None:
        return _rediriger(erreur="Rendez-vous introuvable.", onglet="rdv")

    # Decision 2026-07-25 (Calendars.ReadWrite): a refusal CANCELS the Outlook
    # meeting (notifying the client via Bookings) — but only for a still-active
    # à_confirmer import. An annulée_client one is already cancelled by the
    # client; do not re-cancel (Graph would 404). Best-effort: a Graph failure
    # never blocks the refusal — the juriste is told to cancel manually.
    graph_annule = False
    avertissement = ""
    gid = hearing.get("graph_event_id")
    if (
        hearing.get("confirmation") == "à_confirmer"
        and gid and Config.bookings_configured()
    ):
        try:
            graph_calendrier.annuler_reservation(
                gid, "Rendez-vous refusé par le juriste."
            )
            graph_annule = True
        except (GraphError, GraphNotConfigured):
            logger.exception("reception: graph cancel failed")
            avertissement = (
                "Rendez-vous refusé, mais la réunion n'a PAS pu être annulée "
                "côté Outlook — annulez-la manuellement pour prévenir le client."
            )

    _updated, errors = update_hearing(hid, {"confirmation": "refusée"})
    if errors:
        return _rediriger(erreur=" ".join(errors), onglet="rdv")
    # No CTag bump — a refused/pending import was never in DAV.
    log_bookings_event(
        "reception_rdv_refuse", "refused" if avertissement else "success",
        hearing_id=hid, graph_annule=graph_annule,
        reason="graph_error" if avertissement else None,
    )
    if avertissement:
        return _rediriger(erreur=avertissement, onglet="rdv")
    message = (
        "Rendez-vous refusé — la réunion Outlook a été annulée et le client "
        "notifié." if graph_annule else "Rendez-vous refusé."
    )
    return _rediriger(message=message, onglet="rdv")


@reception_bp.post("/rdv/<hid>/divergence/<action>")
@login_required
def rdv_divergence(hid: str, action: str):
    if action not in ("appliquer", "ignorer", "annuler", "conserver"):
        return _rediriger(erreur="Action inconnue.", onglet="rdv")
    hearing = _rdv_ou_erreur(hid)
    if hearing is None:
        return _rediriger(erreur="Rendez-vous introuvable.", onglet="rdv")
    div = hearing.get("bookings_divergence") or {}
    if not div.get("motif"):
        return _rediriger(erreur="Aucune divergence à traiter.", onglet="rdv")

    data: dict = {}
    bump = False
    tombstone = False
    if action == "appliquer" and div.get("motif") == "modifié_côté_client":
        # Apply the client's new slot from the stashed values, then clear. The
        # event STAYS live (still confirmed) — an update, not a removal, so no
        # tombstone.
        data["bookings_divergence"] = None
        try:
            if div.get("nouveau_debut"):
                data["start_datetime"] = datetime.fromisoformat(div["nouveau_debut"])
            if div.get("nouveau_fin"):
                data["end_datetime"] = datetime.fromisoformat(div["nouveau_fin"])
        except ValueError:
            logger.warning("reception: bad divergence datetime")
        bump = True
    elif action == "annuler" and div.get("motif") == "annulé_côté_client":
        # A CONFIRMED (already-synced) event LEAVES the DAV live set. A CTag
        # bump alone does NOT propagate the removal — DavX5 keeps its local
        # copy unless a tombstone reports the 404 (RFC 6578). Record one, or
        # the cancelled meeting lingers on the device forever.
        data["confirmation"] = "annulée_client"
        data["bookings_divergence"] = None
        bump = True
        tombstone = True
    else:
        # ignorer / conserver → dismiss the alert (vu=True), keep the event.
        data["bookings_divergence"] = {**div, "vu": True}

    _updated, errors = update_hearing(hid, data)
    if errors:
        return _rediriger(erreur=" ".join(errors), onglet="rdv")
    sync_name = collection_for(hearing.get("dossier_id"))
    if tombstone:
        record_tombstone(sync_name, hid)
    if bump:
        bump_ctag(sync_name)
    log_bookings_event("reception_rdv_divergence_traitee", hearing_id=hid,
                       action=action)
    return _rediriger(message="Divergence traitée.", onglet="rdv")
