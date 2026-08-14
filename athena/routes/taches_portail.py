"""Gestionnaire Cloud Tasks + réconciliation cron du portail (spec L1 §8.3-8.4).

MACHINE blueprint — no @login_required, CSRF-exempt (main.py), reachable
only by App Engine-internal dispatches: the ``X-AppEngine-QueueName`` /
``X-Appengine-Cron`` headers are STRIPPED from all external traffic, so the
in-handler guards below are proof of origin (the before_request bypasses in
security.py/main.py rely on the same fact).

Retry semantics (§8.3): any raised exception → 5xx → the queue retries per
its policy; « nothing to do » branches return 200. Everything here is
idempotent — the single non-idempotent effect (the accusé email) is guarded
by the transactional test-and-set ``poser_accuse``.
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

from firebase_admin import storage
from flask import Blueprint, abort, jsonify, render_template, request

from client.config import PORTAIL_BUCKET, PORTAIL_QUEUE
from client.services import taches
from config import Config
from models import portail_invitation as pi
from models.dossier import get_dossier
from models.partie import display_name, get_partie
from services import portail_emission as emission
from tz import to_mtl
from utils import courriel
from utils.format_fr import format_date_fr
from utils.graph import GraphError, GraphNotConfigured
from utils.logging_setup import log_portail_event
from utils.template_fields import selected_address
from utils.validators import format_phone_display

logger = logging.getLogger(__name__)

taches_portail_bp = Blueprint(
    "taches_portail", __name__, url_prefix="/taches/portail"
)

_HASH_CHUNK = 8 * 1024 * 1024  # 8 MiB streaming slices (§8.3.b)


def _bucket():
    return storage.bucket(PORTAIL_BUCKET)


# ── SHA-512 en flux ──────────────────────────────────────────────────────


def sha512_flux(fileobj) -> str:
    """Streaming SHA-512 over 8 MiB slices — never the whole file in memory."""
    h = hashlib.sha512()
    while True:
        chunk = fileobj.read(_HASH_CHUNK)
        if not chunk:
            break
        h.update(chunk)
    return h.hexdigest()


def _sha512_blob(blob) -> str:
    with blob.open("rb") as f:
        return sha512_flux(f)


# ── Accusé de réception « bordereau » (spec A.2) ─────────────────────────

# Bloc DESTINATAIRE (cabinet) — coordonnées statiques du bordereau fourni,
# SANS le cellulaire (décision utilisateur 2026-07-25). Migration vers
# config.FIRM_* (partagé avec les factures) = suite possible.
_CABINET = {
    "nom": "Me Jason Poirier Lavoie",
    "organisation": "Poirier Lavoie, avocat",
    "adresse_lignes": [
        "9-4970, chemin de la Côte-des-Neiges",
        "Montréal (Québec) H3V 1A4",
    ],
    "telephone": "(514) 737-2525",
    "telecopieur": "(514) 737-6565",
}


def _taille_lisible(octets: int) -> str:
    if octets >= 1024 * 1024:
        return f"{octets / (1024 * 1024):.1f} Mo".replace(".", ",")
    if octets >= 1024:
        return f"{round(octets / 1024)} ko"
    return f"{octets} o"


def _adresse_lignes(partie: dict) -> list[str]:
    """3-line letter address (mirror of templates/parties/_address_letter).

    The BLOCK is chosen by ``template_fields.selected_address`` — the one
    authority, shared with the gabarits: reading ``address_*`` directly left a
    personne morale's accusé de réception with a blank address (a company's
    address can only be entered under « Adresse professionnelle »). Only the
    three-line letter assembly stays here; template_fields has no such format.
    """
    adresse = selected_address(partie)

    def champ(suffixe: str) -> str:
        return adresse.get(suffixe, "")

    provinces = {
        "QC": "Québec", "ON": "Ontario", "BC": "Colombie-Britannique",
        "AB": "Alberta", "MB": "Manitoba", "SK": "Saskatchewan",
        "NB": "Nouveau-Brunswick", "NS": "Nouvelle-Écosse",
        "PE": "Île-du-Prince-Édouard", "NL": "Terre-Neuve-et-Labrador",
        "YT": "Yukon", "NT": "Territoires du Nord-Ouest", "NU": "Nunavut",
    }
    pays = {"CA": "Canada", "US": "États-Unis"}
    street, unit = champ("street"), champ("unit")
    city = champ("city")
    prov = provinces.get(champ("province").upper(), champ("province"))
    postal = champ("postal_code")
    country = pays.get(champ("country").upper(), champ("country"))

    lignes: list[str] = []
    ligne1 = f"{unit}-{street}" if unit and street else street
    if ligne1:
        lignes.append(ligne1)
    ligne2 = city
    if prov:
        ligne2 = f"{ligne2} ({prov})" if ligne2 else f"({prov})"
    if postal:
        ligne2 = f"{ligne2} {postal}".strip()
    if ligne2.strip():
        lignes.append(ligne2.strip())
    if country:
        lignes.append(country)
    return lignes


def _client_expediteur(invitation: dict, partie: dict | None) -> dict:
    """Build the SENDER block. With a party → full contact; else name+email."""
    courriel_client = invitation.get("email", "")
    if partie is None:
        return {
            "nom": invitation.get("client_name") or courriel_client,
            "organisation": "",
            "adresse_lignes": [],
            "telephone": "",
            "courriel": courriel_client,
        }
    telephone = (
        partie.get("phone_cell") or partie.get("phone_home")
        or partie.get("phone_work") or ""
    )
    organisation = (
        partie.get("organization")
        if partie.get("type") != "organization" else ""
    )
    return {
        "nom": display_name(partie) or invitation.get("client_name") or courriel_client,
        "organisation": organisation or "",
        "adresse_lignes": _adresse_lignes(partie),
        "telephone": format_phone_display(telephone) if telephone else "",
        "courriel": partie.get("email") or courriel_client,
    }


def _corps_accuse(invitation: dict, fichiers: list[dict], quand_utc: datetime,
                  partie: dict | None, dossier: dict | None) -> tuple[str, str]:
    """Render the accusé « bordereau » email. Returns (objet, corps_html)."""
    display_label = invitation.get("display_label", "")
    objet = f"Accusé de réception — {display_label}"
    local = to_mtl(quand_utc)
    quand = f"{format_date_fr(local.date())} à {local.strftime('%H h %M')}"

    liste = [
        {
            "nom": f.get("name") or "",
            "taille": _taille_lisible(int(f.get("size_gcs") or 0)),
            "sha512": (f.get("sha512") or "").upper(),
        }
        for f in fichiers
        if f.get("sha512")
    ]
    dossier_ctx = None
    file_number = ""
    if dossier:
        dossier_ctx = {
            "court_file_number": dossier.get("court_file_number", ""),
            "title": dossier.get("title", ""),
        }
        file_number = dossier.get("file_number", "")

    corps = render_template(
        "reception/_accuse_bordereau.html",
        quand=quand,
        client=_client_expediteur(invitation, partie),
        cabinet=_CABINET,
        file_number=file_number,
        dossier=dossier_ctx,
        fichiers=liste,
    )
    return objet, corps


# ── Traitement d'une soumission (§8.3 « soumise ») ───────────────────────


def _construire_manifeste(bucket, inv_id: str, batch: str,
                          envelope: dict) -> dict:
    """(a)+(b): rapprocher fichiers déclarés ↔ objets présents, hacher."""
    prefix = f"submissions/{inv_id}/{batch}/"
    blobs = {b.name: b for b in bucket.list_blobs(prefix=prefix + "files/")}
    fichiers: list[dict] = []
    declares = set()
    for f in envelope.get("files", []):
        objet = str(f.get("objet") or "")
        declares.add(objet)
        entree = {
            "objet": objet,
            "name": str(f.get("name") or ""),
            "size_declared": int(f.get("size") or 0),
            "size_gcs": 0,
            "content_type": str(f.get("content_type") or ""),
            "sha512": None,
            "etat": "reçu",
            "divergence": None,
        }
        blob = blobs.get(objet)
        if blob is None:
            # Declared but never landed — recorded, never blocking (§8.3.a).
            entree["etat"] = "manquant"
            entree["divergence"] = "objet absent du stockage"
        else:
            entree["size_gcs"] = int(blob.size or 0)
            if entree["size_gcs"] != entree["size_declared"]:
                entree["divergence"] = "taille déclarée ≠ taille reçue"
            entree["sha512"] = _sha512_blob(blob)
        fichiers.append(entree)
    # Objects present but undeclared (a client that lost state mid-batch):
    # hashed and surfaced rather than silently ignored.
    for name, blob in sorted(blobs.items()):
        if name not in declares:
            fichiers.append({
                "objet": name,
                "name": name.rsplit("/", 1)[-1],
                "size_declared": 0,
                "size_gcs": int(blob.size or 0),
                "content_type": blob.content_type or "",
                "sha512": _sha512_blob(blob),
                "etat": "reçu",
                "divergence": "non déclaré dans l'enveloppe",
            })
    return {
        "batch": batch,
        "invitation_id": inv_id,
        "hashed_at": datetime.now(timezone.utc).isoformat(),
        # Recopiés de l'enveloppe pour que Réception (horodatage, IP/UA —
        # §9.2) et l'accusé (date de RÉCEPTION, pas de traitement) n'aient
        # jamais à relire envelope.json.
        "submitted_at": str(envelope.get("submitted_at") or ""),
        "http": envelope.get("http") or {},
        "files": fichiers,
        "etat_lot": "soumis",
    }


# A batch whose newest object stopped moving this long ago is considered
# abandoned rather than in flight (a 1 GiB upload on a slow link stays well
# inside this window; the cron runs every 15 min).
_ABANDON_APRES = timedelta(hours=2)


def _lot_abandonne(bucket, prefix: str) -> bool:
    """True when *prefix* holds files but no envelope, and has gone quiet.

    Never raises: the reconciliation sweep must finish even if one prefix
    misbehaves (the remaining lots still deserve their scan).
    """
    try:
        blobs = list(bucket.list_blobs(prefix=prefix + "files/"))
        if not blobs:
            return False
        recent = max(
            (b.updated for b in blobs if b.updated is not None), default=None
        )
        if recent is None:
            return False
        return datetime.now(timezone.utc) - recent > _ABANDON_APRES
    except Exception:
        logger.exception("reconciliation: abandoned-lot scan failed")
        return False


def _traiter_soumission(inv_id: str, batch: str) -> None:
    bucket = _bucket()
    prefix = f"submissions/{inv_id}/{batch}/"

    env_blob = bucket.blob(prefix + "envelope.json")
    if not env_blob.exists():
        # No envelope → the client never finalized; nothing durable exists.
        log_portail_event(
            "tache_recue", "refused",
            invitation_id=inv_id, batch=batch, reason="envelope_missing",
        )
        return

    manifest_blob = bucket.blob(prefix + "manifeste.json")
    if manifest_blob.exists():
        # Idempotence (§8.3.b / §13.m): hashes are computed exactly once.
        manifeste = json.loads(manifest_blob.download_as_bytes())
    else:
        envelope = json.loads(env_blob.download_as_bytes())
        manifeste = _construire_manifeste(bucket, inv_id, batch, envelope)
        manifest_blob.upload_from_string(
            json.dumps(manifeste, ensure_ascii=False),
            content_type="application/json",
        )
        log_portail_event(
            "manifeste_ecrit",
            invitation_id=inv_id, batch=batch,
            files_count=len(manifeste["files"]),
        )

    # (d) invitation: statut → soumise + append the submission if absent
    # (both idempotent). A failure here must RETRY — raise.
    recus = [f for f in manifeste["files"] if f.get("etat") != "manquant"]
    total = sum(int(f.get("size_gcs") or 0) for f in recus)
    if not pi.ajouter_soumission(inv_id, batch, len(recus), total):
        raise RuntimeError("portail invitation submission update failed")

    # (e) accusé — the transactional marker is the ONLY guard of the ONLY
    # non-idempotent effect. Once won, a send failure is logged at ERROR but
    # NOT raised: the marker is already set, so a retry could never resend —
    # it would only burn queue attempts on no-ops.
    if pi.poser_accuse(inv_id, batch):
        if not recus:
            # Aucun fichier n'a atteint GCS (lot abandonné en cours, ou
            # charge de finalisation falsifiée) : NE PAS expédier un accusé
            # attestant « réception des fichiers suivants » avec zéro fichier
            # (faux dans un contexte probatoire). Le marqueur poser_accuse a
            # été posé à dessein — il fait converger la réconciliation (les
            # objets ne réapparaîtront pas, le lot est clos côté client) au
            # lieu de la laisser ré-enfiler ce lot vide toutes les 15 min. Le
            # lot figure déjà en Réception (manifeste tout-« manquant » +
            # soumission files_count=0), où le juriste peut relancer le client.
            log_portail_event(
                "accuse_envoye", "refused",
                invitation_id=inv_id, batch=batch, reason="aucun_fichier_recu",
            )
            return
        invitation = pi.lire_invitation(inv_id) or {}
        # Sender (client) + judicial-file block resolved main-side; a lookup
        # failure degrades the block, never the accusé (best-effort).
        try:
            partie = get_partie(invitation.get("partie_id") or "")
        except Exception:
            partie = None
        try:
            dossier = get_dossier(invitation.get("dossier_id") or "")
        except Exception:
            dossier = None
        # L'accusé atteste la date de RÉCEPTION (l'enveloppe écrite = la
        # soumission acquise, §7.4) — jamais l'heure de traitement, qui peut
        # suivre de plusieurs minutes (file, réconciliation).
        try:
            quand = datetime.fromisoformat(manifeste.get("submitted_at") or "")
        except ValueError:
            quand = datetime.now(timezone.utc)
        # Le RENDU est aussi best-effort — et pour une raison plus grave que
        # les deux lectures ci-dessus : poser_accuse a DÉJÀ commis son
        # marqueur (le test-and-set transactionnel, unique garde de l'unique
        # effet non idempotent). Une exception ici remonterait en 5xx, Cloud
        # Tasks rejouerait, poser_accuse rendrait alors False — et l'accusé ne
        # partirait JAMAIS, sans même un courriel_echec, la réconciliation
        # jugeant le lot complet. L'au-plus-une-fois dégénérerait en
        # zéro-fois pour un bordereau à valeur probatoire. On dégrade donc en
        # échec de courriel, journalisé, comme le fait déjà le bloc d'envoi.
        try:
            objet, corps = _corps_accuse(invitation, recus, quand, partie, dossier)
        except Exception:
            logger.exception("accusé render failed")
            log_portail_event(
                "courriel_echec", "failure",
                invitation_id=inv_id, batch=batch, reason="rendu_accuse",
            )
            return
        try:
            courriel.envoyer(invitation.get("email", ""), objet, corps)
            log_portail_event(
                "accuse_envoye", invitation_id=inv_id, batch=batch,
            )
        except GraphNotConfigured:
            log_portail_event(
                "courriel_echec", "refused",
                invitation_id=inv_id, batch=batch,
                reason="graph_not_configured",
            )
        except GraphError:
            logger.exception("accusé send failed")
            log_portail_event(
                "courriel_echec", "failure",
                invitation_id=inv_id, batch=batch, reason="graph_error",
            )


def _enveloppe_intake_valide(envelope: dict) -> bool:
    """Contrôle de forme minimal (§4.2) — le portail a déjà tout borné."""
    return (
        isinstance(envelope, dict)
        and envelope.get("type") == "intake"
        and isinstance(envelope.get("donnees"), dict)
        and isinstance(envelope.get("parties_adverses"), list)
        and isinstance(envelope.get("consentement"), dict)
    )


def _corps_confirmation_intake(quand_utc: datetime) -> tuple[str, str]:
    """Gabarit A.3 — confirmation de RÉCEPTION, jamais d'ouverture.

    Le libellé est juridiquement chargé : accuser réception d'un formulaire ne
    forme aucun mandat et n'ouvre aucun dossier. La dernière phrase le dit
    explicitement, et ne doit pas être adoucie. Aucune donnée du formulaire
    n'est reprise dans le courriel — la boîte du client n'est pas le lieu où
    faire circuler les noms qu'il vient de déclarer.
    """
    local = to_mtl(quand_utc)
    quand = f"{format_date_fr(local.date())} à {local.strftime('%H h %M')}"
    objet = "Confirmation de réception — formulaire d'ouverture"
    corps = render_template(
        "reception/_confirmation_intake.html",
        quand=quand, cabinet=_CABINET,
    )
    return objet, corps


def _traiter_intake(inv_id: str, batch: str, invitation: dict) -> None:
    """Branche « ouverture » du traitement d'une soumission (L3 §4.2).

    Aucune empreinte de fichier : l'enveloppe EST la soumission. Ce qui doit
    absolument arriver, c'est que ``soumissions[]`` ET ``accuses[batch]``
    finissent renseignés — la réconciliation ne connaît que ces deux critères,
    donc un lot qui n'en pose qu'un serait ré-enfilé toutes les 15 minutes à
    perpétuité.
    """
    bucket = _bucket()
    prefix = f"submissions/{inv_id}/{batch}/"
    env_blob = bucket.blob(prefix + "envelope.json")
    if not env_blob.exists():
        log_portail_event(
            "tache_recue", "refused",
            invitation_id=inv_id, batch=batch, reason="envelope_missing",
        )
        return

    try:
        envelope = json.loads(env_blob.download_as_bytes())
    except Exception:
        logger.exception("intake envelope unreadable")
        envelope = {}

    lisible = _enveloppe_intake_valide(envelope)
    if not lisible:
        # Consigner FORT, mais poser quand même les deux marqueurs : sans eux
        # la réconciliation ré-enfilerait ce lot indéfiniment. Réception
        # l'affiche avec un bandeau, donc rien ne disparaît en silence.
        log_portail_event(
            "intake_soumis", "failure",
            invitation_id=inv_id, batch=batch, reason="enveloppe_malformee",
        )

    if not pi.ajouter_soumission(inv_id, batch, 0, 0):
        raise RuntimeError("portail invitation submission update failed")

    if pi.poser_accuse(inv_id, batch):
        if not lisible:
            log_portail_event(
                "intake_confirmation_envoyee", "refused",
                invitation_id=inv_id, batch=batch,
                reason="enveloppe_malformee",
            )
            return
        try:
            quand = datetime.fromisoformat(envelope.get("submitted_at") or "")
        except ValueError:
            quand = datetime.now(timezone.utc)
        objet, corps = _corps_confirmation_intake(quand)
        try:
            courriel.envoyer(invitation.get("email", ""), objet, corps)
            log_portail_event(
                "intake_confirmation_envoyee",
                invitation_id=inv_id, batch=batch,
            )
        except GraphNotConfigured:
            log_portail_event(
                "courriel_echec", "refused",
                invitation_id=inv_id, batch=batch,
                reason="graph_not_configured",
            )
        except GraphError:
            logger.exception("intake confirmation send failed")
            log_portail_event(
                "courriel_echec", "failure",
                invitation_id=inv_id, batch=batch, reason="graph_error",
            )


# ── POST /taches/portail/evenement ───────────────────────────────────────


@taches_portail_bp.post("/evenement")
def evenement():
    # Proof of origin: App Engine strips X-AppEngine-* from ALL external
    # traffic; only a genuine dispatch from OUR queue carries this value.
    if request.headers.get("X-AppEngine-QueueName") != PORTAIL_QUEUE:
        abort(403)

    retry_count = request.headers.get("X-AppEngine-TaskRetryCount", "0")
    payload = request.get_json(silent=True) or {}
    event = payload.get("event")
    inv_id = str(payload.get("invitation_id") or "")
    batch = payload.get("batch")

    log_portail_event(
        "tache_recue", invitation_id=inv_id or None, batch=batch,
        evenement=str(event), retry_count=retry_count,
    )

    # A malformed payload is a bug, not a transient: retrying it ten times
    # would change nothing. 200 + WARNING instead of a retry storm.
    if event not in taches.EVENEMENTS or not inv_id:
        log_portail_event(
            "tache_recue", "refused", invitation_id=inv_id or None,
            reason="malformed",
        )
        return jsonify({"ok": False, "motif": "charge invalide"}), 200

    if event == "ouverte":
        # Transactional CAS — a plain read-check-write could race the
        # « soumise » task and regress the statut (Cloud Tasks guarantees
        # no ordering).
        if not pi.marquer_ouverte(inv_id):
            raise RuntimeError("statut ouverte update failed")  # retry
        return jsonify({"ok": True})

    if event == "renvoi":
        invitation = pi.lire_invitation(inv_id)
        # peut_relancer, NOT est_active: must mirror the portal's own test
        # (D-2 allows a resend while « soumise »; D-4 caps the total), or this
        # pre-check silently drops a renvoi the client was told was sent.
        if invitation is None or not pi.peut_relancer(invitation):
            return jsonify({"ok": True, "motif": "invitation inactive"})
        ok, _message, lien_manuel = emission.renvoyer_invitation(inv_id)
        if not ok:
            raise RuntimeError("renvoi failed")  # possibly transient — retry
        if lien_manuel and Config.graph_configured():
            # L'envoi a échoué (GraphError transitoire) alors que Graph EST
            # configuré ; en contexte de tâche, personne ne peut remettre le
            # lien manuel au client — lever pour que la file réessaie
            # (§8.3 : échec → lever, la reprise appartient à Cloud Tasks).
            # Graph non configuré : rien à réessayer, 200.
            raise RuntimeError("renvoi email send failed")
        return jsonify({"ok": True})

    # event == "soumise"
    if not batch:
        log_portail_event(
            "tache_recue", "refused", invitation_id=inv_id, reason="no_batch",
        )
        return jsonify({"ok": False, "motif": "charge invalide"}), 200
    # Aiguillage documents / ouverture. Il se fait sur l'INVITATION, pas sur
    # l'enveloppe : la lecture de envelope.json vit à l'intérieur du
    # court-circuit d'idempotence de _traiter_soumission (« si le manifeste
    # existe, ne rien recalculer »), donc un aiguillage placé là ne
    # s'exécuterait qu'au premier essai — jamais sur un rejeu ni sur une
    # réparation de la réconciliation.
    invitation = pi.lire_invitation(inv_id)
    if invitation is not None and invitation.get("type") == "intake":
        _traiter_intake(inv_id, str(batch), invitation)
    else:
        _traiter_soumission(inv_id, str(batch))
    return jsonify({"ok": True})


# ── GET /taches/portail/reconciliation (cron, §8.4) ──────────────────────


@taches_portail_bp.get("/reconciliation")
def reconciliation():
    # Cron requests carry X-Appengine-Cron: true — stripped from external
    # traffic exactly like the queue header.
    if request.headers.get("X-Appengine-Cron") != "true":
        abort(403)

    bucket = _bucket()
    lots_vus = 0
    repares = 0

    # Two-level prefix walk (delimiter "/"): submissions/{inv}/{batch}/ —
    # trivial volume at a single-practice scale.
    racine = bucket.list_blobs(prefix="submissions/", delimiter="/")
    list(racine)  # consume the iterator so .prefixes populates
    for p_inv in sorted(racine.prefixes):
        niveau2 = bucket.list_blobs(prefix=p_inv, delimiter="/")
        list(niveau2)
        for p_batch in sorted(niveau2.prefixes):
            if not bucket.blob(p_batch + "envelope.json").exists():
                # No envelope = the client never pressed « Soumettre ». Usually
                # an upload still in flight — but if the newest object stopped
                # moving a while ago, the batch is ABANDONED: nothing will ever
                # reference it, Réception cannot see it, and the 90-day
                # lifecycle deletes the client's files unnoticed. Surface it.
                if _lot_abandonne(bucket, p_batch):
                    parts = p_batch.strip("/").split("/")
                    log_portail_event(
                        "lot_abandonne", "failure",
                        invitation_id=parts[1], batch=parts[2],
                    )
                continue
            lots_vus += 1
            parts = p_batch.strip("/").split("/")
            inv_id, batch = parts[1], parts[2]

            invitation = pi.lire_invitation(inv_id)
            complet = False
            if invitation:
                batches = {
                    s.get("batch")
                    for s in (invitation.get("soumissions") or [])
                }
                complet = batch in batches and bool(
                    (invitation.get("accuses") or {}).get(batch)
                )
            if complet:
                continue  # fully processed — intact (§8.4.4)

            # The queue lost work — replay it. Every repair is an ERROR by
            # design: it must be SEEN (§8.4.3). A failed re-enqueue must not
            # abort the sweep — the remaining lots still deserve their scan
            # (this lot comes back next cycle).
            try:
                taches.signaler("soumise", inv_id, batch=batch)
            except Exception:
                logger.exception("reconciliation re-enqueue failed")
                log_portail_event(
                    "tache_enfilage_echec", "failure",
                    invitation_id=inv_id, batch=batch, evenement="soumise",
                )
                continue
            repares += 1
            log_portail_event(
                "reconciliation_reparation", "failure",
                invitation_id=inv_id, batch=batch,
            )

    log_portail_event(
        "reconciliation_execute", lots_vus=lots_vus, lots_repares=repares,
    )
    return jsonify({"lots_vus": lots_vus, "repares": repares})
