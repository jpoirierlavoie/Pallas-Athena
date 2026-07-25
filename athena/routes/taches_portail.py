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
from datetime import datetime, timezone

from firebase_admin import storage
from flask import Blueprint, abort, jsonify, request
from markupsafe import escape

from client.config import PORTAIL_BUCKET, PORTAIL_QUEUE
from client.services import taches
from config import Config
from models import portail_invitation as pi
from services import portail_emission as emission
from tz import to_mtl
from utils import courriel
from utils.format_fr import format_date_fr
from utils.graph import GraphError, GraphNotConfigured
from utils.logging_setup import log_portail_event

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


# ── Accusé de réception (gabarit A.2) ────────────────────────────────────


def _taille_lisible(octets: int) -> str:
    if octets >= 1024 * 1024:
        return f"{octets / (1024 * 1024):.1f} Mo".replace(".", ",")
    if octets >= 1024:
        return f"{round(octets / 1024)} ko"
    return f"{octets} o"


def _corps_accuse(display_label: str, fichiers: list[dict],
                  quand_utc: datetime) -> tuple[str, str]:
    objet = f"Accusé de réception — {display_label}"
    local = to_mtl(quand_utc)
    quand = f"{format_date_fr(local.date())} à {local.strftime('%H h %M')}"
    lignes = "".join(
        "<tr>"
        f"<td style=\"padding:4px 12px 4px 0;\">{escape(f['name'])}</td>"
        f"<td style=\"padding:4px 12px 4px 0; white-space:nowrap;\">"
        f"{_taille_lisible(int(f.get('size_gcs') or 0))}</td>"
        f"<td style=\"padding:4px 0; font-family:monospace; font-size:11px; "
        f"word-break:break-all;\">{f.get('sha512') or ''}</td>"
        "</tr>"
        for f in fichiers
        if f.get("sha512")
    )
    corps = (
        "<p>Bonjour,</p>"
        f"<p>Nous accusons réception, le {quand}, des fichiers suivants :</p>"
        "<table style=\"border-collapse:collapse; font-size:13px;\">"
        "<tr><th align=\"left\" style=\"padding:4px 12px 4px 0;\">Fichier</th>"
        "<th align=\"left\" style=\"padding:4px 12px 4px 0;\">Taille</th>"
        "<th align=\"left\" style=\"padding:4px 0;\">Empreinte SHA-512</th></tr>"
        f"{lignes}</table>"
        "<p>L'empreinte SHA-512 est une signature numérique de l'intégrité de "
        "chaque fichier tel que reçu. Le présent accusé confirme uniquement la "
        "<strong>réception technique</strong> des fichiers énumérés ; il ne "
        "constitue ni une opinion sur leur contenu, ni leur versement au "
        "dossier, ni la formation ou la modification d'un mandat.</p>"
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
        invitation = pi.lire_invitation(inv_id) or {}
        # L'accusé atteste la date de RÉCEPTION (l'enveloppe écrite = la
        # soumission acquise, §7.4) — jamais l'heure de traitement, qui peut
        # suivre de plusieurs minutes (file, réconciliation).
        try:
            quand = datetime.fromisoformat(manifeste.get("submitted_at") or "")
        except ValueError:
            quand = datetime.now(timezone.utc)
        objet, corps = _corps_accuse(
            invitation.get("display_label", ""), recus, quand,
        )
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
        if invitation is None or not pi.est_active(invitation):
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
                continue  # batch never finalized — not a submission
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
