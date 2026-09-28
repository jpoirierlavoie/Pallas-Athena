"""Vérifications de conformité — identité et conflits d'intérêts (D7, lot 4a).

Pure vocabulary and pure provenance rules — no Firestore, no Flask, no model
import — so the model (``models/partie.py``), the coverage report
(``mcp/coverage.py``, which must import no model), the partie routes and,
from lot 4b, the connector's enums all read ONE definition.

Why provenance exists. A decided status is a professional attestation: the
fiche renders it « le … par Me … », and the coverage report's two
deontological checks treat it as done. Until lot 4a nothing recorded WHO
decided — so the day the connector can inscribe a status (D7), a Claude
write would have read, on screen and in the report, exactly like the
lawyer's own attestation. Four keys per check now carry the answer:

* ``{field}_date`` — when the CURRENT decided status was inscribed (a true
  timestamp; never caller-supplied — the model pops it from every payload);
* ``{field}_source`` — ``"juriste"`` or ``"mcp"``. Absent or ``""`` means
  the lawyer (every record written before lot 4a, the ``category_source``
  pattern of the documents);
* ``{field}_confirmed_at`` / ``{field}_confirmed_by`` — set ONLY by the
  « Confirmer » action of the fiche (``models.partie.confirm_kyc_status``).

The rules, all enforced by :func:`apply_status_transition`:

* only a status TRANSITION changes provenance. A web form re-submitting an
  unchanged value touches nothing — so re-saving the fiche never confirms a
  presumed inscription, and never demotes a confirmed one;
* a transition must NAME its source; the model refuses one that does not
  (fail closed — an omitted source must never stamp a Claude write as the
  lawyer's, and there is deliberately no default anywhere);
* a transition by the lawyer is the lawyer's decision (``juriste``), with
  no confirmation needed;
* a transition by the connector (``mcp``) is PRESUMED until confirmed, and
  is refused outright on a status the lawyer decided or confirmed;
* « non_vérifié » clears the date, the source and the confirmation — the
  PA-D07 invariant « date present ⇔ status decided » self-heals on it.

:func:`is_presumed` / :func:`is_decided` are the two readings every consumer
shares: a presumed status is NOT decided, so the coverage report keeps the
check open until the lawyer confirms it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

FIELD_IDENTITY = "identity_verified"
FIELD_CONFLICT = "conflict_check"
FIELDS: tuple[str, ...] = (FIELD_IDENTITY, FIELD_CONFLICT)

NON_VERIFIE = "non_vérifié"

IDENTITY_STATUSES: tuple[str, ...] = ("non_vérifié", "vérifié", "exempté")
CONFLICT_STATUSES: tuple[str, ...] = ("non_vérifié", "vérifié", "conflit_détecté")
STATUSES: dict[str, tuple[str, ...]] = {
    FIELD_IDENTITY: IDENTITY_STATUSES,
    FIELD_CONFLICT: CONFLICT_STATUSES,
}

# `identity_verified` has THREE decided states, not two: « exempté » is a
# legitimate terminal outcome (a client the regulation exempts). And
# « conflit_détecté » is decided too: the check WAS run; its outcome is a
# conflict.
DECIDED: dict[str, tuple[str, ...]] = {
    FIELD_IDENTITY: ("vérifié", "exempté"),
    FIELD_CONFLICT: ("vérifié", "conflit_détecté"),
}

SOURCE_JURISTE = "juriste"
SOURCE_MCP = "mcp"
VALID_SOURCES: tuple[str, ...] = (SOURCE_JURISTE, SOURCE_MCP)

# French labels of the statuses, for the fiche's badges.
STATUS_LABELS: dict[str, str] = {
    "non_vérifié": "Non vérifié",
    "vérifié": "Vérifié",
    "exempté": "Exempté",
    "conflit_détecté": "Conflit détecté",
}

# The refusals of apply_status_transition — constants, so a caller (a route,
# the connector's handler) can recognise one without parsing French.
PROVENANCE_REQUIRED = (
    "La provenance de la vérification de conformité n'est pas précisée : "
    "rien n'a été enregistré."
)
LAWYER_ATTESTATION = (
    "Seul le juriste peut modifier une vérification qu'il a consignée ou "
    "confirmée : rien n'a été enregistré."
)
INVALID_STATUS = "Statut de vérification invalide."


def date_key(field: str) -> str:
    return f"{field}_date"


def notes_key(field: str) -> str:
    return f"{field}_notes"


def source_key(field: str) -> str:
    return f"{field}_source"


def confirmed_at_key(field: str) -> str:
    return f"{field}_confirmed_at"


def confirmed_by_key(field: str) -> str:
    return f"{field}_confirmed_by"


# Every key the MODEL owns — popped from any caller payload, so neither the
# web form, a CardDAV PUT nor the connector can forge a provenance.
PROVENANCE_KEYS: tuple[str, ...] = tuple(
    key(field)
    for field in FIELDS
    for key in (date_key, source_key, confirmed_at_key, confirmed_by_key)
)


def _check_field(field: str) -> None:
    if field not in FIELDS:
        raise ValueError(f"unknown KYC field: {field!r}")


def stored_status(partie: Optional[dict], field: str) -> str:
    """The status as stored, « non_vérifié » when absent (a legacy record)."""
    _check_field(field)
    return str((partie or {}).get(field) or NON_VERIFIE)


def source_of(partie: Optional[dict], field: str) -> str:
    """``"mcp"`` or ``"juriste"`` — absent/blank reads as the lawyer."""
    _check_field(field)
    raw = str((partie or {}).get(source_key(field)) or "")
    return SOURCE_MCP if raw == SOURCE_MCP else SOURCE_JURISTE


def is_presumed(partie: Optional[dict], field: str) -> bool:
    """A decided status the connector inscribed and the lawyer has not yet
    confirmed. Presumed is NOT decided: the check stays open."""
    return (
        stored_status(partie, field) in DECIDED[field]
        and source_of(partie, field) == SOURCE_MCP
        and not (partie or {}).get(confirmed_at_key(field))
    )


def is_decided(partie: Optional[dict], field: str) -> bool:
    """A decided status the lawyer recorded — or confirmed."""
    return (
        stored_status(partie, field) in DECIDED[field]
        and not is_presumed(partie, field)
    )


def _clear_provenance(merged: dict, field: str) -> None:
    merged[date_key(field)] = None
    merged[source_key(field)] = ""
    merged[confirmed_at_key(field)] = None
    merged[confirmed_by_key(field)] = ""


def apply_status_transition(
    merged: dict,
    existing: Optional[dict],
    data: dict,
    field: str,
    *,
    source: Optional[str],
    now: datetime,
) -> Optional[str]:
    """Stamp *merged* for a submitted *field* status; return a refusal or None.

    *data* is the caller's payload (presence decides: a partial update that
    does not carry the key never touches the check), *existing* the stored
    record (``None`` on a creation), *merged* the document about to be
    written. *source* names who is deciding — ``None`` refuses any
    transition (fail closed; see the module docstring). An invalid source is
    a programming error and raises.

    Nothing is written to *merged* when a refusal is returned.
    """
    _check_field(field)
    if source is not None and source not in VALID_SOURCES:
        raise ValueError(f"unknown KYC source: {source!r}")
    if field not in data:
        return None
    new = data.get(field)
    if not new:
        # A blank value has always been left alone here (the stored date
        # included) — a form that posts nothing for the select.
        return None
    old = stored_status(existing, field)
    if new not in STATUSES[field]:
        if new == (existing or {}).get(field):
            return None  # a legacy value, re-submitted unchanged
        return INVALID_STATUS
    changed = new != old
    if changed:
        if source is None:
            return PROVENANCE_REQUIRED
        if source == SOURCE_MCP and is_decided(existing, field):
            return LAWYER_ATTESTATION
    if new == NON_VERIFIE:
        # Clears even when unchanged: a pre-PA-D07 record (« non_vérifié »
        # beside a stale date) heals on its next KYC save.
        _clear_provenance(merged, field)
        return None
    if not changed:
        return None  # unchanged decided status: provenance untouched
    merged[date_key(field)] = now
    merged[source_key(field)] = source
    merged[confirmed_at_key(field)] = None
    merged[confirmed_by_key(field)] = ""
    return None
