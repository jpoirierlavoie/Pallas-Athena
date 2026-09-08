"""Firm profile — the ``settings/cabinet`` singleton.

The live source of the firm's own identity: the lawyer's name, the firm's
name, the address, the phone/fax, the courriel and the two tax registration
numbers. Read through :func:`utils.cabinet.cabinet_dict`, which is the ONE
derivation point every consumer shares (gabarits, the note d'honoraires, the
note print, the budget-PDF footer, the portal invitation and accusé emails).

WHY THIS COLLECTION EXISTS. ``Config.FIRM_*`` (config.py) are class-body
``os.environ.get()`` calls — evaluated once per gunicorn worker at import, so
NOTHING at runtime can write them. Editing the firm profile in the
application therefore requires the live values to live here, with the env
vars demoted to a SEED (fresh deploy) and a FALLBACK (Firestore unreadable).
The env vars are deliberately kept, not deleted: ``main.py``'s
``app.config.from_object(Config)`` still publishes them, and they are what
makes a first boot on an empty database render correctly.

SINGLETON, keyed by its own name (Architecture Rule 6, documented
exception): the lookup key IS the document id, so assembly is one keyed
``get()`` that exists or does not, and creation is IMPLICIT inside
:func:`update_cabinet` — there is no ``create_cabinet``. Rule 7 fields are
present (``created_at``/``updated_at``/``etag``) though nothing reads the
etag: this collection is not DAV-exposed, so there is no CTag to bump and no
``If-Match`` to serve.

TWO RULES CARRY THIS MODULE, and both are the same trap seen from opposite
sides — on a full-document-``set()`` model, a key that is present decides,
and a key that is absent survives:

1. **Once the document exists it is the WHOLE truth.** :func:`get_cabinet`
   bases its projection on ``_BLANK``, never on the env seed. A
   ``{**seed, **stored}`` merge would resurrect an env value for a field the
   lawyer deliberately CLEARED (the fax he no longer has), which is the
   deletion trap in mirror image: injecting a seed is an un-deletion.
2. **:func:`_normalize` gates every branch on presence.** ``update_cabinet``
   merges ``{**existing, **data}`` and writes the full document, so a key
   injected by the normalizer IS a deletion — the defect
   ``models/partie._normalize`` shipped for ``mandataires``.

PHONES ARE STORED E.164 (``+15147372525``), like ``models/partie``, and
``cabinet_dict`` renders them in the local form. The display form is what
four surfaces print — two of them without any stripping (the budget-PDF
footer and every gabarit's ``{{cabinet.telephone}}``), both client-facing —
so the rendering belongs in the one authority rather than at each reader.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from config import Config
from models import db
from security import sanitize
from utils.logging_setup import log_unexpected
from utils.validators import (
    apply_address_defaults,
    normalize_email,
    normalize_phone,
    normalize_postal_code,
    validate_phone,
)

logger = logging.getLogger(__name__)

COLLECTION = "settings"
DOC_ID = "cabinet"

# The stored shape is the INPUT shape, not cabinet_dict()'s derived output:
# the six address_* suffixes are exactly what apply_address_defaults(prefix=
# "address") expects, so that helper is reused rather than reimplemented, and
# the env -> Firestore seeding stays a provably lossless 1:1 copy.
_FIELDS: tuple[str, ...] = (
    "nom",
    "organisation",
    "address_street",
    "address_unit",
    "address_city",
    "address_province",
    "address_postal_code",
    "address_country",
    "telephone",
    "telecopieur",
    "courriel",
    "gst_number",
    "qst_number",
)

_BLANK: dict = {key: "" for key in _FIELDS}

# The firm's trade name has never had a setting behind it: it lives as a
# literal in routes/taches_portail.py (_CABINET_REPLI) and again in
# app.yaml's GRAPH_SENDER_NAME. This seeds the field so those stop being the
# live source; tests/test_settings_cabinet.py pins this equal to the
# taches_portail literal so the pair cannot drift apart again.
ORGANISATION_SEED = "Poirier Lavoie, avocat"

_TEXT_MAX_LENGTH = 2000

_ADDRESS_PARTS = (
    "street", "unit", "city", "province", "postal_code", "country",
)


def _as_str(value: object) -> str:
    """Coerce a stored value to a string.

    A DAV/vobject write path can leave a LIST in a text field (the
    ``mcp.handlers._addr_str`` motive), and a settings document edited by
    hand in the console can hold anything at all. Every consumer of
    ``cabinet_dict`` concatenates or renders these values, so the coercion
    belongs here rather than at each of them.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(_as_str(v) for v in value if v not in (None, ""))
    return str(value)


