"""Notes — timestamped journal entries, usually linked to a case file.

Each note becomes a VJOURNAL resource in a CalDAV collection:
    /dav/dossier-{dossierId}/{noteId}.ics   when linked to a dossier
    /dav/general/{noteId}.ics               when it has none

``dossier_id`` is OPTIONAL (July 2026): a note with none is a free journal
entry — legal watch, a research memo tied to no file — and lives in the
« Général » collection alongside dossier-less tasks and hearings. Callers
must still distinguish "no dossier chosen" from "dossier not found": the
model can no longer tell them apart, so blanking an unknown id silently
turns a dossier note into a general one.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import icalendar

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from models import db
from security import sanitize
from utils.logging_setup import log_unexpected, sanitize_log_value

logger = logging.getLogger(__name__)

COLLECTION = "notes"

VALID_CATEGORIES = (
    "appel",
    "rencontre",
    "recherche",
    "stratégie",
    "correspondance",
    "audience",
    "autre",
)

CATEGORY_LABELS = {
    "appel": "Appel",
    "rencontre": "Rencontre",
    "recherche": "Recherche",
    "stratégie": "Stratégie",
    "correspondance": "Correspondance",
    "audience": "Audience",
    "autre": "Autre",
}


def _to_utc(dt: datetime) -> datetime:
    """Coerce a datetime to timezone-aware UTC (for iCalendar UTC stamps)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _default_doc() -> dict:
    """Return a dict with every note field set to its default value."""
    return {
        "id": "",
        "dossier_id": "",
        "dossier_file_number": "",
        "dossier_title": "",
        "title": "",
        "content": "",
        "category": "autre",
        "pinned": False,
        # A dateless note is a pure note (VJOURNAL without DTSTART) — jtx
        # Board files it under « Notes » instead of the dated journal view.
        "dateless": False,
        # The single « Théorie de la cause » note of a dossier (the Analyse
        # sheet). Hidden from the Notes views, INCLUDED on the DAV and MCP
        # read paths — see list_notes(include_analyse=...).
        "is_analyse": False,
        # DAV
        "vjournal_uid": "",
        # Metadata
        "created_at": None,
        "updated_at": None,
        "etag": "",
    }


# Note content (Markdown) is long-form — meeting minutes, research, strategy —
# so it gets a far more generous cap than the short scalar fields. The ceiling
# sits well under Firestore's 1 MiB document limit and the 1 MB request-size
# guard in security.py, and the content textarea carries a matching ``maxlength``
# so the cap is enforced (and visible) in the browser instead of silently
# truncating on save. Every other string field (title, denormalized dossier
# labels) keeps the app-wide 2000-char bound.
CONTENT_MAX_LENGTH = 100_000
_FIELD_MAX_LENGTH = 2000


def _sanitize_data(data: dict) -> dict:
    """Sanitize all string values in *data*.

    ``content`` is bounded to :data:`CONTENT_MAX_LENGTH`; every other string
    field to :data:`_FIELD_MAX_LENGTH`.
    """
    out: dict = {}
    for key, val in data.items():
        if isinstance(val, str):
            limit = CONTENT_MAX_LENGTH if key == "content" else _FIELD_MAX_LENGTH
            out[key] = sanitize(val, max_length=limit)
        else:
            out[key] = val
    return out


def _validate(data: dict) -> list[str]:
    """Return a list of validation error messages (empty = valid)."""
    errors: list[str] = []

    # dossier_id is deliberately NOT required: an empty one means the note
    # belongs to « Général ». The caller is responsible for refusing a
    # dossier_id that was supplied but does not resolve — see
    # routes/notes._enrich_dossier_info and mcp/handlers.create_note.
    if not data.get("title", "").strip():
        errors.append("Le titre de la note est requis.")
    if not data.get("content", "").strip():
        errors.append("Le contenu de la note est requis.")

    category = data.get("category", "")
    if category and category not in VALID_CATEGORIES:
        errors.append("Catégorie invalide.")

    return errors


# ── CRUD ──────────────────────────────────────────────────────────────────


