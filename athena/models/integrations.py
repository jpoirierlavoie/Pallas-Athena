"""Integration settings — the ``settings/integrations`` singleton.

The live tuning of the three Graph-backed subsystems: the Bookings sync's
subject keywords, its keyword→type map and window, the Outlook mirror's
window, the outbound-email display name, and the predicate-debug flag. A
SIBLING of ``settings/cabinet`` in the SAME ``settings`` collection — so no
new collection, no ``firestore.rules`` change and no index (a keyed
``get()``).

WHY IT EXISTS, and what it deliberately does NOT cover. ``Config.*`` are
class-body ``os.environ.get()`` calls evaluated once per gunicorn worker at
import, so nothing at runtime can write them. Two of these nine values
(``bookings_type_par_mot_cle``, ``bookings_type_defaut``) read NO env var at
all today — they are hardcoded class-body literals, so adding a Bookings
service has always meant editing ``config.py``. Making them editable is pure
gain.

The FOUR deploy-time values and the two kill switches are enumerated in
``utils/integrations_defaults`` with the reasoning; the short version is that
each of the four is half of an operation whose other half lives in Secret
Manager or Exchange RBAC, so editing it alone can only produce an outage.

THE THREE RULES OF THE SINGLETON DOCTRINE hold here as in
``models/settings.py`` — with ONE reasoned deviation, marked below:

1. **Once the document exists it is the WHOLE truth.** :func:`get_integrations`
   projects on ``_BLANK``, never on the ``Config`` seed. A
   ``{**seed, **stored}`` merge would resurrect a deploy-time value for a
   field the lawyer deliberately cleared — the deletion trap in mirror image.
2. **:func:`_normalize` gates every branch on key PRESENCE**, because
   ``update_integrations`` merges then writes the full document, so a key the
   normalizer injects IS a deletion (the ``models/partie._normalize`` defect).
3. **A write REFUSES while the store is unreadable.**
   ``models/settings.update_cabinet`` does ``_read_raw() or _seed_from_env()``
   as its merge base, which is harmless there only because that form posts all
   thirteen fields. Here it would merge the deploy-time seed over whatever the
   store actually holds. So :func:`update_integrations` refuses outright on
   ``unreadable`` and creates on ``absent`` — the deletion trap seen from the
   write side.

PROVENANCE IS PART OF THE CONTRACT, not a diagnostic nicety.
:func:`get_integrations_state` returns ``(record, provenance)`` with
provenance ∈ ``store`` | ``absent`` | ``unreadable``, because the Bookings
sync's absence loop CANCELS any in-window stored booking whose UID it did not
detect this cycle. Reverting to the deploy-time keyword set is therefore not
merely degraded — if the lawyer renamed a Bookings service and updated the
stored keywords, a seed revert makes the predicate match NOTHING and the
absence loop stamps ``annulée_client`` on genuine client bookings. Non-empty
is not the same as matching. So the route disarms the cancellation loop
whenever provenance is not ``store``, exactly as ``taches_outlook`` disarms
deletions on ``fenetre_pleine``, while leaving creation armed.

``absent`` must run NORMALLY (it is a fresh deploy, before anyone has opened
the page) or the lot would ship a bootstrap freeze of both syncs.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from config import Config
from models import db
from security import sanitize
from utils.integrations_defaults import (
    DEFAULTS,
    EDITABLE_FIELDS,
    JOURS_BORNES,
    JOURS_FIELDS,
    parse_bool,
    parse_int,
    plier,
)
from utils.logging_setup import log_unexpected

logger = logging.getLogger(__name__)

COLLECTION = "settings"
DOC_ID = "integrations"

_FIELDS: tuple[str, ...] = EDITABLE_FIELDS

# ── DELIBERATE DEVIATION from models/settings._BLANK (which is all-"") ─────
#
# Rule 1 exists to prevent an UN-DELETION of a field the lawyer cleared. That
# reasoning applies to a fax number; it does not apply to a lookahead. « A
# lookahead of cleared » is not a thing anyone can mean, and 0 would silently
# reduce the Outlook mirror to maintaining nothing. So the five non-clearable
# fields are REQUIRED by _validate (the form always posts all nine, so a
# stored document always carries them) and _BLANK carries the code default
# purely so an unreachable path degrades to today's behaviour rather than to
# zero.
#
# ``graph_sender_name`` keeps "" as a LEGITIMATE clear: config.py documents
# empty as « aucun name dans la charge sendMail », the historical payload.
# Rule 1 is untouched for every field where a clear is meaningful.
_BLANK: dict = dict(DEFAULTS)

_TEXT_MAX_LENGTH = 2000

# A keyword or a type token longer than this is not a real value; the cap
# keeps a hand-edited console document from rendering a wall of text into the
# settings form.
_TOKEN_MAX_LENGTH = 120
_MAX_KEYWORDS = 25
_MAX_TYPE_ENTRIES = 25


def _seed_from_config() -> dict:
    """Project the nine ``Config`` attributes into the stored shape.

    NEVER writes. The bootstrap for a fresh deploy and the fallback whenever
    Firestore is unreadable.

    DERIVED, not hand-written: a field name is its ``Config`` attribute
    lowercased, so this cannot drift from :data:`EDITABLE_FIELDS` the way a
    transcribed list would. ``tests/test_settings_integrations.py`` pins the
    correspondence both ways.

    It reads the ``Config`` CLASS attributes — which is what keeps every
    existing fixture authoritative: ``tests/test_bookings_sync.py`` and
    ``tests/test_taches_outlook.py`` monkeypatch ``Config`` and run through
    the view, where Firestore is a ``MagicMock``; the state read below then
    reports ``unreadable`` and this seed returns exactly what they patched.
    """
    seed: dict = {}
    for name in _FIELDS:
        seed[name] = getattr(Config, name.upper(), DEFAULTS.get(name))
    return seed


def _read_state() -> tuple[Optional[dict], str]:
    """``(payload, state)`` with state ∈ ``ok`` | ``absent`` | ``unreadable``.

    ``models/settings._read_raw`` collapses its three guards into a single
    ``None`` so its caller has one fallback branch. This one keeps them
    APART, because the caller's policy differs by cause: an absent document
    is a fresh deploy (run normally), while an unreadable store must disarm a
    destructive loop and refuse a write.

    The three guards are otherwise identical, and the ``isinstance`` check is
    load-bearing rather than defensive habit: a ``MagicMock`` snapshot has a
    TRUTHY ``.exists`` and returns a ``MagicMock`` from ``.to_dict()``, so
    without it a mock store would feed mock objects into a keyword predicate
    and most assertions would still pass — a fake store that accepts what the
    real one refuses proves nothing.
    """
    try:
        snap = db.collection(COLLECTION).document(DOC_ID).get()
    except Exception:
        log_unexpected(
            "integrations settings read failed", reason="store_unreadable"
        )
        return None, "unreadable"
    if not getattr(snap, "exists", False):
        return None, "absent"
    data = snap.to_dict()
    if not isinstance(data, dict):
        return None, "unreadable"
    return data, "ok"


def _project(stored: dict) -> dict:
    """``_BLANK`` as the base — never the seed. See rule 1."""
    out = dict(_BLANK)
    for key, value in stored.items():
        if key not in _FIELDS:
            continue
        if key == "bookings_subject_keywords":
            if isinstance(value, (list, tuple)):
                out[key] = tuple(str(v) for v in value if str(v).strip())
            continue
        if key == "bookings_type_par_mot_cle":
            if isinstance(value, dict):
                out[key] = {
                    str(k): str(v) for k, v in value.items()
                    if str(k).strip() and str(v).strip()
                }
            continue
        if key in JOURS_BORNES:
            out[key] = parse_int(value, DEFAULTS[key])
            continue
        if key == "bookings_debug_payload":
            out[key] = parse_bool(value)
            continue
        out[key] = "" if value is None else str(value)
    return out


def get_integrations_state() -> tuple[dict, str]:
    """The record plus its PROVENANCE. Always a complete record. FAILS OPEN.

    Provenance is what lets each entry point choose its own policy — the
    ``ok`` vs ``[]`` distinction ``list_hearings_in_range_state`` already
    documents, applied to configuration. See the module docstring for why the
    Bookings sync cannot simply fail open on the keyword set.
    """
    stored, state = _read_state()
    if state != "ok" or stored is None:
        return _seed_from_config(), state
    return _project(stored), "store"


def get_integrations() -> dict:
    """The record alone, for callers with no policy to choose.

    Deliberately UNCACHED, and the read profile is why: this is reached on
    three paths only — the two crons (6/hour each) and the outbound-email
    paths (a handful a day) — i.e. ~12-15 keyed ``get()``s per hour, on
    requests that already perform 5-50 Firestore reads. A TTL would let the
    lawyer save a keyword, the cron fire eight seconds later, and the run use
    the old value: the exact shape « a page that appears to apply a setting
    and does not » forbids, for no measurable saving. A per-worker cache would
    additionally let the two gunicorn workers disagree about the keyword set.

    THE TRIGGER TO REVISIT is the same one ``get_cabinet`` names: a context
    processor or any per-page reader. This design creates neither.
    """
    return get_integrations_state()[0]


def _normalize(data: dict) -> dict:
    """Normalize IN PLACE, every branch gated on key presence (rule 2)."""
    if "bookings_subject_keywords" in data:
        raw = data["bookings_subject_keywords"]
        if isinstance(raw, str):
            from utils.integrations_defaults import parse_keywords
            raw = parse_keywords(raw)
        data["bookings_subject_keywords"] = tuple(
            str(k).strip() for k in (raw or ()) if str(k).strip()
        )
    if "bookings_type_par_mot_cle" in data:
        raw = data["bookings_type_par_mot_cle"]
        if isinstance(raw, str):
            from utils.integrations_defaults import parse_type_map
            raw = parse_type_map(raw)
        # Keys folded with the SAME function the predicate folds with. An
        # unfolded key can never match, silently — worse than an empty map,
        # because the page looks like it accepted the mapping.
        data["bookings_type_par_mot_cle"] = {
            plier(str(k)): str(v).strip()
            for k, v in (raw or {}).items()
            if str(k).strip() and str(v).strip()
        }
    if "bookings_type_defaut" in data:
        data["bookings_type_defaut"] = str(
            data["bookings_type_defaut"] or ""
        ).strip()
    for key in JOURS_FIELDS:
        if key in data:
            data[key] = parse_int(data[key], DEFAULTS[key])
    if "bookings_debug_payload" in data:
        data["bookings_debug_payload"] = parse_bool(data["bookings_debug_payload"])
    if "graph_sender_name" in data:
        data["graph_sender_name"] = str(data["graph_sender_name"] or "").strip()
    return data


def _sanitize_data(data: dict) -> dict:
    """Sanitize the free-text surfaces; leave typed values alone."""
    out: dict = {}
    for key, val in data.items():
        if isinstance(val, str):
            out[key] = sanitize(val, max_length=_TEXT_MAX_LENGTH)
        elif key == "bookings_subject_keywords" and isinstance(val, tuple):
            out[key] = tuple(
                sanitize(v, max_length=_TOKEN_MAX_LENGTH) for v in val
            )
        elif key == "bookings_type_par_mot_cle" and isinstance(val, dict):
            out[key] = {
                sanitize(k, max_length=_TOKEN_MAX_LENGTH):
                    sanitize(v, max_length=_TOKEN_MAX_LENGTH)
                for k, v in val.items()
            }
        else:
            out[key] = val
    return out


def _validate(data: dict) -> list[str]:
    """French validation errors, empty when the record is acceptable."""
    # Lazily imported: keeps the module-level import graph of a model out of
    # the vocabulary check, and the pairing is documented — this validator and
    # models/hearing's vocabulary move together.
    from models.hearing import VALID_HEARING_TYPES_EXTRAJUDICIAIRE as _TYPES

    errors: list[str] = []

    mots = data.get("bookings_subject_keywords") or ()
    if not mots:
        errors.append(
            "Au moins un mot-clé d'objet est requis : sans lui la synchro "
            "n'importerait plus rien, et la boucle d'absence finirait par "
            "déclarer « annulées côté client » les réservations déjà "
            "importées."
        )
    if len(mots) > _MAX_KEYWORDS:
        errors.append(f"Maximum {_MAX_KEYWORDS} mots-clés.")
    for mot in mots:
        if "," in mot:
            errors.append(
                f"Le mot-clé « {mot} » contient une virgule : le champ est "
                "séparé par des virgules, donc il ne pourrait pas se relire."
            )

    carte = data.get("bookings_type_par_mot_cle") or {}
    if len(carte) > _MAX_TYPE_ENTRIES:
        errors.append(f"Maximum {_MAX_TYPE_ENTRIES} correspondances.")
    for cle, valeur in carte.items():
        if valeur not in _TYPES:
            errors.append(
                f"« {valeur} » n'est pas un type de rendez-vous "
                f"extrajudiciaire (mot-clé « {cle} »). Valeurs admises : "
                + ", ".join(_TYPES)
                + "."
            )

    defaut = data.get("bookings_type_defaut") or ""
    if not defaut:
        errors.append("Le type par défaut est requis.")
    elif defaut not in _TYPES:
        # The forum is DERIVED from the type (models.hearing.forum_of), so a
        # judiciaire value here would make a client consultation read
        # forum="judiciaire" on every surface.
        errors.append(
            f"Le type par défaut « {defaut} » n'est pas extrajudiciaire. "
            "Valeurs admises : " + ", ".join(_TYPES) + "."
        )

    for key in JOURS_FIELDS:
        if key not in data:
            errors.append(f"Le champ « {key} » est requis.")
            continue
        low, high = JOURS_BORNES[key]
        val = data[key]
        if not isinstance(val, int) or not (low <= val <= high):
            errors.append(
                f"« {key} » doit être un entier entre {low} et {high} "
                "(calendarView de Graph plafonne une fenêtre à 1825 jours)."
            )

    return errors


def unmapped_keywords(record: dict) -> list[str]:
    """Keywords with no entry in the type map — a WARNING, never a refusal.

    An unmapped keyword falls back to ``bookings_type_defaut``, which is
    legitimate: mapping every service is not required. But it becomes
    PROBABLE the moment the map is editable, so the settings page shows it and
    the sync emits a typed event. This cross-check is only possible because
    both halves of the coupled pair are on the editable side of the 9/4 line.
    """
    carte = record.get("bookings_type_par_mot_cle") or {}
    return sorted(
        mot for mot in (record.get("bookings_subject_keywords") or ())
        if plier(mot) not in carte
    )


def update_integrations(data: dict) -> tuple[Optional[dict], list[str]]:
    """Write the integration settings. Creation is implicit.

    REFUSES while the store is unreadable (rule 3): merging the deploy-time
    seed over whatever the store actually holds would silently revert the
    lawyer's other values.
    """
    existing, state = _read_state()
    if state == "unreadable":
        return None, [
            "Le magasin de réglages est illisible ; l'enregistrement est "
            "refusé pour ne pas écraser vos valeurs. Réessayez dans un "
            "moment."
        ]
    base = _project(existing) if existing is not None else _seed_from_config()
    merged = {**_BLANK, **base, **_sanitize_data(_normalize(dict(data)))}
    merged = {k: v for k, v in merged.items() if k in _FIELDS}

    errors = _validate(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    stored = dict(merged)
    # Firestore has no tuple; the projection reads a list back as a tuple.
    stored["bookings_subject_keywords"] = list(
        merged["bookings_subject_keywords"]
    )
    stored["id"] = DOC_ID
    stored["created_at"] = (existing or {}).get("created_at") or now
    stored["updated_at"] = now
    stored["etag"] = str(uuid.uuid4())

    try:
        db.collection(COLLECTION).document(DOC_ID).set(stored)
    except Exception:
        log_unexpected("integrations settings write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]

    return merged, []


def changed_field_names(before: dict, after: dict) -> list[str]:
    """Sorted names of the fields whose value differs. NAMES ONLY.

    What the observability event carries. Values are never logged: a display
    name is a person's or a firm's name, and the redaction filter does not
    scrub names.
    """
    return sorted(
        key for key in _FIELDS
        if before.get(key) != after.get(key)
    )