def _seed_from_env() -> dict:
    """Project ``Config.FIRM_*`` into the stored shape. NEVER writes.

    The bootstrap for a fresh deploy and the fallback whenever Firestore is
    unreadable. Under pytest every ``FIRM_*`` is ``""`` (there is no
    ``conftest.py`` seeding them), so this returns a near-blank record —
    which is exactly today's behaviour, and is what keeps
    ``routes/taches_portail``'s ``_CABINET_REPLI`` branch firing in the
    existing suite.

    ``FIRM_PROVINCE`` defaults to ``"QC"`` in config and is unset in
    production, so seeding through ``apply_address_defaults`` migrates it to
    ``"Québec"`` — the convention every contact address in the app already
    follows, and which ``utils/budget_pdf`` was expanding by hand at one
    consumer out of three.
    """
    seed = dict(_BLANK)
    seed["nom"] = Config.FIRM_NAME or ""
    seed["organisation"] = ORGANISATION_SEED
    seed["address_street"] = Config.FIRM_STREET or ""
    seed["address_unit"] = Config.FIRM_UNIT or ""
    seed["address_city"] = Config.FIRM_CITY or ""
    seed["address_province"] = Config.FIRM_PROVINCE or ""
    seed["address_postal_code"] = Config.FIRM_POSTAL_CODE or ""
    seed["courriel"] = Config.FIRM_EMAIL or ""
    seed["gst_number"] = Config.GST_NUMBER or ""
    seed["qst_number"] = Config.QST_NUMBER or ""
    # app.yaml stores the DISPLAY form ("(514) 737-2525"); normalize so the
    # seed and an edited record are the same shape downstream.
    seed["telephone"] = normalize_phone(Config.FIRM_PHONE or "") or ""
    seed["telecopieur"] = normalize_phone(Config.FIRM_FAX or "") or ""
    apply_address_defaults(seed, prefix="address")
    return seed


def _read_raw() -> Optional[dict]:
    """The stored document, or ``None`` when there is nothing usable.

    ``None`` covers all three "no record" cases so :func:`get_cabinet` has a
    single fallback branch: the read failed, the document does not exist, or
    the payload is not a mapping.

    The ``isinstance`` check is load-bearing, not defensive habit.
    ``tests/test_taches_portail.py`` patches ``google.cloud.firestore.Client``
    with a ``MagicMock``, whose snapshot has a TRUTHY ``.exists`` and returns
    a ``MagicMock`` from ``.to_dict()``. Without it, ``<MagicMock ...>``
    would be interpolated into a client email template and most assertions
    would still pass — a fake store that accepts what the real one refuses
    proves nothing.
    """
    try:
        snap = db.collection(COLLECTION).document(DOC_ID).get()
    except Exception:
        log_unexpected("cabinet settings read failed")
        return None
    if not getattr(snap, "exists", False):
        return None
    data = snap.to_dict()
    if not isinstance(data, dict):
        return None
    return data


def get_cabinet() -> dict:
    """The firm profile, always a complete record. FAILS OPEN.

    Every caller renders this into a document, an email or a PDF, so a read
    failure must degrade to the deploy-time values rather than blank a
    letterhead or an invoice header.

    Deliberately UNCACHED. ``cabinet_dict`` has eight low-frequency,
    user-initiated call sites and — since the compliance signer moved into
    its route — no per-page reader, which is the case a cache would serve; a
    sub-kilobyte keyed ``get()`` on paths already doing 5-50 reads is noise,
    and a per-worker cache would let the two gunicorn workers disagree.
    THE TRIGGER TO REVISIT: the moment a context processor or any per-page
    reader appears, the 60 s fail-open TTL of ``main.py``'s reception badge
    becomes the right answer. Not before.
    """
    stored = _read_raw()
    if stored is None:
        return _seed_from_env()
    # _BLANK is the base, NEVER the env seed — see rule 1 in the module
    # docstring. A field the lawyer cleared stays cleared, and a field added
    # by a later version reads "" on an older document (additive, no
    # migration) instead of falling back to a stale env var.
    return {
        **_BLANK,
        **{k: _as_str(v) for k, v in stored.items() if k in _FIELDS},
    }


