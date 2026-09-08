"""Integration-settings vocabulary, defaults and parsers — PURE, stdlib only.

Imported by BOTH ``config.py`` (class body) and ``models/integrations.py``, so
there is exactly ONE parser for each value. Today those parsers live inline in
the ``Config`` class body; a second copy in the model is precisely the drift
this repo already tracks as debt for every hand-mirrored vocabulary.

**Never import ``models`` here, and never import ``config``.** ``config.py``
imports this at class-body evaluation, and ``models/__init__.py`` builds a
Firestore client at import — either direction would make a cycle, and a
``models`` import would put a Firestore client construction inside the four
credential-less Graph test files.

THE 9/4 LINE. Nine integration values are editable in « Paramètres →
Intégrations »; four are deploy-time only. The line is not drawn on
convenience — each of the four is HALF of an operation whose other half is not
in this application:

* ``GRAPH_TENANT_ID`` / ``GRAPH_CLIENT_ID`` are two-thirds of a credential
  triple whose third member (``graph-client-secret``) lives in Secret Manager.
  Edited alone they make the Entra token POST answer 401, which stops outbound
  email, the Bookings sync AND the Outlook mirror at once.
* ``GRAPH_SENDER_UPN`` / ``BOOKINGS_JURISTE_UPN`` are mailbox identities gated
  by Exchange RBAC scope — certificate-authenticated PowerShell that
  ``graph-client-secret`` cannot administer, with a 30 min-2 h propagation
  cache. Repointed from a web form without the Exchange half, every call
  answers 403.

A field whose only reachable effect when used alone is an outage is worse than
a read-only row that names the out-of-band half.

THE STRUCTURAL DIVIDEND, and the reason this is the right line rather than
merely a smaller one: ``Config.graph_configured()`` reads TENANT_ID +
CLIENT_ID + CLIENT_SECRET + SENDER_UPN, and ``bookings_configured()`` adds
BOOKINGS_JURISTE_UPN. **All five are on the deploy-time side.** So both
classmethods and all eight of their call sites are untouched, and the
class-attribute trap never arises: ``Config`` is never instantiated, so a
``@property`` read on the class would return the truthy property OBJECT and
``graph_configured()`` would answer True with no credentials — fail-open and
silent. That failure has no code path here, by construction rather than by
care.
"""

import unicodedata
from typing import Iterable, Mapping

# ── The 9/4 inventory ──────────────────────────────────────────────────────
#
# A field name is its ``Config`` attribute lowercased, so the seed projection
# is DERIVABLE (``getattr(Config, name.upper())``) instead of hand-written —
# and a test pins the correspondence both ways. That is what stops this
# becoming a fourth hand-kept inventory.

EDITABLE_FIELDS: tuple[str, ...] = (
    "bookings_subject_keywords",
    "bookings_type_par_mot_cle",
    "bookings_type_defaut",
    "bookings_sync_lookahead_days",
    "bookings_sync_lookback_days",
    "bookings_debug_payload",
    "miroir_outlook_lookahead_days",
    "miroir_outlook_lookback_days",
    "graph_sender_name",
)

# Shown on the page as read-only rows, with the out-of-band half named.
DEPLOY_ONLY_FIELDS: tuple[str, ...] = (
    "graph_tenant_id",
    "graph_client_id",
    "graph_sender_upn",
    "bookings_juriste_upn",
)

# Deliberately on NEITHER list. A kill switch reachable only through the
# application is useless at the moment the application is what you are trying
# to stop — so these stay env-only, and the page says so rather than omitting
# them silently.
KILL_SWITCHES: tuple[str, ...] = (
    "bookings_sync_active",
    "miroir_outlook_actif",
)

# ── Defaults (the current code defaults, in one place) ────────────────────

DEFAULTS: dict = {
    "bookings_subject_keywords": ("Consultation",),
    "bookings_type_par_mot_cle": {
        "consultation": "consultation",
        "rencontre": "rencontre",
    },
    "bookings_type_defaut": "consultation",
    "bookings_sync_lookahead_days": 90,
    "bookings_sync_lookback_days": 1,
    "bookings_debug_payload": False,
    "miroir_outlook_lookahead_days": 365,
    "miroir_outlook_lookback_days": 30,
    "graph_sender_name": "",
}