def create_note(data: dict) -> tuple[Optional[dict], list[str]]:
    """Validate, generate IDs, write to Firestore. Returns (doc, errors)."""
    merged = {**_default_doc(), **_sanitize_data(data)}

    errors = _validate(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    note_id = merged.get("id") or str(uuid.uuid4())
    vjournal_uid = merged.get("vjournal_uid") or str(uuid.uuid4())

    merged.update({
        "id": note_id,
        "created_at": merged.get("created_at") or now,
        "updated_at": now,
        "etag": str(uuid.uuid4()),
        "vjournal_uid": vjournal_uid,
    })

    try:
        db.collection(COLLECTION).document(note_id).set(merged)
    except Exception:
        log_unexpected("note write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]

    return merged, []


def get_note(note_id: str) -> Optional[dict]:
    """Fetch a single note by ID."""
    try:
        doc = db.collection(COLLECTION).document(note_id).get()
        if doc.exists:
            return doc.to_dict()
    except Exception as exc:
        logger.warning("get_note failed for %s: %s", sanitize_log_value(note_id), exc)
    return None


def list_notes(
    dossier_id: Optional[str] = None,
    category: Optional[str] = None,
    search: Optional[str] = None,
    pinned_first: bool = True,
    include_analyse: bool = False,
) -> list[dict]:
    """Return notes, pinned first then newest first.

    Search scans title + content (client-side, same as other modules).

    ``include_analyse`` — the « Théorie de la cause » note (``is_analyse``)
    is EXCLUDED by default so the Notes views never show it. The DAV
    collection paths and the MCP note tools MUST pass ``True``: a DAV
    caller left on the default silently drops the note from DavX5 (the
    client just stops seeing the resource — no error anywhere). Python
    filter on purpose — no Firestore index.
    """
    try:
        query = db.collection(COLLECTION)

        if dossier_id:
            query = query.where(filter=FieldFilter("dossier_id", "==", dossier_id))

        results = [doc.to_dict() for doc in query.stream()]

        if not include_analyse:
            results = [r for r in results if not r.get("is_analyse")]

        # Client-side filters
        if category and category in VALID_CATEGORIES:
            results = [r for r in results if r.get("category") == category]

        if search:
            q = search.lower()
            results = [
                r for r in results
                if q in (r.get("title", "") or "").lower()
                or q in (r.get("content", "") or "").lower()
            ]

        # Sort: pinned first (if requested), then newest first
        results.sort(
            key=lambda n: (
                0 if pinned_first and n.get("pinned") else 1,
                -(n.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
            ),
        )

        return results
    except Exception:
        return []


# Bounded read caps for the default /notes/ list view (no search/category
# filter). Pinned notes are a small curated set; the recent-unpinned cap
# covers day-to-day browsing. Older notes stay reachable via search.
PINNED_LIMIT = 50
RECENT_LIMIT = 100


def list_notes_recent(
    dossier_id: Optional[str] = None,
    pinned_limit: int = PINNED_LIMIT,
    recent_limit: int = RECENT_LIMIT,
    include_analyse: bool = False,
) -> list[dict]:
    """Return pinned notes plus the most recent unpinned notes, bounded.

    Two server-side queries (``pinned == True`` then ``pinned == False``,
    each ordered by ``created_at`` descending and limited) replace the
    full-collection stream of :func:`list_notes` for the default list view.
    The concatenation preserves the legacy pinned-first / newest-first
    display order with at most ``pinned_limit + recent_limit`` reads.

    Requires the composite index (pinned ASC, created_at DESC) and, when
    *dossier_id* is given, (dossier_id ASC, pinned ASC, created_at DESC).

    ``include_analyse`` — same contract as :func:`list_notes`.

    Returns [] on failure (the list view degrades to an empty state).
    """
    try:
        results: list[dict] = []
        for pinned, limit in ((True, pinned_limit), (False, recent_limit)):
            query = db.collection(COLLECTION).where(
                filter=FieldFilter("pinned", "==", pinned)
            )
            if dossier_id:
                query = query.where(
                    filter=FieldFilter("dossier_id", "==", dossier_id)
                )
            query = query.order_by(
                "created_at", direction=firestore.Query.DESCENDING
            ).limit(limit)
            results.extend(doc.to_dict() for doc in query.stream())
        if not include_analyse:
            results = [r for r in results if not r.get("is_analyse")]
        return results
    except Exception as exc:
        logger.warning("list_notes_recent: query failed: %s", exc)
        return []


def update_note(
    note_id: str, data: dict
) -> tuple[Optional[dict], list[str]]:
    """Update an existing note. Returns (updated_doc, errors)."""
    existing = get_note(note_id)
    if not existing:
        return None, ["Note introuvable."]

    merged = {**existing, **_sanitize_data(data)}

    errors = _validate(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    merged["updated_at"] = now
    merged["etag"] = str(uuid.uuid4())

    try:
        db.collection(COLLECTION).document(note_id).set(merged)
    except Exception:
        log_unexpected("note write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]

    return merged, []


def delete_note(note_id: str) -> tuple[bool, str]:
    """Delete a note. Returns (success, error_message)."""
    existing = get_note(note_id)
    if not existing:
        return False, "Note introuvable."

    try:
        db.collection(COLLECTION).document(note_id).delete()
        return True, ""
    except Exception:
        log_unexpected("note delete failed")
        return False, "Erreur lors de la suppression. Veuillez réessayer."


def toggle_pin(note_id: str) -> tuple[Optional[dict], list[str]]:
    """Toggle the pinned status of a note."""
    existing = get_note(note_id)
    if not existing:
        return None, ["Note introuvable."]
    return update_note(note_id, {"pinned": not existing.get("pinned", False)})


# ── Théorie de la cause (feuille « Analyse ») ────────────────────────────

# The SUMMARY of the single analyse note (shown as-is in jtx Board). The
# sheet's tab label is « Analyse »; the note keeps its full name.
ANALYSE_TITLE = "Théorie de la cause"

# Seed content: the 8-block « Gabarit B » working template (méthode
# d'élaboration de la théorie d'une cause, École du Barreau). Verbatim from
# SPEC_Analyse_theorie_de_la_cause.md — Annexe A; edit the spec first if the
# template must change. Markdown tables + « ☐ » checkboxes render through the
# `markdown` filter (tables extension already active).
_ANALYSE_SEED = """\
# Théorie de la cause

*Dossier : … | Partie représentée : ☐ Demandeur ☐ Défendeur ☐ Mis en cause | Rédigé par : … | Date de l'analyse : …*

Outil de travail interne (méthode d'élaboration de la théorie d'une cause, version complète et stratégique). Les blocs F et G — forces/faiblesses et théorie adverse — n'ont pas vocation à être versés au dossier de la Cour.

---

## Bloc A — Identification et cadre procédural

### Parties et leur qualité

| Partie | Rôle | Qualité / capacité / intérêt (art. 85 C.p.c.) |
|---|---|---|
| … | … | … |

### Cadre procédural

Tribunal et compétence d'attribution : …
District (compétence territoriale) : …
Montant ou valeur en jeu : …
Voie procédurale envisagée : …

### Verrous préliminaires

- ☐ **Prescription** — délai applicable : … *(à défaut de délai particulier, 3 ans : art. 2925 C.c.Q.)* — point de départ : … — date pour agir : …
- ☐ Intérêt et qualité pour agir (art. 85 C.p.c.)
- ☐ Compétence (matière et territoire)
- ☐ Mise en demeure / avis préalable requis ou envoyé
- ☐ Autres conditions de recevabilité : …

*Questions-repères : le client a-t-il l'intérêt et la qualité requis ? Le recours est-il encore dans les délais ? Le bon tribunal est-il saisi ? Une démarche préalable est-elle exigée ?*

---

## Bloc B — Les faits

### Récit chronologique

…

### Cartographie des faits

| Fait | Générateur du droit ? | Admis / non contesté | Contesté (à prouver) | Défavorable |
|------|:---:|:---:|:---:|:---:|
| … | ☐ | ☐ | ☐ | ☐ |

### Faits défavorables à gérer

(comment les neutraliser ou les expliquer) …

### Faits manquants ou à investiguer

(documents, témoins, expertises à obtenir) …

*Questions-repères : quels faits font naître le droit invoqué ? Lesquels l'autre partie admettra-t-elle ? Quels faits me nuisent, et comment les aborder de front ? Que dois-je encore aller chercher ?*

---

## Bloc C — Le fondement juridique et ses éléments constitutifs

### Fondement(s) invoqué(s)

Cause d'action (ou, en défense, moyens opposés) : …
Sources : ☐ législation … ☐ jurisprudence … ☐ doctrine …

### Éléments constitutifs à réunir

*Exemple — responsabilité civile : faute, préjudice, lien de causalité (art. 1457 C.c.Q. extracontractuel ; art. 1458 C.c.Q. contractuel).*

| Élément constitutif | Fait(s) qui l'établit | Preuve disponible | Solide ? |
|---|---|---|:--:|
| … | … | … | ☐ |
| … | … | … | ☐ |
| … | … | … | ☐ |

### Moyens de défense / d'exception envisageables

(les miens et ceux de l'adversaire) …

*Questions-repères : ai-je isolé chacune des conditions que la loi exige ? Chaque condition est-elle appuyée par un fait et par une preuve ? Une seule condition non établie fait-elle échouer le recours ?*

---

## Bloc D — Qualification et syllogisme

**Majeure (la règle) :** …

**Mineure (les faits qualifiés) :** …

**Conclusion (l'application) :** …

### Qualification juridique retenue

(nature exacte du rapport ou de l'acte) …

*Questions-repères : chaque condition de la règle trouve-t-elle appui dans un fait ? Un fait vient-il contredire l'application de la règle ?*

---

## Bloc E — La stratégie de preuve

### Fardeau et norme

Fardeau de preuve — qui doit prouver quoi (art. 2803 C.c.Q.) : …
Norme applicable : prépondérance des probabilités (art. 2804 C.c.Q.), sauf exigence légale plus stricte : …

### Moyens de preuve

*Art. 2811 C.c.Q. : écrit, témoignage, présomption, aveu, présentation d'un élément matériel.*

| Élément / fait à prouver | Sur qui repose le fardeau | Moyen de preuve prévu | Source / pièce / témoin | Lacune |
|---|---|---|---|---|
| … | … | … | … | … |

*Questions-repères : pour chaque fait contesté, ai-je un moyen de preuve ? La preuve est-elle admissible et disponible ? Où sont mes trous de preuve, et comment les combler ? Quelle preuve l'adversaire opposera-t-il ?*

---

## Bloc F — Analyse critique

### Forces de ma position

- …

### Faiblesses et risques

- …

### Théorie adverse anticipée

(prétentions probables de la partie adverse — faits, fondement, preuve — et ma réponse à chacune)

| Prétention adverse anticipée | Ma réponse / parade |
|---|---|
| … | … |

*Questions-repères : si j'étais l'avocat de l'autre partie, quelle serait ma meilleure théorie ? Quel est le maillon le plus faible de ma cause ? Résiste-t-elle au contre-interrogatoire et au scénario adverse le plus favorable ?*

---

## Bloc G — La théorie de la cause (synthèse persuasive)

### Théorie factuelle

(le récit, cohérent et favorable, de ce qui s'est passé) …

### Théorie juridique

(le fondement de droit qui commande le résultat recherché) …

### Le thème

(l'idée-force, l'angle d'équité ou de bon sens qui donne au tribunal une raison de trancher en ma faveur) …

### Énoncé de la théorie (une à deux phrases)

> « … »

*Test de solidité : la théorie est-elle cohérente (sans contradiction interne), crédible (conforme au bon sens et à l'expérience), complète (elle absorbe même les faits défavorables) et simple (mémorable, exprimable en une phrase) ?*

---

## Bloc H — Conclusions recherchées et suites

### Conclusions recherchées

(remèdes précis, tels qu'ils devront être formulés à l'acte de procédure — clarté, précision, concision, ordre logique et numérotation : art. 99 C.p.c.)

1. …
2. …

### Objectifs réels du client

(et scénarios de règlement acceptables) …

### Prochaines étapes et échéancier

…

### Éléments encore à obtenir

(preuve, expertise, mandat, provision) …
"""


def get_analyse_note(dossier_id: str) -> Optional[dict]:
    """Return the dossier's single ``is_analyse`` note, or ``None``.

    Python scan over the per-dossier list (deliberately no
    ``.where("is_analyse", ...)`` — that would need a composite index
    deployed before the code).
    """
    for note in list_notes(dossier_id=dossier_id, include_analyse=True):
        if note.get("is_analyse"):
            return note
    return None


def has_analyse(dossier_id: str) -> bool:
    """True when the dossier already has its « Théorie de la cause » note."""
    return get_analyse_note(dossier_id) is not None


def create_analyse_note(dossier_id: str) -> tuple[Optional[dict], list[str]]:
    """Create the dossier's single analyse note, pre-seeded. IDEMPOTENT.

    Returns the existing note untouched when one is already there, so a
    re-clicked init button never mints a second one. The CTag bump belongs
    to the ROUTE (house rule) — never here.

    The existence check runs on a DIRECT query that propagates read
    failure (fail CLOSED): :func:`get_analyse_note` flows through
    :func:`list_notes`, which swallows errors into ``[]`` — a transient
    read failure would then look like « no analyse note yet » and this
    function would seed a DUPLICATE over the lawyer's filled analysis.
    """
    try:
        existing: Optional[dict] = None
        query = db.collection(COLLECTION).where(
            filter=FieldFilter("dossier_id", "==", dossier_id)
        )
        for doc in query.stream():
            candidate = doc.to_dict()
            if candidate.get("is_analyse"):
                existing = candidate
                break
    except Exception:
        log_unexpected("analyse existence check failed")
        return None, ["Erreur de lecture. Veuillez réessayer."]
    if existing:
        return existing, []

    from models.dossier import get_dossier

    dossier = get_dossier(dossier_id)
    if not dossier:
        return None, ["Dossier introuvable."]

    return create_note({
        "dossier_id": dossier_id,
        "dossier_file_number": dossier.get("file_number", ""),
        "dossier_title": dossier.get("title", ""),
        "title": ANALYSE_TITLE,
        "content": _ANALYSE_SEED,
        "category": "stratégie",
        "pinned": False,
        "dateless": True,
        "is_analyse": True,
    })


# ── Summary ──────────────────────────────────────────────────────────────


def _find_note_by_vjournal_uid(vjournal_uid: str) -> Optional[dict]:
    """Find a note by its VJOURNAL UID. Used for RELATED-TO resolution."""
    try:
        query = db.collection(COLLECTION).where(
            filter=FieldFilter("vjournal_uid", "==", vjournal_uid)
        ).limit(1)
        for doc in query.stream():
            return doc.to_dict()
    except Exception as exc:
        logger.warning(
            "_find_note_by_vjournal_uid failed for %s: %s",
            sanitize_log_value(vjournal_uid), exc,
        )
    return None


def get_notes_summary(dossier_id: str) -> dict:
    """Return {total} for the MCP get_dossier summary (its only caller).

    Includes the analyse note: the MCP read paths expose it, so the count
    must agree with what the MCP list_notes tool returns.
    """
    notes = list_notes(dossier_id=dossier_id, include_analyse=True)
    return {"total": len(notes)}


# ── RFC-5545 VJOURNAL serialization ─────────────────────────────────────


def note_to_vjournal(note: dict) -> str:
    """Serialize a note to an RFC-5545 VJOURNAL string wrapped in VCALENDAR.

    Properties:
    - UID: note's vjournal_uid
    - SUMMARY: note title
    - DESCRIPTION: note content
    - DTSTART: note created_at (date only) — OMITTED when ``dateless`` is
      set: a VJOURNAL without DTSTART is a pure *Note* in jtx Board instead
      of a dated *Journal* entry. CREATED/DTSTAMP stay unconditional (the
      jtx icalobject.created NOT-NULL trap).
    - CATEGORIES: note category label (French)
    - STATUS: FINAL (notes are always finalized records)
    - LAST-MODIFIED: note updated_at
    - SEQUENCE: 0
    - X-PALLAS-NOTE-CATEGORY: category key (for round-trip fidelity)
    - X-PALLAS-DOSSIER-ID: dossier_id
    - X-PALLAS-ANALYSE: "true" when the note is the théorie de la cause
    """
    cal = icalendar.Calendar()
    cal.add("prodid", "-//Pallas Athena//Note//FR")
    cal.add("version", "2.0")

    journal = icalendar.Journal()
    journal.add("uid", note.get("vjournal_uid", ""))
    journal.add("summary", note.get("title", ""))

    if note.get("content"):
        journal.add("description", note["content"])

    created = note.get("created_at")
    if created and hasattr(created, "date") and not note.get("dateless"):
        journal.add("dtstart", created.date())

    # CREATED + DTSTAMP as UTC date-times. Required for jtx Board: its
    # icalobject.created column is NOT NULL, and DavX5/ical4android writes
    # null (SQLITE_CONSTRAINT_NOTNULL on update) when the VJOURNAL omits
    # CREATED. DTSTAMP is mandatory per RFC 5545 §3.6.3.
    if created and hasattr(created, "hour"):
        journal.add("created", _to_utc(created))
    stamp = note.get("updated_at") or created
    if stamp and hasattr(stamp, "hour"):
        journal.add("dtstamp", _to_utc(stamp))

    journal.add("status", "FINAL")

    if note.get("category"):
        label = CATEGORY_LABELS.get(note["category"], note["category"])
        journal.add("categories", [label])

    updated = note.get("updated_at")
    if updated:
        journal.add("last-modified", updated)

    journal.add("sequence", 0)

    # Custom X- properties
    if note.get("category"):
        journal.add("x-pallas-note-category", note["category"])
    if note.get("dossier_id"):
        journal.add("x-pallas-dossier-id", note["dossier_id"])
    if note.get("pinned"):
        journal.add("x-pallas-pinned", "true")
    if note.get("is_analyse"):
        journal.add("x-pallas-analyse", "true")

    cal.add_component(journal)
    return cal.to_ical().decode("utf-8")


def vjournal_to_note(ical_str: str) -> dict:
    """Parse a VJOURNAL string into a note dict (for DAV PUT).

    Extracts standard properties and X-PALLAS-* custom properties.
    """
    cal = icalendar.Calendar.from_ical(ical_str)
    data: dict = {}

    for component in cal.walk():
        if component.name != "VJOURNAL":
            continue

        uid = component.get("uid")
        if uid:
            data["vjournal_uid"] = str(uid)

        summary = component.get("summary")
        if summary:
            data["title"] = str(summary)

        desc = component.get("description")
        if desc:
            data["content"] = str(desc)

        dtstart = component.get("dtstart")
        # A VJOURNAL without DTSTART is a pure note — record that so a PUT
        # round-trip through jtx never re-dates the analyse note. When
        # DTSTART is present the note is (back to) a dated journal entry.
        data["dateless"] = dtstart is None
        if dtstart:
            dt = dtstart.dt
            if hasattr(dt, "hour"):
                data["created_at"] = dt
            else:
                data["created_at"] = datetime.combine(
                    dt, datetime.min.time(), tzinfo=timezone.utc
                )

        # X- properties
        category = component.get("x-pallas-note-category")
        if category:
            cat = str(category)
            if cat in VALID_CATEGORIES:
                data["category"] = cat

        dossier_id = component.get("x-pallas-dossier-id")
        if dossier_id:
            data["dossier_id"] = str(dossier_id)

        pinned = component.get("x-pallas-pinned")
        if pinned and str(pinned).lower() == "true":
            data["pinned"] = True

        # Never set is_analyse=False here: a client that strips unknown
        # X- properties must not demote the stored flag — update_note's
        # merge keeps the existing value when the key is absent.
        analyse = component.get("x-pallas-analyse")
        if analyse and str(analyse).lower() == "true":
            data["is_analyse"] = True

        break  # Only process first VJOURNAL

    return data