def _normalize(data: dict) -> dict:
    """Normalize IN PLACE, every branch gated on key presence.

    ``update_cabinet`` merges then writes the full document, so injecting a
    key the caller did not supply is a deletion. The address block is only
    normalized when at least one address key is present, which is also why
    ``apply_address_defaults``' silent-relocation trap does not bite here:
    the settings form is the only writer and always posts the whole six-key
    block, so there is no partial address to complete with Montréal/Québec
    defaults.
    """
    if any(f"address_{part}" in data for part in _ADDRESS_PARTS):
        apply_address_defaults(data, prefix="address")
        if (data.get("address_postal_code") or "").strip():
            country = data.get("address_country") or "Canada"
            data["address_postal_code"] = (
                normalize_postal_code(data["address_postal_code"], country)
                or data["address_postal_code"]
            )
    for key in ("telephone", "telecopieur"):
        if key in data and (data.get(key) or "").strip():
            data[key] = normalize_phone(data[key]) or data[key]
    if "courriel" in data and (data.get("courriel") or "").strip():
        data["courriel"] = normalize_email(data["courriel"]) or data["courriel"]
    return data


def _sanitize_data(data: dict) -> dict:
    out: dict = {}
    for key, val in data.items():
        if isinstance(val, str):
            out[key] = sanitize(val, max_length=_TEXT_MAX_LENGTH)
        else:
            out[key] = val
    return out


def _validate(data: dict) -> list[str]:
    """French validation errors, empty when the record is acceptable.

    ``nom`` is REQUIRED: it is the compliance signer on every client's
    identity-verification card, the letterhead on every generated document,
    and the sentinel ``routes/taches_portail._composer_cabinet`` falls back
    on — blanking it would silently switch a client email to a hardcoded
    literal.

    The tax numbers get NO format validation, deliberately. They print on
    issued invoices, where a false refusal is worse than a typo, and the
    application has never validated their shape.
    """
    errors: list[str] = []
    if not (data.get("nom") or "").strip():
        errors.append("Le nom du juriste est requis.")
    for key, label in (
        ("telephone", "téléphone"),
        ("telecopieur", "télécopieur"),
    ):
        raw = (data.get(key) or "").strip()
        if raw:
            _, err = validate_phone(raw)
            if err:
                errors.append(f"Numéro de {label} invalide.")
    courriel = (data.get("courriel") or "").strip()
    if courriel and not normalize_email(courriel):
        errors.append("Adresse courriel invalide.")
    postal = (data.get("address_postal_code") or "").strip()
    if postal:
        country = (data.get("address_country") or "Canada").strip()
        if not normalize_postal_code(postal, country):
            errors.append("Code postal invalide.")
    return errors


def update_cabinet(data: dict) -> tuple[Optional[dict], list[str]]:
    """Write the firm profile. Creation is implicit — no ``create_cabinet``.

    Returns ``(document, errors)`` on the house convention. The merge base is
    the STORED record when one exists and the env seed otherwise, so a first
    save from a partially-filled form keeps the deploy-time values for the
    fields it did not carry.
    """
    existing = _read_raw() or _seed_from_env()
    merged = {**_BLANK, **existing, **_sanitize_data(_normalize(dict(data)))}
    merged = {k: v for k, v in merged.items() if k in _FIELDS}

    errors = _validate(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    merged["id"] = DOC_ID
    merged["created_at"] = existing.get("created_at") or now
    merged["updated_at"] = now
    merged["etag"] = str(uuid.uuid4())

    try:
        db.collection(COLLECTION).document(DOC_ID).set(merged)
    except Exception:
        log_unexpected("cabinet settings write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]

    return merged, []


def changed_field_names(before: dict, after: dict) -> list[str]:
    """Sorted names of the fields whose value differs. NAMES ONLY.

    What the observability event carries. The values are never logged: the
    redaction filter scrubs emails and phone numbers but NOT names, and
    ``nom`` is a person's name.
    """
    return sorted(
        key for key in _FIELDS
        if _as_str(before.get(key)) != _as_str(after.get(key))
    )
