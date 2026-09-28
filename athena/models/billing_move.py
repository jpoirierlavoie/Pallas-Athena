"""Moving an un-invoiced time entry or disbursement to another dossier.

ONE rule (lot 3a, step 2) for every path that re-files a billing row:

* ``time_entry.move_time_entry`` / ``expense.move_expense`` — the partial
  write the connector's move reaches (lot 3b);
* ``time_entry.update_time_entry`` / ``expense.update_expense`` — the web
  edit forms, whose dossier picker has always reassigned a row. When the
  submitted dossier differs from the stored one they apply THIS module,
  inside their own transaction, so a form save and a connector move cannot
  follow two rules.

What the rule says:

* an INVOICED row never moves — the invoice is voided first, which is what
  releases its sources (the models refuse before this module is reached);
* the target dossier is re-read INSIDE the writer's transaction, and the
  two label snapshots (``dossier_file_number``, ``dossier_title``) are taken
  from THAT read. Never from the caller: its copy may predate a rename, the
  dossier may have vanished since the caller resolved it, and until this
  module the edit form wrote whatever labels it had resolved outside any
  transaction — a dossier deleted in between left the row filed under a
  dead id;
* the phase stays. Phases are orthogonal to the dossier (Phase O): a
  sub-code the target's protocol does not use is kept, never blanked —
  blanking would move the row's budget consumption to « Non renseignée »
  in silence;
* nothing else moves either: ``move_*`` writes a PARTIAL ``update()`` of
  exactly :data:`LABEL_KEYS` plus the write's own provenance stamp — hours,
  rate, amount, description and phase are out of its reach by the SHAPE of
  the write (the ``set_*_phase`` four-key precedent).

Pure bar the one read it performs through the transaction it is handed. It
imports no model and no ``db``: the target reference is built from the
client of the writer's OWN reference (``ref._client`` — the
``models.concurrency`` idiom), so a test that patches one model's ``db``
never sends this read to another client.
"""

from __future__ import annotations

from typing import Optional

DOSSIERS_COLLECTION = "dossiers"

# The keys a move writes — the dossier link and its two display snapshots.
LABEL_KEYS: tuple[str, ...] = ("dossier_id", "dossier_file_number", "dossier_title")

TARGET_REQUIRED = "Un dossier de destination est requis. Rien n'a été déplacé."
TARGET_NOT_FOUND = (
    "Le dossier de destination est introuvable. Rien n'a été déplacé."
)


def is_addressable(value: object) -> bool:
    """True when *value* can name ONE top-level document.

    The Firestore client re-splits an id on « / », so a slashed id would
    address a record DEEPER in the tree (the ``document.is_addressable_id``
    lesson). No id this application mints carries one.
    """
    return isinstance(value, str) and bool(value.strip()) and "/" not in value


def target_id(to_dossier: object) -> str:
    """The id of the dossier a caller names — ``''`` when it names none.

    *to_dossier* is the dossier record the caller resolved (only its
    ``id`` is trusted; its labels are re-read). A bare id string is
    accepted too.
    """
    if isinstance(to_dossier, dict):
        value = to_dossier.get("id")
    else:
        value = to_dossier
    value = str(value or "").strip()
    return value if is_addressable(value) else ""


def dossier_label(row: dict) -> str:
    """How a refusal names the dossier a row is filed under — its file
    number, else its id (never its title, which can carry a client name)."""
    return str(row.get("dossier_file_number") or row.get("dossier_id") or "—")


def from_mismatch_error(row: dict) -> str:
    """The refusal of a move whose « from » dossier is not the stored one —
    the caller's view is out of date."""
    return (
        "Cet élément n'est plus au dossier indiqué : il est au dossier "
        f"« {dossier_label(row)} ». Rien n'a été déplacé — relisez-le, puis "
        "refaites le déplacement."
    )


def target_fields(transaction, entry_ref, dossier_id: str) -> Optional[dict]:
    """The target dossier's link and labels, read THROUGH *transaction*.

    ``None`` when the dossier does not exist (or *dossier_id* cannot name
    one). A read failure propagates — the writer's transaction then fails
    as a store error, never as « introuvable ».
    """
    if not is_addressable(dossier_id):
        return None
    ref = entry_ref._client.collection(DOSSIERS_COLLECTION).document(dossier_id)
    snap = ref.get(transaction=transaction)
    if not snap.exists:
        return None
    dossier = snap.to_dict() or {}
    return {
        "dossier_id": dossier_id,
        "dossier_file_number": str(dossier.get("file_number") or ""),
        "dossier_title": str(dossier.get("title") or ""),
    }