# Graph's ``calendarView`` caps a window at 1825 days. A lookahead of 0 would
# reduce the mirror to maintaining nothing, which is not a thing a lawyer can
# mean — hence the floor of 1 on both lookaheads. A lookback of 0 IS
# meaningful (today only).
JOURS_BORNES: dict[str, tuple[int, int]] = {
    "bookings_sync_lookahead_days": (1, 1825),
    "bookings_sync_lookback_days": (0, 365),
    "miroir_outlook_lookahead_days": (1, 1825),
    "miroir_outlook_lookback_days": (0, 365),
}

JOURS_FIELDS: tuple[str, ...] = tuple(JOURS_BORNES)


# ── The fold ───────────────────────────────────────────────────────────────

def plier(texte: str) -> str:
    """Case and diacritics neutralized, for comparing French subjects.

    Hoisted verbatim from ``utils/graph_calendrier._plier`` (which now
    re-exports this) so the type-map keys are folded by the SAME function the
    predicate folds with. A second implementation here would reproduce the
    exact trap the accent-folding gotcha documents: « é » precomposed (NFC,
    U+00E9) and decomposed (NFD, e + U+0301) are different strings to Python,
    nothing guarantees which form Bookings stored, and a mapping typed with an
    accent would simply never match — silently, on a page that looked like it
    accepted the value.

    ⚠ NOT the same fold as ``utils/rapprochement._plier``, which additionally
    reduces punctuation to spaces and strips. Do not merge them: that one
    tokenizes names for conflict-check candidates, this one compares a subject
    suffix.
    """
    decompose = unicodedata.normalize("NFD", texte or "")
    return "".join(
        c for c in decompose if unicodedata.category(c) != "Mn"
    ).casefold()


# ── Parsers / formatters (the form round trip) ─────────────────────────────
#
# No regex anywhere in this section, deliberately: every function is a split
# and a strip, so the CWE-1333 linearity invariant the docx engine documents
# holds here for free rather than by inspection.

def parse_keywords(raw: str) -> tuple[str, ...]:
    """Comma-separated → tuple, empties dropped. The ``Config`` parser.

    ``""``, ``" , "`` and ``",,"`` all yield ``()`` — which the model REFUSES,
    because an empty keyword set is a total silent outage: the predicate never
    matches, the sync imports nothing, and the absence loop then flags every
    already-imported reservation « annulée côté client ».
    """
    return tuple(k.strip() for k in (raw or "").split(",") if k.strip())


def format_keywords(values: Iterable[str]) -> str:
    """Tuple → the comma-separated form the textarea shows."""
    return ", ".join(values or ())


def parse_type_map(raw: str) -> dict[str, str]:
    """``mot-clé = type`` per line → dict, keys FOLDED.

    Folding here is what makes the stored map reachable: the lookup folds the
    detected keyword, so an unfolded key could never match and every booking
    would fall back to the default type. A line without ``=`` is skipped
    rather than guessed at; the model's validator is what reports it.
    """
    out: dict[str, str] = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        cle, _, valeur = line.partition("=")
        cle, valeur = plier(cle.strip()), valeur.strip()
        if cle and valeur:
            out[cle] = valeur
    return out


def format_type_map(mapping: Mapping[str, str]) -> str:
    """Dict → the ``mot-clé = type`` block the textarea shows."""
    return "\n".join(f"{k} = {v}" for k, v in sorted((mapping or {}).items()))


def parse_bool(raw: object) -> bool:
    """The ``Config`` class-body idiom (``.lower() == "true"``), reusable.

    Accepts a real bool unchanged so a stored Firestore boolean round-trips.
    """
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() == "true"


def parse_int(raw: object, fallback: int) -> int:
    """Best-effort int with a fallback — the model's validator reports range.

    Returns *fallback* on anything unparseable rather than raising, because
    the caller is a form field and ``_validate`` is where a French refusal
    belongs. A bool is refused explicitly: ``int(True)`` is 1, and a checkbox
    value reaching a day-count field should not silently become one day.
    """
    if isinstance(raw, bool):
        return fallback
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback
