"""« Réception » — revue des transmissions du portail client (spec L1 §9).

Tout est @login_required, français, POST+redirect avec messages en query
(motif maison sans flash). RIEN n'est ingéré automatiquement : chaque octet
client n'entre au stockage canonique que sur clic « Verser » du juriste, et
seuls les fichiers conformes au vocabulaire documents (9 types, ≤ 25 Mo —
décision utilisateur 2026-08-11, élargissant celle du 2026-07-25 de ZIP,
.eml et .msg) sont versables ; les autres se téléchargent (attachment
forcé, §7.5) et se traitent hors application.

Fail-open d'affichage : la base « portail » ou le bucket absents (infra pas
encore créée) rendent des états vides et un avertissement, jamais un 500.
"""

import hashlib
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
    session,
    url_for,
)

from auth import login_required
from client.config import (
    INVITATION_DOCUMENTS_JOURS,
    INVITATION_INTAKE_JOURS,
    PORTAIL_BUCKET,
)
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
    build_attachment_disposition,
    sign_blob_url,
    upload_document,
)
from models.dossier import get_dossier, list_dossiers
from models.folder import get_or_create_folder
from models.hearing import get_hearing, list_hearings, update_hearing
from models.partie import (
    ROLE_LABELS,
    create_partie,
    display_name,
    get_partie,
    list_parties,
    update_partie,
)
from security import sanitize
from services import portail_emission as emission
from utils import graph_calendrier, rapprochement
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


def _lire_enveloppe(inv_id: str, batch: str) -> Optional[dict]:
    """Jumeau de _lire_manifeste pour les ouvertures (L3) : l'enveloppe EST la
    soumission — il n'y a ni fichier ni manifeste."""
    blob = _bucket().blob(_prefix(inv_id, batch) + "envelope.json")
    if not blob.exists():
        return None
    return json.loads(blob.download_as_bytes())


def _archiver_enveloppe(inv_id: str, batch: str) -> None:
    """Déplacer l'enveloppe traitée sous archive/ (cycle de vie 365 j).

    Copie PUIS suppression, dans cet ordre : une panne entre les deux laisse
    l'enveloppe en place, donc rejouable, plutôt que perdue.
    """
    bucket = _bucket()
    source = bucket.blob(_prefix(inv_id, batch) + "envelope.json")
    if not source.exists():
        return
    bucket.copy_blob(
        source, bucket, f"archive/{inv_id}/{batch}/envelope.json"
    )
    source.delete()


def _lire_enveloppe_archive(inv_id: str, batch: str) -> Optional[dict]:
    """Enveloppe d'une ouverture close : archive/ d'abord, submissions/ en
    repli — jumeau exact de _lire_manifeste_archive."""
    bucket = _bucket()
    for chemin in (f"archive/{inv_id}/{batch}/envelope.json",
                   _prefix(inv_id, batch) + "envelope.json"):
        blob = bucket.blob(chemin)
        if blob.exists():
            return json.loads(blob.download_as_bytes())
    return None


def _ecrire_manifeste(inv_id: str, batch: str, manifeste: dict) -> None:
    _bucket().blob(_prefix(inv_id, batch) + "manifeste.json").upload_from_string(
        json.dumps(manifeste, ensure_ascii=False),
        content_type="application/json",
    )


def _versable(entree: dict) -> bool:
    """Précontrôle du versement (vocabulaire documents courant — 9 types
    depuis la décision utilisateur 2026-08-11 : + ZIP, .eml, .msg).

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


# ── Ouvertures (L3 §5) ───────────────────────────────────────────────────

# Table de correspondance formulaire → contact (§5.3). Elle vit ICI, du côté
# juriste, et non dans le portail : c'est au moment du versement que la
# décision se prend, et le portail ne connaît pas ``models``. L'ordre est celui
# du formulaire des contacts, pour que la vue côte à côte se lise comme la
# fiche.
_CORRESPONDANCE = (
    ("prenom", "first_name", "Prénom"),
    ("nom", "last_name", "Nom"),
    ("date_naissance", "birth_date", "Date de naissance"),
    ("denomination", "organization_name", "Dénomination sociale"),
    ("neq", "company_neq", "NEQ"),
    ("langue", "language", "Langue"),
    ("courriel", "email", "Courriel"),
    ("telephone", "phone_cell", "Cellulaire"),
    ("telephone2", "phone_home", "Domicile"),
    ("adresse_rue", "address_street", "Rue"),
    ("adresse_app", "address_unit", "Appartement"),
    ("adresse_ville", "address_city", "Ville"),
    ("adresse_province", "address_province", "Province"),
    ("adresse_code_postal", "address_postal_code", "Code postal"),
    ("adresse_pays", "address_country", "Pays"),
)


def _comparaison(donnees: dict, partie: Optional[dict]) -> list[dict]:
    """Lignes de la vue côte à côte : valeur actuelle ↔ valeur soumise.

    ``applicable`` n'est vrai que pour les champs qui DIFFÈRENT et dont la
    valeur soumise n'est pas vide : c'est ce qui pré-coche les cases (§5.3).
    Un champ soumis vide ne propose jamais d'effacer une valeur au dossier —
    le silence d'un client n'est pas une rétractation.
    """
    lignes = []
    for champ, cible, libelle in _CORRESPONDANCE:
        soumis = (donnees.get(champ) or "").strip()
        actuel = ""
        if partie:
            valeur = partie.get(cible)
            if valeur is None:
                actuel = ""
            elif hasattr(valeur, "strftime"):
                # birth_date est une DATE SEULE à minuit UTC : str() en ferait
                # « 1985-03-17 00:00:00+00:00 », qui ne serait jamais égal au
                # « 1985-03-17 » transmis — le champ paraîtrait toujours
                # différent, donc toujours pré-coché. strftime, jamais to_mtl
                # (Montréal reculerait la date d'un jour).
                actuel = valeur.strftime("%Y-%m-%d")
            else:
                actuel = str(valeur)
        if not soumis and not actuel:
            continue
        lignes.append({
            "champ": champ,
            "cible": cible,
            "libelle": libelle,
            "actuel": actuel,
            "soumis": soumis,
            "differe": bool(soumis) and soumis != actuel,
            "applicable": bool(soumis) and soumis != actuel,
        })
    return lignes


def _candidats_adverses(adverses: list[dict], parties: list[dict]) -> list[dict]:
    """Aide VISUELLE au contrôle des conflits (§5.2) — jamais un verdict."""
    index = [(p["id"], display_name(p)) for p in parties if p.get("id")]
    enrichies = []
    for ligne in adverses:
        nom = (ligne.get("nom") or "").strip()
        if not nom:
            continue
        trouves = rapprochement.candidats(nom, index)
        enrichies.append({
            "nom": nom,
            "precision": (ligne.get("precision") or "").strip(),
            "candidats": [
                {"id": c.cle, "nom": c.nom, "motif": c.motif} for c in trouves
            ],
        })
    return enrichies


def _contexte_ouvertures() -> dict:
    """Onglet « Ouvertures » — même forme que _contexte_rdv : lecture sous
    try/except → drapeau d'erreur, tri en Python, TOUT précalculé pour que le
    gabarit reste sans logique."""
    try:
        toutes = pi.lister_invitations(type_="intake")
    except Exception:
        logger.exception("reception: intake invitations read failed")
        return {"ouvertures": [], "intake_actives": [], "intake_traitees": [],
                "erreur_ouvertures": True}

    contexte: dict = {
        "ouvertures": [],
        "intake_actives": [
            i for i in toutes if i.get("statut") in ("envoyée", "ouverte")
        ],
        "intake_traitees": [
            i for i in toutes
            if i.get("statut") in ("traitée", "refusée", "révoquée")
        ][:20],
        "erreur_ouvertures": False,
    }

    soumises = [i for i in toutes if i.get("statut") == "soumise"]
    if not soumises:
        return contexte

    # Une seule lecture des contacts pour tout l'onglet (jeu de données d'un
    # cabinet solo — borné, aucun index requis).
    try:
        parties = list_parties()
    except Exception:
        logger.exception("reception: parties read failed")
        parties = []

    for inv in soumises:
        # La DERNIÈRE soumission fait foi : la ré-entrée permet de corriger,
        # et c'est la version corrigée que le juriste doit voir.
        lots = inv.get("soumissions") or []
        if not lots:
            continue
        batch = (lots[-1].get("batch") or "")
        enveloppe = None
        try:
            enveloppe = _lire_enveloppe(inv["id"], batch)
        except Exception:
            logger.exception("reception: intake envelope read failed")
            contexte["erreur_bucket"] = True
        donnees = (enveloppe or {}).get("donnees") or {}
        partie = None
        if inv.get("partie_id"):
            try:
                partie = get_partie(inv["partie_id"])
            except Exception:
                logger.exception("reception: partie read failed")
        contexte["ouvertures"].append({
            "invitation": inv,
            "batch": batch,
            "enveloppe": enveloppe,
            # Une enveloppe illisible ne disparaît pas en silence : la fiche
            # s'affiche avec un bandeau et reste actionnable (refus).
            "lisible": bool(donnees),
            "partie": partie,
            "nature": donnees.get("nature") or "physique",
            "lignes": _comparaison(donnees, partie),
            "adverses": _candidats_adverses(
                (enveloppe or {}).get("parties_adverses") or [], parties
            ),
            "consentement": (enveloppe or {}).get("consentement") or {},
            "versions": len(lots),
        })
    return contexte


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
        "ouvertures": [],
        "intake_actives": [],
        "intake_traitees": [],
        "erreur_ouvertures": False,
        "feature_intake": Config.FEATURE_INTAKE,
        "erreur_bucket": False,
        "est_expiree": pi.est_expiree,
    }
    if onglet == "rdv":
        contexte.update(_contexte_rdv())
    if onglet == "ouvertures":
        contexte.update(_contexte_ouvertures())
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
    type_ = request.args.get("type", "documents")
    if type_ not in ("documents", "intake"):
        type_ = "documents"
    # Déclencheur (b) : la fiche partie amène ici avec ?partie_id=, ce qui
    # préremplit le contact et, à la soumission, l'instantané non sensible.
    partie = get_partie(request.args.get("partie_id", "")) or None
    return render_template(
        "reception/inviter.html",
        dossiers=list_dossiers(status_filter="actif"),
        dossier=dossier,
        type_=type_,
        partie=partie,
        email_prefill=(
            (partie.get("email") or partie.get("email_work") or "") if partie
            else (_email_du_dossier(dossier) if dossier else "")
        ),
        jours_choix=_JOURS_CHOIX,
        jours_selection=(
            INVITATION_INTAKE_JOURS if type_ == "intake"
            else INVITATION_DOCUMENTS_JOURS
        ),
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
    # Déclencheur (c) : un sélecteur sur le formulaire existant plutôt qu'un
    # second formulaire — c'est ici le SEUL endroit où le type était écrit en
    # dur, donc le seul à brancher.
    type_ = f.get("type", "documents")
    if type_ not in ("documents", "intake"):
        type_ = "documents"
    dossier_id = f.get("dossier_id", "").strip()
    dossier = get_dossier(dossier_id) if dossier_id else None
    display_label = sanitize(f.get("display_label", ""), max_length=120).strip()
    if not display_label:
        if type_ == "intake":
            # Générique à dessein (§2) : jamais un numéro de dossier ni une
            # partie adverse — ce libellé est le seul que le client voit, et
            # tout le document d'invitation est lu par le service PUBLIC.
            display_label = "Ouverture de votre dossier client"
        elif dossier:
            display_label = f"Dossier {dossier.get('file_number', '')}".strip()
    defaut_jours = (
        INVITATION_INTAKE_JOURS if type_ == "intake"
        else INVITATION_DOCUMENTS_JOURS
    )
    try:
        jours = int(f.get("jours", str(defaut_jours)))
    except ValueError:
        jours = defaut_jours

    # A chosen party links partie_id (richer contact resolved main-side at
    # accusé time). An unresolvable id is ignored, never a blocking error —
    # client_name + email still carry the manual case.
    partie_id = f.get("partie_id", "").strip() or None
    partie = get_partie(partie_id) if partie_id else None
    if partie is None:
        partie_id = None
    client_name = sanitize(f.get("client_name", ""), max_length=200).strip()

    invitation, errors, lien_manuel = emission.emettre_invitation(
        type_,
        f.get("email", ""),
        dossier_id=dossier["id"] if dossier else None,
        partie_id=partie_id,
        client_name=client_name,
        display_label=display_label,
        jours=jours,
        # Le préremplissage n'a de sens que pour une MISE À JOUR : une
        # ouverture sans contact lié part de zéro.
        prefill=(
            pi.prefill_depuis_partie(partie)
            if type_ == "intake" and partie else None
        ),
    )
    if errors:
        return render_template(
            "reception/inviter.html",
            dossiers=list_dossiers(status_filter="actif"),
            dossier=dossier,
            type_=type_,
            partie=partie,
            email_prefill="",
            jours_choix=_JOURS_CHOIX,
            jours_selection=jours,
            errors=errors,
            form=f,
        ), 400
    if lien_manuel:
        # Graph absent/en panne : l'invitation existe, le juriste transmet
        # le lien lui-même (jamais d'invitation morte silencieuse).
        return render_template(
            "reception/lien.html", invitation=invitation, lien=lien_manuel
        )
    return _rediriger(
        message="Invitation transmise par courriel.",
        onglet="ouvertures" if type_ == "intake" else "",
    )


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
                               invitation=None, lots=[], erreur=True,
                               est_intake=False)
    # Une ouverture (L3) se conserve et se consulte comme un lot de documents,
    # à ceci près que l'enveloppe EST la soumission : pas de fichiers, donc pas
    # d'empreintes — le récapitulatif des réponses tient lieu de contenu.
    est_intake = invitation.get("type") == "intake"
    lots = []
    erreur = False
    for soumission in invitation.get("soumissions") or []:
        batch = soumission.get("batch") or ""
        lot = {"batch": batch, "soumission": soumission, "fichiers": []}
        try:
            if est_intake:
                enveloppe = _lire_enveloppe_archive(inv_id, batch) or {}
                donnees = enveloppe.get("donnees") or {}
                lot.update({
                    "lisible": bool(donnees),
                    # Vue en lecture seule : aucune partie de référence, donc
                    # « actuel » reste vide et seul le déclaré s'affiche.
                    "lignes": _comparaison(donnees, None),
                    "adverses": [
                        {"nom": (a.get("nom") or "").strip(),
                         "precision": (a.get("precision") or "").strip()}
                        for a in (enveloppe.get("parties_adverses") or [])
                        if (a.get("nom") or "").strip()
                    ],
                    "consentement": enveloppe.get("consentement") or {},
                    "soumis_le": enveloppe.get("submitted_at") or "",
                    "http": enveloppe.get("http") or {},
                })
            else:
                manifeste = _lire_manifeste_archive(inv_id, batch)
                lot["fichiers"] = (manifeste or {}).get("files") or []
        except Exception:
            logger.exception("reception: archive read failed")
            erreur = True
            lot.setdefault("lisible", False)
        lots.append(lot)
    return render_template("reception/_archive_modal.html",
                           invitation=invitation, lots=lots, erreur=erreur,
                           est_intake=est_intake)


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
    # §7.5 — téléchargement FORCÉ (attachment), content_type DÉCLARÉ, jamais
    # de rendu en ligne depuis la quarantaine. REDIRECTION vers un URL signé
    # V4 (15 min) plutôt que send_file : App Engine Standard PLAFONNE toute
    # réponse à 32 Mo (« Response size was too large » — 2026-08-12, un lot
    # de 68 et 128 Mo répondait 500), donc les octets vont de GCS au
    # navigateur sans transiter par l'application.
    try:
        url = sign_blob_url(blob, {
            "response-content-disposition": build_attachment_disposition(nom),
            "response-content-type": (
                entree.get("content_type") or "application/octet-stream"
            ),
        })
    except Exception:
        logger.exception("reception: quarantine signed url failed")
        return _rediriger(erreur="Téléchargement impossible. Réessayez.")
    return redirect(url)


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
            "Word (doc/docx), JPEG, PNG, TIFF, ZIP et courriels (.eml/.msg) "
            "de 25 Mo ou moins sont admis au dossier. Téléchargez-le et "
            "traitez-le hors application."
        ))

    dossier_id = request.form.get("dossier_id", "").strip()
    dossier = get_dossier(dossier_id) if dossier_id else None
    if dossier is None:
        return _rediriger(erreur="Choisissez le dossier de destination.")

    # Fraîcheur (revue 2026-08-11) : _versable a jugé la taille du
    # MANIFESTE, figée au traitement du lot — le blob VIVANT peut différer.
    # Relire ses métadonnées AVANT de le charger en mémoire, sinon un objet
    # regonflé après la prise d'empreintes arriverait entier en RAM.
    try:
        blob = _bucket().blob(entree["objet"])
        blob.reload()
    except Exception:
        logger.exception("reception: quarantine blob reload failed")
        return _rediriger(erreur="Lecture du fichier en quarantaine impossible.")
    if int(blob.size or 0) > MAX_FILE_SIZE:
        log_portail_event(
            "versement_divergence", "failure",
            invitation_id=inv_id, batch=batch, seq=seq, reason="taille",
        )
        return _rediriger(erreur=(
            "Le fichier en quarantaine ne correspond plus au manifeste "
            "(taille modifiée depuis la réception). Versement refusé — "
            "vérifiez le lot avant toute décision."
        ))

    try:
        octets = blob.download_as_bytes()
    except Exception:
        logger.exception("reception: quarantine download failed")
        return _rediriger(erreur="Lecture du fichier en quarantaine impossible.")

    # Intégrité probante : la description du document au dossier citera
    # l'empreinte du manifeste — ne verser que des octets qui la confirment.
    sha_manifeste = entree.get("sha512") or ""
    if sha_manifeste and hashlib.sha512(octets).hexdigest() != sha_manifeste:
        log_portail_event(
            "versement_divergence", "failure",
            invitation_id=inv_id, batch=batch, seq=seq, reason="sha512",
        )
        return _rediriger(erreur=(
            "Le fichier en quarantaine ne correspond plus à l'empreinte "
            "SHA-512 calculée à la réception. Versement refusé — "
            "retéléchargez le fichier et vérifiez le lot."
        ))

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

    message = ("Rendez-vous confirmé — il apparaît au calendrier et se "
               "synchronise avec vos appareils.")
    # Déclencheur (a) de la phase L3 : le courriel du rendez-vous ne
    # correspond à aucune partie → proposer le formulaire d'ouverture. Une
    # panne d'émission n'annule JAMAIS la confirmation, qui est déjà commise
    # (CTag bumpé) : bandeau, jamais un échec.
    if (
        Config.FEATURE_INTAKE
        and request.form.get("intake") == "on"
        and not partie_liee
        and (hearing.get("client_email") or "").strip()
    ):
        try:
            _inv, erreurs, lien_manuel = emission.emettre_invitation(
                "intake",
                hearing["client_email"],
                client_name=hearing.get("client_nom", ""),
                display_label="Ouverture de votre dossier client",
            )
        except Exception:
            logger.exception("reception: intake invitation from rdv failed")
            erreurs, lien_manuel = ["Erreur inattendue."], ""
        if erreurs:
            message += (" Le formulaire d'ouverture n'a PAS pu être envoyé — "
                        "réessayez depuis l'onglet Ouvertures.")
        elif lien_manuel:
            message += (" Le formulaire d'ouverture a été créé, mais le "
                        "courriel n'est pas parti : transmettez le lien depuis "
                        "l'onglet Ouvertures.")
        else:
            message += " Le formulaire d'ouverture a été envoyé au client."

    return _rediriger(message=message, onglet="rdv")


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


# ── Ouvertures : actions (§5.3) ──────────────────────────────────────────


def _ouverture_ou_erreur(inv_id: str, batch: str):
    """(invitation, enveloppe) ou (None, None) — jamais une exception."""
    try:
        invitation = pi.lire_invitation(inv_id)
    except Exception:
        logger.exception("reception: intake invitation read failed")
        return None, None
    if invitation is None or invitation.get("type") != "intake":
        return None, None
    try:
        return invitation, _lire_enveloppe(inv_id, batch)
    except Exception:
        logger.exception("reception: intake envelope read failed")
        return invitation, None


def _valeurs_choisies(donnees: dict) -> dict:
    """Champs contact issus des seules cases cochées.

    Le formulaire poste ``appliquer=<champ>`` par case ; rien d'autre n'entre.
    Une case non cochée n'est PAS une instruction d'effacer : elle est
    simplement absente du dictionnaire, et update_partie fusionne — donc la
    valeur au dossier survit.
    """
    choisis = set(request.form.getlist("appliquer"))
    valeurs: dict = {}
    for champ, cible, _libelle in _CORRESPONDANCE:
        if champ in choisis:
            valeur = (donnees.get(champ) or "").strip()
            if valeur:
                valeurs[cible] = valeur
    return valeurs


def _creer_adverses_coches(adverses: list[dict], inv_id: str) -> int:
    """Créer les contacts « partie adverse » dont la case reste cochée (D-L3-2).

    Chaque création bumpe le CTag du carnet — le compteur retourné sert au
    message ; le bump, lui, appartient à l'appelant (un seul par requête).
    """
    coches = set(request.form.getlist("creer_adverse"))
    crees = 0
    for ligne in adverses:
        nom = (ligne.get("nom") or "").strip()
        if not nom or nom not in coches:
            continue
        _partie, erreurs = create_partie({
            "type": "individual",
            "contact_role": "partie_adverse",
            "last_name": nom,
            # Provenance dans le champ de notes EXISTANT — aucun champ nouveau,
            # et le juriste voit d'où vient la fiche.
            "notes": f"Déclaré par le client via le portail, invitation {inv_id}.",
        })
        if erreurs:
            logger.warning("reception: adverse contact refused: %s", erreurs)
            continue
        crees += 1
        log_portail_event("intake_adverse_cree", invitation_id=inv_id)
    return crees


def _cloturer(invitation: dict, batch: str, statut: str) -> None:
    """Clore une ouverture : statut + archivage de TOUTES ses enveloppes.

    Toutes, pas seulement celle qui vient d'être traitée : la ré-entrée permet
    au client de corriger, donc une invitation peut porter plusieurs lots dont
    seul le dernier a été examiné. Les précédents restaient sous
    ``submissions/``, que le cycle de vie purge à 90 jours — ce qui aurait fait
    disparaître, sans trace, ce que le client avait d'abord déclaré. Un
    formulaire traité ou refusé se conserve intégralement, comme un lot de
    documents.
    """
    inv_id = invitation["id"]
    pi.maj_statut(inv_id, statut)
    batches = [
        s.get("batch") for s in (invitation.get("soumissions") or [])
        if s.get("batch")
    ]
    if batch and batch not in batches:
        batches.append(batch)
    for b in batches:
        try:
            _archiver_enveloppe(inv_id, b)
        except Exception:
            # L'archivage rate → le cycle de vie du seau purgera à 90 jours.
            # Ne jamais faire échouer un versement déjà commis pour un
            # déplacement d'objet, ni interrompre les lots suivants.
            logger.exception("reception: intake envelope archive failed")


@reception_bp.post("/ouvertures/<inv_id>/<batch>/creer")
@login_required
def ouverture_creer(inv_id: str, batch: str):
    invitation, enveloppe = _ouverture_ou_erreur(inv_id, batch)
    if invitation is None or not enveloppe:
        return _rediriger(erreur="Soumission introuvable.", onglet="ouvertures")
    donnees = enveloppe.get("donnees") or {}

    valeurs = {
        cible: (donnees.get(champ) or "").strip()
        for champ, cible, _l in _CORRESPONDANCE
        if (donnees.get(champ) or "").strip()
    }
    valeurs["type"] = (
        "organization" if donnees.get("nature") == "morale" else "individual"
    )
    valeurs["contact_role"] = "client"
    # Section Conformité INTACTE (§5.3) : la collecte n'est pas une
    # vérification. create_partie applique déjà « non_vérifié » par défaut —
    # ne rien écrire ici est donc délibéré, pas un oubli.
    valeurs["notes"] = (
        f"Ouverture transmise par le client via le portail, "
        f"invitation {inv_id}."
    )

    partie, erreurs = create_partie(valeurs)
    if erreurs or partie is None:
        return _rediriger(
            erreur=" ".join(erreurs) or "Création impossible.",
            onglet="ouvertures",
        )

    crees = _creer_adverses_coches(enveloppe.get("parties_adverses") or [],
                                   inv_id)
    # ⚠️ Le bump du CTag vit dans la ROUTE, jamais dans le modèle : une
    # création de partie qui l'oublie n'atteint JAMAIS le carnet DavX5, en
    # silence. Un seul bump couvre la fiche cliente et les parties adverses.
    bump_ctag("parties")
    _cloturer(invitation, batch, "traitée")
    log_portail_event("intake_partie_creee", invitation_id=inv_id, batch=batch,
                      adverses_crees=crees)
    message = "Fiche créée — vérifiez et complétez-la."
    if crees:
        message += f" {crees} contact(s) « partie adverse » créé(s)."
    # url_for encode le message ; une concaténation le casserait au premier
    # « & » ou accent.
    return redirect(url_for("parties.partie_detail",
                            partie_id=partie["id"], message=message))


@reception_bp.post("/ouvertures/<inv_id>/<batch>/appliquer")
@login_required
def ouverture_appliquer(inv_id: str, batch: str):
    invitation, enveloppe = _ouverture_ou_erreur(inv_id, batch)
    if invitation is None or not enveloppe:
        return _rediriger(erreur="Soumission introuvable.", onglet="ouvertures")
    partie_id = invitation.get("partie_id") or ""
    if not partie_id or not get_partie(partie_id):
        return _rediriger(
            erreur="Le contact lié à cette invitation est introuvable.",
            onglet="ouvertures",
        )

    valeurs = _valeurs_choisies(enveloppe.get("donnees") or {})
    if valeurs:
        _maj, erreurs = update_partie(partie_id, valeurs)
        if erreurs:
            return _rediriger(erreur=" ".join(erreurs), onglet="ouvertures")

    crees = _creer_adverses_coches(enveloppe.get("parties_adverses") or [],
                                   inv_id)
    if valeurs or crees:
        bump_ctag("parties")
    _cloturer(invitation, batch, "traitée")
    log_portail_event("intake_partie_mise_a_jour", invitation_id=inv_id,
                      batch=batch, champs=len(valeurs), adverses_crees=crees)
    message = (
        f"{len(valeurs)} champ(s) appliqué(s)." if valeurs
        else "Aucun champ appliqué."
    )
    if crees:
        message += f" {crees} contact(s) « partie adverse » créé(s)."
    return redirect(url_for("parties.partie_detail",
                            partie_id=partie_id, message=message))


@reception_bp.post("/ouvertures/<inv_id>/<batch>/refuser")
@login_required
def ouverture_refuser(inv_id: str, batch: str):
    invitation, _enveloppe = _ouverture_ou_erreur(inv_id, batch)
    if invitation is None:
        return _rediriger(erreur="Soumission introuvable.", onglet="ouvertures")
    # D-L3-3 : aucun courriel au client — le suivi est humain.
    _cloturer(invitation, batch, "refusée")
    log_portail_event("intake_refuse", "refused", invitation_id=inv_id,
                      batch=batch)
    return _rediriger(message="Ouverture refusée.", onglet="ouvertures")
