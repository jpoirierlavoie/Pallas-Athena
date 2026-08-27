"""La charte système du clavardage — éditable et versionnée (Phase N).

La charte gouverne CHAQUE tour de CHAQUE conversation. Elle vivait en dur
dans ``chat/charter.py`` ; depuis le 2026-08-27 son corps éditable et son
addendum d'exécution planifiée vivent ici, sur le patron append-only de
``models/chat_skill.py`` : une tête (``chat_charter/charte``, Règle 7
complète) plus une sous-collection ``versions/{n:06d}`` write-once
(dérogation Règle 7 documentée : write-once, sans etag), plus les fichiers
de référence adressés par contenu de ``models/chat_reference_files.py``.
**La désactivation n'existe pas** (il y a toujours exactement une charte),
**la suppression non plus** — épinglées par balayage de source.

⚠ **La version 1 est le TEXTE SOURCE, pour toujours ; Firestore commence
à 2.** Tous les tours enregistrés avant ce lot portent ``charter_version:
1``, et c'est ce que la version 1 doit continuer de désigner : les octets
de ``chat.charter.BASE_CHARTER``. Faire repartir Firestore à 1 ferait
mentir le registre sur tout son passé. ``FIRST_FIRESTORE_VERSION`` est le
plancher, appliqué par ``max()`` dans la transaction, pour qu'une tête
corrompue ne puisse pas non plus frapper un 1.

⚠ **Ce module ignore délibérément ``chat/``.** ``get_version(1)`` rend
``None`` : résoudre le texte source est l'affaire de l'appelant
(``chat/turn_engine.py``, qui importe déjà ``chat.charter``). Le sens des
dépendances reste donc ``chat/`` → ``models/``, jamais l'inverse, et
``is_source_version`` est là pour qu'aucun appelant n'ait à connaître le
nombre.

⚠ **Un corps blanc est traité comme ILLISIBLE** par ``get_head``. Il ne
peut pas être écrit (``_validate`` le refuse), mais une écriture hors
application le pourrait — et ``charter.system_blocks`` construit le bloc 0
sans garde, si bien qu'un bloc texte vide fait répondre 400 à Vertex sur
TOUTES les conversations à la fois, jusqu'à ré-édition. Le refus est donc
à l'écriture ET à la lecture.

Le singleton porte un identifiant CONSTANT, ``charte`` — dérogation
documentée à la Règle 6, du même type que ``oauth_clients/{client_id}`` :
la clé de recherche EST l'identifiant. Un uuid4 exigerait un second
mécanisme de repérage (un drapeau interrogé par requête, ou un document
pointeur) et donc un ``limit(1)`` qui peut rendre le mauvais document
après une écriture partielle, là où l'assemblage a besoin d'un ``get()``
cléé qui existe ou n'existe pas. C'est aussi ce qui rend la création
implicite transactionnelle possible : sans identifiant connu d'avance, il
n'y a pas de référence à lire dans la transaction.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from google.cloud import firestore

from models import chat_reference_files as reference_files
from models import db
from security import sanitize
from utils.logging_setup import log_unexpected

logger = logging.getLogger(__name__)

COLLECTION = "chat_charter"
VERSIONS_SUBCOLLECTION = "versions"

# The singleton's id, and the reserved `skill_id` the model uses to read a
# charter reference file through `get_skill_file`. `uuid.UUID("charte")`
# raises, so it can never collide with a skill's id.
DOC_ID = "charte"

SOURCE_VERSION = 1
FIRST_FIRESTORE_VERSION = 2

# The body enters the system prompt of EVERY turn — the same reasoning as a
# skill body, except there is no opting out of it. The floor exists so a
# half-finished save cannot neuter the charter; over-long is REFUSED, never
# truncated (`security.sanitize` would cut silently, and the constitution
# losing its last rule without a word is exactly the failure to prevent).
BODY_MIN_LENGTH = 200
BODY_MAX_LENGTH = 30_000
# The addendum only applies to unattended runs; it is ~700 chars today.
ADDENDUM_MAX_LENGTH = 10_000

# Re-exported: the form template mirrors them in maxlength attributes.
#
# The budget, redone for this carrier: 30 000 (body) + 10 000 (addendum)
# + 6 × 40 000 (files) = 280 000 chars. The form posts MULTIPART, where a
# character costs 1 byte in ASCII, 2 accented, 3 for an em dash — so
# ~297 KB worst case against the 1 MB `_enforce_request_size` ceiling,
# 47 % of margin whatever the alphabet. Urlencoded it would be 1.6 MiB at
# a true ×6 and abort 413 into a raw error page, losing every character
# typed. Raising a cap means redoing this, not editing the number.
MAX_FILES = reference_files.MAX_FILES
FILE_MAX_CHARS = reference_files.FILE_MAX_CHARS
FILE_NAME_MAX_LENGTH = reference_files.FILE_NAME_MAX_LENGTH
FILE_DESCRIPTION_MAX_LENGTH = reference_files.FILE_DESCRIPTION_MAX_LENGTH


def is_source_version(version: object) -> bool:
    """True for the version numbers Firestore does NOT hold.

    The caller resolves those from ``chat.charter``'s constants, without a
    read — which is what keeps a chain born in fallback alive: its pin is
    1, a version that will never exist in ``versions/``, and a keyed get
    would fail for ever into retry exhaustion.
    """
    try:
        return int(version) < FIRST_FIRESTORE_VERSION
    except (TypeError, ValueError):
        return True


def _default_doc() -> dict:
    return {
        "id": DOC_ID,
        "body": "",
        "addendum": "",
        "files": [],
        "current_version": 0,
        "created_at": None,
        "updated_at": None,
        "etag": "",
    }


def _clean(body: object, addendum: object) -> tuple[str, str, list[str]]:
    """Validate FIRST, sanitize second — the order is the whole point.

    ``sanitize(v, max_length=N)`` truncates to N *before* stripping tags,
    so measuring the RESULT would let an over-long body pass silently,
    shortened. The length is therefore checked on the raw value; the floor
    is checked on the sanitized one, because tag-stripping can shorten it.
    """
    errors: list[str] = []
    raw_body = str(body or "")
    raw_addendum = str(addendum or "")

    if len(raw_body) > BODY_MAX_LENGTH:
        errors.append(
            f"Le corps de la charte dépasse "
            f"{reference_files.format_int_fr(BODY_MAX_LENGTH)} caractères "
            f"({reference_files.format_int_fr(len(raw_body))})."
        )
    if len(raw_addendum) > ADDENDUM_MAX_LENGTH:
        errors.append(
            f"L'addendum dépasse "
            f"{reference_files.format_int_fr(ADDENDUM_MAX_LENGTH)} "
            f"caractères ({reference_files.format_int_fr(len(raw_addendum))})."
        )
    if errors:
        return "", "", errors

    clean_body = sanitize(raw_body, max_length=BODY_MAX_LENGTH).strip()
    clean_addendum = sanitize(
        raw_addendum, max_length=ADDENDUM_MAX_LENGTH
    ).strip()

    if not clean_body:
        errors.append("Le corps de la charte est requis.")
    elif len(clean_body) < BODY_MIN_LENGTH:
        errors.append(
            f"Le corps de la charte est trop court (au moins "
            f"{BODY_MIN_LENGTH} caractères) — une charte quasi vide "
            "gouvernerait chaque tour de chaque conversation."
        )
    return clean_body, clean_addendum, errors


def _version_ref(version: int):
    return (
        db.collection(COLLECTION)
        .document(DOC_ID)
        .collection(VERSIONS_SUBCOLLECTION)
        .document(f"{int(version):06d}")
    )


def _version_doc(head: dict, now: datetime) -> dict:
    return {
        "version": int(head["current_version"]),
        "body": head["body"],
        "addendum": head["addendum"],
        "files": list(head.get("files") or []),
        "created_at": now,
    }


def revise_charter(
    *,
    body: str,
    addendum: str = "",
    files: Optional[list] = None,
) -> tuple[Optional[dict], list[str]]:
    """Append the next version and move the head — transactionally.

    **One verb, creation included.** A separate ``create_charter`` would
    need a non-transactional first write, and two tabs open on a blank form
    would then both mint version 2: the second ``set`` overwrites a
    write-once version doc in silence, after which every turn already
    stamped ``2`` designates a text nobody ever saw. Reading the reference
    inside the transaction removes the case rather than guarding it.

    ``files=None`` keeps the current manifest (no content writes); a list —
    even ``[]`` — REPLACES it. Prior versions keep their own manifests and
    no content doc is ever removed.
    """
    clean_body, clean_addendum, errors = _clean(body, addendum)
    entries: Optional[list[dict]] = None
    if files is not None:
        entries, file_errors = reference_files.validate_files(files)
        errors.extend(file_errors)
    if errors:
        return None, errors

    doc_ref = db.collection(COLLECTION).document(DOC_ID)
    transaction = db.transaction()

    @firestore.transactional
    def _txn(txn) -> tuple[Optional[dict], list[str]]:
        snap = doc_ref.get(transaction=txn)
        now = datetime.now(timezone.utc)
        head = (snap.to_dict() or {}) if snap.exists else _default_doc()
        head["id"] = DOC_ID
        head["body"] = clean_body
        head["addendum"] = clean_addendum
        if entries is not None:
            for sha, payload in reference_files.content_writes(
                entries, now
            ).items():
                txn.set(
                    reference_files.file_ref(db, COLLECTION, DOC_ID, sha),
                    payload,
                )
            head["files"] = reference_files.manifest(entries)
        else:
            head["files"] = list(head.get("files") or [])
        # `max` is the floor, not decoration: a corrupted head must never
        # mint version 1, which for ever designates the SOURCE text.
        suivante = int(head.get("current_version") or 0) + 1
        head["current_version"] = max(suivante, FIRST_FIRESTORE_VERSION)
        head["created_at"] = head.get("created_at") or now
        head["updated_at"] = now
        head["etag"] = str(uuid.uuid4())
        txn.set(_version_ref(head["current_version"]), _version_doc(head, now))
        txn.set(doc_ref, head)
        return head, []

    try:
        return _txn(transaction)
    except Exception:
        log_unexpected("chat charter revise failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]


def get_head() -> tuple[Optional[dict], str]:
    """The current charter, and WHY when there isn't one.

    Three states, never two: ``ok`` · ``absent`` (nothing saved yet — the
    normal state of a fresh deployment, and not an incident) · ``erreur``
    (unreadable, or a blank body, which would build an empty system block
    and 400 every conversation at once). A bare ``None`` would conflate the
    last two, so the caller would either cry wolf on every new install or
    swallow real outages.
    """
    try:
        snap = db.collection(COLLECTION).document(DOC_ID).get()
    except Exception as exc:
        logger.warning("charter head read failed: %s", exc)
        return None, "erreur"
    if not snap.exists:
        return None, "absent"
    doc = snap.to_dict() or {}
    if not str(doc.get("body") or "").strip():
        logger.warning("charter head has a blank body")
        return None, "erreur"
    return doc, "ok"


def get_version(version: int) -> Optional[dict]:
    """One stored version — the seam a chain's step 2+ resolves through.

    Returns ``None`` for a source version (see ``is_source_version``): this
    module holds no source text, by design.
    """
    if is_source_version(version):
        return None
    try:
        snap = _version_ref(version).get()
        if snap.exists:
            return snap.to_dict()
    except Exception as exc:
        logger.warning("charter version read failed: %s", exc)
    return None


def get_version_file(
    version: int, filename: str
) -> tuple[Optional[str], Optional[str]]:
    """A charter reference file's content AT A PINNED VERSION."""
    if is_source_version(version):
        return None, "La charte en vigueur n'a aucun fichier de référence."
    doc = get_version(version)
    if doc is None:
        return None, "Version de la charte introuvable."
    rows = doc.get("files") or []
    if not rows:
        return None, "La charte n'a aucun fichier de référence."
    return reference_files.read_from_manifest(
        db, COLLECTION, DOC_ID, rows, filename
    )


def list_versions(limit: int = 50) -> list[dict]:
    try:
        query = (
            db.collection(COLLECTION)
            .document(DOC_ID)
            .collection(VERSIONS_SUBCOLLECTION)
            .order_by("version", direction=firestore.Query.DESCENDING)
            .limit(max(1, int(limit)))
        )
        return [snap.to_dict() for snap in query.stream()]
    except Exception:
        logger.warning("charter list_versions failed", exc_info=True)
        return []


def list_file_contents(rows: list[dict]) -> list[dict]:
    """The UI seam — fails OPEN per file (see the shared helper)."""
    return reference_files.list_contents(db, COLLECTION, DOC_ID, rows)
