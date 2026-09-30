"""A COPY of an administration dépense's pièce justificative, filed in its
dossier (2026-09-30).

A dépense of the administration ledger may be linked to a dossier and carry
a pièce justificative — the supplier's invoice for a transcript, a bailiff's
note. The receipt lives at the FIRM level (``users/{uid}/administration/
{tx_id}/…``), on the entry, where the ledger needs it; the dossier needs it
too, among its documents, where the lawyer looks for what a disbursement
cost. This service files a COPY there, in the dossier's « Mandat ›
Déboursés » system folder (role ``debourses``, found by ROLE — never by
name, never the dossier root), and leaves the original where it is: the
firm-level object is only READ, never moved nor deleted.

ONE predicate decides what may be copied (:func:`admissible`): a
``dépense`` linked to a dossier and carrying a receipt, still standing —
never one reversed (``reversed_by_id``) or cancelled (``annulée``): a
cancelled disbursement filed in « Déboursés » would tell the dossier it
cost something it no longer does. Never an encaissement, an other recette
or a correction either — their « receipt » is not a disbursement of the
dossier.

The link lives on the DOCUMENT (``source_admin_transaction_id`` +
``source_receipt_md5``, written by ``models/document.ingest_blob_as_document
(from_admin_receipt=…)``); the only register field involved is the entry's
``receipt_md5``, which ``models/admin_ledger.attach_receipt`` writes with
the receipt itself. That is how a copy of the CURRENT receipt is told apart
from a copy of a receipt since replaced, with no second register write.

:func:`verser_recu_au_dossier`, in order — every refusal but the copy's
own comes back BEFORE anything is written:

1. the uid through ``storage_identity.require_uid`` — FIRST, before any
   folder is touched (the ``routes/reception.verser`` lesson: a model's own
   uid check runs too late, after its caller has created the folder);
2. the entry through the STRICT reader; not admissible → ``inadmissible``;
3. the dossier through the STRICT reader (absent or unreadable → refused);
4. the receipt's MD5 — the entry's ``receipt_md5``, or, for a receipt
   attached before that field existed, the firm object's own after a
   reload;
5. the copies already filed (``find_receipt_copies``, STRICT: « none » on
   a transient error would be a duplicate): a copy of THIS receipt in THIS
   dossier is the answer (``deja``), nothing written;
6. the source reloaded and its MD5 re-checked (unreadable →
   ``recu_illisible``; changed meanwhile → ``recu_modifie``);
7. the « Déboursés » folder through ``ensure_system_folder`` — a failure
   REFUSES, never files at the root;
8. the copy, a GCS-side rewrite under a RESERVED document id (a second call
   with the same id answers the first's document, never a second one). A
   copy that FAILS here (``ingestion``) comes after step 7, so it can leave
   « Mandat › Déboursés » created behind it — folders a retry reproduces at
   the same ids, never a second pair (the « Projets » case of a failed
   generation).

A refusal carries a machine ``reason`` beside its French ``errors``: the
routes turn the two LASTING ones — the entry's dossier is gone
(``dossier_introuvable``), the stored receipt changed under the call
(``recu_modifie``) — into their own CLOSED banner, since « réessayez » is
no answer to either.

Reached ONLY from ``routes/admin_ledger.py`` (the receipt finalization and
« Verser une copie au dossier »). The connector never reaches it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from firebase_admin import storage

from models import admin_ledger
from models.document import (
    ALLOWED_EXTENSIONS,
    find_receipt_copies,
    ingest_blob_as_document,
)
from models.dossier import get_dossier_strict
from models.folder import SYSTEM_ROLE_DEBOURSES, ensure_system_folder, get_folder
from utils import storage_identity
from utils.logging_setup import log_admin_ledger_event, log_unexpected

# The kind whose receipt is a disbursement of the dossier. An encaissement,
# an other recette or a correction never is.
KIND_ADMISSIBLE = "dépense"
# A cancelled entry (an uncleared one reversed: both legs « annulée ») is no
# disbursement of the dossier any more.
STATUT_ANNULEE = "annulée"
DOCUMENT_CATEGORY = "déboursé"
DOCUMENT_TAG = "pièce_justificative"

# Machine codes of the outcome — a route maps them to its CLOSED
# ``?avertissement=`` codes; the French travels in ``errors``.
VERSEE = "versee"
DEJA = "deja"
INADMISSIBLE = "inadmissible"
REFUSEE = "refusee"

# Machine REASONS of a refusal (``Versement.reason``) — the two a caller
# gives its own banner, because retrying cannot help: the dossier the entry
# names is gone, and the receipt was replaced while the copy was being made
# (the page the lawyer acted from is stale). The others are logged under
# their own reason and read, on the page, as the generic « réessayez ».
REASON_DOSSIER_INTROUVABLE = "dossier_introuvable"
REASON_RECU_MODIFIE = "recu_modifie"

MSG_INADMISSIBLE = (
    "Seule une dépense liée à un dossier et munie d'une pièce justificative "
    "peut en verser une copie au dossier. Rien n'a été versé."
)
MSG_ENTRY_NOT_FOUND = "Écriture introuvable. Rien n'a été versé."
MSG_READ_FAILED = (
    "Lecture impossible pour le moment — réessayez. Rien n'a été versé."
)
MSG_DOSSIER_NOT_FOUND = (
    "Le dossier de cette écriture est introuvable. Rien n'a été versé."
)
MSG_RECEIPT_UNREADABLE = (
    "La pièce justificative de l'écriture n'a pas pu être lue — réessayez. "
    "Rien n'a été versé."
)
MSG_RECEIPT_CHANGED = (
    "La pièce justificative a changé pendant le versement — rechargez la "
    "page, puis réessayez. Rien n'a été versé."
)


@dataclass
class Versement:
    """The outcome: the document (filed now, or already there), the French
    refusals, a machine code (``versee`` | ``deja`` | ``inadmissible`` |
    ``refusee``) and, on a refusal, its machine ``reason`` (``""`` when none
    was recorded — see ``REASON_*``)."""

    document: Optional[dict]
    errors: list[str] = field(default_factory=list)
    code: str = REFUSEE
    reason: str = ""


def admissible(entry: Optional[dict]) -> bool:
    """True when *entry*'s receipt may be copied into its dossier: a
    ``dépense``, linked to a dossier, carrying a pièce justificative, and
    still STANDING — neither reversed (``reversed_by_id``: a cleared one
    keeps « compensée » beside its reversal) nor cancelled (``annulée``). The
    ONE rule — the route, the entry page and the service all read it."""
    if not isinstance(entry, dict):
        return False
    return (
        entry.get("kind") == KIND_ADMISSIBLE
        and isinstance(entry.get("dossier_id"), str)
        and bool(entry.get("dossier_id"))
        and isinstance(entry.get("receipt_storage_path"), str)
        and bool(entry.get("receipt_storage_path"))
        and not entry.get("reversed_by_id")
        and entry.get("status") != STATUT_ANNULEE
    )


def copie_du_recu(copies, dossier_id: str, md5: str) -> Optional[dict]:
    """Among *copies* (``find_receipt_copies``, newest first), the copy of
    the receipt whose MD5 is *md5* filed in *dossier_id* — or ``None``.

    An empty *md5* is an entry whose receipt was attached before
    ``receipt_md5`` existed: that receipt has not been replaced since (a
    replacement writes the field), so every copy of the entry filed in the
    dossier is a copy of it."""
    for doc in copies or ():
        if doc.get("dossier_id") != dossier_id:
            continue
        if not md5 or doc.get("source_receipt_md5") == md5:
            return doc
    return None


def etat_des_copies(entry: dict) -> Optional[dict]:
    """What the entry page shows — DISPLAY only, failing OPEN to ``None``
    (then it shows nothing extra: no « copie versée » claim, no button).

    ``{"courante": the copy of the CURRENT receipt in the entry's dossier,
    or None; "dans_debourses": that copy still sits in « Mandat ›
    Déboursés » (the lawyer may have refiled it — the page then names the
    dossier only, never a folder it left); "autres": [{"document_id",
    "dossier_file_number"}, …] — the copies filed in ANOTHER dossier (the
    entry's dossier changed since), one per dossier; "remplacees":
    [{"document_id", "dossier_file_number"}, …] — every copy of a receipt
    since REPLACED that still sits in the entry's dossier (same entry, a
    ``source_receipt_md5`` other than the entry's ``receipt_md5``), one per
    document, so the lawyer can find a wrong receipt and remove it himself
    — nothing here ever deletes one}``.

    With no ``receipt_md5`` (a receipt attached before the field existed)
    no copy can be told replaced: every copy of the entry is taken for the
    current receipt's (:func:`copie_du_recu`), and ``remplacees`` is
    empty."""
    if not entry.get("receipt_storage_path"):
        return {"courante": None, "dans_debourses": False, "autres": [],
                "remplacees": []}
    try:
        copies = find_receipt_copies(entry.get("id") or "")
    except Exception:
        log_unexpected("admin: receipt copies read failed",
                       transaction_id=entry.get("id"))
        return None
    dossier_id = entry.get("dossier_id") or ""
    md5 = str(entry.get("receipt_md5") or "")
    courante = copie_du_recu(copies, dossier_id, md5) if dossier_id else None
    dans_debourses = False
    if courante is not None and courante.get("folder_id"):
        folder = get_folder(dossier_id, courante["folder_id"])   # fail-open
        dans_debourses = bool(
            folder and folder.get("system_role") == SYSTEM_ROLE_DEBOURSES)
    autres, vus = [], set()
    for doc in copies:
        other = doc.get("dossier_id") or ""
        if not other or other == dossier_id or other in vus:
            continue
        vus.add(other)
        autres.append({
            "document_id": doc.get("id") or "",
            "dossier_file_number": doc.get("dossier_file_number") or "",
        })
    remplacees = [
        {"document_id": doc["id"],
         "dossier_file_number": doc.get("dossier_file_number") or ""}
        for doc in copies
        if md5 and dossier_id
        and doc.get("dossier_id") == dossier_id
        and doc.get("source_receipt_md5") != md5
        and isinstance(doc.get("id"), str) and doc.get("id")
    ]
    return {"courante": courante, "dans_debourses": dans_debourses,
            "autres": autres, "remplacees": remplacees}


def dossier_introuvable(entry: dict) -> bool:
    """True ONLY when the store SAYS the entry's dossier does not exist —
    DISPLAY: the entry page then hides « Verser une copie au dossier », a
    button that could only be refused. A read error is « unknown » and
    answers False (fail-open): the page keeps offering the button, whose
    own STRICT read decides."""
    dossier_id = entry.get("dossier_id")
    if not isinstance(dossier_id, str) or not dossier_id:
        return False
    try:
        return get_dossier_strict(dossier_id) is None
    except Exception:
        log_unexpected("admin: receipt copy dossier read failed",
                       transaction_id=entry.get("id"))
        return False


def _document_filename(entry: dict) -> str:
    """The copy's file name: the receipt's own when documents accept its
    extension, else the base name of the firm-level object (whose extension
    is the one ``api_recu`` validated)."""
    for candidate in (
        str(entry.get("receipt_filename") or ""),
        str(entry.get("receipt_storage_path") or "").rsplit("/", 1)[-1],
    ):
        if "." in candidate:
            ext = "." + candidate.rsplit(".", 1)[1].lower()
            if ext in ALLOWED_EXTENSIONS and candidate.rsplit(".", 1)[0]:
                return candidate
    return ""


def _refus(
    message: str, code: str = REFUSEE, *,
    tx_id: Optional[str] = None, reason: Optional[str] = None,
) -> Versement:
    """A refusal — logged (ids and a machine reason only) once the entry is
    known to be admissible, so a copy that could not be filed after its
    receipt was attached never fails in silence. The reason travels on the
    outcome too (``Versement.reason``), for the caller's closed banner."""
    if reason is not None:
        log_admin_ledger_event(
            "admin_receipt_filed", "refused", transaction_id=tx_id,
            reason=reason,
        )
    return Versement(None, [message], code, reason or "")


def verser_recu_au_dossier(
    tx_id: str, uid: str, *, document_id: str
) -> Versement:
    """File a COPY of entry *tx_id*'s pièce justificative in its dossier's
    « Mandat › Déboursés » folder, under the RESERVED *document_id*. See the
    module docstring for the order; never raises on a refusal."""
    try:
        uid = storage_identity.require_uid(uid)
    except storage_identity.StorageIdentityUnavailable as exc:
        return _refus(storage_identity.public_message(exc))

    try:
        entry = admin_ledger.get_transaction_strict(tx_id)
    except Exception:
        log_unexpected("admin: receipt copy entry read failed",
                       transaction_id=tx_id)
        return _refus(MSG_READ_FAILED)
    if entry is None:
        return _refus(MSG_ENTRY_NOT_FOUND)
    if not admissible(entry):
        return _refus(MSG_INADMISSIBLE, INADMISSIBLE)
    entry.setdefault("id", tx_id)
    dossier_id = entry["dossier_id"]

    try:
        dossier = get_dossier_strict(dossier_id)
    except Exception:
        log_unexpected("admin: receipt copy dossier read failed",
                       transaction_id=tx_id)
        return _refus(MSG_READ_FAILED, tx_id=tx_id, reason="lecture_impossible")
    if dossier is None:
        return _refus(MSG_DOSSIER_NOT_FOUND, tx_id=tx_id,
                      reason=REASON_DOSSIER_INTROUVABLE)

    source = None
    md5 = str(entry.get("receipt_md5") or "")
    if not md5:
        # A receipt attached before ``receipt_md5`` existed: its digest is
        # the firm object's own. A read, never a write.
        source = _reload_source(entry)
        md5 = str(getattr(source, "md5_hash", None) or "")
        if source is None or not md5:
            return _refus(MSG_RECEIPT_UNREADABLE, tx_id=tx_id,
                          reason="recu_illisible")

    try:
        copies = find_receipt_copies(tx_id)
    except Exception:
        log_unexpected("admin: receipt copies read failed",
                       transaction_id=tx_id)
        return _refus(MSG_READ_FAILED, tx_id=tx_id, reason="lecture_impossible")
    deja = copie_du_recu(copies, dossier_id, md5)
    if deja is not None:
        return Versement(deja, [], DEJA)

    filename = _document_filename(entry)
    if not filename:
        return _refus(MSG_RECEIPT_UNREADABLE, tx_id=tx_id,
                      reason="recu_illisible")

    # The source reloaded and its digest re-checked BEFORE the folder: a
    # receipt that cannot be read, or that changed, refuses with nothing
    # written anywhere.
    if source is None:
        source = _reload_source(entry)
        if source is None:
            return _refus(MSG_RECEIPT_UNREADABLE, tx_id=tx_id,
                          reason="recu_illisible")
        if source.md5_hash != md5:
            # The receipt was replaced between the entry's read and this
            # one: the copy would carry the digest of bytes it does not hold.
            return _refus(MSG_RECEIPT_CHANGED, tx_id=tx_id,
                          reason=REASON_RECU_MODIFIE)

    # By its ROLE, created with its « Mandat » parent when missing — and a
    # failure REFUSES: the copy is never filed at the dossier root.
    folder, errors = ensure_system_folder(dossier_id, SYSTEM_ROLE_DEBOURSES)
    if folder is None:
        log_admin_ledger_event(
            "admin_receipt_filed", "refused", transaction_id=tx_id,
            reason="dossier_systeme",
        )
        return Versement(None, errors or [MSG_READ_FAILED], REFUSEE,
                         "dossier_systeme")

    sequence = entry.get("sequence")
    metadata = {
        "category": DOCUMENT_CATEGORY,
        "folder_id": folder["id"],
        "display_name": filename.rsplit(".", 1)[0],
        "tags": [DOCUMENT_TAG],
        "document_date": entry.get("date"),
        "genere_depuis": (
            "Pièce justificative de l'écriture d'administration "
            f"n° {sequence}" if sequence is not None
            else "Pièce justificative d'une écriture d'administration"
        ),
    }
    document, errors = ingest_blob_as_document(
        source, dossier_id, dossier.get("file_number") or "", filename,
        metadata, uid,
        document_id=document_id,
        category_source="juriste",
        lawyer_set_category=False,
        from_admin_receipt={"transaction_id": tx_id, "md5": md5},
    )
    if errors or document is None:
        log_admin_ledger_event(
            "admin_receipt_filed", "refused", transaction_id=tx_id,
            reason="ingestion",
        )
        return Versement(None, errors or [MSG_READ_FAILED], REFUSEE,
                         "ingestion")
    log_admin_ledger_event(
        "admin_receipt_filed", transaction_id=tx_id,
        account_id=entry.get("account_id"),
        dossier_id=dossier_id, document_id=document.get("id"),
    )
    return Versement(document, [], VERSEE)


def _reload_source(entry: dict):
    """The FIRM-level receipt object, reloaded (its size and MD5 are what
    the copy is judged on) — or ``None`` when it cannot be read."""
    try:
        blob = storage.bucket().blob(entry["receipt_storage_path"])
        blob.reload()
    except Exception:
        log_unexpected("admin: receipt copy source unreadable",
                       transaction_id=entry.get("id"))
        return None
    return blob
